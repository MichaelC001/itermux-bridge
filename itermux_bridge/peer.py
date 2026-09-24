"""Per-client state machine: WAIT_IDENTIFY -> ATTACHED -> CLOSED."""

import asyncio
import logging
import os
import socket
import struct
from typing import Optional

from . import imsg_codec as codec
from . import keys as keymap
from . import mouse
from .copymode import CopyMode
from .protocol import CLIENT_CONTROL, Msg
from .tty import ClientTTY

log = logging.getLogger(__name__)

#: Re-exported so callers (and tests) can reference the prefix key.
PREFIX = keymap.PREFIX


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
        #: The prefix-key state machine (bindings, repeat window, multi-byte
        #: sequence assembly). Pure logic, lives in keys.py.
        self.prefix = keymap.PrefixState(loop.time)
        #: How many lines back through iTerm2's scrollback this client is
        #: looking. 0 = live screen.
        self.scroll_offset = 0
        #: copy-mode: text selection with the mouse or keyboard.
        self.copy = CopyMode()
        #: The pane/window we were on before the current one, for tmux's
        #: `last-pane` (Ctrl-B ;) and `last-window` (Ctrl-B l).
        self.last_pane: Optional[str] = None
        self.last_window_pane: Optional[str] = None
        #: Mouse reporting. OFF by default, exactly like tmux — that is what
        #: leaves the terminal's own selection (double-click, drag, right-click
        #: copy) working. Ctrl-B m turns it on.
        self.mouse_on = False
        #: Whether the pane we're showing is zoomed (Ctrl-B z), as last seen by
        #: the screen pump, and whether that zoom has us owning the mouse. A
        #: zoomed pane reads as a whole terminal, and the one thing people then
        #: reach for is the wheel — which with mouse off only scrolls the
        #: client's own scrollback of repaint frames. Ctrl-B m during a zoom
        #: turns it back off until the next zoom.
        self.zoomed = False
        self.zoom_mouse = False
        #: Pane sizing (fit.py): what the panes were last fitted for, the fit
        #: in flight, and the iTerm2 window we resized for this client.
        self.fit_key = None
        self.fit_task = None
        self.fit_window: Optional[str] = None
        #: The last frame written to this client and the conditions it was
        #: drawn under, so the next paint sends only changed rows (view._emit).
        self.last_frame = None
        self.frame_size = None
        self.frame_copy = False

    @property
    def mouse_owned(self) -> bool:
        """Do we currently hold mouse reporting (explicitly, or for a zoom)?"""
        return self.mouse_on or self.zoom_mouse

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
            self._pump_task.add_done_callback(self._pump_done)

    def _pump_done(self, task) -> None:
        """Surface a crashed stdin pump instead of losing the exception."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("stdin pump died: %r", exc, exc_info=exc)

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
        """Run client input through the prefix machine.

        Returns the bytes that belong to the application; any prefix actions it
        recognised are dispatched here.
        """
        result = self.prefix.feed(data)
        for action in result.actions:
            if action == keymap.DETACH:
                log.info("prefix d -> detach")
                self.detach(status=0)
                break
            log.info("prefix -> %s", action)
            self.backend.on_prefix_command(self, action)
        return result.passthrough

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
        # Any fd the client sent that no frame ever claimed is still ours.
        self.decoder.close()

        try:
            self.loop.remove_reader(self.sock.fileno())
        except Exception:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
