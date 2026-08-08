"""Prefix actions: what Ctrl-B <key> actually does to iTerm2.

zoom, splits, pane navigation, window switching, scrollback paging, copy-mode
entry and the mouse toggle. Kept apart from input *routing* (input.py) and from
rendering (view.py) so each stays readable on its own.

`PrefixActions` is mixed into the backend.
"""

import logging

import iterm2

from . import ansi

log = logging.getLogger(__name__)

#: iTerm2 has no zoom API, but it exposes the menu item — and its `checked`
#: state makes it a real toggle, matching Ctrl-B z.
MENU_MAXIMIZE = "Maximize Active Pane"


class PrefixActions:
    """Execution of the tmux prefix bindings against iTerm2."""

    def on_prefix_command(self, peer, action: str) -> None:
        """A tmux prefix binding (Ctrl-B z, etc.) -> the equivalent iTerm2 op."""
        sid = getattr(peer, "iterm_session_id", None)
        if sid is None:
            return
        self._spawn(self._prefix(peer, sid, action), f"prefix {action}")

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

            elif action in ("next-window", "previous-window"):
                # Windows are iTerm2 tabs — switch to the neighbouring tab's
                # active pane, the way Ctrl-B n/p works in tmux.
                from .commands import _window_target
                cmd = ("next-window" if action == "next-window"
                       else "previous-window")
                target = _window_target(self, self.mapper, self.app, cmd, None)
                if target is not None:
                    await target.async_activate()
                    peer.iterm_session_id = target.session_id
                    peer.copy.leave()

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

