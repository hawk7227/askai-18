#!/usr/bin/env python3
"""Read-only Grasshopper UIA collector, v6. Local records only; no OCR or network.

v6 changes (vs v5):
  * LEFT list scrolling no longer clicks a guessed scrollbar position. It uses, in
    order: the list's own UI Automation ScrollPattern (no mouse at all), then the
    mouse wheel at your calibrated LEFT-list point (a point already proven to be
    on-screen). This removes the off-screen click that pushed the cursor into a
    screen corner and tripped the PyAutoGUI fail-safe.
  * Every mouse target is checked to be inside the Grasshopper window AND on a real
    monitor before the pointer moves, and checked again after it moves.
  * Contacts are tracked by identity (their row text), not page/row number, and
    the list is scrolled in small steps with overlap, so no contact is skipped
    and a re-run resumes exactly where it stopped.
  * If Grasshopper does not expose LEFT rows to UI Automation, a positional mode
    measures the real scroll distance from screen pixels and still never skips rows.
  * Fail-safe (pointer slammed into a corner by you) stops cleanly with progress saved.
"""
import argparse, ctypes, hashlib, json, os, re, sys, time
from pathlib import Path
from datetime import datetime, timezone

# Per-monitor DPI awareness BEFORE pyautogui/pywinauto load, so window rectangles
# and cursor coordinates use the same pixel space on mixed-scaling monitors.
if sys.platform == 'win32':
    try: ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try: ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception: pass

import pyautogui as pg
from pywinauto import Desktop
from PIL import Image

pg.FAILSAFE = True   # kept: slam the mouse into a corner to abort
# pyautogui.scroll() on Windows sends RAW wheel units: one real notch = 120.
# (v6.0 sent 1, which Grasshopper ignored - lists never scrolled.)
NOTCH = 120 if sys.platform == 'win32' else 1
pg.PAUSE = .04

class SafeStop(RuntimeError): pass

def timestamp(): return datetime.now(timezone.utc).isoformat()
def sha(s): return hashlib.sha256(s.encode('utf-8')).hexdigest()
def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)
def read(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default

# ---------------------------------------------------------------- window / geometry
def window():
    matches = []
    for w in Desktop(backend='uia').windows():
        try:
            if w.window_text().strip() == 'Grasshopper App' and w.is_visible(): matches.append(w)
        except Exception: pass
    if len(matches) != 1: raise RuntimeError(f'Expected one Grasshopper App window, found {len(matches)}')
    return matches[0]
def rect(w):
    r = w.rectangle(); return [r.left, r.top, r.right, r.bottom]
def point(prompt):
    input(f'Move pointer to {prompt}; press ENTER in PowerShell without clicking: ')
    p = pg.position(); return [p.x, p.y]

def on_monitor(x, y):
    """True when (x, y) lies on an attached monitor (multi-monitor aware)."""
    if sys.platform != 'win32': return True
    class PT(ctypes.Structure): _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]
    fn = ctypes.windll.user32.MonitorFromPoint
    fn.restype = ctypes.c_void_p
    return bool(fn(PT(int(x), int(y)), 0))   # 0 = MONITOR_DEFAULTTONULL

def safe_target(x, y, r, what):
    x, y = int(round(x)), int(round(y))
    if not (r[0] + 8 < x < r[2] - 8 and r[1] + 8 < y < r[3] - 8):
        raise SafeStop(f'{what} target ({x},{y}) is outside the Grasshopper window; nothing clicked')
    if not on_monitor(x, y):
        raise SafeStop(f'{what} target ({x},{y}) is off every monitor - the Grasshopper window is '
                       f'partly off-screen. Move it fully onto one monitor, re-run calibrate, then collect.')
    return x, y

def move_to(x, y, r, what):
    x, y = safe_target(x, y, r, what)
    for attempt in range(4):                      # a bumped mouse or scaling jitter gets retried
        pg.moveTo(x, y)
        p = pg.position()
        if abs(p.x - x) <= 10 and abs(p.y - y) <= 10: return x, y
        time.sleep(.3)
    raise SafeStop(f'Pointer landed at ({p.x},{p.y}) instead of {what} ({x},{y}) after 4 tries; '
                   'keep hands off the mouse, or display scaling disagrees. Nothing clicked.')

def click_at(x, y, r, what):
    x, y = move_to(x, y, r, what)
    pg.click(x, y)

def calibrate(out):
    w = window(); r = rect(w)
    input('Open Grasshopper > Conversations, choose All, keep window fixed; ENTER to begin: ')
    positions = {
        'first': point('center of FIRST row in LEFT list'),
        'second': point('center of SECOND row in LEFT list'),
        'list': point('inside LEFT scrollable list below filters'),
        'history': point('inside RIGHT history, away from composer')}
    if positions['second'][1] - positions['first'][1] < 15: raise RuntimeError('Row calibration invalid')
    for k, p in positions.items():
        if not (r[0] <= p[0] <= r[2] and r[1] <= p[1] <= r[3]): raise RuntimeError(f'{k} point outside app window')
        if not on_monitor(*p): raise RuntimeError(f'{k} point is off-screen')
    for corner in ((r[0], r[1]), (r[2] - 1, r[3] - 1)):
        if not on_monitor(*corner):
            print('WARNING: part of the Grasshopper window is off-screen. Maximize it or move it fully '
                  'onto one monitor, then calibrate again for reliable scrolling.')
            break
    cfg = {'base_rect': r,
           'relative': {k: [(p[0] - r[0]) / (r[2] - r[0]), (p[1] - r[1]) / (r[3] - r[1])] for k, p in positions.items()},
           'row_delta_ratio': (positions['second'][1] - positions['first'][1]) / (r[3] - r[1])}
    write(out / 'calibration.json', cfg); print('Calibration saved.')

def geometry(cfg):
    w = window(); r = rect(w); width = r[2] - r[0]; height = r[3] - r[1]
    base = cfg['base_rect']; bw = base[2] - base[0]; bh = base[3] - base[1]
    if width < 800 or height < 500 or abs(width - bw) / bw > .20 or abs(height - bh) / bh > .20:
        raise SafeStop('Grasshopper window geometry changed substantially; paused safely')
    pts = {k: (round(r[0] + v[0] * width), round(r[1] + v[1] * height)) for k, v in cfg['relative'].items()}
    if not (pts['first'][0] < pts['history'][0] and pts['list'][0] < pts['history'][0]):
        raise SafeStop('List/history coordinates inconsistent; paused safely')
    for k, (x, y) in pts.items():
        if not (r[0] + 8 < x < r[2] - 8 and r[1] + 8 < y < r[3] - 8): raise SafeStop(f'{k} outside safe window interior')
    return w, r, pts, max(16, round(cfg['row_delta_ratio'] * height))

LIST_BOTTOM = None   # real bottom edge of the LEFT list, from UI Automation when available

def left_viewport(r, pts, delta, at_end=False):
    """(top, bottom) of the visible LEFT contact rows in screen pixels."""
    top = max(r[1] + 8, pts['first'][1] - delta // 2)
    bottom = r[3] - round((r[3] - r[1]) * .10)
    if LIST_BOTTOM is not None and pts['list'][1] < LIST_BOTTOM <= r[3] - 8: bottom = LIST_BOTTOM
    elif at_end: bottom = r[3] - 8      # list cannot scroll further: last row may sit lower
    if bottom - top < 2 * delta: raise SafeStop('LEFT list viewport too short for safe scrolling')
    return top, bottom

# ---------------------------------------------------------------- UIA reading
def elements(w):
    result = []; seen = set()
    for el in w.descendants():
        try:
            if not el.is_visible(): continue
            box = el.rectangle()
            if box.width() <= 0 or box.height() <= 0: continue
            typ = el.element_info.control_type or ''
            name = (el.window_text() or '').strip(); value = ''
            if typ in ('Text', 'Document', 'Edit', 'ListItem', 'DataItem'):
                try: value = str(el.iface_value.CurrentValue or '').strip()
                except Exception: pass
                if not value:
                    try: value = str(el.iface_text.DocumentRange.GetText(-1) or '').strip()
                    except Exception: pass
            txt = (value or name)[:200000]
            if not txt: continue
            item = {'text': txt, 'type': typ, 'rect': [box.left, box.top, box.right, box.bottom]}
            key = (txt, tuple(item['rect']))
            if key not in seen: seen.add(key); result.append(item)
        except Exception: continue
    return result

DATE = re.compile(r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b(?:Today|Yesterday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b', re.I)
CLOCK = re.compile(r'\b\d{1,2}:\d{2}\s*(?:AM|PM)?\b', re.I)
PHONE = re.compile(r'(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)')
CALL = re.compile(r'\b(?:incoming call|outgoing call|missed call|call not answered|voicemail|fax)\b', re.I)
INITIALS = re.compile(r'^[A-Z]{1,3}$')
CHROME = {'Conversations', 'Search', 'Active', 'All', 'Messages', 'Calls', 'Voicemails', 'Faxes', 'Dialer', 'Contacts', 'Inbox', 'Setup Assistant', 'Type a message', 'Default Extension'}
# Relative times change between reads ("2m" -> "3m"); strip them from row identity.
VOLATILE = re.compile(r'^(?:\d+\s*(?:s|m|h|d|w|min|mins|hr|hrs)\s*(?:ago)?|now|just now|yesterday|today|\d{1,2}:\d{2}\s*(?:AM|PM)?|\d{1,2}/\d{1,2}(?:/\d{2,4})?|(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2}|\d{1,3})$', re.I)
VOLATILE_IN = re.compile(r'\b(?:\d{1,2}:\d{2}\s*(?:AM|PM)?|\d+\s*(?:s|m|h|d|w|min|mins|hr|hrs)(?:\s*ago)?|just now|now|yesterday|today|\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b', re.I)
def norm(t): return re.sub(r'\s+', ' ', VOLATILE_IN.sub(' ', t)).strip()

def classify(items, r, pts):
    split = (pts['first'][0] + pts['history'][0]) // 2
    header_end = r[1] + round((r[3] - r[1]) * .31)
    composer_start = r[1] + round((r[3] - r[1]) * .89)
    left = []; right = []; chrome = []
    for item in items:
        x1, y1, x2, y2 = item['rect']; txt = item['text']
        if x2 < split: left.append(item)
        elif x1 >= split and header_end < y1 < composer_start:
            kind = 'call_or_voicemail_or_fax' if CALL.search(txt) else 'date_label' if DATE.search(txt) else 'time_label' if CLOCK.fullmatch(txt) else 'possible_message_text'
            right.append({**item, 'classification': kind, 'sender': 'unverified', 'message_boundary': 'unverified'})
        else: chrome.append(item)
    contacts = [{'text': i['text'], 'phone_matches': PHONE.findall(i['text'])} for i in left if PHONE.search(i['text']) or (i['type'] in ('ListItem', 'DataItem') and i['text'] not in CHROME)]
    return {'contact_candidates': contacts, 'history_candidates': right, 'interface_controls': chrome}

def group_rows(items, r, pts, delta, top, bottom):
    """Group LEFT-pane UIA elements into visible contact rows.

    Returns [{'key','label','x','y'}] top-to-bottom, only rows fully inside the viewport.
    """
    split = (pts['first'][0] + pts['history'][0]) // 2
    cands = []
    for it in items:
        x1, y1, x2, y2 = it['rect']
        if x2 >= split or (y2 - y1) > 1.6 * delta: continue      # right pane / big containers
        cy = (y1 + y2) / 2
        if not (top - delta <= cy <= bottom + delta): continue   # keep edge rows whole; filtered below
        t = it['text'].strip()
        if not t or t in CHROME: continue
        cands.append((cy, it))
    cands.sort(key=lambda c: c[0])
    rows = []
    for cy, it in cands:
        if rows and cy - rows[-1]['anchor'] <= .55 * delta: rows[-1]['items'].append((cy, it))
        else: rows.append({'anchor': cy, 'items': [(cy, it)]})
    out = []
    for row in rows:
        items_ = [it for _, it in row['items']]
        # Only rows that are completely inside the viewport: a clipped row would
        # read with missing text and look like a different contact.
        if min(it['rect'][1] for it in items_) < top or max(it['rect'][3] for it in items_) > bottom: continue
        whole = [it for it in items_ if it['type'] in ('ListItem', 'DataItem')]
        y = ((whole[0]['rect'][1] + whole[0]['rect'][3]) / 2 if whole
             else sum(c for c, _ in row['items']) / len(row['items']))
        stable = sorted({norm(it['text']) for it in items_ if not VOLATILE.match(it['text'].strip())} - {''})
        if not stable: continue
        # Identity = the contact's NAME LINE only (name or phone number). Initials
        # badges, previews and dates come and go between reads, so they never count.
        parts = [it for it in items_ if it['type'] not in ('ListItem', 'DataItem')
                 and not INITIALS.match(it['text'].strip()) and not VOLATILE.match(it['text'].strip()) and norm(it['text'])]
        if not parts: parts = [it for it in items_ if norm(it['text'])]
        name_el = min(parts, key=lambda it: (it['rect'][1], it['rect'][0]))
        name = norm(name_el['text'])
        if name_el['type'] in ('ListItem', 'DataItem'):
            name = norm(re.split(r'\s{2,}|\n', name_el['text'])[0])
        ph = PHONE.findall(name)
        ident = ('phone:' + re.sub(r'\D', '', ph[0])[-10:]) if ph else 'name:' + name.lower()
        if name_el['type'] not in ('ListItem', 'DataItem'):
            y = (name_el['rect'][1] + name_el['rect'][3]) / 2        # click ON the name line
        out.append({'key': 'c_' + sha(ident)[:20], 'label': ' | '.join([name] + [t for t in stable if t != name])[:160], 'name': name,
                    'strong': strong_name(name),
                    'x': pts['first'][0], 'y': round(y)})
    return out

def visible_rows(cfg, at_end=False):
    w, r, pts, delta = geometry(cfg)
    top, bottom = left_viewport(r, pts, delta, at_end)
    return group_rows(elements(w), r, pts, delta, top, bottom)

STATUSY = re.compile(r"\b(ext\.?\s*\d|call|voicemail|fax|welcome|thank|please|received|stop|link|appointment|sorry|image)\b|[.?!:]$", re.I)
def strong_name(name):
    '''True when the row's name line is a real contact name or number (not a call/preview line).'''
    if PHONE.search(name): return True
    if INITIALS.match(name.strip()): return False
    words = name.split()
    return (1 <= len(words) <= 5 and len(name) <= 45 and not STATUSY.search(name)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9.'\- ]*", name) is not None)

def header_identity(items, r, pts):
    '''The open conversation's "To:" header (name + phone) = the real contact identity.'''
    split = (pts['first'][0] + pts['history'][0]) // 2
    limit = r[1] + round((r[3] - r[1]) * .23)
    to = [it for it in items if it['text'].strip() in ('To:', 'To') and it['rect'][1] < limit]
    if to:   # anchor on the "To:" label: the header is the text beside it
        t = min(to, key=lambda it: it['rect'][0]); cy = (t['rect'][1] + t['rect'][3]) / 2
        near = lambda it: it['rect'][0] >= t['rect'][2] - 4 and abs((it['rect'][1] + it['rect'][3]) / 2 - cy) <= 45
    else:     # no "To:" label: header strip only (below the app's top bar), and only a phone number counts
        band_top = r[1] + round((r[3] - r[1]) * .10)
        limit = r[1] + round((r[3] - r[1]) * .27)
        top_bar = {re.sub(r'\D', '', m)[-10:] for it in items if it['rect'][1] < band_top for m in PHONE.findall(it['text'])}
        near = lambda it: (band_top <= it['rect'][1] < limit and it['rect'][0] > pts['first'][0] - 40
                           and not any(re.sub(r'\D', '', m)[-10:] in top_bar for m in PHONE.findall(it['text'])))
    texts = [it for it in items if near(it)
             and it['text'].strip() and it['text'].strip() not in CHROME and it['text'].strip() not in ('To:', 'To')]
    texts.sort(key=lambda it: (it['rect'][1], it['rect'][0]))
    for it in texts:
        m = PHONE.search(it['text'])
        if m: return 'phone:' + re.sub(r'\D', '', m.group(0))[-10:], ' | '.join(t['text'].strip() for t in texts)[:120]
    if texts and to: return 'name:' + norm(texts[0]['text']).lower(), ' | '.join(t['text'].strip() for t in texts)[:120]
    return None, ''

def known_headers(out, cfg):
    '''Header identities of conversations already saved (rebuilt from snapshots, so older runs count).'''
    seen = {}
    try: w, r, pts, _ = geometry(cfg)
    except Exception: return seen
    for p in (out / 'snapshots').glob('*_s000.json'):
        try:
            d = json.loads(p.read_text(encoding='utf-8'))
            hid = d.get('header_id') or header_identity(d.get('interface_controls', []) + d.get('history_candidates', []), r, pts)[0]
            if hid: seen[hid] = d.get('key')
        except Exception: continue
    return seen

# ---------------------------------------------------------------- LEFT list scrolling
class LeftScroller:
    """Scrolls ONLY the LEFT conversations list. UIA ScrollPattern first, then wheel
    at the calibrated LEFT-list point. Never clicks the scrollbar."""
    def __init__(self, cfg, delay):
        self.cfg = cfg; self.delay = delay; self.method = None; self.pattern = None; self.notches = 1

    def _find_pattern(self):
        w, r, pts, _ = geometry(self.cfg)
        lx, ly = pts['list']; best = None
        for el in w.descendants():
            try:
                b = el.rectangle()
                if not (b.left <= lx <= b.right and b.top <= ly <= b.bottom): continue
                if b.right >= pts['history'][0]: continue          # must not include RIGHT pane
                sp = el.iface_scroll
                if not sp.CurrentVerticallyScrollable: continue
                area = b.width() * b.height()
                if best is None or area < best[0]: best = (area, sp, b.bottom)
            except Exception: continue
        global LIST_BOTTOM
        if best: LIST_BOTTOM = best[2]
        return best[1] if best else None

    def crop(self, at_end=False):
        w, r, pts, delta = geometry(self.cfg)
        top, bottom = left_viewport(r, pts, delta, at_end)
        width = r[2] - r[0]
        x1 = max(r[0] + 10, pts['first'][0] - round(width * .08))
        x2 = min(pts['history'][0] - 40, pts['first'][0] + round(width * .08))
        if x2 - x1 < 60: raise SafeStop('LEFT contact-list crop invalid')
        for x, y in ((x1, top), (x2, bottom)):
            if not on_monitor(x, y): raise SafeStop('LEFT list is partly off-screen; move the window fully on-screen')
        return pg.screenshot(region=(x1, top, x2 - x1, bottom - top)).convert('L')

    def at_bottom(self):
        try: return self.pattern is not None and self.pattern.CurrentVerticalScrollPercent >= 99.9
        except Exception: return False

    def _step(self, method, down):
        if method == 'uia':
            self.pattern.Scroll(2, 4 if down else 1)          # NoAmount, Small(In|De)crement
        else:
            w, r, pts, _ = geometry(self.cfg)
            move_to(*pts['list'], r, 'LEFT list (wheel)')
            pg.scroll((-1 if down else 1) * NOTCH * self.notches)
        time.sleep(self.delay)

    def step(self, down=True):
        """Scroll one small step. Returns (before_img, after_img)."""
        if self.method is None:
            self.pattern = self._find_pattern()
            order = (['uia'] if self.pattern is not None else []) + ['wheel']
            before = after = None
            for m in order:
                before = self.crop()
                try: self._step(m, down)
                except SafeStop: raise
                except Exception: continue
                after = self.crop()
                moved = abs(measure_shift(before, after, allow_up=not down)[0])
                if moved >= max(8, .2 * geometry(self.cfg)[3]):          # a real scroll, not a 1-px nudge
                    self.method = m
                    print(f'LEFT list scrolling via {"UI Automation" if m == "uia" else "mouse wheel over LEFT list"}')
                    return before, after
            # Nothing moved: either already at the end, or no method works.
            if before is None: before = after = self.crop()
            return before, after
        before = self.crop(); self._step(self.method, down); return before, self.crop()

    def to_top(self, limit=400):
        self.notches = 5                                       # fast trip to the top
        try:
            self.step(down=False)
            if self.method == 'uia':
                try: self.pattern.SetScrollPercent(-1, 0); time.sleep(self.delay)
                except Exception: pass
            still = 0
            for _ in range(limit):
                b, a = self.step(down=False)
                still = still + 1 if measure_shift(b, a, allow_up=True)[0] == 0 else 0
                if still >= 2: return
        finally:
            self.notches = 1

def measure_shift(before, after, allow_up=False):
    """Vertical content shift in px between two grayscale crops (positive = content
    moved up, i.e. list scrolled down). Returns (shift_px, mismatch)."""
    scale = 4
    W, H = 40, max(8, before.height // scale)
    a = before.resize((W, H), Image.BILINEAR); b = after.resize((W, H), Image.BILINEAR)
    A = list(a.tobytes()); B = list(b.tobytes())
    if sum(abs(p - q) for p, q in zip(A, B)) / len(A) < 1.0: return 0, 0.0
    best = (1e9, 0)
    rng = range(-(H * 7 // 10), H * 7 // 10 + 1) if allow_up else range(0, H * 7 // 10 + 1)
    for dy in rng:
        if dy >= 0: pa, pb, rows = dy * W, 0, H - dy
        else: pa, pb, rows = 0, -dy * W, H + dy
        n = rows * W
        diff = sum(abs(A[pa + i] - B[pb + i]) for i in range(0, n, 2)) / (n / 2)
        if diff < best[0]: best = (diff, dy)
    if best[1] == 0: return 0, best[0]
    # Refine to 1-pixel precision at full height so error cannot pile up over a long list.
    Hf = before.height
    a2 = before.resize((W, Hf), Image.BILINEAR); b2 = after.resize((W, Hf), Image.BILINEAR)
    A2 = list(a2.tobytes()); B2 = list(b2.tobytes())
    coarse = round(best[1] * Hf / H); fine = (1e9, coarse)
    for dy in range(coarse - 6, coarse + 7):
        if dy == 0 or abs(dy) >= Hf * .8: continue
        if dy > 0: pa, pb, rows = dy * W, 0, Hf - dy
        else: pa, pb, rows = 0, -dy * W, Hf + dy
        n = rows * W
        diff = sum(abs(A2[pa + i] - B2[pb + i]) for i in range(0, n, 3)) / (n / 3)
        if diff < fine[0]: fine = (diff, dy)
    return fine[1], fine[0]

# ---------------------------------------------------------------- capture one conversation
LAST_HEADER = None
HEADER_DEBUG_DONE = False
def header_debug(out, items, r, pts):
    '''First time a header cannot be read: save what UI Automation shows at the top of the window
    (local file, may contain a name/number) and print a summary WITHOUT text (safe to screenshot).'''
    global HEADER_DEBUG_DONE
    if HEADER_DEBUG_DONE: return
    HEADER_DEBUG_DONE = True
    h = r[3] - r[1]; top = [it for it in items if it['rect'][1] < r[1] + .35 * h]
    write(out / 'header_debug.json', {'window': r, 'points': pts, 'items': top})
    print('NOTE: conversation header not readable - saved header_debug.json. Top-of-window elements (no text shown):')
    for it in sorted(top, key=lambda it: (it['rect'][1], it['rect'][0]))[:30]:
        x1, y1, x2, y2 = it['rect']
        print(f"   {it['type']:10s} x={x1 - r[0]:5d}-{x2 - r[0]:5d} y={y1 - r[1]:4d}-{y2 - r[1]:4d} ({100 * (y1 - r[1]) / h:4.1f}%)"
              f"  len={len(it['text']):3d} phone={'Y' if PHONE.search(it['text']) else '-'} to={'Y' if it['text'].strip() in ('To:', 'To') else '-'}")

def capture_conversation(out, state, journal, cfg, key, label, x, y, scrolls, delay, headers=None):
    '''Returns 'captured' or 'duplicate' (the opened conversation's header is already saved).'''
    try:
        w, r, pts, _ = geometry(cfg)
        click_at(x, y, r, f'contact row {label[:40]!r}'); time.sleep(delay)
        first = elements(w)
        hid, htext = header_identity(first, r, pts)
        global LAST_HEADER
        for wait in range(6):   # the header is drawn a moment after the conversation opens
            if hid and hid != LAST_HEADER: break
            time.sleep(max(.5, delay))
            w, r, pts, _ = geometry(cfg); first = elements(w)
            hid, htext = header_identity(first, r, pts)
        if not hid: header_debug(out, first, r, pts)
        else: LAST_HEADER = hid
        if headers is not None and hid and hid in headers:
            return 'duplicate:' + hid
        row_key = key
        if hid: key = 'h_' + sha(hid)[:20]
        unique = {}; last_signature = None; data = None
        for step in range(scrolls + 1):
            w, r, pts, _ = geometry(cfg)
            data = classify(first if step == 0 else elements(w), r, pts)
            signature = sha(json.dumps([i['text'] for i in data['history_candidates']], ensure_ascii=False))
            for item in data['history_candidates']:
                fingerprint = sha(item['classification'] + '|' + item['text'])
                if fingerprint not in unique: unique[fingerprint] = {**item, 'fingerprint': fingerprint}
            snapshot = {'key': key, 'row_key': row_key, 'row_label': label, 'header_id': hid, 'header_text': htext,
                        'step': step, 'captured_at': timestamp(),
                        'contact_candidates': data['contact_candidates'], 'history_candidates': data['history_candidates'],
                        'interface_controls': data['interface_controls'], 'history_complete': False,
                        'patient_identity_verified': False, 'sender_verified': False}
            write(out / 'snapshots' / f'{key}_s{step:03d}.json', snapshot)
            state['partial'][key] = {'last_step': step, 'unique_candidate_count': len(unique), 'last_snapshot': f'{key}_s{step:03d}.json'}
            write(journal, state)
            if signature == last_signature and step > 0: break
            last_signature = signature
            if step < scrolls:
                move_to(*pts['history'], r, 'RIGHT history'); pg.scroll(4 * NOTCH); time.sleep(delay)
        write(out / 'extracted' / f'{key}.json', {'key': key, 'row_key': row_key, 'row_label': label,
              'header_id': hid, 'header_text': htext, 'captured_at': timestamp(),
              'candidate_contact_labels': data['contact_candidates'],
              'deduplicated_history_text_candidates': list(unique.values()),
              'complete_history_verified': False, 'patient_identity_verified': False,
              'sender_attribution_verified': False, 'source': 'Windows UI Automation'})
        state['completed'].append(key); state['partial'].pop(key, None)
        if row_key != key: state['completed'].append(row_key)
        state.setdefault('labels', {})[key] = htext or label
        if headers is not None and hid: headers[hid] = key
        write(journal, state)
        print(f'{key} [{(htext or label)[:50]}]: {len(unique)} unique text candidates saved (not verified messages)')
        return 'captured'
    except Exception as e:
        state['errors'].append({'key': key, 'at': timestamp(), 'error': str(e)}); write(journal, state)
        raise

# ---------------------------------------------------------------- collection modes
def collect(out, pages, rows, scrolls, delay, capture_all, mode):
    cfg = read(out / 'calibration.json', None)
    if cfg is None: raise RuntimeError('Calibration missing. Run calibrate first.')
    journal = out / 'progress.json'
    state = read(journal, {'completed': [], 'partial': {}, 'errors': []})
    state.setdefault('labels', {})
    budget = None if capture_all else pages * rows
    scroller = LeftScroller(cfg, delay)
    print('Returning LEFT list to the top so the run is repeatable...')
    scroller.to_top()
    if mode == 'auto':
        mode = 'identity' if visible_rows(cfg) else 'positional'
        print(f'Mode: {mode} ({"rows read via UI Automation" if mode == "identity" else "UIA exposes no LEFT rows; using calibrated row spacing + measured scroll"})')
    run = collect_identity if mode == 'identity' else collect_positional
    captured = run(out, state, journal, cfg, scroller, budget, scrolls, delay)
    passes = 1; more = captured
    while capture_all and mode == 'identity' and more > 0 and passes < 4:
        # New messages move contacts to the top while we work; walk again until a pass finds nobody new.
        print(f'Pass {passes} saved new conversations; checking the list again from the top for anyone it moved...')
        scroller.to_top(); passes += 1
        more = run(out, state, journal, cfg, scroller, budget, scrolls, delay)
        captured += more
        if more == 0: print('No new conversations on this pass - complete.'); break
    print(f'Collection finished: {captured} new conversation(s) this run, {len(state["completed"])} total. '
          'All output remains local; verify before clinical use.')

def drawn_rows(cfg, scroller, rows_):
    """Drop rows UI Automation reports but that are not actually painted on screen
    (clipped below the visible list). Clicking those would hit something else."""
    w, r, pts, delta = geometry(cfg)
    top, _ = left_viewport(r, pts, delta, True)
    img = scroller.crop(at_end=True); keep = []
    for row in rows_:
        c = round(row['y'] - top)
        if c - 4 < 0 or c + 5 > img.height: continue
        # painted both just above AND just below the click point (not a clipped sliver)
        if all((lambda e: e[1] - e[0] >= 12)(img.crop((0, a, img.width, b)).getextrema()) for a, b in ((c - 4, c), (c, c + 5))):
            keep.append(row)
    return keep

def collect_identity(out, state, journal, cfg, scroller, budget, scrolls, delay):
    done = set(state['completed']); captured = 0; stalls = 0
    headers = known_headers(out, cfg)
    print(f'{len(headers)} conversation(s) already saved are recognized by their header.')
    opened = set(); offset = 0.0; same_header = {}   # nameless rows are remembered by their position in the whole list
    while budget is None or captured < budget:
        relaxed = stalls > 0 and scroller.method is not None
        rows_ = visible_rows(cfg, at_end=relaxed)
        if relaxed: rows_ = drawn_rows(cfg, scroller, rows_)
        pos = lambda row: (row['key'], round((row['y'] + offset) / 24))
        todo = [row for row in rows_ if not (row['strong'] and row['key'] in done)
                and not ({pos(row), (row['key'], pos(row)[1] - 1), (row['key'], pos(row)[1] + 1)} & opened)]
        if todo:
            row = todo[0]; opened.add(pos(row))
            res = capture_conversation(out, state, journal, cfg, row['key'], row['label'], row['x'], row['y'],
                                       scrolls, delay, headers)
            if res == 'captured': captured += 1; same_header.clear()
            else:
                # Different rows opening the SAME header again and again means the header is not being
                # read (e.g. a stuck pane). Stop rather than skip people.
                same_header[res] = same_header.get(res, set()) | {row['key']}
                if len(same_header[res]) >= 4:
                    raise SafeStop('Several new contacts in a row were read as already saved - the conversation '
                                   'header is not being read correctly. Stopped so no one is skipped.')
            if row['strong']: done.add(row['key'])
            continue                     # re-read rows: clicking can restyle/reorder the list
        before_keys = [row['key'] for row in rows_]
        b, a = scroller.step(down=True)
        shift = measure_shift(b, a)[0]; offset += shift
        after_keys = [row['key'] for row in visible_rows(cfg, at_end=stalls > 0 and scroller.method is not None)]
        if after_keys and before_keys and not (set(before_keys) & set(after_keys)) and shift == 0:
            # A new message moved contacts around. Keep going: saved contacts are skipped by name/header,
            # and the extra pass at the end catches anyone who jumped above us.
            print('Note: contact list re-ordered (new message arrived) - continuing.')
            opened = set(); stalls = 0; continue
        if shift == 0 and after_keys == before_keys:
            stalls += 1
            if stalls >= 3 or (stalls >= 2 and scroller.at_bottom()):
                strict = {row['key'] for row in visible_rows(cfg)}
                more = [row for row in visible_rows(cfg, at_end=True) if row['key'] not in strict]
                img = scroller.crop(); _, _, _, dl = geometry(cfg)
                lo, hi = img.crop((0, max(0, img.height - round(.6 * dl)), img.width, img.height)).getextrema()
                if scroller.method is None and (more or hi - lo >= 12):     # rows reach the bottom edge
                    raise SafeStop('LEFT list would not scroll (neither UI Automation nor mouse wheel moved it), '
                                   'so the end of the list was NOT reached. Nothing was skipped; progress is saved.')
                print('Reached the end of the LEFT list.'); break
        else: stalls = 0
    return captured

def collect_positional(out, state, journal, cfg, scroller, budget, scrolls, delay):
    """Rows identified as list index from the top. Scroll distance is measured from
    pixels so each index maps to one real row; nothing skipped or doubled."""
    done = set(state['completed']); captured = 0; offset = 0.0; stalls = 0; nxt = 0
    while budget is None or captured < budget:
        w, r, pts, delta = geometry(cfg)
        relaxed = stalls > 0 and scroller.method is not None
        top, bottom = left_viewport(r, pts, delta, at_end=relaxed)
        progressed = False; ended = False
        while budget is None or captured < budget:
            y = pts['first'][1] + (nxt - offset / delta) * delta
            if y < top + .3 * delta: nxt += 1; continue          # scrolled past (already handled)
            if y > bottom - (.2 if relaxed else .5) * delta: break
            img = scroller.crop(at_end=relaxed)
            y1b, y2b = max(0, round(y - top - .35 * delta)), min(img.height, round(y - top + .35 * delta))
            if y2b - y1b < 8: break
            band = img.crop((0, y1b, img.width, y2b))
            lo, hi = band.getextrema()
            if hi - lo < 12:
                if y <= left_viewport(r, pts, delta)[1] - .5 * delta:   # blank INSIDE the normal list area
                    ended = True; print('LEFT list has no more rows (blank row area).')
                break
            key = f'row{nxt:05d}'
            if key not in done:
                capture_conversation(out, state, journal, cfg, key, f'list row {nxt}', pts['first'][0], y, scrolls, delay)
                done.add(key); captured += 1
            nxt += 1; progressed = True
        if ended or (budget is not None and captured >= budget): break
        b, a = scroller.step(down=True)
        shift, mismatch = measure_shift(b, a)
        if shift == 0:
            stalls += 1
            if stalls >= 3 or (stalls >= 2 and scroller.at_bottom()):
                if scroller.method is None:
                    raise SafeStop('LEFT list would not scroll; end of list NOT reached. Progress is saved.')
                print('Reached the end of the LEFT list.'); break
            continue
        if mismatch > 25:
            raise SafeStop('LEFT list scroll distance could not be measured reliably; stopped (progress saved)')
        stalls = 0; offset += shift
    return captured

# ---------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(description='Grasshopper collector v6')
    p.add_argument('mode', choices=['calibrate', 'collect', 'inspect', 'rows'])
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--pages', type=int, default=1, help='with --rows: capture pages*rows new conversations this run')
    p.add_argument('--rows', type=int, default=3)
    p.add_argument('--all', action='store_true', help='keep going to the end of the LEFT list')
    p.add_argument('--history-scrolls', type=int, default=8)
    p.add_argument('--delay', type=float, default=.6)
    p.add_argument('--row-mode', choices=['auto', 'identity', 'positional'], default='auto')
    a = p.parse_args()
    if not (1 <= a.pages <= 10000 and 1 <= a.rows <= 100 and 0 <= a.history_scrolls <= 500 and .2 <= a.delay <= 10): p.error('Invalid numeric parameters')
    a.output.mkdir(parents=True, exist_ok=True)
    try:
        if a.mode == 'calibrate': calibrate(a.output)
        elif a.mode == 'inspect':
            w = window(); write(a.output / 'uia_inspection.json', {'captured_at': timestamp(), 'controls': elements(w)}); print('UIA inspection saved locally')
        elif a.mode == 'rows':
            cfg = read(a.output / 'calibration.json', None)
            if cfg is None: raise RuntimeError('Calibration missing. Run calibrate first.')
            rows_ = visible_rows(cfg)
            print(f'{len(rows_)} LEFT rows visible via UI Automation' + ('' if rows_ else ' -> collect will use positional mode'))
            for row in rows_: print(f"  y={row['y']:5d}  {row['key']}  {row['label']}")
        else: collect(a.output, a.pages, a.rows, a.history_scrolls, a.delay, a.all, a.row_mode)
    except pg.FailSafeException:
        print('\nSTOPPED by fail-safe: the mouse pointer reached a screen corner. Progress is saved; '
              'run the same command again to resume.'); sys.exit(2)
    except SafeStop as e:
        print(f'\nSTOPPED safely: {e}\nProgress is saved; fix the above and run the same command to resume.'); sys.exit(3)
    except KeyboardInterrupt:
        print('\nSTOPPED by Ctrl+C. Progress is saved.'); sys.exit(130)

if __name__ == '__main__': main()
