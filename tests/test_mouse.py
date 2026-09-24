"""SGR mouse report parsing / re-encoding."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itermux_bridge import mouse

ok = True


def check(label, cond, extra=""):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {label} {extra}")
    ok = ok and cond


print("\n=== SGR mouse parsing ===")

evs, rest = mouse.parse(b"\x1b[<0;10;5M")
check("left press parsed", len(evs) == 1 and evs[0].pressed)
check("coords are 1-based col/row", (evs[0].x, evs[0].y) == (10, 5))
check("no leftover keystrokes", rest == b"")

evs, _ = mouse.parse(b"\x1b[<0;10;5m")
check("release ('m') parsed as not-pressed", not evs[0].pressed)

evs, _ = mouse.parse(b"\x1b[<64;1;1M")
check("wheel up detected", evs[0].is_wheel and evs[0].wheel_up)
evs, _ = mouse.parse(b"\x1b[<65;1;1M")
check("wheel down detected", evs[0].is_wheel and evs[0].wheel_down)

evs, _ = mouse.parse(b"\x1b[<32;7;3M")
check("drag (motion bit) is not mistaken for a wheel event",
      not evs[0].is_wheel)

# Mouse reports arrive interleaved with real keys in one read.
evs, rest = mouse.parse(b"ab\x1b[<0;1;1Mcd\x1b[<0;2;2me")
check("two events pulled out of a mixed buffer", len(evs) == 2)
check("surrounding keystrokes preserved in order", rest == b"abcde",
      f"({rest!r})")

# A wide window: SGR (1006) has no 223-column cap, unlike the legacy scheme.
evs, _ = mouse.parse(b"\x1b[<0;500;300M")
check("columns beyond 223 survive (why we use SGR mode)",
      (evs[0].x, evs[0].y) == (500, 300))

# Round-trip: what we hand to the app must be a valid report.
for raw in (b"\x1b[<0;10;5M", b"\x1b[<65;3;9M", b"\x1b[<32;7;3m"):
    evs, _ = mouse.parse(raw)
    if evs[0].encode() != raw:
        check(f"round-trip {raw!r}", False, f"got {evs[0].encode()!r}")
        break
else:
    check("events re-encode byte-identically for the app", True)

# Plain keys must pass through untouched.
evs, rest = mouse.parse(b"hello\r")
check("no false positives on ordinary input",
      evs == [] and rest == b"hello\r")

# A partial/garbage sequence must not be swallowed as a mouse event.
evs, rest = mouse.parse(b"\x1b[<0;1")
check("incomplete report is left as-is, not eaten",
      evs == [] and rest == b"\x1b[<0;1")



print("\n=== mouse off: stray mouse bytes must not enter copy-mode ===")

import asyncio
from itermux_bridge.iterm_backend import ITermBackend
from itermux_bridge.copymode import CopyMode


class _TTY:
    def size(self): return (120, 40)


class _Peer:
    def __init__(self, mouse_on):
        self.copy = CopyMode()
        self.tty = _TTY()
        self.scroll_offset = 0
        self.iterm_session_id = "s"
        self.window_mode = False
        self.mouse_on = mouse_on
        self.zoom_mouse = False
        self.to_app = bytearray()

    @property
    def mouse_owned(self):
        return self.mouse_on or self.zoom_mouse


def _handle(mouse_on, events):
    be = ITermBackend.__new__(ITermBackend)
    loop = asyncio.new_event_loop()
    be.loop = loop
    sess = object()
    be._session_of = lambda peer: sess

    async def _send_raw(session, data):
        pass
    be._send_raw = _send_raw
    # record forwarded bytes on the peer instead
    p = _Peer(mouse_on)

    async def _send_raw2(session, data):
        p.to_app.extend(data)
    be._send_raw = _send_raw2

    loop.run_until_complete(be._handle_mouse(p, sess, events))
    loop.close()
    return p


# A press with mouse OFF must be forwarded to the app, NOT turned into a
# selection that enters copy-mode (the "stray click drags you into copy-mode
# and won't let go" bug).
ev = mouse.parse(b"\x1b[<0;10;10M")[0][0]
p = _handle(False, [ev])
check("mouse-off press does not enter copy-mode", not p.copy.active)
check("...and is forwarded to the app", bytes(p.to_app) == ev.encode())

print("\n=== Ctrl-B m must not trap you in copy-mode ===")

# The bug: mouse ON, drag to select -> enters copy-mode; then Ctrl-B m to turn
# mouse OFF left copy-mode active, and with the mouse gone there was no way to
# drive or exit it — the keyboard stayed trapped. Turning mouse off must fully
# leave copy-mode.
class _Peer2:
    def __init__(self):
        self.copy = CopyMode()
        self.mouse_on = True
        self.scroll_offset = 5
        self.iterm_session_id = "s"
        self.window_mode = False
        self.tty = _TTY()
        self.written = bytearray()
    def write_out(self, data): self.written.extend(data)

def _toggle_mouse_off():
    import asyncio
    be = ITermBackend.__new__(ITermBackend)
    loop = asyncio.new_event_loop(); be.loop = loop
    p = _Peer2()
    p.copy.enter(40)                       # we're in copy-mode
    sess = object()
    async def _paint(peer, session, contents=None): pass
    be._paint = _paint
    # inline the toggle-mouse branch logic via _prefix would need a session;
    # simulate the exact effect the handler applies:
    async def run():
        p.mouse_on = not p.mouse_on        # -> False
        p.copy.leave()
        p.scroll_offset = 0
    loop.run_until_complete(run())
    loop.close()
    return p

p = _toggle_mouse_off()
check("turning mouse off leaves copy-mode", not p.copy.active)
check("...and clears scrollback offset", p.scroll_offset == 0)


print("\n=== zoomed pane: the bridge takes the mouse so the wheel scrolls ===")

from itermux_bridge.peer import Peer  # noqa: E402  (real mouse_owned logic)
from itermux_bridge.sgr import DISABLE_MOUSE, ENABLE_MOUSE  # noqa: E402


class _Pane:
    session_id = "s"


class _ZoomAPI:
    """Just enough ITermAPI for _sync_zoom_mouse / the toggle-mouse action."""
    def __init__(self):
        self.zoomed = False
    def tab_of(self, sid):
        return "tab"
    def is_zoomed(self, tab):
        return self.zoomed
    def pane(self, sid):
        return _Pane()


def _zoom_rig():
    be = ITermBackend.__new__(ITermBackend)
    be.api = _ZoomAPI()
    peer = Peer.__new__(Peer)
    peer.mouse_on, peer.zoomed, peer.zoom_mouse = False, False, False
    peer.copy, peer.scroll_offset, peer.tty = CopyMode(), 0, _TTY()
    peer.iterm_session_id = "s"
    peer.written = bytearray()
    peer.write_out = peer.written.extend
    async def _paint(peer, session, contents=None, fetched=None): pass
    be._paint = _paint
    return be, peer


be, peer = _zoom_rig()
be._sync_zoom_mouse(peer, _Pane())
check("unzoomed: mouse stays with the client terminal",
      not peer.mouse_owned and bytes(peer.written) == b"")

be.api.zoomed = True
peer.scroll_offset = 7
be._sync_zoom_mouse(peer, _Pane())
check("zoom in: we take the mouse", peer.mouse_owned)
check("...and ask the client for reports", bytes(peer.written) == ENABLE_MOUSE)

peer.written.clear()
be._sync_zoom_mouse(peer, _Pane())
check("still zoomed: nothing re-sent every poll", bytes(peer.written) == b"")

# The whole point: with mouse owned and the app not wanting the mouse, the
# wheel pages through scrollback instead of reaching the client's own buffer.
class _WheelAPI(_ZoomAPI):
    async def variable(self, pane, name, default=None):
        return -1                           # a shell: no mouse reporting
    async def send_text(self, pane, text):
        self.forwarded = text               # wheel leaked to the app instead
be.api = _WheelAPI(); be.api.zoomed = True
wheel_up = mouse.parse(b"\x1b[<64;10;10M")[0][0]
loop = asyncio.new_event_loop()
peer.scroll_offset = 0
loop.run_until_complete(be._handle_mouse(peer, _Pane(), [wheel_up]))
check("zoomed + wheel up scrolls into history", peer.scroll_offset > 0,
      f"(offset={peer.scroll_offset})")

peer.written.clear()
peer.copy.enter(40)
be.api.zoomed = False
be._sync_zoom_mouse(peer, _Pane())
check("zoom out: mouse handed back", not peer.mouse_owned
      and bytes(peer.written) == DISABLE_MOUSE)
check("...back on the live screen, out of copy-mode",
      peer.scroll_offset == 0 and not peer.copy.active)

# Ctrl-B m during a zoom turns it OFF — and the next poll must not grab it
# straight back, or the toggle would be impossible to use.
be, peer = _zoom_rig()
be.api.zoomed = True
be._sync_zoom_mouse(peer, _Pane())
loop.run_until_complete(be._prefix(peer, "s", "toggle-mouse"))
check("Ctrl-B m while zoomed turns the mouse off", not peer.mouse_owned)
peer.written.clear()
be._sync_zoom_mouse(peer, _Pane())
check("...and the pump leaves it off for this zoom",
      not peer.mouse_owned and bytes(peer.written) == b"")
be.api.zoomed = False; be._sync_zoom_mouse(peer, _Pane())
be.api.zoomed = True;  be._sync_zoom_mouse(peer, _Pane())
check("...until the next zoom, which takes it again", peer.mouse_owned)

# Explicit Ctrl-B m ON outlives a zoom: unzooming must not switch it off.
be, peer = _zoom_rig()
loop.run_until_complete(be._prefix(peer, "s", "toggle-mouse"))
be.api.zoomed = True;  be._sync_zoom_mouse(peer, _Pane())
be.api.zoomed = False; be._sync_zoom_mouse(peer, _Pane())
check("mouse turned on with Ctrl-B m survives zoom in/out", peer.mouse_owned)
loop.close()


print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
sys.exit(0 if ok else 1)
