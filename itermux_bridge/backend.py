"""What a backend must provide, and what the bridge calls back into.

Two interfaces meet here:

* `Backend` — what the *bridge* needs from whatever owns the real terminals.
  Today that's iTerm2; the protocol layers above never import `iterm2`, so
  another provider only has to implement this.

* `PeerEvents` — what `Peer` calls when input arrives. The gateway/peer layer
  knows nothing about panes; it just routes bytes.

Keeping these explicit is what lets the tmux protocol layer be tested — and
reused — without an iTerm2 running.
"""

from typing import Any, List, Optional, Protocol, Tuple


class Pane(Protocol):
    """The smallest addressable terminal unit (an iTerm2 session)."""

    session_id: str
    name: str


class Backend(Protocol):
    """Everything the bridge needs from the thing that owns the terminals."""

    # --- reading -----------------------------------------------------------

    async def screen(self, pane_id: str) -> Any:
        """Current visible contents of a pane."""

    async def history(self, pane_id: str, rows: int, offset: int) -> Any:
        """`rows` lines ending `offset` lines above the live screen, or None."""

    def pane(self, pane_id: str) -> Optional[Pane]:
        """Look up a pane by id, or None if it's gone."""

    def layout(self, pane_id: str) -> Optional[Tuple[Any, List[Pane]]]:
        """(split tree, panes) of the tab containing `pane_id`."""

    # --- writing -----------------------------------------------------------

    async def send_text(self, pane_id: str, text: str) -> None:
        """Type text into a pane."""

    async def activate(self, pane_id: str) -> None:
        """Make a pane the active one in its tab."""

    async def split(self, pane_id: str, vertical: bool) -> Optional[str]:
        """Split a pane; returns the new pane's id."""

    async def close_pane(self, pane_id: str) -> None:
        """Close a pane."""

    async def zoom(self, pane_id: str) -> None:
        """Toggle maximize for a pane."""


class PeerEvents(Protocol):
    """Callbacks `Peer` fires as client input arrives.

    `Peer` owns the tmux protocol and the client's tty; it delegates every
    decision about *what the bytes mean* to an implementation of this.
    """

    def on_attach(self, peer) -> None: ...
    def on_detach(self, peer) -> None: ...
    def on_input(self, peer, keys: bytes) -> None: ...
    def on_mouse(self, peer, events) -> None: ...
    def on_copy_key(self, peer, data: bytes) -> None: ...
    def on_prefix_command(self, peer, action: str) -> None: ...
    def on_resize(self, peer, cols: int, rows: int) -> None: ...
    def on_command(self, peer, argv) -> None: ...
