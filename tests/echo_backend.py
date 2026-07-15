"""Stub backend: proves the protocol + fd-passing path with no iTerm2 involved.

Per the design doc's §15 advice — isolate "fd forwarding works" from
"iTerm2 API integration works" and kill the risks one at a time.
"""

import logging

log = logging.getLogger(__name__)


class EchoBackend:
    """Paints a banner on attach, echoes keystrokes back to the client."""

    def __init__(self) -> None:
        self.attached = []

    def on_attach(self, peer) -> None:
        self.attached.append(peer)
        cols, rows = peer.tty.size()
        peer.write_out(
            b"\033[?1049h"        # alternate screen
            b"\033[2J\033[H"      # clear, home
            b"\033[1;32m itermux-bridge \033[0m protocol layer is up.\r\n\r\n"
            + f" term={peer.term} size={cols}x{rows} pid={peer.client_pid}\r\n"
              .encode()
            + b" type to echo, press q to detach.\r\n\r\n > "
        )

    def on_input(self, peer, data: bytes) -> None:
        if b"q" in data:
            peer.detach()
            return
        peer.write_out(data)

    def on_resize(self, peer, cols, rows) -> None:
        log.info("resize %dx%d", cols, rows)

    def on_command(self, peer, argv) -> None:
        # Attach-vs-command is decided by the command, not the identify sequence.
        if not argv or argv[0] in ("attach", "attach-session", "a", "at"):
            peer.attach()
            return
        peer.write_out(f"command: {argv}\r\n".encode())

    def on_mouse(self, peer, events):
        pass

    def on_copy_key(self, peer, data):
        pass

    def on_prefix_command(self, peer, action):
        pass

    def on_detach(self, peer) -> None:
        if peer in self.attached:
            self.attached.remove(peer)
