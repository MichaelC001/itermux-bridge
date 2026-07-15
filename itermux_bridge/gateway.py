"""The Unix socket listener — the thing `tmux -S <path>` connects to."""

import asyncio
import logging
import os
import socket
import stat
from pathlib import Path

from .peer import Peer

log = logging.getLogger(__name__)

MAX_FDS = 4


class Gateway:
    def __init__(self, sock_path: Path, backend) -> None:
        self.sock_path = Path(sock_path).expanduser()
        self.backend = backend
        self.server: socket.socket = None
        self.peers: list = []
        self.loop = None

    def _prepare_path(self) -> None:
        d = self.sock_path.parent
        d.mkdir(parents=True, exist_ok=True)
        # tmux's make_label() insists the socket dir isn't group/world writable.
        os.chmod(d, 0o700)

        if self.sock_path.exists():
            # A stale socket from a crashed run; only remove if nothing listens.
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(self.sock_path))
            except (ConnectionRefusedError, FileNotFoundError):
                self.sock_path.unlink(missing_ok=True)
            else:
                probe.close()
                raise RuntimeError(f"socket already in use: {self.sock_path}")
            finally:
                probe.close()

    def start(self, loop) -> None:
        self.loop = loop
        self._prepare_path()

        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.sock_path))
        os.chmod(self.sock_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        self.server.listen(16)
        self.server.setblocking(False)

        loop.add_reader(self.server.fileno(), self._accept)
        log.info("listening on %s", self.sock_path)

    def _accept(self) -> None:
        try:
            conn, _ = self.server.accept()
        except OSError:
            return
        conn.setblocking(False)
        peer = Peer(conn, self.backend, self.loop)
        self.peers.append(peer)
        self.loop.add_reader(conn.fileno(), self._readable, peer)
        log.debug("accepted client")

    def _readable(self, peer: Peer) -> None:
        try:
            # Ancillary fds (SCM_RIGHTS) ride alongside the payload bytes.
            data, fds, _flags, _addr = socket.recv_fds(peer.sock, 65535, MAX_FDS)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as e:
            log.debug("recv failed: %s", e)
            self._drop(peer)
            return

        if not data:
            self._drop(peer)
            return

        peer.decoder.feed(data, fds)
        try:
            while (msg := peer.decoder.next_msg()) is not None:
                peer.handle(msg)
                if peer.closed:
                    break
        except ValueError as e:
            log.error("protocol error: %s", e)
            self._drop(peer)
            return

        if peer.closed:
            self._drop(peer)

    def _drop(self, peer: Peer) -> None:
        peer.close()
        if peer in self.peers:
            self.peers.remove(peer)

    def stop(self) -> None:
        for peer in list(self.peers):
            self._drop(peer)
        if self.server:
            try:
                self.loop.remove_reader(self.server.fileno())
            except Exception:
                pass
            self.server.close()
        self.sock_path.unlink(missing_ok=True)
        log.info("gateway stopped")
