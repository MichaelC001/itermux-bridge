"""Render iTerm2 ScreenContents back into an ANSI byte stream.

The asymmetry the design doc flagged (§6): GetBufferRequest hands back
structured text + CellStyle, not the original escape sequences. To drive a dumb
tmux client we have to re-encode style into SGR ourselves.

We emit SGR only when the style actually changes between cells, which keeps the
byte count close to what the app originally wrote.
"""

from typing import List, Optional

from .sgr import (ACTIVE_DIVIDER_SGR, ACTIVE_TITLE_SGR, BEGIN_SYNC, CLEAR,
                  COPY_STATUS_SGR, CSI, DISABLE_MOUSE, DIVIDER_SGR,
                  ENABLE_MOUSE, END_SYNC, ENTER_ALT, EXIT_ALT, HIDE_CURSOR,
                  HOME, RESET_SGR, SELECTION_SGR, SHOW_CURSOR, TITLE_SGR,
                  _char_width, _color_sgr, _style_key, _style_sgr)

def _window_top(contents, rows: int, scroll_offset: int = 0) -> int:
    """First line of the `rows`-tall window we show the client.

    The obvious answer — always take the tail — is wrong when the iTerm2 pane is
    TALLER than the client. A freshly-cleared 59-row pane keeps its content on
    rows 0-2 and leaves 3-58 blank; taking the last 40 rows then shows the client
    nothing but empty space. Anchor the window on the CURSOR instead: that is
    where the action is, and for a scrolled-to-bottom buffer it degenerates to
    the tail anyway.
    """
    total = contents.number_of_lines
    scroll = max(0, scroll_offset)

    cursor = contents.cursor_coord.y - _origin_y(contents)
    if 0 <= cursor < total:
        # Keep the cursor on screen, preferring to show what's above it.
        top = cursor - rows + 1
    else:
        top = total - rows

    top -= scroll
    return max(0, min(top, max(0, total - rows)))


def visible_lines(contents, cols: int, rows: int, scroll_offset: int = 0):
    """The plain text of exactly the rows render() will draw.

    copy-mode extracts from this, so what you select is what you get.
    """
    total = contents.number_of_lines
    top = _window_top(contents, rows, scroll_offset)
    n = min(total - top, rows)

    out = []
    for i in range(rows):
        if i >= n:
            out.append("")
            continue
        text = contents.line(top + i).string
        # Same normalisation the renderer applies, so columns line up.
        clean = []
        col = 0
        for ch in text:
            if ch == "\0" or ord(ch) < 32:
                ch = " "
            w = _char_width(ch)
            if col + w > cols:
                break
            clean.append(ch)
            col += w
        out.append("".join(clean))
    return out


class Frame(bytes):
    """A rendered frame: the bytes to write, plus where each row starts.

    Every row is self-contained — it positions the cursor, resets SGR and
    erases the row before drawing — so one can be re-sent on its own. The screen
    pump uses that to send only the rows that changed (`diff`), instead of the
    whole screen each time: typing into a 200x56 Claude Code pane used to cost a
    22KB frame per keystroke.

    It is still `bytes`, so anything that just writes a frame keeps working.
    """
    head: bytes
    rows: list
    tail: bytes


def _frame(out, starts, tail_start: int) -> Frame:
    data = bytes(out)
    f = Frame(data)
    f.head = data[:starts[0] if starts else tail_start]
    f.rows = [data[a:b] for a, b in zip(starts, starts[1:] + [tail_start])]
    f.tail = data[tail_start:]
    return f


def diff(prev, frame: Frame) -> bytes:
    """What to write to turn a screen showing `prev` into `frame`.

    Only the rows whose bytes changed, then the frame's tail (cursor, copy-mode
    status, end of sync) — which is always sent. `prev` None means the client's
    screen is unknown, so everything goes.
    """
    if prev is None or len(prev.rows) != len(frame.rows):
        return bytes(frame)
    changed = [r for old, r in zip(prev.rows, frame.rows) if old != r]
    return frame.head + b"".join(changed) + frame.tail


def render(contents, cols: int, rows: int, scroll_offset: int = 0,
           copy=None) -> bytes:
    """Full-screen repaint of a ScreenContents as ANSI bytes.

    Two coordinate traps in the iTerm2 API, both learned the hard way against a
    live session:

    1. `contents` is not a tidy 0-indexed screen grid — it can hold more lines
       than the client has rows. Rendering line(0..rows) would paint the TOP of
       the buffer while the user is looking at the BOTTOM. We take the last
       `rows` lines instead.
    2. `cursor_coord.y` is absolute within iTerm2's buffer (we observed y=192 on
       a ~50-row terminal), not screen-relative. Clamping it to rows-1 silently
       parks the cursor on the last row; it has to be rebased against the same
       window of lines we just drew.
    """
    total = contents.number_of_lines
    top = _window_top(contents, rows, scroll_offset)
    n = min(total - top, rows)

    out = bytearray()
    # One atomic frame: the terminal presents nothing until END_SYNC, so a frame
    # split across several write()s is never seen half-drawn. Because the whole
    # frame is atomic, the cursor never has to be hidden while we draw — the
    # terminal won't show it stepping across the rows. Crucially we also stop
    # TOGGLING its visibility every frame: ?25l…?25h on each repaint (and the
    # pump repaints continuously) makes an input method — the CJK candidate
    # window is anchored to the cursor — treat every frame as the cursor
    # vanishing and returning, so its popup drifts and jumps mid-typing. We just
    # reposition a cursor that stays continuously visible.
    out += BEGIN_SYNC + RESET_SGR + HOME

    # Deliberately NO \033[2J here. Erasing the whole screen every frame makes
    # the terminal flash blank before the repaint lands — visible flicker on
    # every keystroke, not just when the layout changes. Instead we EVERY row
    # below (`range(rows)`, not `range(n)`) and erase each one right before
    # writing it, so stale content from a taller previous frame is overwritten
    # without the screen ever going blank.
    last_key: Optional[tuple] = None
    starts = []
    for i in range(rows):
        starts.append(len(out))
        out += CSI + b"%d;1H" % (i + 1)   # cursor to row start
        # Reset before erasing: \033[2K clears using the CURRENT background
        # colour, so a background still active from the previous line would
        # paint this entire row with it.
        out += RESET_SGR
        last_key = ()                     # SGR state is now "default"
        out += CSI + b"2K"                # clear the row

        if i >= n:
            continue                      # row past the pane's content: blank

        line = contents.line(top + i)
        text = line.string

        # Clip by DISPLAY CELLS, not characters. A CJK glyph is one index in
        # `string` but occupies two terminal columns, so `text[:cols]` overruns
        # the line — the terminal wraps it, which shifts everything below down
        # and smears the current background attribute across the wrapped tail.
        # (Seen live: a 146-char line was 187 cells wide on a 188-col grid.)
        col = 0
        for ch in text:
            # iTerm2 hands back NUL for cells that were never written (see
            # style_at's "uninitialized cells" note). Emitting those raw injects
            # \x00 into the client's byte stream, desyncs its parser and drops
            # the connection. Any other stray control char would too — render
            # them all as blanks.
            if ch == "\0" or ord(ch) < 32:
                ch = " "

            w = _char_width(ch)
            if col + w > cols:
                break

            # style_at() is indexed by CELL, not by character. Verified against a
            # live line: len(string)=72, display cells=97, and the style array is
            # 97 long. A wide glyph occupies two style slots, so indexing it with
            # the character position drifts by one for every CJK char before it —
            # colours and bold end mid-word (`speak`|`er.wave...`).
            # copy-mode: selected cells are drawn in reverse video. Keying off
            # the SELECTION_SGR sentinel keeps the run-length diffing correct.
            selected = (copy is not None and copy.selection is not None
                        and copy.selection.contains(i, col))
            if selected:
                if last_key != "SEL":
                    out += SELECTION_SGR
                    last_key = "SEL"
            else:
                style = line.style_at(col)
                key = _style_key(style)
                if key != last_key:
                    out += _style_sgr(style)
                    last_key = key
            out += ch.encode("utf-8", "replace")
            col += w

        # Pad to the client's width with the row's trailing style. iTerm2 only
        # returns as many cells as the line has content, but a background (an
        # input box, a selection) runs to the edge — stopping at the last
        # character chops the highlight off so it hugs the text.
        if col < cols:
            out += b" " * (cols - col)

    tail_start = len(out)
    out += RESET_SGR

    if copy is not None and copy.active:
        # In copy-mode the cursor is OURS, not the application's, and a status
        # line makes it obvious the keyboard is no longer going to the app.
        label = b" COPY  arrows/hjkl move  v select  y copy  q quit "
        out += CSI + b"%d;1H" % rows
        out += COPY_STATUS_SGR + label[:cols].ljust(cols)[:cols] + RESET_SGR
        cy = min(max(copy.cy, 0), rows - 1)
        cx = min(max(copy.cx, 0), cols - 1)
        out += CSI + b"%d;%dH" % (cy + 1, cx + 1)
        out += SHOW_CURSOR + END_SYNC
        return _frame(out, starts, tail_start)

    # Rebase the cursor. cursor_coord.y is an ABSOLUTE line number in the
    # session's whole history (we've seen y=705 on a 59-row grid), so it has to
    # be measured against the same origin as the lines we were handed. That
    # origin is windowed_coord_range.start.y — NOT number_of_lines_above_screen,
    # which is 0 here and would leave the cursor clamped to the last row (the
    # symptom: cursor stuck at the bottom instead of on the app's input line).
    coord = contents.cursor_coord
    cy = coord.y - _origin_y(contents) - top
    cx = min(max(coord.x, 0), cols - 1)

    if scroll_offset > 0 or not (0 <= cy < rows):
        # Scrolled back into history: the live cursor isn't on this screen, so
        # hide it rather than parking it somewhere arbitrary. (This is the one
        # place we DO hide it — the cursor genuinely has no on-screen home.)
        # END_SYNC is mandatory on EVERY path — leaving the block open would
        # freeze the client's display until some later frame happened to close it.
        out += HIDE_CURSOR + END_SYNC
        return _frame(out, starts, tail_start)

    # Reposition the cursor and make sure it's visible — but SHOW_CURSOR is a
    # no-op when it's already shown, so it doesn't cause the IME-drift toggle.
    out += CSI + b"%d;%dH" % (cy + 1, cx + 1)
    out += SHOW_CURSOR
    out += END_SYNC
    return _frame(out, starts, tail_start)


def render_panes(panes, cols: int, rows: int, active_id: str = "",
                 copy=None, titles=None) -> bytes:
    """Composite several panes into one screen, with dividers between them.

    `panes` is a list of (Region, ScreenContents) — the whole tab at once, which
    is what attaching to a *window* rather than a single pane means.

    `titles` maps session_id -> pane name; when given, each pane gets a one-line
    title bar at its top (tmux's `pane-border-status top`), so you can tell the
    panes apart. The active pane's title is highlighted.
    """
    titles = titles or {}
    show_titles = bool(titles)

    # Build the screen as a grid of (char, sgr) so overlapping writes are
    # impossible and we can lay panes down in any order.
    blank = (" ", RESET_SGR)
    grid = [[blank for _ in range(cols)] for _ in range(rows)]

    cursor = None

    for region, contents in panes:
        if contents is None:
            continue

        # Reserve the top row of the pane for its title bar.
        content_y = region.y + (1 if show_titles else 0)
        content_h = region.height - (1 if show_titles else 0)
        if show_titles and content_h >= 1:
            _draw_pane_title(grid, region, cols,
                             titles.get(region.session_id, ""),
                             region.session_id == active_id)

        total = contents.number_of_lines
        top = max(0, total - content_h)
        n = min(total - top, content_h)

        for i in range(n):
            y = content_y + i
            if not (0 <= y < rows):
                continue
            line = contents.line(top + i)
            text = line.string

            col = 0
            trailing = RESET_SGR
            for ch in text:
                if ch == "\0" or ord(ch) < 32:
                    ch = " "
                w = _char_width(ch)
                if col + w > region.width:
                    break
                gx = region.x + col
                if 0 <= gx < cols:
                    # style_at() is CELL-indexed, not character-indexed — see the
                    # note in render(). Using the character position shifts every
                    # colour boundary by one per preceding wide glyph.
                    sgr = _style_sgr(line.style_at(col))
                    trailing = sgr
                    grid[y][gx] = (ch, sgr)
                    # A double-width glyph owns the next cell too; mark it so
                    # nothing else writes there and shifts the row. It must carry
                    # the SAME style — the terminal paints the glyph's background
                    # across both cells.
                    if w == 2 and gx + 1 < cols:
                        grid[y][gx + 1] = ("", sgr)
                col += w

            # Pad the rest of the region with the row's trailing style, NOT a
            # reset. iTerm2 gives us only as many cells as the line has content,
            # but a background (an input box, a selection) runs to the edge of
            # the pane. Filling the remainder with RESET_SGR chops the highlight
            # off at the last character — visible as a background that hugs the
            # text instead of spanning the row.
            while col < region.width:
                gx = region.x + col
                if 0 <= gx < cols:
                    grid[y][gx] = (" ", trailing)
                col += 1

        if region.session_id == active_id:
            coord = contents.cursor_coord
            cy = content_y + (coord.y - _origin_y(contents) - top)
            cx = region.x + coord.x
            if 0 <= cy < rows and 0 <= cx < cols:
                cursor = (cy, cx)

    _draw_dividers(grid, panes, cols, rows, active_id)

    # copy-mode overlays the finished grid. Its coordinates are SCREEN-absolute
    # (that's what the mouse reports), so it doesn't care how the panes are laid
    # out. Without this the mode worked but was invisible in window mode: the
    # selection existed server-side while the screen showed nothing, which reads
    # exactly like "copy-mode doesn't work".
    if copy is not None and copy.active:
        sel = copy.selection
        if sel is not None:
            for y in range(rows):
                for x in range(cols):
                    # Confine the highlight to the pane copy-mode is bound to,
                    # or a drag across the divider would select the neighbouring
                    # pane's text — and the divider glyphs — along with it.
                    if sel.contains(y, x) and copy.in_bounds(y, x):
                        ch, _sgr = grid[y][x]
                        grid[y][x] = (ch, SELECTION_SGR)

        label = " COPY  arrows/hjkl move  v select  y copy  q quit "
        for x in range(cols):
            ch = label[x] if x < len(label) else " "
            grid[rows - 1][x] = (ch, COPY_STATUS_SGR)
        cursor = (min(max(copy.cy, 0), rows - 1),
                  min(max(copy.cx, 0), cols - 1))

    # Emit the grid, re-issuing SGR only when the style changes.
    out = bytearray()
    # One atomic frame: the terminal shows nothing until END_SYNC. No \033[2J —
    # this loop writes every row, so a per-row erase overwrites stale content
    # without flashing the screen blank first.
    # No per-frame HIDE_CURSOR — see render(): toggling ?25l/?25h every repaint
    # makes the IME candidate window drift. The sync block keeps the cursor from
    # being seen stepping across the rows.
    out += BEGIN_SYNC + RESET_SGR + HOME
    last = None
    starts = []
    for y in range(rows):
        starts.append(len(out))
        out += CSI + b"%d;1H" % (y + 1)
        out += RESET_SGR
        last = RESET_SGR
        out += CSI + b"2K"
        for x in range(cols):
            ch, sgr = grid[y][x]
            if not ch:          # the second half of a wide glyph
                continue
            if sgr != last:
                out += sgr
                last = sgr
            out += ch.encode("utf-8", "replace")

    tail_start = len(out)
    out += RESET_SGR
    if cursor:
        out += CSI + b"%d;%dH" % (cursor[0] + 1, cursor[1] + 1)
        out += SHOW_CURSOR
    else:
        # No active-pane cursor on screen — hide it rather than leaving it parked
        # at a stale spot.
        out += HIDE_CURSOR
    out += END_SYNC
    return _frame(out, starts, tail_start)


def _draw_pane_title(grid, region, cols: int, name: str, active: bool) -> None:
    """Fill a pane's top row with its name, tmux's pane-border-status top."""
    sgr = ACTIVE_TITLE_SGR if active else TITLE_SGR
    label = f" {name} " if name else " "

    # Clip the label to the pane width (by display cells, for CJK names).
    text, used = [], 0
    for ch in label:
        w = _char_width(ch)
        if used + w > region.width:
            break
        text.append(ch)
        used += w

    y = region.y
    if not (0 <= y < len(grid)):
        return
    col = 0
    for ch in text:
        gx = region.x + col
        w = _char_width(ch)
        if gx >= cols:
            break
        grid[y][gx] = (ch, sgr)
        if w == 2 and gx + 1 < cols:
            grid[y][gx + 1] = ("", sgr)
        col += w
    # Pad the rest of the title row so the band spans the whole pane width.
    while col < region.width:
        gx = region.x + col
        if 0 <= gx < cols:
            grid[y][gx] = (" ", sgr)
        col += 1


def _draw_dividers(grid, panes, cols: int, rows: int,
                   active_id: str = "") -> None:
    """Fill the one-cell gaps the layout left between panes.

    Pick the box-drawing glyph from which neighbouring cells are *also* gaps, so
    that crossings and tees join up instead of one divider overwriting another.

    Dividers that touch the ACTIVE pane are drawn in the highlight colour, so you
    can see at a glance which pane your keystrokes go to — the same cue tmux
    gives with pane-active-border-style.
    """
    occupied = [[False] * cols for _ in range(rows)]
    for region, _ in panes:
        for y in range(region.y, min(region.y + region.height, rows)):
            for x in range(region.x, min(region.x + region.width, cols)):
                occupied[y][x] = True

    def gap(x: int, y: int) -> bool:
        return 0 <= x < cols and 0 <= y < rows and not occupied[y][x]

    # The active pane's rectangle, grown by one cell: that ring is its border.
    active = next((r for r, _ in panes if r.session_id == active_id), None)

    def borders_active(x: int, y: int) -> bool:
        if active is None:
            return False
        return (active.x - 1 <= x <= active.x + active.width and
                active.y - 1 <= y <= active.y + active.height)

    # (up, down, left, right) -> glyph
    GLYPHS = {
        (True, True, True, True): "┼",
        (True, True, True, False): "┤",
        (True, True, False, True): "├",
        (True, True, False, False): "│",
        (True, False, True, True): "┴",
        (False, True, True, True): "┬",
        (True, False, True, False): "┘",
        (True, False, False, True): "└",
        (False, True, True, False): "┐",
        (False, True, False, True): "┌",
        (False, False, True, True): "─",
    }

    for y in range(rows):
        for x in range(cols):
            if occupied[y][x]:
                continue
            key = (gap(x, y - 1), gap(x, y + 1), gap(x - 1, y), gap(x + 1, y))
            # Lone gaps (no gap neighbours) default to a horizontal rule; a
            # divider that only continues one way still reads as a line.
            glyph = GLYPHS.get(key)
            if glyph is None:
                glyph = "│" if (key[0] or key[1]) else "─"
            sgr = (ACTIVE_DIVIDER_SGR if borders_active(x, y)
                   else DIVIDER_SGR)
            grid[y][x] = (glyph, sgr)


def _origin_y(contents) -> int:
    """Absolute line number of the first line in this ScreenContents."""
    try:
        return int(contents.windowed_coord_range.start.y)
    except Exception:
        return 0
