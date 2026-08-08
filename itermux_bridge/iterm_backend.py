"""Bridges tmux clients to live iTerm2 sessions."""

import asyncio
import logging
from typing import Dict, Optional

import iterm2

from . import ansi, copymode, layout, mouse
from .mapper import SessionMapper
from .actions import PrefixActions
from .iterm.api import ITermAPI
from .input import InputRouter
from .view import REFRESH_INTERVAL, ScreenView

log = logging.getLogger(__name__)


class ITermBackend(PrefixActions, InputRouter, ScreenView):
    """Backend the Gateway talks to. One iTerm2 session per attached client."""

    def __init__(self, connection, app, mapper: SessionMapper) -> None:
        self.connection = connection
        self.app = app
        #: The ONLY component that touches the iTerm2 SDK. Everything above
        #: goes through it, which is what keeps those layers testable.
        self.api = ITermAPI(connection, app)
        self.mapper = mapper
        self.loop = asyncio.get_event_loop()
        #: peer -> pump task streaming that peer's target session
        self._pumps: Dict[object, asyncio.Task] = {}

    # --- Gateway callbacks -------------------------------------------------

    def on_attach(self, peer) -> None:
        self._pumps[peer] = self._spawn(self._attach(peer), "attach")

    async def _attach(self, peer) -> None:
        if peer.requested_session_id:
            # An explicit `-t` wins — commands.py already resolved it.
            session = self.api.pane(peer.requested_session_id)

            # ...unless it's the client's OWN terminal. Rendering a pane into
            # itself is a feedback loop: the paint changes the pane, which
            # triggers another paint. It never settles and just looks hung, so
            # say so instead of silently freezing.
            if session is not None and peer.ttyname:
                tty = await self.api.variable(session, "tty")
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

    def _session_of(self, peer):
        return self.api.pane(getattr(peer, "iterm_session_id", None))

    def _spawn(self, coro, what: str):
        """Run a coroutine as a task, but never let its exception vanish.

        A bare create_task() swallows any exception raised inside — the task
        just dies and the peer is left half-broken with no trace in the log,
        which is exactly how several of the "it silently stopped working" bugs
        hid. Log it instead.
        """
        task = self.loop.create_task(coro)

        def _report(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                log.error("%s failed: %r", what, exc, exc_info=exc)

        task.add_done_callback(_report)
        return task

    def on_resize(self, peer, cols: int, rows: int) -> None:
        # We deliberately do NOT resize the iTerm2 session to match the client:
        # that would visibly reflow the user's real window. We letterbox
        # instead — render what fits.
        log.debug("client resized to %dx%d", cols, rows)

    def on_command(self, peer, argv) -> None:
        from .commands import dispatch
        dispatch(self, peer, argv)

    def on_detach(self, peer) -> None:
        task = self._pumps.pop(peer, None)
        if task:
            task.cancel()

    # --- internals ---------------------------------------------------------

    async def _send(self, session, text: str) -> None:
        await self.api.send_text(session, text)

    async def _send_raw(self, session, data: bytes) -> None:
        """Send raw control bytes (e.g. a mouse report) to the session."""
        await self.api.send_text(session, data.decode("latin-1"))

    def _tab_of(self, session):
        for w in self.app.terminal_windows:
            for t in w.tabs:
                if any(s.session_id == session.session_id for s in t.sessions):
                    return t
        return None

