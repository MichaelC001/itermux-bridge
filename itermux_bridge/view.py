"""Screen rendering & repaint scheduling.

Everything about *what the client sees and when*: polling iTerm2 for changes,
deciding whether a frame is worth sending, compositing window mode, and fetching
scrollback. Split out of the backend because this is where the subtle bugs live
(stale frames overwriting a scrolled view, copy-mode chrome not being erased),
and they are far easier to reason about in isolation.

`ScreenView` is mixed into the backend — it uses `self.app`, `self.mapper`,
`self._tab_of`, `self._spawn` from there.
"""

import asyncio
import logging

import iterm2

from . import ansi, layout

log = logging.getLogger(__name__)

# Coalesce screen repaints. iTerm2 fires updates far faster than a terminal
# needs repainting; batching to ~50ms keeps `cat` of a big file from melting
# the WebSocket.
REFRESH_INTERVAL = 0.05


class ScreenView:
    """Repaint scheduling and screen composition for one backend."""

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
                    # The pane we were showing went away (closed, or its tab
                    # did). Breaking here just froze the client's screen with no
                    # explanation. Follow tmux: move to another pane if one is
                    # left, otherwise say so and detach cleanly.
                    replacement = await self.mapper.session_for_attach(
                        self.app, exclude_tty=peer.ttyname)
                    if replacement is None:
                        peer.write_out(
                            b"\r\n\033[31mitermux-bridge:\033[m that pane closed "
                            b"and no other iTerm2 session is available.\r\n")
                        peer.detach(status=0, message="pane closed")
                        break
                    log.info("pane %s vanished; following to %s",
                             sid, replacement.session_id)
                    peer.iterm_session_id = replacement.session_id
                    peer.copy.leave()
                    peer.scroll_offset = 0
                    current = replacement

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
