Grasshopper Collector v6.1  (replaces v5 and v6.0)

v6.1 FIXES (found on the first live run)
- Lists never scrolled: pyautogui on Windows counts wheel in raw units (1 notch = 120); v6.0 sent 1.
  Contact list AND message history now scroll for real.
- A contact could be saved twice (initials/preview read differently). Identity = name line or phone only.
- If the list will not scroll, it now STOPS and says so instead of claiming it reached the end.
- analyze_needs.py: reads who sent each practice message (e.g. LaMonica Hodges), marks automatic replies,
  ignores Grasshopper banners, and no longer drops patient messages that mention 'today' or 'voicemail'.
- check_run.py: counts-only summary of an output folder (no message text).

WHY v5 CRASHED
v5 clicked a guessed scrollbar spot (35.4% across the window, 94% down). That spot
was off your screen, so Windows pinned the cursor into the screen corner, and
PyAutoGUI's fail-safe fired (traceback line 1514 = the check right after the move).
v5 had a second problem too: one track click jumps a whole screen, so with
--rows 3 it skipped every contact below row 3.

WHAT v6 DOES
- LEFT list scrolls via UI Automation (no mouse), else the mouse wheel at your
  calibrated LEFT-list point. It never clicks the scrollbar.
- Every click target is checked to be inside the window AND on a monitor,
  and the pointer position is checked again after the move.
- Contacts are tracked by identity (phone number / row text), scrolled in small
  overlapping steps, so none are skipped or captured twice. A re-run resumes.
- If Grasshopper hides the rows from UI Automation, it falls back to row spacing
  plus a pixel-measured scroll distance (also no skips).
- Fail-safe, Ctrl+C or a safety stop exits cleanly with progress saved.

RUN  (Windows PowerShell, one command per line; use your existing output folder)
cd $HOME\Downloads\Grasshopper_Collector_v6
python collector_v6.py rows --output <your output folder>
python collector_v6.py collect --output <your output folder> --pages 2 --rows 3
python collector_v6.py collect --output <your output folder> --all

"rows" only prints the contacts it can see; it does not click anything.
--pages N --rows M  = capture N*M new conversations this run.
--all               = keep going to the end of the list.
Keep the Grasshopper window fully on one monitor (maximized is best). If you moved
or resized it since calibrating, run:  python collector_v6.py calibrate --output <folder>

NOTE: v5 saved records as p0000_r000 etc. v6 names them c_<id> (or row00000), so
contacts captured by v5 are captured once more under the new names. v5 files are untouched.
