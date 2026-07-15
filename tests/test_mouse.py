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

print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
sys.exit(0 if ok else 1)
