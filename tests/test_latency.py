"""The keystroke latency trace: one log line per measured key, stages in order.

It is the tool for "typing feels slow" reports, so it has to attribute time to
the right stage — and cost nothing when switched off.
"""

import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itermux_bridge import latency

ok = True


def check(label, cond, extra=""):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {label} {extra}")
    ok = ok and bool(cond)


class Peer:
    ttyname = "/dev/ttys012"


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


cap = Capture()
logging.getLogger("itermux_bridge.latency").addHandler(cap)
logging.getLogger("itermux_bridge.latency").setLevel(logging.INFO)

tmp = Path(tempfile.mkdtemp())
latency.FLAG = tmp / "trace"


def fresh():
    latency._checked_at = -1.0          # re-read the flag file now
    return latency.KeyTrace(Peer())


print("=== flag file absent: tracing is off ===")
t = fresh()
t.key_in(); t.sent(); t.polled(0.0, True); t.painted(); t.drained()
check("nothing is logged", cap.lines == [])

print("\n=== flag file present ===")
latency.FLAG.touch()
t = fresh()
t.key_in()
t.key_in()                              # typed while the first is in flight
t.sent()
t.polled(0.0, False)                    # app hadn't echoed yet
import time
t.polled(time.monotonic(), True)        # this poll saw the change
t.painted()
t.drained()
check("one keystroke -> one line", len(cap.lines) == 1, f"({cap.lines})")
line = cap.lines[-1] if cap.lines else ""
for stage in ("total=", "send", "wait", "fetch", "render", "to-client"):
    check(f"reports {stage.rstrip('=')}", stage in line)
check("counts the polls it took", "(2 polls)" in line, f"({line})")
check("folds the second key into the measurement", "keys=2" in line)
check("names the client", "/dev/ttys012" in line)

t.drained()
check("drained with nothing in flight logs nothing more", len(cap.lines) == 1)

# A frame that drains before any change was seen must not end the measurement:
# it would report a keystroke as done before its echo reached the screen.
t = fresh()
t.key_in(); t.sent(); t.drained()
check("an unrelated frame draining doesn't end it", len(cap.lines) == 1)
t.polled(time.monotonic(), True); t.painted(); t.drained()
check("...the echo's frame does", len(cap.lines) == 2)

print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
sys.exit(0 if ok else 1)
