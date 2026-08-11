"""Prefix key (Ctrl-B) handling.

The server must swallow the prefix and its command — never forward them to the
application. Without this, `Ctrl-B d` is just typed into whatever is running.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itermux_bridge import keys as keymap
from itermux_bridge.keys import PREFIX, PrefixState


class _Clock:
    """Fake monotonic clock so the repeat window is deterministic."""
    def __init__(self): self.t = 1000.0
    def time(self): return self.t


class FakePeer:
    """Drives PrefixState the way Peer does, recording what came out.

    The prefix machine is pure logic now (keys.py), so these tests need no
    socket, no fds and no iTerm2 — they exercise the real code path.
    """

    def __init__(self):
        self.clock = _Clock()
        self.loop = self.clock          # .time() is all PrefixState needs
        self.prefix = PrefixState(self.clock.time)
        self.detached = False
        self.backend = type("B", (), {"actions": None})()
        self.backend.actions = []

    def _handle_prefix(self, data: bytes) -> bytes:
        result = self.prefix.feed(data)
        for action in result.actions:
            if action == keymap.DETACH:
                self.detached = True
                break
            self.backend.actions.append(action)
        return result.passthrough

    # tests poke these; keep the old names working
    @property
    def _await_command(self): return self.prefix.awaiting
    @property
    def _pending(self): return self.prefix.pending


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
                    (b"h", "select-left"), (b"j", "select-down"),
                    (b"c", "new-window"), (b"l", "last-window"),
                    (b";", "last-pane")):
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

# `l` is last-window in tmux, NOT "select right" — the arrow key does that.
# Binding it to select-right would quietly break every tmux user's muscle memory.
p = FakePeer()
p._handle_prefix(PREFIX + b"l")
check("Ctrl-B l is last-window (as in tmux), not select-right",
      p.backend.actions == ["last-window"], f"({p.backend.actions})")

print("\n=== window shortcuts ===")

for digit in (b"0", b"3", b"9"):
    p = FakePeer()
    p._handle_prefix(PREFIX + digit)
    want = f"select-window-{digit.decode()}"
    check(f"Ctrl-B {digit.decode()} -> {want}",
          p.backend.actions == [want], f"({p.backend.actions})")

print("\n=== resize (Ctrl+arrow) ===")

for seq, action in ((b"\x1b[1;5A", "resize-up"), (b"\x1b[1;5B", "resize-down"),
                    (b"\x1b[1;5C", "resize-right"), (b"\x1b[1;5D", "resize-left")):
    p = FakePeer()
    out = p._handle_prefix(PREFIX + seq)
    check(f"Ctrl-B Ctrl+arrow -> {action}",
          p.backend.actions == [action] and out == b"",
          f"({p.backend.actions})")

# Resizing is repeatable, so you can hold it.
p = FakePeer()
p._handle_prefix(PREFIX + b"\x1b[1;5C")
p._handle_prefix(b"\x1b[1;5C")
check("resize repeats without re-pressing the prefix",
      p.backend.actions == ["resize-right"] * 2, f"({p.backend.actions})")

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
p._handle_prefix(PREFIX + b"m")
check("Ctrl-B m is bound to toggle-mouse",
      p.backend.actions == ["toggle-mouse"], f"({p.backend.actions})")

print("\n=== repeat (bind -r): hold arrows to walk panes ===")

# tmux's repeat: after a repeatable action, the next repeatable key fires
# WITHOUT re-pressing the prefix. Pane navigation is the case everyone hits.
p = FakePeer()
p._handle_prefix(PREFIX + b"\x1b[D")     # Ctrl-B Left
p._handle_prefix(b"\x1b[D")              # bare Left  (repeat)
p._handle_prefix(b"\x1b[D")              # bare Left  (repeat)
check("Ctrl-B ← then ← ← repeats without re-pressing prefix",
      p.backend.actions == ["select-left"] * 3, f"({p.backend.actions})")

p = FakePeer()
p._handle_prefix(PREFIX + b"\x1b[D")
p._handle_prefix(b"\x1b[A")
p._handle_prefix(b"\x1b[C")
check("directions can change within the repeat window",
      p.backend.actions == ["select-left", "select-up", "select-right"])

# After the window times out, a bare key is normal input again.
p = FakePeer()
p._handle_prefix(PREFIX + b"\x1b[D")
p.clock.t += 1.0                          # past the 500ms window
out = p._handle_prefix(b"x")
check("a key after the window times out is typed to the app",
      out == b"x" and p.backend.actions == ["select-left"], f"({out!r})")

# A non-repeatable key inside the window closes it and passes through.
p = FakePeer()
p._handle_prefix(PREFIX + b"\x1b[D")
out = p._handle_prefix(b"x")
check("a non-repeatable key in the window is typed, not run",
      out == b"x" and p.backend.actions == ["select-left"], f"({out!r})")

# A non-repeatable action (zoom) does NOT open a repeat window.
p = FakePeer()
p._handle_prefix(PREFIX + b"z")
out = p._handle_prefix(b"z")
check("zoom does not repeat; a second bare z is typed",
      out == b"z" and p.backend.actions == ["zoom"], f"({out!r})")

print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
sys.exit(0 if ok else 1)
