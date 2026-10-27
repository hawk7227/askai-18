#!/usr/bin/env python3
"""Turn collected Grasshopper conversations into an A.S.K. playbook.

Runs 100% locally. Reads <output>/snapshots/*.json from collector_v6.py and writes:
  <output>/playbook/needs_report.html     - readable report (de-identified examples)
  <output>/playbook/playbook.json         - per-need catalog: volume, patient phrasings,
                                            staff reply templates, info staff asked for,
                                            and the proposed A.S.K. draft + approval rule
  <output>/playbook/messages_labeled.csv  - every message with sender guess + need label
                                            (contains PHI - never share this one)

Sender: Grasshopper draws practice (outgoing) bubbles on the right and patient
(incoming) bubbles on the left; we use bubble position. Labels are keyword rules,
so they are a first pass for review, not clinical truth.
"""
import argparse, csv, html, json, re, sys
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------- need catalog
# Each need: patterns that detect it in a PATIENT message, the fields A.S.K. must
# collect, the draft it prepares, and who must approve before anything is sent/changed.
NEEDS = {
 'pharmacy_change': dict(
    title='Pharmacy change / send Rx to another pharmacy',
    rx=[r'\bpharmac(y|ies)\b.*\b(change|switch|different|another|new|instead|transfer|send|move|closed|out of stock)\b',
        r'\b(change|switch|send|transfer|move)\b.*\b(pharmacy|cvs|walgreens|walmart|fry\'?s|safeway|costco|target|bashas|sam\'?s)\b',
        r'\b(cvs|walgreens|walmart|fry\'?s|safeway|costco|bashas)\b.*\b(instead|rather|closer|doesn\'?t have|out of)\b'],
    fields=['patient identity (name + DOB)', 'new pharmacy name', 'pharmacy address or cross streets', 'pharmacy phone',
            'which prescription(s) to send', 'cancel at old pharmacy? (yes/no)'],
    draft='Update preferred pharmacy on chart + queue e-Rx re-send of the listed active prescriptions to the new pharmacy',
    approval='provider', auto_reply='Ask for any missing pharmacy details, then tell patient it is waiting for provider approval'),
 'refill': dict(
    title='Refill / renewal request',
    rx=[r'\brefill', r'\brenew(al)?\b.*\b(rx|prescription|med|medication)', r'\b(ran|run|running) out\b',
        r'\bout of (my )?(med|medication|pills|inhaler)', r'\bmore of (my|the)\b', r'\bneed (a |another )?(new )?(rx|prescription|script)\b'],
    fields=['patient identity (name + DOB)', 'medication name + strength', 'pharmacy (confirm current)', 'last visit date (from chart)',
            'any new symptoms/side effects'],
    draft='Refill order pre-filled from the last prescription + pharmacy; flags if a visit is required first',
    approval='provider', auto_reply='Confirm medication + pharmacy; if a visit is due, offer booking link'),
 'rx_problem': dict(
    title='Prescription not received / insurance / prior auth',
    rx=[r'\bprior auth', r'\b(pa|p\.a\.)\b.*\b(insurance|pharmacy)', r'\bnot covered\b', r'\binsurance (won\'?t|will not|denied|doesn\'?t)',
        r'\bpharmacy (says|said|doesn\'?t have|didn\'?t get|never got|has no)', r'\b(didn\'?t|never|haven\'?t) (get|got|receive|received)\b.*\b(rx|prescription|script|med)',
        r'\b(too expensive|cost too much|coupon|goodrx|generic)\b'],
    fields=['patient identity', 'medication', 'pharmacy', 'what the pharmacy/insurance said', 'insurance card (if PA)'],
    draft='Task: re-send Rx / start prior authorization / propose covered alternative (provider picks)',
    approval='provider', auto_reply='Acknowledge, collect pharmacy message + insurance info'),
 'appointment_book': dict(
    title='Book an appointment',
    rx=[r'\b(book|schedule|make|set up|get) (an? |my )?(appointment|appt|visit|consult)', r'\b(any|next) (availability|available|opening|slot)',
        r'\bcan i (be seen|see|get seen|come in)', r'\bavailable (today|tomorrow|this week)'],
    fields=['patient identity', 'reason for visit', 'visit type (video/phone)', 'preferred day/time'],
    draft='Booking link pre-filled with visit type + reason, or a held slot',
    approval='none (patient self-books; assistant notified)', auto_reply='Send booking link with options'),
 'appointment_change': dict(
    title='Reschedule / cancel / running late',
    rx=[r'\breschedul', r'\bcancel', r'\b(move|change) (my )?(appointment|appt|visit|time)', r'\brunning late\b', r'\bcan\'?t make (it|my)\b'],
    fields=['patient identity', 'which appointment', 'new preferred time', 'reason (optional)'],
    draft='Proposed new slot + cancellation per fee policy',
    approval='assistant', auto_reply='Offer open slots; confirm after assistant approves'),
 'visit_access': dict(
    title='Can\'t join video / link / login help',
    rx=[r'\b(link|video|zoom|camera|mic|audio)\b.*\b(not work|doesn\'?t work|won\'?t|can\'?t|isn\'?t|broken|error)',
        r'\bcan\'?t (join|log ?in|connect|get in|hear|see)', r'\b(waiting|been waiting)\b.*\b(room|for (the )?(doctor|provider))', r'\bwhere is (the )?link'],
    fields=['patient identity', 'appointment', 'device/browser', 'what they see'],
    draft='Re-send visit link + notify provider patient is waiting',
    approval='none (immediate)', auto_reply='Resend link with steps; ping provider'),
 'labs_results': dict(
    title='Lab orders / results',
    rx=[r'\blab(s|work)?\b', r'\bblood ?work\b', r'\bresults?\b', r'\b(quest|labcorp|sonora)\b', r'\b(test|x-?ray|ultrasound|mri|ct scan|imaging)\b'],
    fields=['patient identity', 'which test', 'lab location (for orders)', 'date done (for results)'],
    draft='Lab order draft OR result-review task with a plain-language result message for provider to edit',
    approval='provider', auto_reply='Confirm which test; result message only after provider review'),
 'forms_records': dict(
    title='Work/school note, forms, records',
    rx=[r'\b(work|school|doctor\'?s|excuse) (note|letter|excuse)', r'\b(fmla|disability|paperwork|form|forms)\b',
        r'\b(medical )?records\b', r'\bletter (for|to)\b', r'\bvisit (summary|notes)\b'],
    fields=['patient identity', 'document type', 'dates covered', 'who it goes to (employer/school/fax/email)'],
    draft='Pre-filled note/form from the visit record for signature',
    approval='provider', auto_reply='Collect dates + recipient, say it is awaiting signature'),
 'referral': dict(
    title='Referral to specialist',
    rx=[r'\brefer(ral|red)?\b', r'\b(specialist|dermatolog|cardiolog|orthop|neurolog|gastro|obgyn|ob-gyn|therapist|psychiatr)'],
    fields=['patient identity', 'specialty', 'reason', 'insurance', 'preferred location'],
    draft='Referral letter + fax/send packet',
    approval='provider', auto_reply='Collect specialty + insurance'),
 'billing': dict(
    title='Billing / payment / refund / insurance',
    rx=[r'\b(bill|billing|charge[ds]?|refund|payment|paid|pay|invoice|receipt|cost|price|fee|copay|deductible|superbill)\b',
        r'\binsurance\b(?!.*\b(won\'?t|denied|prior)\b)'],
    fields=['patient identity', 'visit date', 'amount in question', 'what they want (receipt/refund/explanation)'],
    draft='Receipt/superbill or refund request with payment record attached',
    approval='admin (refunds) / none (receipts)', auto_reply='Send receipt; refunds go to admin queue'),
 'clinical_question': dict(
    title='Symptoms / side effects / medical question',
    rx=[r'\b(side effect|reaction|rash|swelling|hives|worse|not better|still (sick|hurts|have)|fever|pain|bleeding|vomit|dizzy|nause)',
        r'\b(how (much|many|often) (should|do) i take|dose|dosage|can i take|is it ok to|should i)\b'],
    fields=['patient identity', 'symptoms + since when', 'current meds', 'red-flag screen'],
    draft='Triage summary + suggested reply or "book a visit" for provider to choose',
    approval='provider', auto_reply='Red flags -> tell patient to call 911/ER now; otherwise collect details, provider replies'),
 'media_received': dict(
    title='Photo / document sent',
    rx=[r'^image received$', r'\b(photo|picture|pic|image|screenshot|attached|attachment)\b'],
    fields=['what the image is (rash, card, Rx label, form)', 'attach to which chart section'],
    draft='Attach to chart (Documents) + label; route to the matching need',
    approval='assistant', auto_reply='Ask what the image is for if unclear'),
}
RED_FLAGS = re.compile(r'\b(chest pain|can\'?t breathe|trouble breathing|suicid|kill myself|overdose|stroke|passing out|seizure|severe bleeding|throat (closing|swelling))\b', re.I)
SMALLTALK = re.compile(r'^(ok(ay)?|k|thanks?( you)?|ty|thx|great|perfect|sounds good|got it|yes|no|yep|nope|cool|👍|🙏|bye|you too|we hope so too\.?)[.! ]*$', re.I)
NEED_RX = {k: [re.compile(p, re.I) for p in v['rx']] for k, v in NEEDS.items()}

# What staff asked for (detected in practice messages that end with '?' or say 'please send')
ASKED = {
 'date of birth': r'\b(dob|date of birth|birthday)\b', 'pharmacy name/location': r'\bwhich pharmacy|pharmacy (name|address|location)|cross streets?\b',
 'medication name': r'\bwhich (med|medication|prescription)|name of (the )?(med|medication)', 'photo': r'\b(send|attach) (a |us a )?(photo|picture|pic)',
 'insurance info': r'\binsurance (card|info|information|name)|member id', 'symptoms details': r'\b(how long|since when|any (fever|other symptoms))',
 'preferred time': r'\bwhat (day|time)|when (are|would) you (available|like)', 'email': r'\bemail( address)?\b', 'full name': r'\bfull name|first and last',
 'address': r'\baddress\b', 'employer/school': r'\b(employer|school|who should (it|the note))'}
ASKED_RX = {k: re.compile(v, re.I) for k, v in ASKED.items()}

# ---------------------------------------------------------------- de-identification
PHONE = re.compile(r'(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)')
EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+')
DATE = re.compile(r'\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}(?:,? \d{4})?\b', re.I)
ADDR = re.compile(r'\b\d{2,6} [A-Z0-9][\w .]{2,40}\b(?:st|street|ave|avenue|rd|road|blvd|dr|drive|ln|lane|way|pkwy|ct|pl)\b\.?', re.I)
LONGNUM = re.compile(r'\b\d{5,}\b')
NAMEISH = re.compile(r'\b([A-Z][a-z]{1,15})(?: [A-Z]\.?)?(?: [A-Z][a-z]{1,20}(?:-[A-Z][a-z]+)?)\b')
KEEP_CAPS = {'Walgreens', 'Walmart', 'Costco', 'Safeway', 'Target', 'Quest', 'Labcorp', 'Monday', 'Tuesday', 'Wednesday', 'Thursday',
             'Friday', 'Saturday', 'Sunday', 'Good', 'Thank', 'Thanks', 'Hello', 'Please', 'Dr', 'The', 'This', 'We', 'You', 'Your', 'Hi', 'Hey',
             'Which', 'What', 'When', 'Where', 'How', 'Who', 'Why', 'Is', 'Are', 'Can', 'Could', 'Would', 'Will', 'Do', 'Does', 'Did', 'Sure',
             'Sorry', 'Yes', 'No', 'Our', 'My', 'It', 'If', 'And', 'But', 'Also', 'Here', 'There', 'Just', 'Okay', 'Ok', 'Medazon', 'Rite', 'Aid',
             'Fry', 'Bashas', 'Sonora', 'Urgent', 'Care', 'Emergency', 'Room', 'Provider', 'Doctor', 'Nurse', 'Office', 'Pharmacy', 'Insurance'}
def deid(t, names=()):
    t = EMAIL.sub('[email]', t); t = PHONE.sub('[phone]', t); t = ADDR.sub('[address]', t)
    t = DATE.sub('[date]', t); t = LONGNUM.sub('[number]', t)
    for n in names:
        for part in {n} | set(n.split()):
            if len(part) > 2: t = re.sub(r'\b' + re.escape(part) + r'\b', '[name]', t, flags=re.I)
    t = NAMEISH.sub(lambda m: m.group(0) if m.group(1) in KEEP_CAPS else '[name]', t)
    return t
def template(t, names=()):
    return re.sub(r'\s+', ' ', deid(t, names)).strip()[:220]

# ---------------------------------------------------------------- load conversations
SKIP_KINDS = {'date_label', 'time_label'}
# The collector's labels are broad ("today"/"Monday" anywhere => date_label, "voicemail" anywhere
# => call). Only drop lines that really ARE a date/time/call line; keep real patient messages.
REAL_DATE = re.compile(r'^(?:today|yesterday|(?:mon|tues|wednes|thurs|fri|satur|sun)day|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}(?:,? \d{4})?|\d{1,2}/\d{1,2}(?:/\d{2,4})?)(?:,? (?:at )?\d{1,2}:\d{2}\s*(?:am|pm)?)?$', re.I)
REAL_TIME = re.compile(r'^\d{1,2}:\d{2}\s*(?:am|pm)?$', re.I)
REAL_CALL = re.compile(r'^(?:incoming|outgoing|missed) call\b|^call not answered\b|^voicemail\b|^fax\b|^(?:new )?voicemail from\b', re.I)
# Grasshopper's line under each practice bubble: "4:39 PM LaMonica Hodges" / "8:06 AM Instant, Response"
META = re.compile(r'^(\d{1,2}:\d{2}\s*(?:am|pm)?)\s+(.{2,60})$', re.I)
AUTO = re.compile(r'\b(instant|auto(matic)?)\b.*\bresponse\b|^response$', re.I)
UI_TEXT = {'new', 'you can drag and drop files into the conversation to share them', 'type a message', 'all', 'messages',
           'calls', 'voicemails', 'faxes', 'to:'}
def load(out):
    snaps = defaultdict(list)
    for p in sorted((out / 'snapshots').glob('*.json')):
        try: d = json.loads(p.read_text(encoding='utf-8'))
        except Exception: continue
        snaps[d['key']].append(d)
    convs = {}
    for key, steps in snaps.items():
        steps.sort(key=lambda d: d['step'])
        label = steps[0].get('row_label', key)
        names = [x for x in label.split(' | ') if not PHONE.search(x) and len(x) > 3 and not SMALLTALK.match(x)][:1]
        # step 0 = newest screen; each later step scrolled UP to older messages.
        # Build timeline oldest -> newest: older steps first, each screen top -> bottom.
        seen = set(); timeline = []
        for d in reversed(steps):
            items = []
            for i in d['history_candidates']:
                t = i['text'].strip(); k = i.get('classification')
                if t.lower() in UI_TEXT: continue
                if k == 'date_label' and not REAL_DATE.match(t): k = 'possible_message_text'
                if k == 'time_label' and not REAL_TIME.match(t): k = 'possible_message_text'
                if k == 'call_or_voicemail_or_fax' and not REAL_CALL.match(t): k = 'possible_message_text'
                if k in SKIP_KINDS: continue
                items.append({**i, 'classification': k})
            if not items: continue
            xs1 = min(i['rect'][0] for i in items); xs2 = max(i['rect'][2] for i in items)
            for i in sorted(items, key=lambda i: i['rect'][1]):
                txt = i['text'].strip()
                meta = META.match(txt)
                if meta or AUTO.match(txt):
                    by = meta.group(2).strip() if meta else txt
                    # attach to the closest message above it (the bubble it labels)
                    if timeline and 'by' not in timeline[-1]:
                        timeline[-1]['by'] = by
                        if AUTO.search(by): timeline[-1]['sender'] = 'auto'
                    continue
                fp = (i.get('classification'), txt)
                if fp in seen or len(txt) < 2: continue
                seen.add(fp)
                left_gap = i['rect'][0] - xs1; right_gap = xs2 - i['rect'][2]
                if i.get('classification') == 'call_or_voicemail_or_fax': sender = 'call'
                elif abs(left_gap - right_gap) < 12: sender = 'unknown'
                else: sender = 'practice' if right_gap < left_gap else 'patient'
                timeline.append({'text': txt, 'sender': sender})
        convs[key] = {'label': label, 'names': names, 'messages': timeline}
    return convs

def needs_of(text):
    if SMALLTALK.match(text.strip()): return []
    return [k for k, rxs in NEED_RX.items() if any(r.search(text) for r in rxs)]

# ---------------------------------------------------------------- analyze
def analyze(convs):
    stats = {k: {'conversations': set(), 'patient_msgs': 0, 'phrasings': Counter(), 'replies': Counter(),
                 'asked': Counter(), 'turns_to_resolve': [], 'answers': 0, 'handled_by': Counter(), 'auto_replies': 0} for k in NEEDS}
    rows = []; unlabeled = Counter(); red_flag_convs = 0; sender_counts = Counter()
    for key, c in convs.items():
        msgs = c['messages']; open_needs = []; flagged = False; last_practice_asked = False
        for idx, m in enumerate(msgs):
            sender_counts[m['sender']] += 1
            labels = needs_of(m['text']) if m['sender'] in ('patient', 'unknown') else []
            if m['sender'] == 'practice':
                last_practice_asked = '?' in m['text']
            # A patient answer to the assistant's question belongs to the open need.
            answer = m['sender'] == 'patient' and not labels and open_needs and last_practice_asked
            if labels: open_needs = labels
            if m['sender'] in ('patient', 'unknown') and RED_FLAGS.search(m['text']): flagged = True
            rows.append({'conversation': key, 'index': idx, 'sender': m['sender'],
                         'needs': ';'.join(labels) or (';'.join(n + ':answer' for n in open_needs) if answer else ''), 'text': m['text']})
            if answer:
                for n in open_needs: stats[n]['answers'] += 1
                # the replies after the answer are still the handling of that need
                j = idx + 1
                while j < len(msgs) and msgs[j]['sender'] != 'patient':
                    if msgs[j]['sender'] == 'practice':
                        for n in open_needs:
                            stats[n]['replies'][template(msgs[j]['text'], c['names'])] += 1
                            if msgs[j].get('by'): stats[n]['handled_by'][msgs[j]['by']] += 1
                    j += 1
                continue
            if m['sender'] == 'patient' and not labels and not SMALLTALK.match(m['text']) and len(m['text']) > 12:
                unlabeled[template(m['text'], c['names'])] += 1
            for n in labels:
                s = stats[n]; s['conversations'].add(key); s['patient_msgs'] += 1
                s['phrasings'][template(m['text'], c['names'])] += 1
                # the practice's next replies (until the patient speaks again) = how it was handled
                j = idx + 1; turns = 0
                while j < len(msgs) and msgs[j]['sender'] != 'patient':
                    if msgs[j]['sender'] == 'auto': s['auto_replies'] += 1
                    if msgs[j]['sender'] == 'practice':
                        if msgs[j].get('by'): s['handled_by'][msgs[j]['by']] += 1
                        r = msgs[j]['text']; s['replies'][template(r, c['names'])] += 1; turns += 1
                        for a, rx in ASKED_RX.items():
                            if rx.search(r): s['asked'][a] += 1
                    j += 1
                if turns: s['turns_to_resolve'].append(turns)
        red_flag_convs += flagged
    total = len(convs)
    playbook = []
    for k, s in sorted(stats.items(), key=lambda kv: -len(kv[1]['conversations'])):
        n = len(s['conversations']); spec = NEEDS[k]
        playbook.append({
            'need': k, 'title': spec['title'], 'conversations': n, 'share_of_conversations': round(100 * n / total, 1) if total else 0,
            'patient_messages': s['patient_msgs'],
            'example_patient_phrasings': [t for t, _ in s['phrasings'].most_common(8)],
            'staff_reply_templates': [{'text': t, 'times': c} for t, c in s['replies'].most_common(8)],
            'staff_asked_for': [{'item': a, 'times': c} for a, c in s['asked'].most_common()],
            'patient_follow_up_answers': s['answers'],
            'handled_by': dict(s['handled_by'].most_common()), 'auto_replies_seen': s['auto_replies'],
            'ask_draft': spec['draft'], 'ask_collects': spec['fields'], 'approval': spec['approval'], 'ask_auto_reply': spec['auto_reply'],
        })
    summary = {'conversations': total, 'messages': len(rows), 'sender_guess_counts': dict(sender_counts),
               'conversations_with_red_flag_language': red_flag_convs,
               'unlabeled_patient_messages_top': [{'text': t, 'times': c} for t, c in unlabeled.most_common(40)]}
    return playbook, summary, rows

# ---------------------------------------------------------------- report
def report(playbook, summary, path):
    e = html.escape
    cards = []
    for p in playbook:
        if not p['conversations']: continue
        li = lambda xs: ''.join(f'<li>{e(x)}</li>' for x in xs) or '<li class=m>none captured</li>'
        cards.append(f'''<section><h2>{e(p["title"])} <span>{p["conversations"]} conversations · {p["share_of_conversations"]}%</span></h2>
<div class=g><div><h3>How patients ask</h3><ul>{li(p["example_patient_phrasings"])}</ul></div>
<div><h3>How the assistant replied</h3><ul>{li([f"{r['text']}  (×{r['times']})" for r in p["staff_reply_templates"]])}</ul></div>
<div><h3>What she had to ask for</h3><ul>{li([f"{a['item']} (×{a['times']})" for a in p["staff_asked_for"]])}</ul>
<h3>Handled by</h3><ul>{li([f"{who} (×{n})" for who, n in p["handled_by"].items()] + ([f"automatic reply (×{p['auto_replies_seen']})"] if p["auto_replies_seen"] else []))}</ul></div>
<div class=a><h3>A.S.K. would</h3><p><b>Collect:</b> {e(", ".join(p["ask_collects"]))}</p><p><b>Draft:</b> {e(p["ask_draft"])}</p>
<p><b>Approval:</b> {e(p["approval"])}</p><p><b>Reply to patient:</b> {e(p["ask_auto_reply"])}</p></div></div></section>''')
    un = ''.join(f'<li>{e(u["text"])} (×{u["times"]})</li>' for u in summary['unlabeled_patient_messages_top'][:25])
    doc = f'''<!doctype html><meta charset=utf-8><title>Patient Needs Playbook</title><style>
body{{font:15px system-ui;margin:24px;background:#f6f7f9;color:#111}}section{{background:#fff;border-radius:12px;padding:16px 20px;margin:14px 0;box-shadow:0 1px 3px #0002}}
h1{{margin:0 0 4px}}h2{{margin:0 0 10px;font-size:20px}}h2 span{{font-size:14px;color:#FF7113;margin-left:8px}}h3{{font-size:13px;text-transform:uppercase;color:#555;margin:8px 0 4px}}
.g{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}}ul{{margin:0;padding-left:18px}}li{{margin:3px 0}}.m{{color:#999}}.a{{background:#fff4ec;border-radius:8px;padding:4px 12px}}
</style><h1>Patient Needs Playbook</h1><p>{summary["conversations"]} conversations · {summary["messages"]} messages · sender guess {e(json.dumps(summary["sender_guess_counts"]))} ·
red-flag language in {summary["conversations_with_red_flag_language"]} conversation(s). Examples are auto de-identified — review before sharing.</p>
{"".join(cards)}<section><h2>Patient messages no rule matched <span>new need types hide here</span></h2><ul>{un or "<li class=m>none</li>"}</ul></section>'''
    path.write_text(doc, encoding='utf-8')

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--output', required=True, type=Path); a = ap.parse_args()
    if not (a.output / 'snapshots').exists(): sys.exit(f'No snapshots in {a.output}. Run collector_v6.py collect first.')
    convs = load(a.output)
    playbook, summary, rows = analyze(convs)
    dest = a.output / 'playbook'; dest.mkdir(exist_ok=True)
    (dest / 'playbook.json').write_text(json.dumps({'summary': summary, 'needs': playbook}, ensure_ascii=False, indent=2), encoding='utf-8')
    with open(dest / 'messages_labeled.csv', 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=['conversation', 'index', 'sender', 'needs', 'text']); w.writeheader(); w.writerows(rows)
    report(playbook, summary, dest / 'needs_report.html')
    print(f'{summary["conversations"]} conversations, {summary["messages"]} messages analyzed.')
    for p in playbook:
        if p['conversations']: print(f'  {p["conversations"]:4d}  {p["title"]}')
    print(f'Report: {dest / "needs_report.html"}')

if __name__ == '__main__': main()
