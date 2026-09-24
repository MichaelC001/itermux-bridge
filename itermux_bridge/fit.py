"""Size iTerm2 panes to the attached client, the way tmux sizes a window.

Without this, a client narrower than the pane on the Mac (a phone, a laptop
next to a big external display) sees every row cut off at its right edge: the
program in the pane is drawing for the Mac's width, and cropping is all the
renderer can do. tmux never shows that because it resizes the window to its
client and the program redraws. So do the same: while a client is attached,
iTerm2's panes take the client's size; when the last client leaves, the Mac
window gets its original frame back.

`target_sizes` is the pure part. `SizeFitter` is mixed into the backend and
uses `self.api`, `self._spawn` from there.
"""

import logging
from typing import Dict, NamedTuple, Tuple

from . import layout

log = logging.getLogger(__name__)


def target_sizes(window_mode: bool, root, pane_id: str, cols: int,
                 rows: int) -> Dict[str, Tuple[int, int]]:
    """{session_id: (width, height)} each pane needs to be shown whole.

    Pane mode draws one pane over the whole client. Window mode gives each pane
    its rectangle in the composite, minus the title bar drawn in its top row.
    """
    if not window_mode:
        return {pane_id: (cols, rows)}
    return {r.session_id: (max(2, r.width), max(1, r.height - 1))
            for r in layout.regions(root, cols, rows)}


class _Held(NamedTuple):
    """An iTerm2 window we resized, what to put back, and who is fitting it.

    The frame alone isn't enough: fitting one pane of a split moves its
    divider, and restoring the frame only rescales the split proportionally
    (measured: a 46|93 split came back 69|69). So each fitted tab's pane sizes
    are kept too.
    """
    frame: object
    peers: set
    grids: dict         # tab_id -> {session_id: (cols, rows)} before we fit


class SizeFitter:
    """Keeps the panes a client is looking at sized to that client."""

    def _held_windows(self) -> Dict[str, _Held]:
        if not hasattr(self, "_held"):
            self._held = {}
        return self._held

    def _maybe_fit(self, peer, pane) -> None:
        """Refit when what the client shows changes. Called every poll.

        No RPC here: the key is built from the tab object iTerm2 keeps current
        and the client's size, so a steady view costs nothing.
        """
        tab = self.api.tab_of(pane.session_id)
        if tab is None or peer.tty is None:
            return
        cols, rows = peer.tty.size()
        # In window mode moving between panes of one tab (Ctrl-B o) changes the
        # pane but not the layout, so key on the tab there.
        target = tab.tab_id if peer.window_mode else pane.session_id
        key = (target, peer.window_mode,
               tuple(s.session_id for s in tab.sessions), cols, rows)
        if key == peer.fit_key:
            return
        peer.fit_key = key
        # Only the latest fit matters — a client being dragged to a new size
        # produces a burst of them.
        if peer.fit_task is not None:
            peer.fit_task.cancel()
        peer.fit_task = self._spawn(
            self._fit(peer, pane, tab, cols, rows), "fit")

    async def _fit(self, peer, pane, tab, cols: int, rows: int) -> None:
        window = self.api.window_of(tab)
        if window is None:
            return
        # A client running in a pane of this same window would be resized by
        # this too, report its new size, and trigger another fit — forever.
        if await self._client_inside(peer, window):
            return

        held = self._held_windows().get(window.window_id)
        if held is None:
            frame = await self.api.frame(window)
            if frame is None:
                return          # without the original frame we can't undo it
            held = _Held(frame, set(), {})
            self._held_windows()[window.window_id] = held
        if tab.tab_id not in held.grids:
            held.grids[tab.tab_id] = {
                s.session_id: (s.grid_size.width, s.grid_size.height)
                for s in tab.sessions}
        if peer.fit_window != window.window_id:
            # Moved here from another window. Not _release_fit(): that cancels
            # peer.fit_task, which is this very coroutine.
            self._release_window(peer)
        held.peers.add(peer)
        peer.fit_window = window.window_id

        sizes = target_sizes(peer.window_mode, tab.root, pane.session_id,
                             cols, rows)
        if await self.api.set_grid_sizes(sizes):
            log.info("fit %d pane(s) to client %dx%d", len(sizes), cols, rows)
        else:
            log.info("could not fit panes to %dx%d (fullscreen window?); "
                     "rows will be cropped", cols, rows)

    async def _client_inside(self, peer, window) -> bool:
        if not peer.ttyname:
            return False
        for t in window.tabs:
            for s in t.all_sessions:
                if await self.api.variable(s, "tty") == peer.ttyname:
                    return True
        return False

    def _release_fit(self, peer) -> None:
        """The client is gone: stop fitting for it and give its window back."""
        if peer.fit_task is not None:
            peer.fit_task.cancel()
            peer.fit_task = None
        self._release_window(peer)

    def _release_window(self, peer) -> None:
        """This client no longer fits its window; restore it if nobody does."""
        wid, peer.fit_window = peer.fit_window, None
        held = self._held_windows().get(wid)
        if held is None:
            return
        held.peers.discard(peer)
        if held.peers:
            # Another client still looks at this window: let it refit to its
            # own size on its next poll, as tmux follows the latest client.
            for other in held.peers:
                other.fit_key = None
            return
        del self._held_windows()[wid]
        self._spawn(self._restore(wid, held), "restore window size")

    async def _restore(self, wid: str, held: _Held) -> None:
        # Pane sizes first — that puts the dividers back — then the frame,
        # which also puts the window back where it was on screen.
        for sizes in held.grids.values():
            await self.api.set_grid_sizes(sizes)
        await self.api.set_frame(wid, held.frame)
