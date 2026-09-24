"""Pane sizing: iTerm2 panes follow the attached client, like a tmux window.

The regression this guards: a client narrower than the pane on the Mac saw
every row cut off at its right edge. Drives fit.py against fakes — no iTerm2.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itermux_bridge.fit import SizeFitter, target_sizes

ok = True


def check(label, cond, extra=""):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {label} {extra}")
    ok = ok and bool(cond)


class Grid:
    def __init__(self, w, h):
        self.width, self.height = w, h


class Sess:
    def __init__(self, sid, w=100, h=40):
        self.session_id = sid
        self.grid_size = Grid(w, h)


class Split:
    def __init__(self, vertical, children):
        self.vertical, self.children = vertical, children


print("=== target_sizes ===")

sizes = target_sizes(False, None, "p", 90, 30)
check("pane mode: the pane takes the whole client", sizes == {"p": (90, 30)})

a, b = Sess("a"), Sess("b")
sizes = target_sizes(True, Split(True, [a, b]), "a", 101, 30)
check("window mode: every pane gets a size", set(sizes) == {"a", "b"})
check("...side by side they fill the client less one divider column",
      sizes["a"][0] + sizes["b"][0] == 100, f"({sizes})")
check("...one row shorter than the region, for the pane's title bar",
      sizes["a"][1] == 29 and sizes["b"][1] == 29, f"({sizes})")


print("\n=== SizeFitter ===")


class TTY:
    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows

    def size(self):
        return self.cols, self.rows


class Peer:
    def __init__(self, cols=80, rows=24, tty="/dev/ttys099"):
        self.tty = TTY(cols, rows)
        self.ttyname = tty
        self.window_mode = False
        self.fit_key = self.fit_task = self.fit_window = None


class Tab:
    def __init__(self, tab_id, sessions):
        self.tab_id, self.sessions = tab_id, sessions
        self.all_sessions = sessions
        self.root = Split(True, sessions)


class Window:
    def __init__(self, window_id, tabs):
        self.window_id, self.tabs = window_id, tabs


class API:
    """Records what the fitter asked iTerm2 to do."""

    def __init__(self, windows, ttys=None, fits=True):
        self.windows_ = windows
        self.ttys = ttys or {}           # session_id -> tty
        self.fits = fits
        self.grids, self.restored = [], []

    def tab_of(self, sid):
        return next((t for w in self.windows_ for t in w.tabs
                     if any(s.session_id == sid for s in t.sessions)), None)

    def window_of(self, tab):
        return next((w for w in self.windows_ if tab in w.tabs), None)

    async def frame(self, window):
        return f"frame-of-{window.window_id}"

    async def set_frame(self, wid, frame):
        self.restored.append((wid, frame))

    async def set_grid_sizes(self, sizes):
        # Yield like the real RPC does: a cancellation (e.g. a fit cancelling
        # itself) is only delivered at a real suspension point.
        await asyncio.sleep(0)
        self.grids.append(dict(sizes))
        return self.fits

    async def variable(self, s, name, default=None):
        return self.ttys.get(s.session_id, default)


class Backend(SizeFitter):
    def __init__(self, api):
        self.api = api
        self.loop = asyncio.new_event_loop()

    def _spawn(self, coro, what):
        return self.loop.create_task(coro)

    def settle(self):
        self.loop.run_until_complete(asyncio.sleep(0.01))


def rig(**kw):
    pane = Sess("p")
    win = Window("w1", [Tab("t1", [pane])])
    return Backend(API([win], **kw)), pane


be, pane = rig()
peer = Peer(80, 24)
be._maybe_fit(peer, pane); be.settle()
check("attach fits the pane to the client", be.api.grids == [{"p": (80, 24)}],
      f"({be.api.grids})")

be._maybe_fit(peer, pane); be.settle()
check("a steady view does not refit every poll", len(be.api.grids) == 1)

peer.tty.cols = 60
be._maybe_fit(peer, pane); be.settle()
check("client resize refits", be.api.grids[-1] == {"p": (60, 24)},
      f"({be.api.grids[-1]})")

be._release_fit(peer); be.settle()
check("last client leaving restores the Mac window's original frame",
      be.api.restored == [("w1", "frame-of-w1")], f"({be.api.restored})")

# Two clients on one window: the first to leave must NOT restore it under the
# one still attached; that one refits to its own size instead.
be, pane = rig()
p1, p2 = Peer(80, 24, "/dev/ttys001"), Peer(120, 40, "/dev/ttys002")
be._maybe_fit(p1, pane); be.settle()
be._maybe_fit(p2, pane); be.settle()
be._release_fit(p2); be.settle()
check("one of two clients leaving keeps the window fitted",
      be.api.restored == [])
be._maybe_fit(p1, pane); be.settle()
check("...and the remaining client refits to its own size",
      be.api.grids[-1] == {"p": (80, 24)}, f"({be.api.grids[-1]})")
be._release_fit(p1); be.settle()
check("...restored once both are gone", len(be.api.restored) == 1)

# A client running inside the same iTerm2 window would be resized by the fit,
# report a new size, and refit forever. Leave that window alone.
be, pane = rig(ttys={"p": "/dev/ttys050"})
peer = Peer(80, 24, "/dev/ttys050")
be._maybe_fit(peer, pane); be.settle()
check("client inside the same window: no fit", be.api.grids == [])
be._release_fit(peer); be.settle()
check("...and nothing to restore", be.api.restored == [])

# Fullscreen windows refuse set_grid_size: fall back to cropping, and still
# hand the (unchanged) frame back cleanly.
be, pane = rig(fits=False)
peer = Peer(80, 24)
be._maybe_fit(peer, pane); be.settle()
check("unfittable window doesn't raise", len(be.api.grids) == 1)

# Moving to another iTerm2 window (attach elsewhere, cross-window select)
# gives the first one back.
a, b = Sess("a"), Sess("b")
api = API([Window("w1", [Tab("t1", [a])]), Window("w2", [Tab("t2", [b])])])
be = Backend(api)
peer = Peer(80, 24)
be._maybe_fit(peer, a); be.settle()
be._maybe_fit(peer, b); be.settle()
check("switching windows restores the one we left",
      api.restored == [("w1", "frame-of-w1")], f"({api.restored})")
check("...and fits the new one", api.grids[-1] == {"b": (80, 24)})

print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
sys.exit(0 if ok else 1)
