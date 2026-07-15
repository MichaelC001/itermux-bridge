"""Split-tree -> screen regions.

Models iTerm2's Splitter/Session tree: a Splitter has `.vertical` and
`.children`; a leaf Session has `.session_id` and `.grid_size`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itermux_bridge import layout


class Grid:
    def __init__(self, w, h):
        self.width, self.height = w, h


class Sess:
    def __init__(self, sid, w, h):
        self.session_id = sid
        self.grid_size = Grid(w, h)


class Split:
    def __init__(self, vertical, children):
        self.vertical = vertical
        self.children = children


ok = True


def check(label, cond, extra=""):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {label} {extra}")
    ok = ok and cond


def overlaps(regions):
    cells = set()
    for r in regions:
        for y in range(r.y, r.y + r.height):
            for x in range(r.x, r.x + r.width):
                if (x, y) in cells:
                    return True
                cells.add((x, y))
    return False


print("\n=== split layout ===")

# Single pane fills everything.
rs = layout.regions(Sess("a", 80, 24), 80, 24)
check("single pane fills the screen",
      len(rs) == 1 and (rs[0].width, rs[0].height) == (80, 24))

# Two side by side: 80 cols - 1 divider = 79, split ~40/39.
rs = layout.regions(Split(True, [Sess("a", 40, 24), Sess("b", 40, 24)]), 80, 24)
check("vertical split -> two columns", len(rs) == 2)
check("a divider column is reserved",
      rs[0].width + rs[1].width == 79, f"({rs[0].width}+{rs[1].width})")
check("second column starts after the divider",
      rs[1].x == rs[0].x + rs[0].width + 1)
check("both are full height", all(r.height == 24 for r in rs))

# Two stacked.
rs = layout.regions(Split(False, [Sess("a", 80, 12), Sess("b", 80, 12)]), 80, 24)
check("horizontal split -> two rows",
      len(rs) == 2 and rs[0].height + rs[1].height == 23)
check("second row starts after the divider",
      rs[1].y == rs[0].y + rs[0].height + 1)

# The real mini2 shape: 2 columns, each split into 3 rows.
tree = Split(True, [
    Split(False, [Sess("a", 99, 16), Sess("b", 99, 17), Sess("c", 99, 15)]),
    Split(False, [Sess("d", 101, 15), Sess("e", 101, 17), Sess("f", 101, 16)]),
])
rs = layout.regions(tree, 120, 40)
check("6-pane tree -> 6 regions", len(rs) == 6)
check("no two panes overlap", not overlaps(rs))
check("everything stays on screen",
      all(r.x >= 0 and r.y >= 0 and r.x + r.width <= 120
          and r.y + r.height <= 40 for r in rs))

left = [r for r in rs if r.session_id in "abc"]
right = [r for r in rs if r.session_id in "def"]
check("left column panes share an x", len({r.x for r in left}) == 1)
check("right column is to the right of the left",
      min(r.x for r in right) > max(r.x + r.width for r in left) - 1)
check("each column's 3 panes stack vertically",
      len({r.y for r in left}) == 3 and len({r.y for r in right}) == 3)

# Proportions: iTerm2 had rows 16/17/15, so the middle pane should be tallest.
heights = {r.session_id: r.height for r in rs}
check("pane heights follow iTerm2's real proportions",
      heights["b"] >= heights["a"] >= heights["c"], f"({heights})")

# Degenerate: a tiny client must not produce negative or zero-sized regions.
rs = layout.regions(tree, 10, 6)
check("tiny client still yields usable regions",
      all(r.width >= 1 and r.height >= 1 for r in rs))

print("\n=== active pane border ===")

from itermux_bridge import ansi


class Grid2:
    def __init__(self, w, h): self.width, self.height = w, h


class FakeLine:
    string = "x"
    def style_at(self, i): return None


class FakeContents:
    number_of_lines = 1
    def __init__(self):
        class P: pass
        self.cursor_coord = P()
        self.cursor_coord.x = self.cursor_coord.y = 0
    def line(self, i): return FakeLine()


# Two side-by-side panes: the divider between them must be highlighted when one
# of them is active, so you can see where your keystrokes land.
rs = layout.regions(Split(True, [Sess("a", 40, 24), Sess("b", 40, 24)]), 80, 24)
panes = [(r, FakeContents()) for r in rs]

out_a = ansi.render_panes(panes, 80, 24, active_id="a")
check("active pane's border uses the highlight colour",
      ansi.ACTIVE_DIVIDER_SGR in out_a)

# With no active pane, every divider is the dim colour.
out_none = ansi.render_panes(panes, 80, 24, active_id="")
check("no highlight when nothing is active",
      ansi.ACTIVE_DIVIDER_SGR not in out_none
      and ansi.DIVIDER_SGR in out_none)

# Switching the active pane must move the highlight, not just add one.
out_b = ansi.render_panes(panes, 80, 24, active_id="b")
check("the highlight follows the active pane", out_a != out_b)

print("\n=== copy-mode in WINDOW mode ===")

from itermux_bridge.copymode import CopyMode

# Regression: copy-mode was only wired into the single-pane renderer. In window
# mode (`-t @N`) the state changed server-side but NOTHING was drawn — no status
# bar, no selection — which is indistinguishable from "copy-mode doesn't work".
cm = CopyMode()
cm.enter(rows=24)
out = ansi.render_panes(panes, 80, 24, active_id="a", copy=cm)
check("copy-mode status bar is drawn in window mode", b"COPY" in out)

cm.cy, cm.cx = 5, 2
cm.start_selection()
cm.cy, cm.cx = 5, 20
out = ansi.render_panes(panes, 80, 24, active_id="a", copy=cm)
check("the selection is highlighted in window mode",
      ansi.SELECTION_SGR in out)

# And with copy-mode off, neither appears.
out = ansi.render_panes(panes, 80, 24, active_id="a", copy=CopyMode())
check("no copy-mode chrome when the mode is off",
      b"COPY" not in out and ansi.SELECTION_SGR not in out)

print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
sys.exit(0 if ok else 1)
