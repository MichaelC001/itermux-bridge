"""imsg decoder hardening: hostile / malformed input must not exhaust us."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itermux_bridge import imsg_codec as codec

ok = True


def check(label, cond, extra=""):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {label} {extra}")
    ok = ok and cond


print("\n=== decoder limits ===")

# A client that streams bytes which never complete a frame used to grow the
# buffer without bound. A frame can never exceed MAX_IMSGSIZE, so anything past
# that is refused.
d = codec.Decoder()
try:
    for _ in range(3):
        d.feed(b"\x00" * 8000)
    check("truncated-frame flood is refused", False)
except ValueError as e:
    check("truncated-frame flood is refused", "overflow" in str(e))

# Normal frames still decode.
d = codec.Decoder()
d.feed(codec.pack(207))                       # MSG_READY
msg = d.next_msg()
check("a normal frame still decodes", msg is not None and msg.type == 207)

# An over-long declared length is rejected outright.
d = codec.Decoder()
import struct
d.feed(struct.pack(codec.HDR_FMT, 200, codec.MAX_IMSGSIZE + 100, 8, 0))
try:
    d.next_msg()
    check("over-long declared length rejected", False)
except ValueError:
    check("over-long declared length rejected", True)

# Excess fds are closed rather than leaked into our process table.
d = codec.Decoder()
fds = [os.open("/dev/null", os.O_RDONLY) for _ in range(12)]
d.feed(b"", fds)
check("excess fds are reclaimed, not leaked",
      len(d._fds) <= codec.Decoder.MAX_PENDING_FDS,
      f"({len(d._fds)} kept)")
d.close()
check("close() releases the rest", len(d._fds) == 0)

print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
sys.exit(0 if ok else 1)
