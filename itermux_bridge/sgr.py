"""SGR encoding: iTerm2 CellStyle -> ANSI escape sequences, and the
control sequences the bridge uses.

The reverse of what a terminal normally does. GetBufferRequest hands back
structured text plus a CellStyle; to drive a dumb tmux client we have to turn
that back into SGR ourselves. Kept apart from screen composition (ansi.py) —
this half is about *one cell's appearance*, that half is about *the whole
screen's layout*.
"""

import unicodedata
from typing import List


CSI = b"\033["

# Screen setup/teardown
ENTER_ALT = b"\033[?1049h"
EXIT_ALT = b"\033[?1049l"
HIDE_CURSOR = b"\033[?25l"
SHOW_CURSOR = b"\033[?25h"
CLEAR = b"\033[2J"
HOME = b"\033[H"
RESET_SGR = b"\033[m"

# Mouse reporting, asked of the CLIENT's terminal so it forwards events to us.
#   1000 = button press/release
#   1002 = also report drags while a button is held
#   1003 = report ALL motion (we don't want this: far too chatty over a socket)
#   1006 = SGR encoding — "\033[<b;x;yM/m", which unlike the legacy X10 scheme
#          isn't capped at column 223, so it works on wide windows.
#: Dim grey, so dividers read as chrome rather than content.
DIVIDER_SGR = b"\033[0;90m"

#: The border around the ACTIVE pane, so you can see where your keys will go.
#: Green, matching tmux's own pane-active-border-style default.
ACTIVE_DIVIDER_SGR = b"\033[0;32m"

#: Pane title bar. Reverse-video keeps it readable as a header band; the active
#: pane's title is green to match its border.
TITLE_SGR = b"\033[0;7m"
ACTIVE_TITLE_SGR = b"\033[0;7;32m"


#: Selected text in copy-mode: reverse video, like tmux.
SELECTION_SGR = b"\033[0;7m"

#: The copy-mode status line, so it's obvious the mode is active.
COPY_STATUS_SGR = b"\033[0;30;43m"      # black on yellow

# Synchronized updates (DEC private mode 2026). BEGIN tells the terminal to stop
# presenting frames; END commits everything since, atomically.
#
# This is what kills the flicker. Without it the client sees the erase and the
# repaint as separate visual states — worse, a full frame is tens of KB and the
# pty won't swallow it in one write(), so _flush() splits it and the terminal
# renders half-drawn screens. Terminals that don't implement 2026 ignore it.
BEGIN_SYNC = b"\033[?2026h"
END_SYNC = b"\033[?2026l"

ENABLE_MOUSE = b"\033[?1000h\033[?1002h\033[?1006h"
DISABLE_MOUSE = b"\033[?1006l\033[?1002l\033[?1000l"


def _char_width(ch: str) -> int:
    """Terminal columns a character occupies (0, 1 or 2).

    CJK and emoji are double-width: one index in iTerm2's `line.string`, two
    cells on screen. Get this wrong and long CJK lines overflow the client's
    width and wrap.
    """
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _color_sgr(color, is_fg: bool) -> List[bytes]:
    """One CellStyle.Color -> SGR parameters."""
    base = 38 if is_fg else 48
    default = 39 if is_fg else 49

    if color is None:
        return [str(default).encode()]

    if color.is_rgb:
        c = color.rgb
        return [b"%d;2;%d;%d;%d" % (base, c.red, c.green, c.blue)]

    if color.is_standard:
        n = color.standard
        # The 8 basic colors and their bright variants have short forms; every
        # terminal understands them, so prefer those over 38;5;n.
        if 0 <= n <= 7:
            return [str((30 if is_fg else 40) + n).encode()]
        if 8 <= n <= 15:
            return [str((90 if is_fg else 100) + (n - 8)).encode()]
        return [b"%d;5;%d" % (base, n)]

    # Alternate colors (DEFAULT / REVERSED_DEFAULT / SYSTEM_MESSAGE) have no
    # direct SGR equivalent — the terminal's own default is the right answer.
    return [str(default).encode()]


def _style_sgr(style) -> bytes:
    """Full SGR sequence for a cell style (always reset-then-set: simple and
    correct, and the diffing in render() keeps us from emitting it per cell)."""
    if style is None:
        return RESET_SGR

    params: List[bytes] = [b"0"]
    if style.bold:
        params.append(b"1")
    if style.faint:
        params.append(b"2")
    if style.italic:
        params.append(b"3")
    if style.underline:
        params.append(b"4")
    if style.blink:
        params.append(b"5")
    if style.inverse:
        params.append(b"7")
    if style.invisible:
        params.append(b"8")
    if style.strikethrough:
        params.append(b"9")

    params.extend(_color_sgr(style.fg_color, True))
    params.extend(_color_sgr(style.bg_color, False))
    return CSI + b";".join(params) + b"m"


def _style_key(style) -> tuple:
    """Cheap comparable identity for a style, so we only re-emit SGR on change."""
    if style is None:
        return ()

    def ckey(c):
        if c is None:
            return None
        if c.is_rgb:
            return ("rgb", c.rgb.red, c.rgb.green, c.rgb.blue)
        if c.is_standard:
            return ("std", c.standard)
        if c.is_alternate:
            return ("alt", c.alternate)
        return None

    return (style.bold, style.faint, style.italic, style.underline,
            style.blink, style.inverse, style.invisible, style.strikethrough,
            ckey(style.fg_color), ckey(style.bg_color))


