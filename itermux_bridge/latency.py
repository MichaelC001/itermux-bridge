"""Keystroke-to-screen latency trace, for finding where typing lag comes from.

On while `~/.itermux/trace` exists (touch it / rm it; checked about once a
second, so no restart needed — a restart drops every attached client). Each
measured keystroke logs one line splitting its round trip into stages:

    send       key read from the client -> iTerm2 accepted it (send_text RPC)
    wait       -> the first poll that saw the screen change; includes the app's
                  own time to echo, and how many polls that took
    fetch      that poll's screen fetch from iTerm2
    render     composing the frame and queueing it for the client
    to-client  queued -> fully written to the client's tty. Large here means
               the client (or the link behind it) isn't reading fast enough.

Keys typed while one is being measured are counted into it, not measured on
their own: the trace follows one keystroke at a time, like a stopwatch.
"""

import logging
import time

from .config import ITMUX_DIR

FLAG = ITMUX_DIR / "trace"

log = logging.getLogger("itermux_bridge.latency")

_checked_at = -1.0
_on = False


def enabled() -> bool:
    global _checked_at, _on
    now = time.monotonic()
    if now - _checked_at > 1.0:
        _checked_at, _on = now, FLAG.exists()
    return _on


class KeyTrace:
    """One keystroke's trip: client -> iTerm2 -> screen -> client."""

    def __init__(self, peer) -> None:
        self.peer = peer
        self._t = None

    def key_in(self) -> None:
        if self._t is not None:
            self._t["keys"] += 1
            return
        if enabled():
            self._t = {"in": time.monotonic(), "keys": 1, "polls": 0}

    def sent(self) -> None:
        if self._t is not None and "sent" not in self._t:
            self._t["sent"] = time.monotonic()

    def polled(self, started: float, changed: bool) -> None:
        """A pump poll finished; `started` is when its fetch began."""
        t = self._t
        if t is None or "sent" not in t or "seen" in t:
            return
        t["polls"] += 1
        if changed:
            t["poll_start"], t["seen"] = started, time.monotonic()

    def painted(self) -> None:
        if self._t is not None and "seen" in self._t \
                and "painted" not in self._t:
            self._t["painted"] = time.monotonic()

    def drained(self) -> None:
        t = self._t
        if t is None or "painted" not in t:
            return
        self._t = None
        now = time.monotonic()
        ms = lambda a, b: (t[b] - t[a]) * 1000     # noqa: E731
        log.info("⌨️  latency tty=%s total=%.0fms | send %.0f | wait %.0f "
                 "(%d polls) | fetch %.0f | render %.0f | to-client %.0f | "
                 "keys=%d",
                 self.peer.ttyname, (now - t["in"]) * 1000,
                 ms("in", "sent"), ms("sent", "poll_start"), t["polls"],
                 ms("poll_start", "seen"), ms("seen", "painted"),
                 (now - t["painted"]) * 1000, t["keys"])
