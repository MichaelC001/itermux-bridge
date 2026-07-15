"""Per-client state machine: WAIT_IDENTIFY -> ATTACHED -> CLOSED."""

import asyncio
import logging
import os
import socket
import struct
from typing import Optional

from . import imsg_codec as codec
from . import mouse
from .copymode import CopyMode
from .protocol import CLIENT_CONTROL, Msg
from .tty import ClientTTY

log = logging.getLogger(__name__)

#: The tmux prefix key, Ctrl-B (0x02). tmux's own default.
PREFIX = b"\x02"

#: Prefix bindings, using tmux's own defaults. Each maps to an action the
#: backend translates into an iTerm2 operation. ('d' -> detach is handled
#: separately, since it's the peer's own business, not the backend's.)
PREFIX_KEYS = {
    b"z": "zoom",              # toggle maximize the active pane
    b"o": "next-pane",         # cycle to the next pane
    b"x": "kill-pane",
    b'"': "split-horizontal",  # split into top/bottom
    b"%": "split-vertical",    # split into left/right
    # vi-style pane selection, which tmux also accepts.
    b"h": "select-left",
    b"j": "select-down",
    b"k": "select-up",
    b"l": "select-right",
    b"[": "copy-mode",         # enter copy-mode, as in tmux
    b"]": "paste",
    b"m": "toggle-mouse",      # tmux's `set -g mouse on/off`
    # Paging for keyboards with no PgUp/PgDn (MacBooks, most compact boards).
    # These reach the scrollback WITHOUT having to enter copy-mode first.
    b"\x15": "page-up",        # Ctrl-B Ctrl-U
    b"\x04": "page-down",      # Ctrl-B Ctrl-D
    b"u": "page-up",           # Ctrl-B u  (no modifier needed at all)
    b"n": "page-down",         # Ctrl-B n  ('d' is taken by detach)
}

#: Arrow keys are MULTI-BYTE escape sequences, so they can't live in the
#: single-byte table above — matching a byte at a time never sees them and
#: `Ctrl-B <Up>` silently does nothing. Both the normal (CSI) and application
#: (SS3) cursor-key forms are sent by real terminals depending on mode.
PREFIX_SEQS = {
    b"\x1b[A": "select-up",     b"\x1bOA": "select-up",
    b"\x1b[B": "select-down",   b"\x1bOB": "select-down",
    b"\x1b[C": "select-right",  b"\x1bOC": "select-right",
    b"\x1b[D": "select-left",   b"\x1bOD": "select-left",
    # `Ctrl-B PgUp` enters copy-mode AND pages up in one go, as tmux does —
    # otherwise reading a pane's history means Ctrl-B [ first, every time.
    b"\x1b[5~": "page-up",
    b"\x1b[6~": "page-down",
}

#: Longest sequence we may need to accumulate before deciding.
_MAX_SEQ = max(len(s) for s in PREFIX_SEQS)


def _cstr(payload: bytes) -> str:
    return payload.split(b"\0", 1)[0].decode("utf-8", "replace")


class Peer:
    """One connected tmux client."""

    def __init__(self, sock: socket.socket, backend, loop) -> None:
        self.sock = sock
        self.backend = backend
        self.loop = loop
        self.decoder = codec.Decoder()

        self.term = ""
        self.ttyname = ""
        self.cwd = ""
        self.environ: dict = {}
        self.client_pid: Optional[int] = None
        self.flags = 0
        self.requested_session_id: Optional[str] = None
        #: True when the client attached to a WINDOW (@N) — render every
        #: pane of the tab at once, not just one.
        self.window_mode = False

        self.stdin_fd: Optional[int] = None
        self.stdout_fd: Optional[int] = None
        self.tty: Optional[ClientTTY] = None

        self.attached = False
        self.closed = False
        self.detaching = False
        self._pump_task: Optional[asyncio.Task] = None
        self._outbuf = bytearray()
        self._write_armed = False
        #: True once PREFIX is seen, while we wait for the command key.
        self._await_command = False
        #: Bytes collected since the prefix, for multi-byte keys (arrows).
        self._pending = bytearray()
        #: How many lines back through iTerm2's scrollback this client is
        #: looking. 0 = live screen.
        self.scroll_offset = 0
        #: copy-mode: text selection with the mouse or keyboard.
        self.copy = CopyMode()
        #: Mouse reporting. OFF by default, exactly like tmux — that is what
        #: leaves the terminal's own selection (double-click, drag, right-click
        #: copy) working. Ctrl-B m turns it on.
        self.mouse_on = False

    @property
    def backlogged(self) -> bool:
        """True when the client hasn't drained what we already queued.

        Each repaint is a self-contained full-screen paint, so a newer one makes
        an undelivered older one redundant. Callers use this to drop frames
        instead of growing _outbuf without bound behind a slow reader.
        """
        return len(self._outbuf) > 0

    @property
    def is_control(self) -> bool:
        """Control mode (-C/-CC). We don't implement it; see design §6."""
        return bool(self.flags & CLIENT_CONTROL)

    # --- outbound ---------------------------------------------------------

    def send(self, msg_type: int, payload: bytes = b"") -> None:
        if self.closed:
            return
        try:
            self.sock.sendall(codec.pack(msg_type, payload))
        except OSError as e:
            log.debug("send to peer failed: %s", e)
            self.close()

    def write_out(self, data: bytes) -> None:
        """Queue terminal bytes for the client's stdout fd.

        The fd is non-blocking, and a full-screen repaint is tens of KB — far
        more than a pty will swallow at once. Two things go wrong with a naive
        os.write() here, and both cost us a live session:

          * a partial write silently truncates the byte stream mid-escape-
            sequence, and
          * a full buffer raises BlockingIOError, which — being an OSError —
            got treated as a fatal error and tore the client down.

        So buffer the remainder and flush it when the fd reports writable.
        """
        if self.stdout_fd is None or self.closed or self.detaching:
            return
        self._outbuf.extend(data)
        self._flush()

    def _flush(self) -> None:
        if self.stdout_fd is None or self.closed:
            return
        while self._outbuf:
            try:
                n = os.write(self.stdout_fd, self._outbuf)
            except BlockingIOError:
                break                       # pty full; retry when writable
            except (OSError, BrokenPipeError) as e:
                log.debug("write to client stdout failed: %s", e)
                self.close()
                return
            if n <= 0:
                break
            del self._outbuf[:n]

        # Only watch for writability while we actually have a backlog.
        if self._outbuf and not self._write_armed:
            self.loop.add_writer(self.stdout_fd, self._flush)
            self._write_armed = True
        elif not self._outbuf and self._write_armed:
            self.loop.remove_writer(self.stdout_fd)
            self._write_armed = False

    # --- inbound ----------------------------------------------------------

    def handle(self, msg: codec.Msg) -> None:
        # Mirror peer_check_version(): anything that isn't MSG_VERSION must
        # carry our protocol version in the low byte of peerid.
        if msg.type != Msg.VERSION and msg.version != codec.PROTOCOL_VERSION:
            log.warning("peer bad version %d (want %d)", msg.version,
                        codec.PROTOCOL_VERSION)
            self.send(Msg.VERSION)
            self.close()
            return

        t = msg.type
        if t == Msg.IDENTIFY_FLAGS:
            if len(msg.payload) >= 4:
                self.flags = int.from_bytes(msg.payload[:4], "little")
        elif t == Msg.IDENTIFY_TERM:
            self.term = _cstr(msg.payload)
        elif t == Msg.IDENTIFY_TTYNAME:
            self.ttyname = _cstr(msg.payload)
        elif t in (Msg.IDENTIFY_CWD, Msg.IDENTIFY_OLDCWD):
            self.cwd = _cstr(msg.payload)
        elif t == Msg.IDENTIFY_ENVIRON:
            kv = _cstr(msg.payload)
            if "=" in kv:
                k, v = kv.split("=", 1)
                self.environ[k] = v
        elif t == Msg.IDENTIFY_CLIENTPID:
            if len(msg.payload) >= 4:
                self.client_pid = int.from_bytes(msg.payload[:4], "little",
                                                 signed=True)
        elif t == Msg.IDENTIFY_STDIN:
            self.stdin_fd = msg.fd
        elif t == Msg.IDENTIFY_STDOUT:
            self.stdout_fd = msg.fd
        elif t == Msg.IDENTIFY_DONE:
            self._on_identify_done()
        elif t == Msg.RESIZE:
            self._on_resize()
        elif t == Msg.COMMAND:
            self._on_command(msg.payload)
        elif t in (Msg.DETACH, Msg.DETACHKILL):
            self.detach()
        elif t == Msg.EXITING:
            # Client acknowledging our MSG_EXIT: it has given up its tty and is
            # winding down. Reply MSG_EXITED and let it close the connection.
            self.send(Msg.EXITED)
        else:
            log.debug("ignoring msg type %s", t)

    def _on_identify_done(self) -> None:
        if self.stdout_fd is None:
            log.error("client identified without an stdout fd")
            self.close()
            return

        # We can NOT decide attach-vs-command here. `tmux attach` and `tmux ls`
        # send an identical identify sequence — both from a real tty — and the
        # client always follows with MSG_COMMAND (client.c always sends
        # MSG_COMMAND; only the *command* differs). The real server likewise
        # waits for the command and lets attach-session do the attaching.
        #
        # So: don't seize the tty and don't send MSG_READY yet. Sit tight until
        # MSG_COMMAND tells us which kind of client this is. (An earlier
        # is-it-a-tty test only appeared to work because the test harness piped
        # stdout, making command clients non-ttys by accident.)
        log.debug("identified: term=%s tty=%s pid=%s",
                  self.term, self.ttyname, self.client_pid)

    def attach(self, session_id: Optional[str] = None) -> None:
        """Promote this client to an attached terminal (`attach-session`).

        session_id, when given, is the iTerm2 session the client asked for via
        `-t`; otherwise the backend picks one.
        """
        if self.attached or self.closed or self.stdout_fd is None:
            return
        self.requested_session_id = session_id

        tty = ClientTTY(self.stdout_fd)
        if not tty.is_tty():
            self.write_out(b"open terminal failed: not a terminal\r\n")
            self.detach(status=1)
            return

        # MSG_READY moves the client into client_dispatch_attached(). Only send
        # it to a client we're actually attaching — a command client must stay
        # in client_dispatch_wait(), where MSG_EXIT exits silently instead of
        # tacking a stray "[exited]" onto the command's output.
        self.send(Msg.READY)

        self.tty = tty
        self.tty.start()
        # Non-blocking: a slow client must never stall the event loop, and
        # _flush()/add_writer() depend on getting EAGAIN rather than blocking.
        os.set_blocking(self.stdout_fd, False)
        self.attached = True

        cols, rows = self.tty.size()
        log.info("client attached: term=%s tty=%s %dx%d pid=%s",
                 self.term, self.ttyname, cols, rows, self.client_pid)

        self.backend.on_attach(self)
        if self.stdin_fd is not None:
            self._pump_task = self.loop.create_task(self._pump_stdin())

    def _on_resize(self) -> None:
        if not self.tty:
            return
        cols, rows = self.tty.size()
        log.debug("resize -> %dx%d", cols, rows)
        self.backend.on_resize(self, cols, rows)

    def _on_command(self, payload: bytes) -> None:
        """MSG_COMMAND payload is `struct msg_command { int argc; }` followed by
        argc NUL-terminated strings (client.c packs argv into `data + 1`).

        Splitting the whole payload on NUL — without stripping the argc header —
        yields a bogus leading arg like '\\x01'.
        """
        if len(payload) < 4:
            log.warning("short MSG_COMMAND payload")
            self.backend.on_command(self, [])
            return

        argc = int.from_bytes(payload[:4], "little", signed=True)
        packed = payload[4:]
        argv = [a.decode("utf-8", "replace")
                for a in packed.split(b"\0") if a][:max(argc, 0)]
        log.info("command: %s", argv)
        self.backend.on_command(self, argv)

    async def _pump_stdin(self) -> None:
        """Forward the client's keystrokes to the backend."""
        fd = self.stdin_fd
        os.set_blocking(fd, False)
        ev = asyncio.Event()
        self.loop.add_reader(fd, ev.set)
        try:
            while not self.closed:
                await ev.wait()
                ev.clear()
                try:
                    data = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                except OSError:
                    break
                if not data:
                    break
                data = self._handle_prefix(data)
                if not data:
                    continue

                # Split mouse reports from keystrokes and route each to the
                # handler that understands it. Mouse events must ALWAYS reach
                # on_input (the only place that parses them) — once copy-mode was
                # active they were being funnelled into the keyboard handler,
                # which walked the raw escape bytes as if they were keys, so
                # drag-to-select silently did nothing.
                events, keys = mouse.parse(data)

                if events:
                    self.backend.on_mouse(self, events)

                if not keys:
                    continue

                # The KEYBOARD, though, does belong to copy-mode while it's up:
                # arrows move the selection instead of being typed into the app.
                if self.copy.active:
                    self.backend.on_copy_key(self, keys)
                else:
                    self.backend.on_input(self, keys)
        finally:
            try:
                self.loop.remove_reader(fd)
            except Exception:
                pass

    def _handle_prefix(self, data: bytes) -> bytes:
        """Intercept the tmux prefix key (Ctrl-B) and its command.

        This is the server's job in real tmux: the prefix is swallowed, never
        forwarded to the application. Without it, `Ctrl-B d` just gets typed
        into whatever program is running.

        The prefix and the command that follows may arrive in the same read or
        be split across reads, so the "am I waiting for a command?" bit has to
        live on the peer, not in this call.
        """
        out = bytearray()

        for byte in data:
            ch = bytes([byte])

            if self._await_command:
                self._pending += ch

                # An arrow key is ESC [ A — three bytes. Keep collecting while
                # what we have could still become a bound sequence, otherwise a
                # byte-at-a-time match would never see it.
                if any(s.startswith(self._pending) and s != self._pending
                       for s in PREFIX_SEQS):
                    if len(self._pending) < _MAX_SEQ:
                        continue

                seq = bytes(self._pending)
                self._pending = bytearray()
                self._await_command = False

                action = PREFIX_SEQS.get(seq) or (
                    PREFIX_KEYS.get(seq) if len(seq) == 1 else None)

                if seq in (b"d", b"D"):
                    log.info("prefix d -> detach")
                    self.detach(status=0)
                    # Return what was typed BEFORE the prefix — those keys are
                    # real input and the app should still get them. Returning
                    # b"" here would silently swallow them.
                    return bytes(out)

                if seq == PREFIX:
                    # Ctrl-B Ctrl-B sends a literal Ctrl-B, as in real tmux.
                    out += PREFIX
                    continue

                if action:
                    log.info("prefix %r -> %s", seq, action)
                    self.backend.on_prefix_command(self, action)
                    continue

                # Unbound key: real tmux beeps and drops it. Don't pass either
                # the prefix or the key through, or we'd inject junk into the
                # app (e.g. `Ctrl-B c` would type a stray "c").
                log.debug("prefix + unbound key %r; ignored", seq)
                continue

            if ch == PREFIX:
                self._await_command = True
                self._pending = bytearray()
                continue

            out += ch

        return bytes(out)

    # --- teardown ---------------------------------------------------------

    def detach(self, status: int = 0, message: str = "") -> None:
        """Clean detach: restore the tty, tell the client to exit.

        MSG_EXIT's payload is a 4-byte int exit status (optionally followed by
        a NUL-terminated message). client.c leaves client_exitval at its default
        of 1 when the payload is empty — so an empty MSG_EXIT makes a clean
        detach look like a failure to any script checking $?.
        """
        if self.closed or self.detaching:
            return
        log.info("client detaching (status=%d)", status)
        payload = struct.pack("=i", status)
        if message:
            payload += message.encode() + b"\0"
        self.send(Msg.EXIT, payload)

        # Do NOT close the socket here. If we drop it now the client hits EOF
        # before it reads MSG_EXIT, prints "server exited unexpectedly" and
        # keeps its default exit status of 1. Real tmux keeps the peer alive
        # and lets the client acknowledge (MSG_EXITING) and hang up; we tear
        # down when the read side sees the client's own EOF.
        self.detaching = True

        # Restore the terminal now — the client is on its way out and we want
        # the tty sane even if it lingers.
        if self.tty:
            self.tty.stop()
            self.tty = None

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.attached = False

        if self._pump_task:
            self._pump_task.cancel()

        # Unregister BOTH fds from the selector before closing them. Cancelling
        # _pump_stdin does not run its `finally: remove_reader` synchronously —
        # that happens a loop turn later, by which point we'd have closed the fd
        # and the kernel may have handed the same number to a new connection.
        # The stale registration would then fire on someone else's traffic, and
        # the late remove_reader() would tear down THEIR reader.
        if self.stdin_fd is not None:
            try:
                self.loop.remove_reader(self.stdin_fd)
            except Exception:
                pass
        if self._write_armed and self.stdout_fd is not None:
            try:
                self.loop.remove_writer(self.stdout_fd)
            except Exception:
                pass
            self._write_armed = False
        if self.tty:
            self.tty.stop()

        self.backend.on_detach(self)

        for fd in (self.stdin_fd, self.stdout_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.stdin_fd = self.stdout_fd = None

        try:
            self.loop.remove_reader(self.sock.fileno())
        except Exception:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
