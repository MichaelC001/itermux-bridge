"""Input routing: keyboard, mouse, and the copy-mode key handler.

Decides what each byte from the client MEANS — app input, a mouse selection, a
copy-mode movement — and drives copy-mode's keyboard state machine. Split out of
the backend because routing bugs (mouse bytes reaching the copy-mode handler,
copy-mode trapping the keyboard) all lived at these seams.

`InputRouter` is mixed into the backend; it uses `self.app`, `self._session_of`,
`self._paint`, `self._spawn`, `self._send*` from there.
"""

import asyncio
import logging

from . import ansi, copymode, layout, mouse

log = logging.getLogger(__name__)


class InputRouter:
    """Keyboard/mouse routing and copy-mode key handling."""

    def on_mouse(self, peer, events) -> None:
        """Mouse reports — always handled here, even while copy-mode is up."""
        session = self._session_of(peer)
        if session is not None:
            self._spawn(self._handle_mouse(peer, session, events), "mouse")

    def on_input(self, peer, keys: bytes) -> None:
        """Client keystrokes -> the mapped iTerm2 session."""
        session = self._session_of(peer)
        if session is None or not keys:
            return

        # Typing anything jumps back to the live screen, like a terminal does.
        if peer.scroll_offset:
            peer.scroll_offset = 0
            self._spawn(self._paint(peer, session), "paint")

        # Log only the length: keystrokes carry passwords (§11.5).
        log.debug("input: %d bytes", len(keys))
        peer.trace.key_in()
        # Typing into a pane whose tab isn't selected on the Mac (someone
        # switched tabs, or it never was) makes every echo lag by seconds:
        # iTerm2 doesn't refresh a hidden tab's screen for the API. Bring the
        # tab forward within its window — only when the client types, so
        # someone at the Mac browsing tabs isn't fought over on every poll.
        if not self.api.is_shown(session):
            self._spawn(self.api.reveal(session), "reveal")
        self._spawn(self._send_keys(peer, session,
                                    keys.decode("utf-8", "replace")),
                    "send-keys")

    async def _send_keys(self, peer, session, text: str) -> None:
        await self._send(session, text)
        peer.trace.sent()

    async def _handle_mouse(self, peer, session, events) -> None:
        """Wheel scrolls our scrollback view; everything else goes to the app.

        Which one depends on whether the program running in iTerm2 actually
        asked for mouse reporting (mouseReportingMode). A TUI like vim or the
        Claude CLI handles the wheel itself; a plain shell does not, and there
        the wheel should page through history — the same split iTerm2 and tmux
        make.
        """
        # We only OWN the mouse when the user turned it on (Ctrl-B m). With mouse
        # off — the default — we never requested reporting, so any mouse bytes
        # that arrive are the CLIENT terminal's own state leaking through (some
        # other program left ?1000h on). Treating those as a selection dragged us
        # into copy-mode on a stray click and wouldn't let go. Hand them to the
        # app instead, exactly as a bare tmux with `mouse off` does.
        if not peer.mouse_owned:
            for ev in events:
                await self._send_raw(session, ev.encode())
            return

        mode = await self.api.variable(session, "mouseReportingMode", -1)
        # iTerm2 reports -1 for "mouse reporting off" — NOT 0. A truthiness test
        # (`bool(mode)`) treats -1 as enabled and forwards the wheel to a plain
        # shell that will never use it, so scrollback silently stops working.
        app_wants_mouse = mode is not None and int(mode) >= 0

        for ev in events:
            if ev.is_wheel and not app_wants_mouse:
                await self._scroll(peer, session, ev)
                continue
            if app_wants_mouse:
                await self._send_raw(session, ev.encode())
                continue

            # The app doesn't want the mouse — so a press-drag-release is a text
            # SELECTION. We requested mouse reporting from the client, which
            # means its terminal no longer does native selection for us; if we
            # dropped these events (as we used to) there would be no way to
            # select text at all.
            await self._mouse_select(peer, session, ev)

    def _sync_zoom_mouse(self, peer, pane) -> None:
        """Take the mouse while `pane` is zoomed, give it back when it isn't.

        Called by the screen pump every poll, so a zoom toggled from iTerm2
        itself is picked up too, not just Ctrl-B z. Acts only on a zoom
        *transition*: that is what lets Ctrl-B m switch it off mid-zoom without
        the next poll switching it straight back on.
        """
        zoomed = self.api.is_zoomed(self.api.tab_of(pane.session_id))
        if zoomed == peer.zoomed:
            return
        was_owned = peer.mouse_owned
        peer.zoomed = zoomed
        peer.zoom_mouse = zoomed
        if peer.mouse_owned == was_owned:
            return          # Ctrl-B m already had it on; nothing changes hands

        # Same reset as Ctrl-B m, for the same reason: a selection or scrolled
        # view the mouse was driving can't be driven once the mouse is gone.
        peer.copy.leave()
        peer.scroll_offset = 0
        peer.write_out(ansi.ENABLE_MOUSE if peer.mouse_owned
                       else ansi.DISABLE_MOUSE)
        log.info("zoom %s: mouse reporting %s", "in" if zoomed else "out",
                 "on" if peer.mouse_owned else "off")

    async def _mouse_select(self, peer, session, ev) -> None:
        """Press-drag-release selects text; releasing copies it, as tmux does."""
        cm = peer.copy
        y, x = ev.y - 1, ev.x - 1       # SGR reports are 1-based

        if ev.pressed and not (ev.cb & mouse.MOTION_BIT):
            # Button down: start a fresh selection here, confined to the pane
            # that was clicked (not necessarily the active one). Only *enter*
            # copy-mode if we're not already in it — re-entering would reset
            # bounds/pending state mid-session.
            if peer.tty is None:
                return
            cols, rows = peer.tty.size()
            bounds = self._bounds_at(peer, session, cols, rows, y, x)
            if not cm.active:
                cm.enter(rows, peer.scroll_offset, bounds=bounds)
            else:
                cm.bounds = bounds
            cm.cy, cm.cx = y, x
            cm.clamp()
            cm.start_selection()
            await self._paint(peer, session)
            return

        if not cm.active:
            return

        if ev.pressed and (ev.cb & mouse.MOTION_BIT):
            # Dragging: extend the selection, but only within the pane — drag
            # past the divider and it stops at the edge rather than swallowing
            # the neighbour.
            cm.cy, cm.cx = y, x
            cm.clamp()
            await self._paint(peer, session)
            return

        if not ev.pressed:
            # Button up: a real selection gets copied; a bare click just exits.
            cm.cy, cm.cx = y, x
            cm.clamp()
            if cm.selection is not None and (cm.selection.y0, cm.selection.x0) \
                    != (cm.selection.y1, cm.selection.x1):
                await self._copy_selection(peer, session)
            else:
                cm.leave()
            await self._paint(peer, session)

    async def _scroll(self, peer, session, ev) -> None:
        """Move this peer's view through iTerm2's scrollback."""
        if peer.tty is None:
            return
        _cols, rows = peer.tty.size()
        step = max(1, rows // 4)      # a quarter page per notch, like iTerm2

        before = peer.scroll_offset
        if ev.wheel_up:
            peer.scroll_offset += step
        elif ev.wheel_down:
            peer.scroll_offset = max(0, peer.scroll_offset - step)
        else:
            return

        if peer.scroll_offset != before:
            await self._paint(peer, session)

    def on_copy_key(self, peer, data: bytes) -> None:
        """Keyboard input while copy-mode is active — it drives the selection."""
        self._spawn(self._copy_key(peer, data), "copy-key")

    async def _copy_key(self, peer, data: bytes) -> None:
        session = self._session_of(peer)
        if session is None or peer.tty is None:
            return
        cols, rows = peer.tty.size()
        cm = peer.copy

        # An arrow key is ESC [ A — three bytes that can arrive split across
        # reads. Treating a bare ESC as "quit" the instant it lands means a split
        # arrow key kicks you out of copy-mode (and its trailing "[A" leaks to
        # the app). So we stash an INCOMPLETE escape tail and resume it next
        # time. This is the intermittent "accidentally left copy-mode" you saw.
        #
        # But a stashed ESC that ISN'T continued by [ or O was a real Escape
        # (the user quitting): flush it as such instead of swallowing it.
        if cm.pending == b"\x1b" and data[:1] not in (b"[", b"O"):
            cm.leave()
            peer.scroll_offset = 0
            cm.pending = bytearray()
            # `data` here is the *next* key; fall through and process it too, but
            # copy-mode is now off, so hand it back to the app.
            if data:
                self.on_input(peer, data)
            await self._paint(peer, session)
            return

        data = bytes(cm.pending) + data
        cm.pending = bytearray()

        i = 0
        while i < len(data):
            b = data[i:i + 1]

            # An incomplete escape tail (ESC, ESC-[, ESC-O, ESC-[-5/6) at the END
            # of this chunk: stash it and wait for the rest rather than misreading
            # the ESC as quit.
            rest = data[i:]
            if rest == b"\x1b" or rest in (b"\x1b[", b"\x1bO") or \
                    (rest[:2] == b"\x1b[" and rest[2:3] in (b"5", b"6")
                     and len(rest) < 4):
                cm.pending = bytearray(rest)
                # A lone ESC might be a real Escape (quit) OR the start of a
                # split arrow key. Disambiguate the way terminals do — with a
                # short timeout: if nothing continues it, it was Escape.
                if rest == b"\x1b":
                    self._spawn(self._esc_timeout(peer), "esc-timeout")
                # Stashing changed nothing on screen, so DON'T repaint here.
                # Repainting would emit a fresh copy-mode frame that then races
                # (and loses to) the timeout's leave-repaint, leaving the COPY
                # status bar stuck on screen.
                return

            # Arrows arrive as ESC [ A etc.
            if data[i:i + 3] in (b"\x1b[A", b"\x1bOA"):
                self._move_v(peer, cm, -1, rows, cols); i += 3; continue
            if data[i:i + 3] in (b"\x1b[B", b"\x1bOB"):
                self._move_v(peer, cm, 1, rows, cols); i += 3; continue
            if data[i:i + 3] in (b"\x1b[C", b"\x1bOC"):
                cm.move(0, 1, rows, cols); i += 3; continue
            if data[i:i + 3] in (b"\x1b[D", b"\x1bOD"):
                cm.move(0, -1, rows, cols); i += 3; continue
            if data[i:i + 4] == b"\x1b[5~":          # PgUp
                self._page(peer, rows, -1); i += 4; continue
            if data[i:i + 4] == b"\x1b[6~":          # PgDn
                self._page(peer, rows, 1); i += 4; continue

            i += 1
            if b in (b"q", b"\x1b"):
                cm.leave()
                peer.scroll_offset = 0
                break

            # Paging without PgUp/PgDn: laptop keyboards (MacBooks, most 60%
            # boards) have no dedicated page keys, so the vi bindings tmux also
            # accepts are the ones that actually get used.
            if b == b"\x15":                        # Ctrl-U: half page up
                self._page(peer, rows, -1)
            elif b == b"\x04":                      # Ctrl-D: half page down
                self._page(peer, rows, 1)
            elif b == b"\x06":                      # Ctrl-F: full page down
                self._page(peer, rows, 1, full=True)
            # NB: no Ctrl-B binding here. tmux gives copy-mode its own key table
            # so the prefix doesn't apply, but ours is global — _handle_prefix
            # swallows \x02 before it can ever reach us. Ctrl-U/PgUp cover this.
            elif b == b"b":                         # 'b': full page up (vi)
                self._page(peer, rows, -1, full=True)
            elif b == b"h":
                cm.move(0, -1, rows, cols)
            elif b == b"j":
                self._move_v(peer, cm, 1, rows, cols)
            elif b == b"k":
                self._move_v(peer, cm, -1, rows, cols)
            elif b == b"l":
                cm.move(0, 1, rows, cols)
            elif b in (b"v", b" "):
                cm.start_selection()
            elif b in (b"y", b"\r", b"\n"):
                await self._copy_selection(peer, session)
                break
            elif b == b"0":
                cm.to_line_start()
            elif b == b"$":
                cm.to_line_end(cols)
            elif b == b"g":
                cm.to_top()
            elif b == b"G":
                cm.to_bottom(rows)

        await self._paint(peer, session)

    def _move_v(self, peer, cm, dy: int, rows: int, cols: int) -> None:
        """Move the copy-mode cursor vertically, scrolling at the edges.

        Hitting the top of the screen pulls in the next line of history, and the
        bottom walks back towards the live view — so holding an arrow key just
        keeps going, instead of parking against the edge.
        """
        top = cm.bounds[1] if cm.bounds else 0
        bottom = (cm.bounds[1] + cm.bounds[3] - 1) if cm.bounds else rows - 1

        if dy < 0 and cm.cy <= top:
            peer.scroll_offset += 1
            return
        if dy > 0 and cm.cy >= bottom and peer.scroll_offset > 0:
            peer.scroll_offset -= 1
            return
        cm.move(dy, 0, rows, cols)

    async def _esc_timeout(self, peer) -> None:
        """Resolve a stashed lone ESC as a real Escape if nothing continues it.

        A split arrow key's "[A" tail arrives within a couple ms; a user pressing
        Escape to quit never does. So wait briefly: if the ESC is still pending
        and still lone, it was Escape — leave copy-mode.
        """
        await asyncio.sleep(0.05)
        cm = peer.copy
        if cm.active and cm.pending == b"\x1b":
            cm.pending = bytearray()
            cm.leave()
            peer.scroll_offset = 0
            session = self._session_of(peer)
            if session is not None:
                await self._paint(peer, session)

    def _page(self, peer, rows: int, direction: int, full: bool = False) -> None:
        """Scroll the view. direction: -1 = back in history, +1 = towards live."""
        step = rows if full else max(1, rows // 2)
        if direction < 0:
            peer.scroll_offset += step
        else:
            peer.scroll_offset = max(0, peer.scroll_offset - step)

    async def _copy_selection(self, peer, session) -> None:
        """Copy the selection to the client's system clipboard, then leave."""
        cm = peer.copy
        sel = cm.selection
        if sel is None:
            cm.leave()
            return

        cols, rows = peer.tty.size()
        lines = await self._screen_text(peer, session, cols, rows)
        if lines is None:
            cm.leave()
            return

        text = copymode.extract(lines, sel, cm.bounds)
        cm.leave()
        peer.scroll_offset = 0

        if text:
            # OSC 52 reaches the client's SYSTEM clipboard — a buffer inside the
            # bridge would be unreachable from anywhere the user actually pastes.
            peer.write_out(copymode.osc52(text))
            log.info("copied %d chars to the clipboard", len(text))

    def _bounds_at(self, peer, session, cols: int, rows: int, y: int, x: int):
        """The rectangle of whichever pane contains screen cell (y, x)."""
        if not peer.window_mode:
            return None
        tab = self._tab_of(session)
        if tab is None or len(tab.sessions) < 2:
            return None
        for r in layout.regions(tab.root, cols, rows):
            if r.x <= x < r.x + r.width and r.y <= y < r.y + r.height:
                return (r.x, r.y, r.width, r.height)
        # Clicked a divider: fall back to the active pane.
        return self._pane_bounds(peer, session, cols, rows)

    def _pane_bounds(self, peer, session, cols: int, rows: int):
        """The active pane's rectangle on screen, or None in single-pane mode.

        copy-mode is confined to this, so a selection can't run across a divider
        into the neighbouring pane — the same rule real tmux enforces.
        """
        if not peer.window_mode:
            return None
        tab = self._tab_of(session)
        if tab is None or len(tab.sessions) < 2:
            return None
        for r in layout.regions(tab.root, cols, rows):
            if r.session_id == session.session_id:
                return (r.x, r.y, r.width, r.height)
        return None

