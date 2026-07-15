"""End-to-end: does the REAL tmux binary shake hands with our fake server?

We give tmux a genuine PTY (it refuses to attach without a tty), let it do its
full MSG_IDENTIFY_* dance over SCM_RIGHTS, and then check that bytes we write to
the stdout fd it passed us actually surface on that PTY.

This is the single highest-risk claim in the whole design. If it passes, the
protocol layer is real.
"""

import asyncio
import os
import pty
import subprocess
import sys
import tempfile
import termios
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itermux_bridge.gateway import Gateway
from tests.echo_backend import EchoBackend

TMUX = "/opt/homebrew/bin/tmux"


async def main() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="itermux-test-"))
    sock = tmpdir / "test.sock"

    backend = EchoBackend()
    gw = Gateway(sock, backend)
    loop = asyncio.get_running_loop()
    gw.start(loop)

    # tmux needs a real terminal or it bails with "open terminal failed".
    master, slave = pty.openpty()
    # Give it a known size so we can assert on it.
    import fcntl, struct
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))

    proc = subprocess.Popen(
        [TMUX, "-S", str(sock), "attach"],
        stdin=slave, stdout=slave, stderr=slave,
        env={**os.environ, "TERM": "xterm-256color"},
        start_new_session=True,
    )
    os.close(slave)

    # Collect whatever tmux's terminal receives.
    got = bytearray()
    os.set_blocking(master, False)

    def on_master():
        try:
            chunk = os.read(master, 4096)
        except (BlockingIOError, OSError):
            return
        got.extend(chunk)

    loop.add_reader(master, on_master)

    # Give the handshake time to complete.
    for _ in range(50):
        await asyncio.sleep(0.1)
        if backend.attached and b"protocol layer is up" in got:
            break

    ok = True

    def check(label, cond, extra=""):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {label} {extra}")
        ok = ok and cond

    print("\n=== tmux 3.7b <-> itermux-bridge handshake ===")
    check("tmux client connected and identified", bool(backend.attached))

    if backend.attached:
        peer = backend.attached[0]
        check("received stdin fd via SCM_RIGHTS", peer.stdin_fd is not None,
              f"(fd={peer.stdin_fd})")
        check("received stdout fd via SCM_RIGHTS", peer.stdout_fd is not None,
              f"(fd={peer.stdout_fd})")
        check("TERM propagated", peer.term == "xterm-256color", f"({peer.term!r})")
        check("window size read via TIOCGWINSZ on passed fd",
              peer.tty.size() == (100, 30), f"({peer.tty.size()})")
        check("client pid reported", peer.client_pid is not None,
              f"({peer.client_pid})")

    check("server output reached the client's real terminal",
          b"protocol layer is up" in bytes(got))
    check("no version mismatch error",
          b"protocol version mismatch" not in bytes(got))

    # Echo path: type into the pty, expect it echoed back by the backend.
    if backend.attached:
        got.clear()
        os.write(master, b"XYZ")
        for _ in range(20):
            await asyncio.sleep(0.05)
            if b"XYZ" in bytes(got):
                break
        check("keystrokes flow client -> server -> back out", b"XYZ" in bytes(got))

        # 'q' triggers detach in the stub backend.
        os.write(master, b"q")
        for _ in range(30):
            await asyncio.sleep(0.05)
            if proc.poll() is not None:
                break
        check("clean detach: tmux client exited", proc.poll() is not None,
              f"(rc={proc.poll()})")
        # MSG_EXIT must carry a 4-byte status or the client defaults to 1.
        check("detach reports success exit status", proc.poll() == 0,
              f"(rc={proc.poll()})")

    if proc.poll() is None:
        proc.kill()
        proc.wait()
    gw.stop()
    os.close(master)

    tail = bytes(got)[-200:]
    if not ok:
        print(f"\n  last bytes from tmux terminal: {tail!r}")
    print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
