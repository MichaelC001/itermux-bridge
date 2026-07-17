"""copy-mode: selection maths, text extraction, OSC 52."""

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itermux_bridge import copymode
from itermux_bridge.copymode import CopyMode, Selection

ok = True


def check(label, cond, extra=""):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {label} {extra}")
    ok = ok and cond


print("\n=== selection geometry ===")

s = Selection(1, 2, 3, 4)
check("contains a cell inside the range", s.contains(2, 0))
check("contains the start cell", s.contains(1, 2))
check("contains the end cell", s.contains(3, 4))
check("excludes before the start on the first row", not s.contains(1, 1))
check("excludes after the end on the last row", not s.contains(3, 5))
check("excludes rows outside entirely", not s.contains(0, 3) and not s.contains(4, 0))

# Dragging upward/backwards must select the same region as dragging forwards.
back = Selection(3, 4, 1, 2)
check("a backwards drag normalizes to the same range",
      back.normalized() == s.normalized())
check("...and selects the same cells", back.contains(2, 0))

# Single-row selection.
one = Selection(5, 3, 5, 6)
check("single-row range respects both bounds",
      one.contains(5, 3) and one.contains(5, 6)
      and not one.contains(5, 2) and not one.contains(5, 7))

print("\n=== text extraction ===")

lines = ["hello world", "second line", "third line here"]

check("single-row extract",
      copymode.extract(lines, Selection(0, 0, 0, 4)) == "hello")
check("mid-row extract",
      copymode.extract(lines, Selection(0, 6, 0, 10)) == "world")

multi = copymode.extract(lines, Selection(0, 6, 2, 4))
check("multi-row extract joins with newlines",
      multi == "world\nsecond line\nthird", f"({multi!r})")

check("a backwards drag extracts the same text",
      copymode.extract(lines, Selection(2, 4, 0, 6)) == multi)

check("trailing whitespace is stripped per line",
      copymode.extract(["abc    ", "def"], Selection(0, 0, 1, 2)) == "abc\ndef")

check("out-of-range rows are clamped, not crashing",
      copymode.extract(lines, Selection(0, 0, 99, 5)) != "")

print("\n=== OSC 52 (system clipboard) ===")

seq = copymode.osc52("hi there")
check("starts the OSC 52 clipboard sequence", seq.startswith(b"\033]52;c;"))
check("ends with BEL", seq.endswith(b"\a"))
payload = seq[len(b"\033]52;c;"):-1]
check("payload is base64 of the text",
      base64.b64decode(payload).decode() == "hi there")

# Unicode must survive the round trip.
seq = copymode.osc52("中文 ✓")
payload = seq[len(b"\033]52;c;"):-1]
check("utf-8 survives the round trip",
      base64.b64decode(payload).decode() == "中文 ✓")

print("\n=== mode state machine ===")

cm = CopyMode()
check("starts inactive", not cm.active)

cm.enter(rows=24)
check("enter activates the mode", cm.active)
check("cursor starts on the last row", cm.cy == 23)
check("no selection until one is started", cm.selection is None)

cm.start_selection()
cm.move(-2, 5, rows=24, cols=80)
check("moving after starting a selection extends it",
      cm.selection == Selection(23, 0, 21, 5), f"({cm.selection})")

cm.move(-100, -100, rows=24, cols=80)
check("movement is clamped to the screen", cm.cy == 0 and cm.cx == 0)

cm.leave()
check("leaving deactivates and drops the selection",
      not cm.active and cm.selection is None)

print("\n=== confined to one pane (window mode) ===")

# Regression: the selection was in screen coordinates with no pane boundary, so
# dragging across a divider selected the NEIGHBOURING pane's text — and the
# divider glyphs themselves — into the clipboard.
#
# A 2-column layout: left pane occupies cols 0-9, divider at 10, right at 11-20.
LEFT = (0, 0, 10, 5)          # x, y, width, height

screen = [
    "AAAAAAAAAA│BBBBBBBBBB",
    "aaaaaaaaaa│bbbbbbbbbb",
    "1111111111│2222222222",
]

# Drag from inside the left pane all the way across into the right one.
wide = Selection(0, 0, 0, 20)
check("extract stops at the pane's right edge",
      copymode.extract(screen, wide, LEFT) == "AAAAAAAAAA",
      f"({copymode.extract(screen, wide, LEFT)!r})")
check("...so the divider is never copied",
      "│" not in copymode.extract(screen, wide, LEFT))
check("...and neither is the other pane's text",
      "B" not in copymode.extract(screen, wide, LEFT))

multi = copymode.extract(screen, Selection(0, 0, 2, 20), LEFT)
check("a multi-row selection is clipped on every row",
      multi == "AAAAAAAAAA\naaaaaaaaaa\n1111111111", f"({multi!r})")

# Without bounds (single-pane mode) the whole row is fair game, as before.
check("no bounds -> the full row is selectable",
      copymode.extract(screen, wide) == "AAAAAAAAAA│BBBBBBBBBB")

# The cursor must be clamped inside the pane.
cm = CopyMode()
cm.enter(rows=24, bounds=LEFT)
check("entering bounded copy-mode puts the cursor inside the pane",
      LEFT[0] <= cm.cx < LEFT[0] + LEFT[2]
      and LEFT[1] <= cm.cy < LEFT[1] + LEFT[3], f"({cm.cx},{cm.cy})")

cm.cx, cm.cy = 50, 50
cm.clamp()
check("the cursor cannot leave the pane",
      cm.cx == 9 and cm.cy == 4, f"({cm.cx},{cm.cy})")

check("in_bounds rejects cells in the other pane",
      cm.in_bounds(0, 5) and not cm.in_bounds(0, 15)
      and not cm.in_bounds(0, 10))   # the divider itself

print("\n=== split escape sequences (the accidental-quit bug) ===")

import asyncio
from itermux_bridge.iterm_backend import ITermBackend


class _TTY:
    def size(self): return (120, 40)


class _Peer:
    def __init__(self):
        self.copy = CopyMode()
        self.tty = _TTY()
        self.scroll_offset = 0
        self.iterm_session_id = "s"
        self.window_mode = False
        self.to_app = bytearray()


def _drive(inputs):
    """Feed a list of byte chunks through _copy_key; return the peer."""
    be = ITermBackend.__new__(ITermBackend)
    loop = asyncio.new_event_loop()
    be.loop = loop
    sess = object()
    be._session_of = lambda peer: sess

    async def _paint(peer, session, contents=None): pass
    be._paint = _paint
    be.on_input = lambda peer, keys: peer.to_app.extend(keys)

    p = _Peer(); p.copy.enter(40)
    for chunk in inputs:
        loop.run_until_complete(be._copy_key(p, chunk))
    # Let any scheduled ESC-timeout tasks finish so the loop closes cleanly.
    loop.run_until_complete(asyncio.sleep(0.1))
    loop.close()
    return p


# A split arrow key (ESC then [A) must NOT quit copy-mode — this was the
# intermittent "accidentally left / can't stay in copy-mode".
p = _drive([b"\x1b", b"[A"])
check("split arrow key does not quit copy-mode", p.copy.active)
check("...and leaks nothing to the app", not p.to_app)

# A real Escape (ESC not continued by [ or O) still quits.
p = _drive([b"\x1b", b"x"])
check("a real Esc (not a split arrow) still quits", not p.copy.active)

# A complete arrow key in one chunk still works.
p = _drive([b"\x1b[A"])
check("a whole arrow key in one read still works", p.copy.active)

# q still quits.
p = _drive([b"q"])
check("q quits", not p.copy.active)

# Split PgUp (ESC[ then 5~) must not quit either.
p = _drive([b"\x1b[", b"5~"])
check("split PgUp does not quit copy-mode", p.copy.active)

print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
sys.exit(0 if ok else 1)
