"""Typing into a pane whose tab isn't selected brings that tab forward.

iTerm2 doesn't refresh a hidden tab's screen for the API, so the echo of every
key typed there showed up seconds late (measured: ~1s in a real session, not
at all within 8s in a clean test). Drives InputRouter.on_input against fakes.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itermux_bridge.input import InputRouter
from itermux_bridge.latency import KeyTrace

ok = True


def check(label, cond, extra=""):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {label} {extra}")
    ok = ok and bool(cond)


class API:
    def __init__(self, shown):
        self.shown, self.revealed = shown, 0

    def is_shown(self, pane):
        return self.shown

    async def reveal(self, pane):
        self.revealed += 1


class Peer:
    scroll_offset = 0
    ttyname = "/dev/ttys001"

    def __init__(self):
        self.trace = KeyTrace(self)


class Backend(InputRouter):
    def __init__(self, shown):
        self.api = API(shown)
        self.sent = []
        self.loop = asyncio.new_event_loop()

    def _session_of(self, peer):
        return object()

    def _spawn(self, coro, what):
        return self.loop.create_task(coro)

    async def _send(self, session, text):
        self.sent.append(text)

    def run(self, keys):
        self.on_input(Peer(), keys)
        self.loop.run_until_complete(asyncio.sleep(0.01))


be = Backend(shown=False)
be.run(b"a")
check("key into a hidden tab: the tab is brought forward", be.api.revealed == 1)
check("...and the key still reaches the app", be.sent == ["a"])

be = Backend(shown=True)
be.run(b"b")
check("key into a shown tab: no extra call", be.api.revealed == 0)
check("...key delivered", be.sent == ["b"])

print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
sys.exit(0 if ok else 1)
