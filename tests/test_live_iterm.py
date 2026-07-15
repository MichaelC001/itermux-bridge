"""Live end-to-end: real tmux attaches to a real iTerm2 session.

Requires iTerm2 running with the Python API enabled, and a bridge listening on
the configured socket. This is the milestone the whole design hinges on (M3/M4):
does an ordinary tmux client render a live iTerm2 session and type into it?

Run:  .venv/bin/python tests/test_live_iterm.py
"""

import asyncio
import os
import pty
import struct
import subprocess
import sys
import fcntl
import termios
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itermux_bridge.config import Config

TMUX = "/opt/homebrew/bin/tmux"


def main() -> int:
    sock = Config.load().socket_path
    if not sock.exists():
        print(f"no bridge socket at {sock} — start the bridge first")
        return 1

    ok = True

    def check(label, cond, extra=""):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {label} {extra}")
        ok = ok and cond

    print("\n=== live: tmux attach -> real iTerm2 session ===")

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))

    proc = subprocess.Popen(
        [TMUX, "-S", str(sock), "attach"],
        stdin=slave, stdout=slave, stderr=slave,
        env={**os.environ, "TERM": "xterm-256color"},
        start_new_session=True,
    )
    os.close(slave)
    os.set_blocking(master, False)

    got = bytearray()
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            chunk = os.read(master, 65536)
            if chunk:
                got.extend(chunk)
        except (BlockingIOError, OSError):
            pass
        time.sleep(0.05)

    screen = bytes(got)

    # Assert on the client's PROCESS STATE, not on strings found in the screen.
    # We may well be rendering an iTerm2 session that is displaying this very
    # test's source or output — searching the pixels for "server exited
    # unexpectedly" happily matches text we ourselves just painted.
    check("client still attached and alive", proc.poll() is None,
          f"(rc={proc.poll()})")
    check("received a screen repaint from iTerm2", len(screen) > 100,
          f"({len(screen)} bytes)")
    check("entered alternate screen", b"\033[?1049h" in screen)
    check("emitted SGR styling (styles re-encoded)", b"\033[0" in screen)
    # NUL bytes desync the client's parser — this is the bug that dropped it.
    check("no NUL bytes in the stream", 0 not in screen)

    printable = sum(1 for b in screen if 32 <= b < 127)
    check("screen carries printable content", printable > 50, f"({printable} chars)")

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    os.close(master)

    print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
    if not ok:
        print(f"tail: {screen[-300:]!r}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
