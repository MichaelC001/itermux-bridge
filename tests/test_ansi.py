"""ANSI re-encoder tests.

Fakes stand in for iTerm2's objects, mirroring the real API surface exactly:
CellStyle exposes bold/italic/... plus fg_color/bg_color returning a Color with
is_standard / is_rgb / is_alternate, and LineContents exposes .string /
.style_at(x). Verified against the installed iterm2 package.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itermux_bridge import ansi


class Color:
    def __init__(self, standard=None, rgb=None, alternate=None):
        self._s, self._rgb, self._alt = standard, rgb, alternate

    @property
    def is_standard(self): return self._s is not None

    @property
    def is_rgb(self): return self._rgb is not None

    @property
    def is_alternate(self): return self._alt is not None

    @property
    def standard(self): return self._s

    @property
    def rgb(self):
        class RGB:
            def __init__(s, t): s.red, s.green, s.blue = t
        return RGB(self._rgb)

    @property
    def alternate(self): return self._alt


class Style:
    def __init__(self, **kw):
        for f in ("bold", "faint", "italic", "underline", "blink",
                  "inverse", "invisible", "strikethrough"):
            setattr(self, f, kw.get(f, False))
        self.fg_color = kw.get("fg")
        self.bg_color = kw.get("bg")


class Line:
    """style_at() is indexed by CELL, not by character — a wide glyph occupies
    two style slots. Verified on a live line: len(string)=72, display cells=97,
    style array length=97."""

    def __init__(self, text, styles=None):
        self.string = text
        self._styles = styles or {}

    def style_at(self, x):
        return self._styles.get(x)


class Contents:
    def __init__(self, lines, cx=0, cy=0, origin=None):
        self._lines = lines
        self.number_of_lines = len(lines)
        class C: pass
        self.cursor_coord = C()
        self.cursor_coord.x, self.cursor_coord.y = cx, cy
        if origin is not None:
            # Mirrors iterm2's windowed_coord_range.start.y — the absolute line
            # number of the first line handed to us.
            start = C()
            start.y = origin
            wcr = C()
            wcr.start = start
            self.windowed_coord_range = wcr

    def line(self, i):
        return self._lines[i]


ok = True


def check(label, cond, extra=""):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {label} {extra}")
    ok = ok and cond


print("\n=== ANSI re-encoding (iTerm2 CellStyle -> SGR) ===")

# Plain text round-trips, cursor positioned.
out = ansi.render(Contents([Line("hello")], cx=3, cy=0), 80, 24)
check("text present", b"hello" in out)
check("cursor positioned to iTerm2's coord", b"\033[1;4H" in out, "(row1,col4)")
check("cursor shown before the frame is committed",
      ansi.SHOW_CURSOR in out and out.endswith(ansi.END_SYNC))

# Regression: a normal live frame must NOT hide the cursor. Emitting ?25l…?25h
# on every repaint (and the pump repaints continuously) makes an input method —
# whose candidate window is anchored to the cursor — see the cursor vanish and
# return each frame, so its popup drifts and jumps while you're typing.
check("live frame does not toggle the cursor off (IME anchor stays put)",
      ansi.HIDE_CURSOR not in out)

# Basic 16-color -> short SGR form.
s = Style(fg=Color(standard=1), bg=Color(standard=4))
out = ansi.render(Contents([Line("R", {0: s})]), 80, 24)
check("standard fg 1 -> SGR 31", b"31" in out, f"({out!r})")
check("standard bg 4 -> SGR 44", b"44" in out)

# Bright color (8-15) -> 90/100 range.
s = Style(fg=Color(standard=9))
out = ansi.render(Contents([Line("B", {0: s})]), 80, 24)
check("bright fg 9 -> SGR 91", b"91" in out)

# 256-color palette.
s = Style(fg=Color(standard=200))
out = ansi.render(Contents([Line("X", {0: s})]), 80, 24)
check("palette fg 200 -> 38;5;200", b"38;5;200" in out)

# Truecolor.
s = Style(fg=Color(rgb=(10, 20, 30)))
out = ansi.render(Contents([Line("X", {0: s})]), 80, 24)
check("rgb fg -> 38;2;10;20;30", b"38;2;10;20;30" in out)

# Attributes.
s = Style(bold=True, italic=True, underline=True, strikethrough=True)
out = ansi.render(Contents([Line("X", {0: s})]), 80, 24)
check("bold/italic/underline/strike -> 1;3;4;9",
      all(p in out for p in (b"1", b"3", b"4", b"9")))

# Style diffing: identical adjacent cells must not re-emit SGR.
s = Style(fg=Color(standard=2))
line = Line("aaaa", {i: s for i in range(4)})
out = ansi.render(Contents([line]), 80, 24)
check("SGR emitted once for a run of same-styled cells",
      out.count(b"32") == 1, f"(count={out.count(b'32')})")

# Alternate (default) color maps to 39/49, not a bogus palette index.
s = Style(fg=Color(alternate="DEFAULT"))
out = ansi.render(Contents([Line("X", {0: s})]), 80, 24)
check("alternate/default fg -> SGR 39", b"39" in out)

# Wider than the client: must clip, not wrap.
out = ansi.render(Contents([Line("x" * 200)]), 20, 24)
check("long line clipped to client width", out.count(b"x") == 20,
      f"(got {out.count(b'x')})")

# More lines than the client has rows: must clip.
out = ansi.render(Contents([Line("y") for _ in range(50)]), 80, 10)
check("extra rows clipped to client height", out.count(b"y") == 10,
      f"(got {out.count(b'y')})")

# Regression: a buffer taller than the client must show the BOTTOM (what the
# user is actually looking at), not the top of the scrollback. Observed live:
# async_get_screen_contents() returned 59 lines for a ~50-row terminal.
lines = [Line(f"old{i}") for i in range(20)] + [Line(f"new{i}") for i in range(5)]
# The cursor sits at the bottom, as it does in any live shell.
out = ansi.render(Contents(lines, cy=24), 80, 5)
check("renders the tail of the buffer, not the head",
      b"new4" in out and b"old0" not in out)

# Regression: but taking the tail UNCONDITIONALLY is wrong when the pane is
# taller than the client. A freshly-cleared 59-row iTerm2 pane keeps its content
# on rows 0-2 and leaves the rest blank; showing the last 40 rows then displays
# nothing at all. Anchor on the cursor instead.
lines = [Line("CONTENT_HERE"), Line("prompt")] + [Line("") for _ in range(57)]
out = ansi.render(Contents(lines, cy=1), 80, 40)
check("content at the TOP of a tall, mostly-blank pane is still shown",
      b"CONTENT_HERE" in out)

# Unicode must survive.
out = ansi.render(Contents([Line("日本語 ✓")]), 80, 24)
check("utf-8 encoded", "日本語".encode() in out)

# Regression: iTerm2 returns NUL for uninitialized cells (249 of them in one
# real session). Emitting them raw desyncs the client's parser and drops the
# connection with "server exited unexpectedly".
out = ansi.render(Contents([Line("ab\x00\x00cd")]), 80, 24)
check("NUL cells rendered as spaces, never emitted raw", b"\x00" not in out)
check("text around NULs preserved", b"ab" in out and b"cd" in out)

# No stray control bytes at all outside our own escape sequences.
out = ansi.render(Contents([Line("x\x01\x07\x1bz")]), 80, 24)
body = out.replace(b"\033[", b"")  # our CSI intros are legitimate
check("no bare control chars leak into the stream",
      not any(b < 32 and b != 0x1b for b in body))

# Regression: cursor_coord.y is an ABSOLUTE line number in the session's whole
# history, not a screen row. Observed live on a 59-row grid: origin=650,
# cursor.y=705 -> screen row 55, which held the app's "❯" input prompt. Without
# rebasing on windowed_coord_range.start.y the cursor clamps to the last row and
# lands in the wrong place in TUIs like the Claude CLI.
lines = [Line(f"L{i}") for i in range(59)]
out = ansi.render(Contents(lines, cx=2, cy=705, origin=650), 162, 59)
check("absolute cursor y rebased onto the right screen row",
      b"\033[56;3H" in out, "(row 55 -> CSI 56;3H)")

# And it must still be right when we also clip to the tail of a taller buffer.
lines = [Line(f"L{i}") for i in range(80)]
# origin 100 -> buffer covers absolute 100..179; a 20-row client shows 160..179.
out = ansi.render(Contents(lines, cx=0, cy=170, origin=100), 80, 20)
check("cursor correct when the buffer is also clipped to the tail",
      b"\033[11;1H" in out, "(abs 170 -> row 10 -> CSI 11;1H)")

print("\n=== double-width (CJK) handling ===")

# Regression: CJK glyphs are ONE index in line.string but TWO terminal cells.
# Clipping with text[:cols] overruns the client's width, the line wraps, and the
# active background smears across the wrapped tail. (Live: a 146-char line was
# 187 cells wide.) 5 CJK chars = 10 cells, so a 10-col client fits exactly 5.
out = ansi.render(Contents([Line("中文测试行" * 4)]), 10, 24)
n_cjk = sum(out.count(c.encode()) for c in "中文测试行")
check("exactly 5 double-width glyphs fit in 10 columns", n_cjk == 5,
      f"(got {n_cjk})")

# Mixed ASCII + CJK must also respect cell width.
out = ansi.render(Contents([Line("ab中文cd")]), 6, 24)   # a,b=2 cells, 中文=4 -> 6
check("mixed ascii+CJK fills exactly the width",
      b"ab" in out and "中文".encode() in out and b"cd" not in out)

print("\n=== stale content ===")

# Regression 1: switching layouts (e.g. Ctrl-B z zooming out of the multi-pane
# view) repaints a DIFFERENT shape. If we only touched the rows the new frame
# fills, the rest would keep the old split layout — the previous pane borders
# and status bars bleeding through beneath the zoomed pane.
out = ansi.render(Contents([Line("only one line")]), 80, 40)
check("every row is erased, even past the pane's content",
      out.count(b"\x1b[2K") == 40, f"({out.count(b'2K')} rows)")

# Regression 2: erasing the WHOLE screen (\033[2J) each frame fixes the stale
# rows but flashes the terminal blank before every repaint — visible flicker on
# each keystroke. Overwrite row by row instead; never blank the screen.
check("no full-screen erase (that is what caused the flicker)",
      ansi.CLEAR not in out)

# Regression 3: the frame is emitted as one synchronized update, so the client
# cannot present it half-drawn (a full frame is tens of KB and gets split across
# several write()s by the pty).
check("frame is wrapped in a synchronized update",
      out.startswith(ansi.BEGIN_SYNC) and out.endswith(ansi.END_SYNC))

# An unclosed sync block freezes the client's display until some later frame
# happens to close it — so END_SYNC must be emitted on EVERY return path,
# including the scrolled-back one that returns early without placing a cursor.
out = ansi.render(Contents([Line("x") for _ in range(50)], cx=0, cy=0),
                  80, 10, scroll_offset=20)
check("scrolled-back frame still closes the sync block",
      out.count(ansi.BEGIN_SYNC) == 1 and out.endswith(ansi.END_SYNC))

print("\n=== style indexing (cell, not character) ===")

# Regression: style_at(x) is indexed by CELL. "中文" is 2 chars but 4 cells, so
# the style for the "R" that follows lives at slot 4, not slot 2. Indexing by
# character position drifts by one per preceding wide glyph — live symptom:
# bold/colour ended mid-word ("speak" bold, "er.wave.2.fill" blue).
red = Style(fg=Color(standard=1))
line = Line("中文R", {4: red})          # cells: 0,1 = 中  2,3 = 文  4 = R
out = ansi.render(Contents([line]), 20, 3)
row = out.split(b"\x1b[1;1H")[1]
before_r = row.split("R".encode())[0]
check("colour applies to the char at the right CELL",
      b"31" in before_r and before_r.rindex(b"31") > before_r.rindex("文".encode()),
      "(red starts at R, not earlier)")

# And the wide glyphs themselves must NOT pick up that style.
check("preceding wide glyphs keep their own (unstyled) look",
      row.index("中".encode()) < row.index(b"31"))

print("\n=== trailing background ===")

# Regression: iTerm2 returns only as many cells as the line has content, but a
# background (an input box, a selection) runs to the edge of the pane. Padding
# the rest with a RESET chops the highlight off at the last character — the
# symptom was a grey input-box background that hugged the typed text instead of
# spanning the row.
bg = Style(bg=Color(rgb=(55, 55, 55)))
# "有没有 abc" is 7 chars but 10 CELLS, and style_at is cell-indexed.
line = Line("有没有 abc", {i: bg for i in range(10)})
out = ansi.render(Contents([line]), 40, 3)
row0 = out.split(b"\x1b[1;1H")[1].split(b"\x1b[2;1H")[0]
# Everything after the last SGR must be spaces — i.e. no reset before the pad.
tail = row0.split(b"48;2;55;55;55m")[-1]
check("row is padded to the full width", b" " * 20 in tail,
      f"({tail[-30:]!r})")
check("no SGR reset between the text and the padding "
      "(that is what chopped the background)",
      ansi.RESET_SGR not in tail)

print("\n=== background bleed ===")

# \033[2K erases with the CURRENT background, so a background left over from the
# previous line would paint the whole next row. Each row must reset first.
bg = Style(bg=Color(standard=7))
lines = [Line("x", {0: bg}), Line("y")]
out = ansi.render(Contents(lines), 20, 24)
row2 = out.split(b"\x1b[2;1H")[1]
check("SGR is reset before erasing each row (no bg bleed onto the next line)",
      row2.startswith(ansi.RESET_SGR), f"({row2[:12]!r})")

print("\n=== row diff: send only the rows that changed ===")

a = ansi.render(Contents([Line("one"), Line("two"), Line("three")], cy=2), 30, 5)
check("a frame is still bytes", isinstance(a, bytes))
check("head + rows + tail reassemble the frame exactly",
      a.head + b"".join(a.rows) + a.tail == bytes(a))
check("one segment per client row", len(a.rows) == 5)
check("no previous frame: everything is sent", ansi.diff(None, a) == bytes(a))

same = ansi.diff(a, ansi.render(Contents([Line("one"), Line("two"),
                                          Line("three")], cy=2), 30, 5))
check("unchanged screen: no row is re-sent",
      b"one" not in same and b"two" not in same and b"three" not in same)
check("...but the tail (cursor, end of sync) still goes",
      same.endswith(ansi.END_SYNC) and b"\033[3;1H" in same)

b = ansi.render(Contents([Line("one"), Line("TWO!"), Line("three")], cy=2), 30, 5)
d = ansi.diff(a, b)
check("one changed line: only that row is sent",
      b"TWO!" in d and b"one" not in d and b"three" not in d, f"({len(d)} bytes)")
check("...a fraction of the full frame", len(d) < len(b) / 2,
      f"({len(d)} of {len(b)} bytes)")

# view._emit: the conditions under which a full frame is forced.
from itermux_bridge.view import ScreenView  # noqa: E402
from itermux_bridge.copymode import CopyMode  # noqa: E402


class _EmitPeer:
    def __init__(self):
        self.copy = CopyMode()
        self.last_frame = self.frame_size = None
        self.frame_copy = False
        self.cols, self.written = 30, []
        self.ttyname = "/dev/ttys000"
        from itermux_bridge.latency import KeyTrace
        self.trace = KeyTrace(self)
        me = self

        class T:
            def size(self):
                return me.cols, 5
        self.tty = T()

    def write_out(self, data):
        self.written.append(bytes(data))


v, ep = ScreenView(), _EmitPeer()
v._emit(ep, a)
v._emit(ep, b)
check("_emit sends the diff once it has a previous frame",
      ep.written[-1] == ansi.diff(a, b))
ep.cols = 40
v._emit(ep, b)
check("client resized: full frame", ep.written[-1] == bytes(b))
ep.copy.enter(5)
v._emit(ep, b)
check("copy-mode entered: full frame", ep.written[-1] == bytes(b))
ep.copy.leave()
v._emit(ep, b)
# render() drew copy-mode's status bar in the tail, over the last row; a diff
# would leave it stranded there (caught against a real terminal emulator).
check("copy-mode left: full frame, so its status bar is erased",
      ep.written[-1] == bytes(b))


print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
sys.exit(0 if ok else 1)
