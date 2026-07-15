"""Mouse event parsing and re-encoding.

The client's terminal reports mouse events in SGR form (DECSET 1006):

    ESC [ < Cb ; Cx ; Cy M     button press / motion
    ESC [ < Cb ; Cx ; Cy m     button release

Cb packs the button and its modifiers:

    bits 0-1  button: 0=left 1=middle 2=right 3=release(legacy)
    bit  2    (4)  shift
    bit  3    (8)  alt/meta
    bit  4    (16) ctrl
    bit  5    (32) motion (drag)
    bit  6    (64) wheel — button becomes 64=up, 65=down

Cx/Cy are 1-based columns/rows.

Two things are done with these:
  * wheel events, when the app hasn't asked for mouse reporting, scroll our view
    of iTerm2's scrollback instead (the tmux copy-mode behaviour);
  * everything else is re-encoded and handed to the app running in iTerm2, so
    clicking and dragging in the tmux client behaves as it does in iTerm2.
"""

import re
from typing import List, NamedTuple, Optional, Tuple

#: ESC [ < b ; x ; y (M|m)
SGR_RE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")

WHEEL_BIT = 64
MOTION_BIT = 32

WHEEL_UP = 64
WHEEL_DOWN = 65


class MouseEvent(NamedTuple):
    cb: int          # raw button/modifier byte
    x: int           # 1-based column
    y: int           # 1-based row
    pressed: bool    # True for 'M', False for 'm' (release)

    @property
    def is_wheel(self) -> bool:
        return bool(self.cb & WHEEL_BIT)

    @property
    def wheel_up(self) -> bool:
        return self.is_wheel and (self.cb & 0b11) == 0

    @property
    def wheel_down(self) -> bool:
        return self.is_wheel and (self.cb & 0b11) == 1

    def encode(self) -> bytes:
        """Back to an SGR mouse report, for the app running in iTerm2."""
        return b"\x1b[<%d;%d;%d%s" % (
            self.cb, self.x, self.y, b"M" if self.pressed else b"m")


def parse(data: bytes) -> Tuple[List[MouseEvent], bytes]:
    """Split a keystroke buffer into (mouse events, everything else).

    Mouse reports are interleaved with ordinary keys in the same read, so pull
    them out and leave the rest to be forwarded as normal input.
    """
    events: List[MouseEvent] = []
    rest = bytearray()
    pos = 0

    for m in SGR_RE.finditer(data):
        rest += data[pos:m.start()]
        pos = m.end()
        events.append(MouseEvent(
            cb=int(m.group(1)),
            x=int(m.group(2)),
            y=int(m.group(3)),
            pressed=m.group(4) == b"M",
        ))

    rest += data[pos:]
    return events, bytes(rest)
