#!/usr/bin/env python3
"""Summarize a collector_v6 output folder WITHOUT printing message text (safe to screenshot)."""
import json, sys
from collections import Counter, defaultdict
from pathlib import Path

out = Path(sys.argv[1] if len(sys.argv) > 1 else r'C:\Users\marcu\Documents\GrasshopperOutput')
prog = json.loads((out / 'progress.json').read_text(encoding='utf-8')) if (out / 'progress.json').exists() else {}
done = prog.get('completed', []); labels = prog.get('labels', {})
snaps = defaultdict(list)
for p in (out / 'snapshots').glob('*.json'):
    try: d = json.loads(p.read_text(encoding='utf-8'))
    except Exception: continue
    snaps[d.get('key', p.stem.rsplit('_s', 1)[0])].append(d)

print(f'Folder: {out}')
print(f'Completed conversations: {len(done)}   snapshot files: {sum(len(v) for v in snaps.values())}   keys with snapshots: {len(snaps)}')

# Same contact saved under different keys? Compare by row label and by the
# RIGHT-pane history content fingerprint.
label_count = Counter(labels.get(k, '?') for k in done)
hist_fp = {}
for k, steps in snaps.items():
    s0 = min(steps, key=lambda d: d.get('step', 0))
    hist_fp[k] = hash(tuple(sorted(i['text'] for i in s0.get('history_candidates', []))))
fp_count = Counter(hist_fp.values())

print('\n key                     steps  history_items  same_history_as_another  label')
for k in done:
    steps = snaps.get(k, [])
    items = max((len(d.get('history_candidates', [])) for d in steps), default=0)
    dup = 'YES' if fp_count.get(hist_fp.get(k), 0) > 1 else ''
    print(f' {k:22s} {len(steps):5d}  {items:13d}  {dup:23s}  {labels.get(k, "?")[:45]}')

dupe_labels = [l for l, c in label_count.items() if c > 1]
dupe_hist = sum(c for c in fp_count.values() if c > 1)
print(f'\nDuplicate labels: {len(dupe_labels)}   conversations sharing identical history: {dupe_hist}')
empty = [k for k in done if all(not d.get('history_candidates') for d in snaps.get(k, []))]
print(f'Conversations with NO message text captured: {len(empty)}')
errs = prog.get('errors', [])
print(f'Errors logged: {len(errs)}' + ''.join(f'\n  {e.get("key")}: {str(e.get("error"))[:110]}' for e in errs[-3:]))
