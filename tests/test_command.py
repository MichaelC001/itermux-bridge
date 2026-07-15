"""One-shot command clients: `tmux -S sock list-panes` etc.

These never attach — no tty, no screen pump — they just want MSG_COMMAND
answered on their stdout fd. Verifies the CLIENT_TERMINAL split in peer.py.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itermux_bridge.gateway import Gateway

TMUX = "/opt/homebrew/bin/tmux"


class FakeBackend:
    """Answers commands from a canned inventory; no iTerm2 needed."""

    def __init__(self):
        self.commands = []
        self.keys = []

    def on_attach(self, peer):
        pass

    def on_input(self, peer, data):
        pass

    def on_resize(self, peer, cols, rows):
        pass

    def on_mouse(self, peer, events):
        pass

    def on_copy_key(self, peer, data):
        pass

    def on_prefix_command(self, peer, action):
        pass

    def on_detach(self, peer):
        pass

    def on_command(self, peer, argv):
        self.commands.append(argv)
        if not argv or argv[0] in ("attach", "attach-session"):
            peer.attach()
            return
        if argv and argv[0] in ("ls", "list-sessions"):
            peer.write_out(b"$0: 8 panes\n")
            peer.detach(status=0)
        elif argv and argv[0] in ("list-panes", "lsp"):
            peer.write_out(b"%0: [zsh] (active)\n%1: [vim]\n")
            peer.detach(status=0)
        elif argv and argv[0] == "send-keys":
            self.keys.append(argv[1:])
            peer.detach(status=0)
        else:
            peer.write_out(f"unknown: {argv}\n".encode())
            peer.detach(status=1)


def run(sock, args):
    return subprocess.run(
        [TMUX, "-S", str(sock)] + args,
        capture_output=True, timeout=10,
        env={**os.environ, "TERM": "xterm-256color"},
    )


async def main() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="itermux-cmd-"))
    sock = tmpdir / "cmd.sock"

    backend = FakeBackend()
    gw = Gateway(sock, backend)
    gw.start(asyncio.get_running_loop())

    ok = True

    def check(label, cond, extra=""):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {label} {extra}")
        ok = ok and cond

    print("\n=== one-shot command clients ===")

    # Run the blocking tmux client off-thread so our event loop keeps serving.
    r = await asyncio.get_running_loop().run_in_executor(
        None, run, sock, ["list-panes"])
    out = r.stdout.decode()
    check("list-panes reached the server", ["list-panes"] in backend.commands)
    check("command output came back on the client's stdout",
          "%0: [zsh] (active)" in out, f"({out.strip()!r})")
    check("exit status 0", r.returncode == 0, f"(rc={r.returncode})")
    check("no tty seized for a command client (no raw-mode garbage)",
          "\x1b[?1049h" not in out)
    # A command client must stay in client_dispatch_wait(); sending it MSG_READY
    # moves it to the attached path, which appends a stray "[exited]".
    check("clean output, no stray '[exited]'", "[exited]" not in out)

    r = await asyncio.get_running_loop().run_in_executor(
        None, run, sock, ["send-keys", "-t", "%1", "echo", "hi", "Enter"])
    check("send-keys argv parsed", backend.keys == [["-t", "%1", "echo", "hi", "Enter"]],
          f"({backend.keys})")
    check("send-keys exit 0", r.returncode == 0, f"(rc={r.returncode})")

    # Regression: a command run from a REAL TTY (`tmux -S sock ls` typed at a
    # terminal, not piped) must still be treated as a command, not an attach.
    # Deciding by "is stdout a tty?" gets this wrong — it only looked right
    # because the checks above pipe stdout. The command itself is the signal.
    import fcntl, pty, select, struct, termios, threading
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))

    # Drain the pty master on a thread. Polling it from inside this coroutine
    # starves against the event loop that is busy serving the very client we're
    # reading from, and we miss the bytes entirely.
    buf = bytearray()

    def drain():
        while True:
            r, _, _ = select.select([master], [], [], 0.2)
            if not r:
                continue
            try:
                c = os.read(master, 4096)
            except OSError:
                return
            if not c:
                return
            buf.extend(c)

    threading.Thread(target=drain, daemon=True).start()

    proc = subprocess.Popen(
        [TMUX, "-S", str(sock), "ls"],
        stdin=slave, stdout=slave, stderr=slave,
        env={**os.environ, "TERM": "xterm-256color"}, start_new_session=True)
    os.close(slave)

    for _ in range(150):
        await asyncio.sleep(0.02)
        if proc.poll() is not None and buf:
            break
    await asyncio.sleep(0.2)

    tty_out = bytes(buf).decode("utf-8", "replace")
    check("`ls` from a real tty produced its output",
          "$0: 8 panes" in tty_out, f"({tty_out.strip()!r})")
    check("`ls` from a real tty did NOT attach (no '[exited]')",
          "[exited]" not in tty_out)
    check("`ls` from a real tty exited 0", proc.poll() == 0, f"(rc={proc.poll()})")

    if proc.poll() is None:
        proc.kill()
    proc.wait()
    os.close(master)

    gw.stop()
    print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
