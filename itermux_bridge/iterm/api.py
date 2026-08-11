"""The only place that talks to the iTerm2 SDK.

Every `async_*` call, every menu item, every walk over `app.terminal_windows`
lives here. The layers above (view / input / actions / commands) go through this
class, which is what makes them testable without iTerm2 running and what would
let another provider be dropped in.

Errors are swallowed and reported as `None` / no-op rather than raised: a pane
can disappear at any moment between us looking it up and acting on it, and every
caller would otherwise need the same try/except.
"""

import logging
from typing import Any, List, Optional, Tuple

import iterm2

log = logging.getLogger(__name__)

#: iTerm2 has no zoom API, but it exposes the menu item — and its `checked`
#: state makes it a real toggle, matching Ctrl-B z.
MENU_MAXIMIZE = "Maximize Active Pane"


class ITermAPI:
    """Thin, failure-tolerant wrapper over the iTerm2 Python API."""

    def __init__(self, connection, app) -> None:
        self.connection = connection
        self.app = app

    # --- lookup ------------------------------------------------------------

    def pane(self, pane_id: Optional[str]):
        """The iTerm2 session with this id, or None if it's gone."""
        if not pane_id:
            return None
        return self.app.get_session_by_id(pane_id)

    def windows(self) -> List[Any]:
        return list(self.app.terminal_windows)

    def focused_pane(self):
        """The pane the user is looking at, or None."""
        window = self.app.current_terminal_window
        if window is None:
            windows = self.windows()
            if not windows:
                return None
            window = windows[0]
        tab = window.current_tab
        return tab.current_session if tab else None

    def tab_of(self, pane_id: str):
        """The tab containing a pane, or None."""
        for w in self.windows():
            for t in w.tabs:
                if any(s.session_id == pane_id for s in t.sessions):
                    return t
        return None

    def grid_size(self, pane) -> Tuple[int, int]:
        g = pane.grid_size
        return int(g.width), int(g.height)

    async def refresh(self) -> None:
        try:
            await self.app.async_refresh()
        except Exception as e:
            log.debug("app refresh failed: %s", e)

    # --- reading -----------------------------------------------------------

    async def screen(self, pane):
        """Visible contents of a pane, or None."""
        if pane is None:
            return None
        try:
            return await pane.async_get_screen_contents()
        except Exception as e:
            log.debug("screen fetch failed: %s", e)
            return None

    async def history(self, pane, rows: int, offset: int):
        """`rows` lines ending `offset` above the live screen, or None.

        async_get_screen_contents() only returns the visible grid, so scrolled
        views need the lower-level RPC with an explicit line range.
        """
        if pane is None:
            return None
        try:
            contents = await pane.async_get_screen_contents()
            origin = int(contents.windowed_coord_range.start.y)
        except Exception as e:
            log.debug("cannot locate screen origin: %s", e)
            return None

        start = max(0, origin - offset)
        rng = iterm2.util.WindowedCoordRange(
            iterm2.util.CoordRange(
                iterm2.util.Point(0, start),
                iterm2.util.Point(0, start + rows)))
        try:
            result = await iterm2.rpc.async_get_screen_contents(
                pane.connection, pane.session_id, rng, True)
            resp = result.get_buffer_response
            if resp.status != iterm2.api_pb2.GetBufferResponse.Status.Value("OK"):
                return None
            return iterm2.screen.ScreenContents(resp)
        except Exception as e:
            log.debug("history fetch failed: %s", e)
            return None

    async def variable(self, pane, name: str, default=None):
        """A session variable (tty, mouseReportingMode, ...) or `default`."""
        if pane is None:
            return default
        try:
            value = await pane.async_get_variable(name)
        except Exception:
            return default
        return default if value is None else value

    # --- writing -----------------------------------------------------------

    async def send_text(self, pane, text: str) -> None:
        if pane is None:
            return
        try:
            await pane.async_send_text(text)
        except Exception as e:
            log.warning("send_text failed: %s", e)

    async def activate(self, pane) -> None:
        if pane is None:
            return
        try:
            await pane.async_activate()
        except Exception as e:
            log.warning("activate failed: %s", e)

    async def split(self, pane, vertical: bool):
        """Split a pane; returns the new pane or None."""
        if pane is None:
            return None
        try:
            return await pane.async_split_pane(vertical=vertical)
        except Exception as e:
            log.warning("split failed: %s", e)
            return None

    async def close_pane(self, pane) -> None:
        if pane is None:
            return
        try:
            await pane.async_close()
        except Exception as e:
            log.warning("close pane failed: %s", e)

    async def new_window(self, near_pane=None):
        """New tmux window = new iTerm2 tab. Returns its active pane or None.

        Created in the same iTerm2 window as `near_pane` so `Ctrl-B c` lands
        where you're working, not in some other window.
        """
        target = None
        if near_pane is not None:
            for w in self.windows():
                if any(s.session_id == near_pane.session_id
                       for t in w.tabs for s in t.sessions):
                    target = w
                    break
        if target is None:
            target = self.app.current_terminal_window
        if target is None:
            return None
        try:
            tab = await target.async_create_tab()
        except Exception as e:
            log.warning("new tab failed: %s", e)
            return None
        return tab.current_session if tab else None

    async def set_name(self, pane, name: str) -> None:
        """Rename — tmux's rename-window maps to the tab's title."""
        if pane is None:
            return
        try:
            await pane.async_set_name(name)
        except Exception as e:
            log.warning("rename failed: %s", e)

    async def resize(self, tab, pane, direction: str, amount: int = 1) -> None:
        """Grow/shrink a pane, tmux's resize-pane.

        iTerm2 has no per-pane resize call: you set `preferred_size` on the
        sessions you want changed and then commit with async_update_layout().
        """
        if tab is None or pane is None:
            return
        try:
            size = pane.preferred_size
            w, h = int(size.width), int(size.height)
        except Exception:
            w, h = self.grid_size(pane)

        if direction in ("left", "right"):
            w = max(2, w + (amount if direction == "right" else -amount))
        elif direction in ("above", "below"):
            h = max(2, h + (amount if direction == "below" else -amount))
        else:
            return

        try:
            pane.preferred_size = iterm2.util.Size(w, h)
            await tab.async_update_layout()
        except Exception as e:
            log.debug("resize failed: %s", e)

    async def zoom(self, pane) -> None:
        """Toggle 'Maximize Active Pane' for the pane's tab.

        The menu acts on whatever iTerm2 considers active, so point it at the
        pane we mean first.
        """
        if pane is None:
            return
        await self.activate(pane)
        try:
            await iterm2.MainMenu.async_select_menu_item(
                self.connection, MENU_MAXIMIZE)
        except Exception as e:
            log.warning("zoom failed: %s", e)

    async def neighbour(self, tab, pane, direction: str):
        """The pane in `direction` ('left'/'right'/'above'/'below'), or None."""
        if tab is None or pane is None:
            return None
        mapping = {
            "left": iterm2.NavigationDirection.LEFT,
            "right": iterm2.NavigationDirection.RIGHT,
            "above": iterm2.NavigationDirection.ABOVE,
            "below": iterm2.NavigationDirection.BELOW,
        }
        if direction not in mapping:
            return None
        await self.activate(pane)
        try:
            # Returns a session ID (a string), not a Session object.
            new_id = await tab.async_select_pane_in_direction(mapping[direction])
        except Exception as e:
            log.debug("select_pane_in_direction failed: %s", e)
            return None
        return self.pane(new_id) if new_id else None
