"""Bridges tmux clients to live iTerm2 sessions."""

import asyncio
import logging
from typing import Dict, Optional

import iterm2

from . import ansi, copymode, layout, mouse
from .mapper import SessionMapper

log = logging.getLogger(__name__)

# Coalesce screen repaints. iTerm2 fires updates far faster than a terminal
# needs repainting (the doc's §14.4 perf note); batching to ~50ms keeps `cat`
# of a big file from melting the WebSocket.
REFRESH_INTERVAL = 0.05

#: iTerm2 has no zoom API, but it exposes the menu item — and its `checked`
#: state makes it a real toggle, matching Ctrl-B z.
MENU_MAXIMIZE = "Maximize Active Pane"


class ITermBackend:
    """Backend the Gateway talks to. One iTerm2 session per attached client."""

    def __init__(self, connection, app, mapper: SessionMapper) -> None:
        self.connection = connection
        self.app = app
        self.mapper = mapper
        self.loop = asyncio.get_event_loop()
        #: peer -> pump task streaming that peer's target session
        self._pumps: Dict[object, asyncio.Task] = {}

    # --- Gateway callbacks -------------------------------------------------

    def on_attach(self, peer) -> None:
        self._pumps[peer] = self.loop.create_task(self._attach(peer))

    async def _attach(self, peer) -> None:
        if peer.requested_session_id:
            # An explicit `-t` wins — commands.py already resolved it.
            session = self.app.get_session_by_id(peer.requested_session_id)

            # ...unless it's the client's OWN terminal. Rendering a pane into
            # itself is a feedback loop: the paint changes the pane, which
            # triggers another paint. It never settles and just looks hung, so
            # say so instead of silently freezing.
            if session is not None and peer.ttyname:
                try:
                    tty = await session.async_get_variable("tty")
                except Exception:
                    tty = None
                if tty == peer.ttyname:
                    peer.write_out(
                        b"\033[31mitermux-bridge:\033[m that pane is this very "
                        b"terminal.\r\nAttaching it to itself would loop "
                        b"forever. Pick another pane, or attach from a "
                        b"different terminal.\r\n")
                    peer.detach(status=1, message="cannot attach to self")
                    return
        else:
            # Never auto-attach a client to the terminal it is running in — that
            # renders the pane into itself and loops forever. peer.ttyname comes
            # from MSG_IDENTIFY_TTYNAME.
            session = await self.mapper.session_for_attach(
                self.app, exclude_tty=peer.ttyname)

        if session is None:
            peer.write_out(
                b"\033[31mitermux-bridge:\033[m no other iTerm2 session to attach to.\r\n"
                b"Open another iTerm2 tab, or run this from a different terminal.\r\n")
            peer.detach(status=1, message="no iTerm2 session")
            return

        peer.iterm_session_id = session.session_id
        pane = self.mapper.pane_id(session.session_id)
        log.info("peer(tty=%s) -> iTerm2 session %s (pane %s)",
                 peer.ttyname, session.session_id, pane)

        # Deliberately NOT requesting mouse reporting — tmux's own default is
        # `mouse off`, and that is what makes selection feel native: the client's
        # terminal keeps the mouse, so double-click-to-select-a-word, drag,
        # right-click and copy-to-system-clipboard all behave exactly as they do
        # outside tmux. Asking for \033[?1000h takes the mouse away from the
        # terminal, and then a hand-rolled copy-mode has to reimplement all of
        # that — worse. `Ctrl-B m` toggles reporting on for the cases that want
        # it (scrollback via the wheel, or a TUI that handles clicks itself).
        peer.write_out(ansi.ENTER_ALT + ansi.CLEAR)
        await self._pump_screen(peer, session)

    def on_mouse(self, peer, events) -> None:
        """Mouse reports — always handled here, even while copy-mode is up."""
        session = self._session_of(peer)
        if session is not None:
            self.loop.create_task(self._handle_mouse(peer, session, events))

    def on_input(self, peer, keys: bytes) -> None:
        """Client keystrokes -> the mapped iTerm2 session."""
        session = self._session_of(peer)
        if session is None or not keys:
            return

        # Typing anything jumps back to the live screen, like a terminal does.
        if peer.scroll_offset:
            peer.scroll_offset = 0
            self.loop.create_task(self._paint(peer, session))

        # Log only the length: keystrokes carry passwords (§11.5).
        log.debug("input: %d bytes", len(keys))
        self.loop.create_task(
            self._send(session, keys.decode("utf-8", "replace")))

    def _session_of(self, peer):
        sid = getattr(peer, "iterm_session_id", None)
        return self.app.get_session_by_id(sid) if sid else None

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
        if not peer.mouse_on:
            for ev in events:
                await self._send_raw(session, ev.encode())
            return

        try:
            mode = await session.async_get_variable("mouseReportingMode")
        except Exception:
            mode = -1
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

    def on_resize(self, peer, cols: int, rows: int) -> None:
        # We deliberately do NOT resize the iTerm2 session to match the client:
        # that would visibly reflow the user's real window. We letterbox
        # instead — render what fits.
        log.debug("client resized to %dx%d", cols, rows)

    def on_command(self, peer, argv) -> None:
        from .commands import dispatch
        dispatch(self, peer, argv)

    def on_prefix_command(self, peer, action: str) -> None:
        """A tmux prefix binding (Ctrl-B z, etc.) -> the equivalent iTerm2 op."""
        sid = getattr(peer, "iterm_session_id", None)
        if sid is None:
            return
        self.loop.create_task(self._prefix(peer, sid, action))

    def on_copy_key(self, peer, data: bytes) -> None:
        """Keyboard input while copy-mode is active — it drives the selection."""
        self.loop.create_task(self._copy_key(peer, data))

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
                    self.loop.create_task(self._esc_timeout(peer))
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

    async def _screen_text(self, peer, session, cols: int, rows: int):
        """The plain text of what the client is actually LOOKING AT.

        In window mode the selection is in screen coordinates that span the whole
        composite, so extracting from just the active pane's lines would copy the
        wrong text. Rebuild the screen the same way the renderer lays it out.
        """
        if not peer.window_mode:
            try:
                contents = await session.async_get_screen_contents()
            except Exception:
                return None
            return ansi.visible_lines(contents, cols, rows, peer.scroll_offset)

        tab = self._tab_of(session)
        if tab is None:
            return None
        regions = layout.regions(tab.root, cols, rows)
        by_id = {s.session_id: s for s in tab.sessions}

        screen = [[" "] * cols for _ in range(rows)]
        for r in regions:
            s = by_id.get(r.session_id)
            if s is None:
                continue
            try:
                c = await s.async_get_screen_contents()
            except Exception:
                continue
            for i, text in enumerate(
                    ansi.visible_lines(c, r.width, r.height)):
                y = r.y + i
                if not (0 <= y < rows):
                    continue
                col = 0
                for ch in text:
                    gx = r.x + col
                    if gx >= cols:
                        break
                    screen[y][gx] = ch
                    col += 1
        return ["".join(row) for row in screen]

    async def _prefix(self, peer, sid: str, action: str) -> None:
        import iterm2

        session = self.app.get_session_by_id(sid)
        if session is None:
            return

        try:
            if action == "copy-mode":
                cols, rows = peer.tty.size()
                peer.copy.enter(
                    rows, peer.scroll_offset,
                    bounds=self._pane_bounds(peer, session, cols, rows))
                await self._paint(peer, session)
                return

            if action in ("page-up", "page-down"):
                cols, rows = peer.tty.size()
                if not peer.copy.active:
                    peer.copy.enter(
                        rows, peer.scroll_offset,
                        bounds=self._pane_bounds(peer, session, cols, rows))
                self._page(peer, rows, -1 if action == "page-up" else 1)
                # Scrolled all the way back to the live screen: nothing left to
                # be in copy-mode for.
                if peer.scroll_offset == 0:
                    peer.copy.leave()
                await self._paint(peer, session)
                return

            if action == "toggle-mouse":
                # tmux's `set -g mouse on/off`. OFF (the default) leaves the
                # mouse with the client's terminal, so selection is native. ON
                # hands it to us: the wheel pages through scrollback and a TUI
                # gets its clicks — at the cost of the terminal's own selection.
                peer.mouse_on = not peer.mouse_on
                # Leave copy-mode whenever mouse ownership changes. Mouse-driven
                # selection put us there; once the mouse is no longer ours (or
                # its ownership just flipped) there's no way to drive or exit the
                # selection with it, so a leftover copy-mode would trap the
                # keyboard with no way out. Reset fully.
                peer.copy.leave()
                peer.scroll_offset = 0
                peer.write_out(ansi.ENABLE_MOUSE if peer.mouse_on
                               else ansi.DISABLE_MOUSE)
                log.info("mouse reporting %s",
                         "on" if peer.mouse_on else "off")
                await self._paint(peer, session)
                return

            if action == "paste":
                return          # the client's own terminal handles paste

            if action == "zoom":
                await self._zoom(session)

            elif action in ("split-horizontal", "split-vertical"):
                new = await session.async_split_pane(
                    vertical=(action == "split-vertical"))
                # Follow the new pane, like tmux does.
                if new is not None:
                    peer.iterm_session_id = new.session_id

            elif action == "kill-pane":
                await session.async_close()
                peer.detach(status=0)
                return

            elif action in ("next-pane", "select-left", "select-right",
                            "select-up", "select-down"):
                target = await self._neighbour(session, action)
                if target is not None:
                    await target.async_activate()
                    peer.iterm_session_id = target.session_id

            await self.app.async_refresh()
            peer.scroll_offset = 0
            await self._paint(peer, self.app.get_session_by_id(
                peer.iterm_session_id) or session)

        except Exception as e:
            log.warning("prefix %s failed: %s", action, e)

    async def _zoom(self, session) -> None:
        """Toggle iTerm2's 'Maximize Active Pane' — the analogue of Ctrl-B z.

        There is no zoom method on the API objects, but iTerm2 exposes the menu
        item, and its `checked` state tells us whether the pane is already
        zoomed — so this is a real toggle, not a one-way trip.
        """
        import iterm2

        # The menu acts on whatever iTerm2 considers active, so point it at the
        # pane this client is actually looking at first.
        await session.async_activate()
        await iterm2.MainMenu.async_select_menu_item(
            self.connection, MENU_MAXIMIZE)

    async def _neighbour(self, session, action: str):
        """The pane to move to, within the same tab."""
        import iterm2

        tab = None
        for w in self.app.terminal_windows:
            for t in w.tabs:
                if any(s.session_id == session.session_id for s in t.sessions):
                    tab = t
                    break
        if tab is None or len(tab.sessions) < 2:
            return None

        if action == "next-pane":
            ids = [s.session_id for s in tab.sessions]
            i = ids.index(session.session_id)
            return tab.sessions[(i + 1) % len(ids)]

        direction = {
            "select-left": iterm2.NavigationDirection.LEFT,
            "select-right": iterm2.NavigationDirection.RIGHT,
            "select-up": iterm2.NavigationDirection.ABOVE,
            "select-down": iterm2.NavigationDirection.BELOW,
        }[action]
        await session.async_activate()
        try:
            # Returns a session ID (a string), not a Session object.
            new_id = await tab.async_select_pane_in_direction(direction)
        except Exception as e:
            log.debug("select_pane_in_direction failed: %s", e)
            return None
        return self.app.get_session_by_id(new_id) if new_id else None

    def on_detach(self, peer) -> None:
        task = self._pumps.pop(peer, None)
        if task:
            task.cancel()

    # --- internals ---------------------------------------------------------

    async def _send(self, session, text: str) -> None:
        try:
            await session.async_send_text(text)
        except Exception as e:
            log.warning("send_text failed: %s", e)

    async def _send_raw(self, session, data: bytes) -> None:
        """Send raw control bytes (e.g. a mouse report) to the session."""
        try:
            await session.async_send_text(data.decode("latin-1"))
        except Exception as e:
            log.warning("send raw failed: %s", e)

    async def _pump_screen(self, peer, session) -> None:
        """Keep the client's screen in sync until it detaches.

        A ScreenStreamer is bound to ONE session, decided when it is created.
        That was the bug behind "nothing redraws after Ctrl-B z": zooming (or any
        pane switch) moves peer.iterm_session_id, but the streamer stayed parked
        on the pane we attached to, so it never fired again and the screen froze —
        input still reached iTerm2, it just was never painted back.

        And in window mode one streamer is not enough anyway: an update in any
        pane of the tab has to repaint the composite.

        So: poll, and rebuild the watch set whenever the target changes. Polling
        at REFRESH_INTERVAL is what the streamer effectively gave us (we already
        slept that long after every frame), minus the stale-binding trap.
        """
        try:
            await self._paint(peer, session)
            last_sid = peer.iterm_session_id
            last_sig = None

            while not (peer.closed or peer.detaching):
                await asyncio.sleep(REFRESH_INTERVAL)
                if peer.closed or peer.detaching:
                    break

                # Always re-resolve the target: zoom/select-pane/split all move
                # it, and a stale handle is exactly what froze the display.
                sid = peer.iterm_session_id
                current = self.app.get_session_by_id(sid)
                if current is None:
                    break

                # A changed target (or a changed tab shape, e.g. zoom collapsing
                # the split tree) must force a repaint even if no pane emitted.
                sig, contents = await self._signature(peer, current)
                if sid != last_sid or sig != last_sig:
                    # Don't record the new signature if the client is backlogged:
                    # _paint would drop this frame, and we'd never repaint it
                    # because the signature would already look "current".
                    if peer.backlogged:
                        continue
                    last_sid, last_sig = sid, sig
                    # Reuse what _signature already fetched — refetching inside
                    # _paint would double every poll's WebSocket traffic. Only
                    # the single-pane path can take it; window mode fetches all
                    # panes itself.
                    await self._paint(
                        peer, current,
                        contents=None if peer.window_mode else contents)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("screen pump ended: %s", e)
            if not peer.closed:
                peer.detach(status=1, message=str(e))

    async def _signature(self, peer, session):
        """(signature, contents-of-`session`) — changes whenever the view does.

        Covers both WHAT to draw (the tab's pane set, which collapses on zoom)
        and what the panes show. Returns the active session's contents so the
        caller can paint without fetching them a second time.
        """
        sessions = [session]
        if peer.window_mode:
            tab = self._tab_of(session)
            if tab is not None:
                sessions = list(tab.sessions)

        parts = []
        own = None
        for s in sessions:
            try:
                c = await s.async_get_screen_contents()
            except Exception:
                continue
            if s.session_id == session.session_id:
                own = c
            # Hash the visible text plus the cursor: changes on any edit, scroll
            # or cursor move — which is exactly when we must repaint.
            text = "\n".join(c.line(y).string
                             for y in range(c.number_of_lines))
            parts.append((s.session_id, hash(text),
                          c.cursor_coord.x, c.cursor_coord.y))

        # Fold in copy-mode / scroll state. These change what's DRAWN (status
        # bar, selection highlight, scrolled view) without changing iTerm2's
        # content, so the poll must repaint on them too — otherwise entering or
        # leaving copy-mode leaves stale chrome on screen (the "can't get out of
        # copy-mode" symptom: q/Esc worked, but the status bar never got erased).
        cm = peer.copy
        copy_state = (cm.active, cm.cy, cm.cx, cm.anchor, peer.scroll_offset)
        return (tuple(parts), copy_state), own

    def _tab_of(self, session):
        for w in self.app.terminal_windows:
            for t in w.tabs:
                if any(s.session_id == session.session_id for s in t.sessions):
                    return t
        return None

    async def _paint_window(self, peer, session) -> bool:
        """Draw the whole tab — every pane, with dividers.

        Returns False only if there is no tab to draw; a *single*-pane tab is
        still drawn here (as one full-screen region), because that is what a
        zoomed tab looks like. Bailing out to the single-pane renderer instead
        would paint the pane's own grid (iTerm2 keeps it at its pre-zoom size,
        e.g. 100x24) into the client's larger screen and leave the rest blank —
        content stranded in a small box with empty space around it.
        """
        tab = self._tab_of(session)
        if tab is None or not tab.sessions:
            return False

        cols, rows = peer.tty.size()
        regions = layout.regions(tab.root, cols, rows)
        if not regions:
            return False

        by_id = {s.session_id: s for s in tab.sessions}
        panes = []
        titles = {}
        for r in regions:
            s = by_id.get(r.session_id)
            if s is None:
                continue
            try:
                titles[r.session_id] = s.name or ""
                # Scrollback is per-pane, so only the ACTIVE pane scrolls — the
                # others keep showing their live screens. Bailing out of window
                # mode entirely when scrolled (which is what used to happen)
                # collapsed the whole split into a single full-screen pane the
                # moment you pressed PgUp.
                if (peer.scroll_offset > 0
                        and s.session_id == session.session_id):
                    contents = await self._history(
                        s, r.height, peer.scroll_offset)
                    if contents is None:
                        contents = await s.async_get_screen_contents()
                else:
                    contents = await s.async_get_screen_contents()
                panes.append((r, contents))
            except Exception as e:
                log.debug("pane %s contents failed: %s", r.session_id[:8], e)

        if not panes:
            return False

        peer.write_out(ansi.render_panes(
            panes, cols, rows, active_id=session.session_id, copy=peer.copy,
            titles=titles))
        return True

    async def _paint(self, peer, session, contents=None) -> None:
        if peer.closed or peer.tty is None:
            return

        # A caller-supplied `contents` is always the LIVE screen (the pump polls
        # for it). Using it while the client is scrolled back would repaint the
        # live view over the history the user is reading — which is exactly what
        # made paging inside copy-mode appear to do nothing: _page() moved
        # scroll_offset, the frame drew correctly, and 50ms later the pump
        # overwrote it with the live screen.
        if peer.scroll_offset > 0:
            contents = None
        # Drop this frame if the client still hasn't drained the last one. Each
        # paint is a full screen, so the next one supersedes it — queueing both
        # would just grow the buffer behind a slow reader without ever showing
        # them.
        if peer.backlogged:
            log.debug("client backlogged; skipping frame")
            return

        # Window mode: show every pane of the tab at once. Scrollback is
        # per-pane, so a scrolled client falls back to the single-pane view.
        # Window mode handles its own scrolling now (the active pane pages
        # through history while the rest stay live), so don't drop out of the
        # split view just because the client is scrolled back.
        if peer.window_mode:
            if await self._paint_window(peer, session):
                return

        cols, rows = peer.tty.size()

        if peer.scroll_offset > 0:
            # async_get_screen_contents() returns the visible grid only, so it
            # can't serve a scrolled-back view. Ask for an explicit line range.
            scrolled = await self._history(session, rows, peer.scroll_offset)
            if scrolled is not None:
                peer.write_out(ansi.render(scrolled, cols, rows, copy=peer.copy))
                return
            # History unavailable — fall through and show the live screen.
            peer.scroll_offset = 0

        if contents is None:
            contents = await session.async_get_screen_contents()
        peer.write_out(ansi.render(contents, cols, rows,
                                   scroll_offset=peer.scroll_offset,
                                   copy=peer.copy))

    async def _history(self, session, rows: int, offset: int):
        """Fetch `rows` lines ending `offset` lines above the live screen."""
        import iterm2

        try:
            contents = await session.async_get_screen_contents()
            origin = int(contents.windowed_coord_range.start.y)
        except Exception as e:
            log.debug("cannot locate screen origin: %s", e)
            return None

        start = origin - offset
        if start < 0:
            start = 0
        end = start + rows

        rng = iterm2.util.WindowedCoordRange(
            iterm2.util.CoordRange(
                iterm2.util.Point(0, start),
                iterm2.util.Point(0, end)))
        try:
            result = await iterm2.rpc.async_get_screen_contents(
                session.connection, session.session_id, rng, True)
            resp = result.get_buffer_response
            if resp.status != iterm2.api_pb2.GetBufferResponse.Status.Value("OK"):
                return None
            return iterm2.screen.ScreenContents(resp)
        except Exception as e:
            log.debug("history fetch failed: %s", e)
            return None
