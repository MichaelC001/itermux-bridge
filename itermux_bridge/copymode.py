"""copy-mode: select text with the mouse or the keyboard, then copy it.

Mirrors tmux's own model:

    Ctrl-B [        enter copy-mode
    arrows / hjkl   move the cursor
    Space or v      start a selection
    Enter or y      copy the selection and leave
    q or Escape     leave without copying
    PgUp/PgDn       page through the scrollback
    Home/End, g/G   jump to the start/end of the line / buffer

The mouse works too: press-drag-release selects, and a release with a non-empty
selection copies immediately (like tmux's default mouse binding).

The copied text goes to the client's SYSTEM clipboard via OSC 52 — a tmux-style
paste buffer that lives only inside the bridge would be a dead end, since the
whole point is to get the text into whatever the user is doing next.
"""

import base64
from typing import List, NamedTuple, Optional, Tuple


class Selection(NamedTuple):
    """An inclusive character range across lines, in buffer coordinates."""
    y0: int
    x0: int
    y1: int
    x1: int

    def normalized(self) -> "Selection":
        """Reorder so (y0,x0) precedes (y1,x1), whichever way it was dragged."""
        if (self.y0, self.x0) <= (self.y1, self.x1):
            return self
        return Selection(self.y1, self.x1, self.y0, self.x0)

    def contains(self, y: int, x: int) -> bool:
        s = self.normalized()
        if y < s.y0 or y > s.y1:
            return False
        if s.y0 == s.y1:
            return s.x0 <= x <= s.x1
        if y == s.y0:
            return x >= s.x0
        if y == s.y1:
            return x <= s.x1
        return True


class CopyMode:
    """Per-peer copy-mode state. Coordinates are rows/cols on the visible grid."""

    def __init__(self) -> None:
        self.active = False
        self.cy = 0                     # cursor row (screen-relative)
        self.cx = 0                     # cursor column
        self.anchor: Optional[Tuple[int, int]] = None   # where selection began
        self.scroll = 0                 # lines scrolled back while in the mode
        #: The pane copy-mode is confined to, as (x, y, width, height) on the
        #: screen. In window mode a selection must NOT spill across the divider
        #: into the neighbouring pane — real tmux keeps copy-mode inside one
        #: pane, and a selection that crossed panes would copy the divider
        #: glyphs and the other pane's text along with it.
        self.bounds: Optional[Tuple[int, int, int, int]] = None
        #: An incomplete escape sequence (e.g. a lone ESC of a split arrow key)
        #: held over to the next read, so a bare ESC isn't mistaken for "quit".
        self.pending = bytearray()

    # --- lifecycle ---------------------------------------------------------

    def enter(self, rows: int, scroll: int = 0, bounds=None) -> None:
        self.active = True
        self.bounds = bounds
        self.pending = bytearray()
        if bounds is not None:
            bx, by, bw, bh = bounds
            self.cy = by + bh - 1
            self.cx = bx
        else:
            self.cy = max(0, rows - 1)
            self.cx = 0
        self.anchor = None
        self.scroll = scroll

    def clamp(self) -> None:
        """Keep the cursor inside the pane copy-mode is bound to."""
        if self.bounds is None:
            return
        bx, by, bw, bh = self.bounds
        self.cy = min(max(self.cy, by), by + bh - 1)
        self.cx = min(max(self.cx, bx), bx + bw - 1)

    def in_bounds(self, y: int, x: int) -> bool:
        if self.bounds is None:
            return True
        bx, by, bw, bh = self.bounds
        return bx <= x < bx + bw and by <= y < by + bh

    def leave(self) -> None:
        self.active = False
        self.anchor = None
        self.pending = bytearray()

    # --- selection ---------------------------------------------------------

    @property
    def selection(self) -> Optional[Selection]:
        if self.anchor is None:
            return None
        ay, ax = self.anchor
        return Selection(ay, ax, self.cy, self.cx)

    def start_selection(self) -> None:
        self.anchor = (self.cy, self.cx)

    def clear_selection(self) -> None:
        self.anchor = None

    # --- movement ----------------------------------------------------------

    def move(self, dy: int, dx: int, rows: int, cols: int) -> None:
        self.cy = min(max(self.cy + dy, 0), rows - 1)
        self.cx = min(max(self.cx + dx, 0), cols - 1)
        self.clamp()

    def to_line_start(self) -> None:
        self.cx = self.bounds[0] if self.bounds else 0

    def to_line_end(self, cols: int) -> None:
        if self.bounds:
            bx, _by, bw, _bh = self.bounds
            self.cx = bx + bw - 1
        else:
            self.cx = cols - 1

    def to_top(self) -> None:
        self.cy = self.bounds[1] if self.bounds else 0

    def to_bottom(self, rows: int) -> None:
        if self.bounds:
            _bx, by, _bw, bh = self.bounds
            self.cy = by + bh - 1
        else:
            self.cy = rows - 1


def extract(lines: List[str], sel: Selection, bounds=None) -> str:
    """Pull the selected text out of the rendered lines.

    `bounds` (x, y, width, height) confines the result to one pane. Without it a
    multi-pane screen would hand back the neighbouring pane's text and the
    divider glyphs, since the selection is in screen coordinates.
    """
    s = sel.normalized()
    if not lines:
        return ""

    lo_x, hi_x = 0, None
    lo_y, hi_y = 0, len(lines) - 1
    if bounds is not None:
        bx, by, bw, bh = bounds
        lo_x, hi_x = bx, bx + bw          # [lo_x, hi_x)
        lo_y, hi_y = by, min(by + bh - 1, len(lines) - 1)

    y0 = max(lo_y, min(s.y0, hi_y))
    y1 = max(lo_y, min(s.y1, hi_y))

    def cut(line: str, a: int, b) -> str:
        a = max(a, lo_x)
        b = len(line) if b is None else b
        if hi_x is not None:
            b = min(b, hi_x)
        return line[a:b].rstrip()

    if y0 == y1:
        return cut(lines[y0], s.x0, s.x1 + 1)

    out = [cut(lines[y0], s.x0, None)]
    out.extend(cut(lines[y], 0, None) for y in range(y0 + 1, y1))
    out.append(cut(lines[y1], 0, s.x1 + 1))
    return "\n".join(out)


def osc52(text: str) -> bytes:
    """Put `text` on the client's system clipboard.

    OSC 52 is how a remote/multiplexed program reaches the *local* clipboard —
    the terminal that receives it does the pasting. Without this, copied text
    would sit in a buffer inside the bridge, reachable by nothing.
    """
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return b"\033]52;c;" + payload.encode("ascii") + b"\a"
