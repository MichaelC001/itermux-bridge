"""Prefix key (Ctrl-B) handling.

The server must swallow the prefix and its command — never forward them to the
application. Without this, `Ctrl-B d` is just typed into whatever is running.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itermux_bridge.peer import PREFIX, Peer


class FakeBackend:
    def __init__(self):
        self.actions = []

    def on_prefix_command(self, peer, action):
        self.actions.append(action)


class FakePeer(Peer):
    """Just the prefix machine — no socket, no fds."""

    def __init__(self):
        self.detached = False
        self._await_command = False
        self._pending = bytearray()
        self.closed = False
        self.backend = FakeBackend()
        self.mouse_on = False       # tmux's default: the terminal keeps the mouse

    def detach(self, status=0, message=""):
        self.detached = True


ok = True


def check(label, cond, extra=""):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {label} {extra}")
    ok = ok and cond


print("\n=== prefix key (Ctrl-B) ===")

p = FakePeer()
check("ordinary keys pass straight through",
      p._handle_prefix(b"hello") == b"hello")

p = FakePeer()
out = p._handle_prefix(PREFIX + b"d")
check("Ctrl-B d detaches", p.detached)
check("...and forwards nothing to the app", out == b"", f"({out!r})")

# The prefix and its command often arrive in SEPARATE reads — the state has to
# survive across calls, or the prefix leaks through and the 'd' gets typed.
p = FakePeer()
first = p._handle_prefix(PREFIX)
second = p._handle_prefix(b"d")
check("prefix split across two reads still detaches", p.detached)
check("...and neither read leaks bytes to the app",
      first == b"" and second == b"", f"({first!r},{second!r})")

p = FakePeer()
out = p._handle_prefix(PREFIX + PREFIX)
check("Ctrl-B Ctrl-B sends a literal Ctrl-B", out == PREFIX, f"({out!r})")
check("...without detaching", not p.detached)

p = FakePeer()
out = p._handle_prefix(PREFIX + b"z")
check("Ctrl-B + unbound key is swallowed (no stray 'z')", out == b"",
      f"({out!r})")
check("...and does not detach", not p.detached)

# A bare Ctrl-B must not be forwarded — it's a pending prefix, not input.
p = FakePeer()
check("bare Ctrl-B is held, not forwarded", p._handle_prefix(PREFIX) == b"")
check("...and leaves us awaiting a command", p._await_command)

# Text around the prefix must survive intact.
p = FakePeer()
out = p._handle_prefix(b"ab" + PREFIX + b"dcd")
check("Ctrl-B d mid-stream detaches", p.detached)
check("text before the prefix is still delivered", out == b"ab", f"({out!r})")

p = FakePeer()
out = p._handle_prefix(b"x" + PREFIX + b"zy")
check("unbound prefix key drops only the prefix pair",
      out == b"xy", f"({out!r})")

# Ctrl-C and friends must not be mistaken for the prefix.
p = FakePeer()
check("Ctrl-C passes through untouched",
      p._handle_prefix(b"\x03") == b"\x03")

print("\n=== pane bindings ===")

for key, action in ((b"z", "zoom"), (b"o", "next-pane"), (b"x", "kill-pane"),
                    (b'"', "split-horizontal"), (b"%", "split-vertical"),
                    (b"h", "select-left"), (b"l", "select-right")):
    p = FakePeer()
    out = p._handle_prefix(PREFIX + key)
    check(f"Ctrl-B {key.decode('latin-1')} -> {action}",
          p.backend.actions == [action] and out == b"",
          f"({p.backend.actions})")

# A pane binding must not leak its key into the app.
p = FakePeer()
out = p._handle_prefix(b"ab" + PREFIX + b"z" + b"cd")
check("pane binding swallows the key, keeps surrounding text",
      out == b"abcd" and p.backend.actions == ["zoom"], f"({out!r})")

print("\n=== arrow keys (multi-byte) ===")

# Regression: an arrow key is ESC [ A — three bytes. A byte-at-a-time table
# lookup never matches it, so `Ctrl-B <Up>` silently did nothing.
for seq, action in ((b"\x1b[A", "select-up"), (b"\x1b[B", "select-down"),
                    (b"\x1b[C", "select-right"), (b"\x1b[D", "select-left")):
    p = FakePeer()
    out = p._handle_prefix(PREFIX + seq)
    check(f"Ctrl-B {seq!r} -> {action}",
          p.backend.actions == [action] and out == b"",
          f"({p.backend.actions})")

# Application cursor-key mode sends ESC O A instead of ESC [ A.
p = FakePeer()
p._handle_prefix(PREFIX + b"\x1bOA")
check("SS3 form (ESC O A) also works", p.backend.actions == ["select-up"])

# The escape sequence can be split across reads, like any other input.
p = FakePeer()
a = p._handle_prefix(PREFIX + b"\x1b")
b = p._handle_prefix(b"[")
c = p._handle_prefix(b"C")
check("arrow split across three reads still fires",
      p.backend.actions == ["select-right"], f"({p.backend.actions})")
check("...and leaks nothing to the app", a + b + c == b"")

# A bare arrow key (no prefix) must reach the app untouched.
p = FakePeer()
out = p._handle_prefix(b"\x1b[A")
check("arrow WITHOUT the prefix passes through to the app",
      out == b"\x1b[A" and p.backend.actions == [], f"({out!r})")

# Ctrl-B followed by an unbound escape sequence must not leak it.
p = FakePeer()
out = p._handle_prefix(PREFIX + b"\x1b[Z")   # shift-tab
check("Ctrl-B + unbound escape sequence is swallowed", out == b"",
      f"({out!r})")

print("\n=== scrollback ===")

# With the mouse left to the terminal, the wheel no longer pages our scrollback,
# so reading a pane's history must be reachable from the keyboard directly —
# needing `Ctrl-B [` first, every single time, is too clumsy.
for seq, action in ((b"\x1b[5~", "page-up"), (b"\x1b[6~", "page-down")):
    p = FakePeer()
    out = p._handle_prefix(PREFIX + seq)
    check(f"Ctrl-B {seq!r} -> {action}",
          p.backend.actions == [action] and out == b"",
          f"({p.backend.actions})")

# A bare PgUp (no prefix) still belongs to the application.
p = FakePeer()
out = p._handle_prefix(b"\x1b[5~")
check("PgUp WITHOUT the prefix passes through to the app",
      out == b"\x1b[5~" and p.backend.actions == [], f"({out!r})")

print("\n=== mouse ownership ===")

# Regression: we used to request mouse reporting unconditionally on attach. That
# takes the mouse AWAY from the client's terminal, killing double-click-to-select
# -a-word, drag-select, right-click and copy-to-system-clipboard — the very thing
# that makes real tmux feel native (its default is `mouse off`). Default OFF, and
# let Ctrl-B m turn it on for the cases that want the wheel or a TUI's clicks.
p = FakePeer()
check("mouse reporting is OFF by default, like tmux", not p.mouse_on)

p = FakePeer()
p._handle_prefix(PREFIX + b"m")
check("Ctrl-B m is bound to toggle-mouse",
      p.backend.actions == ["toggle-mouse"], f"({p.backend.actions})")

print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
sys.exit(0 if ok else 1)
