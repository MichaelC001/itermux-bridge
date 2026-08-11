"""The tmux prefix key: bindings, the repeat window, and byte-level parsing.

Pure logic — no sockets, no iTerm2, no asyncio. Feed it the bytes a client
typed; it tells you which prefix actions fired and which bytes belong to the
application. That makes the trickiest part of the input path (multi-byte keys
arriving split across reads, `bind -r` repeat) directly testable.
"""

from typing import Callable, List, NamedTuple, Optional, Tuple

#: The tmux prefix key, Ctrl-B (0x02). tmux's own default.
PREFIX = b"\x02"

#: Prefix bindings, using tmux's own defaults. Each maps to an action name the
#: caller translates into a real operation. ('d' -> detach is special-cased: it
#: is the peer's own business, not the backend's.)
PREFIX_KEYS = {
    b"z": "zoom",              # toggle maximize the active pane
    b"o": "next-pane",         # cycle to the next pane
    b"x": "kill-pane",
    b'"': "split-horizontal",  # split into top/bottom
    b"%": "split-vertical",    # split into left/right
    b"c": "new-window",        # the one everybody's fingers know
    b"!": "break-pane",
    b",": "rename-window",
    # vi-style pane selection. NB: tmux binds `l` to last-window, not "right" —
    # so right is the arrow key (or Ctrl-B Right), and `l` matches tmux.
    b"h": "select-left",
    b"j": "select-down",
    b"k": "select-up",
    b";": "last-pane",
    b"l": "last-window",
    b"[": "copy-mode",         # enter copy-mode, as in tmux
    b"]": "paste",
    b"m": "toggle-mouse",      # tmux's `set -g mouse on/off`
    # Window switching, as in tmux.
    b"n": "next-window",
    b"p": "previous-window",
    # Ctrl-B 0..9 jumps straight to a window, as in tmux.
    b"0": "select-window-0", b"1": "select-window-1",
    b"2": "select-window-2", b"3": "select-window-3",
    b"4": "select-window-4", b"5": "select-window-5",
    b"6": "select-window-6", b"7": "select-window-7",
    b"8": "select-window-8", b"9": "select-window-9",
    # Paging for keyboards with no PgUp/PgDn (MacBooks, most compact boards).
    # These reach the scrollback WITHOUT having to enter copy-mode first.
    # NB: tmux binds n/p to window switching, so paging uses u/e instead.
    b"\x15": "page-up",        # Ctrl-B Ctrl-U
    b"\x04": "page-down",      # Ctrl-B Ctrl-D
    b"u": "page-up",           # Ctrl-B u  (no modifier needed at all)
    b"e": "page-down",         # Ctrl-B e  ('d' is detach, 'n' is next-window)
}

#: Arrow keys are MULTI-BYTE escape sequences, so they can't live in the
#: single-byte table above — matching a byte at a time never sees them and
#: `Ctrl-B <Up>` silently does nothing. Both the normal (CSI) and application
#: (SS3) cursor-key forms are sent by real terminals depending on mode.
PREFIX_SEQS = {
    b"\x1b[A": "select-up",     b"\x1bOA": "select-up",
    b"\x1b[B": "select-down",   b"\x1bOB": "select-down",
    b"\x1b[C": "select-right",  b"\x1bOC": "select-right",
    b"\x1b[D": "select-left",   b"\x1bOD": "select-left",
    # `Ctrl-B PgUp` enters copy-mode AND pages up in one go, as tmux does —
    # otherwise reading a pane's history means Ctrl-B [ first, every time.
    b"\x1b[5~": "page-up",
    b"\x1b[6~": "page-down",
    # Ctrl+arrow resizes the pane (tmux's C-Up/C-Down/C-Left/C-Right).
    # xterm encodes the Ctrl modifier as parameter ";5".
    b"\x1b[1;5A": "resize-up",
    b"\x1b[1;5B": "resize-down",
    b"\x1b[1;5C": "resize-right",
    b"\x1b[1;5D": "resize-left",
}

#: Longest sequence we may need to accumulate before deciding.
MAX_SEQ = max(len(s) for s in PREFIX_SEQS)

#: Actions that stay "armed" after firing, so you can hold/tap them again
#: without re-pressing the prefix — tmux's `bind -r`. Pane navigation is the one
#: everybody uses this way: Ctrl-B then ←←← to walk across panes.
REPEATABLE = frozenset({
    "select-left", "select-right", "select-up", "select-down",
    "next-pane", "page-up", "page-down",
    # tmux marks these `bind -r` too: resizing and walking windows are things
    # you do several times in a row.
    "resize-left", "resize-right", "resize-up", "resize-down",
    "next-window", "previous-window",
})

#: How long the repeat window stays open after a repeatable action (seconds).
#: Matches tmux's default `repeat-time` of 500ms.
REPEAT_TIME = 0.5

#: The action name meaning "detach this client" — handled by the peer itself.
DETACH = "detach"


class Result(NamedTuple):
    """What a chunk of input turned into."""
    #: Bytes that belong to the application, in order.
    passthrough: bytes
    #: Prefix actions that fired, in order. May contain DETACH.
    actions: List[str]


class PrefixState:
    """The prefix key state machine.

    `now` is a callable returning a monotonic time in seconds (the event loop's
    clock in production, a fake one in tests) — used only for the repeat window.
    """

    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now
        #: True once PREFIX is seen, while we wait for the command key.
        self.awaiting = False
        #: Bytes collected since the prefix, for multi-byte keys (arrows).
        self.pending = bytearray()
        #: While > now(), a repeatable action can fire again without the prefix.
        self.repeat_until = 0.0

    def feed(self, data: bytes) -> Result:
        """Split `data` into application bytes and prefix actions.

        The prefix and its command may arrive in the same read or be split
        across reads, so the "am I waiting for a command?" state lives here
        rather than in this call.
        """
        out = bytearray()
        actions: List[str] = []

        for byte in data:
            ch = bytes([byte])

            # Repeat window (tmux `bind -r`): after a repeatable action, a key
            # that could be another repeatable one is accepted WITHOUT
            # re-pressing the prefix. Anything that can't begin a repeatable
            # sequence closes the window and is typed normally.
            if not self.awaiting and self.repeat_until:
                if self._now() < self.repeat_until and self._may_repeat(ch):
                    self.awaiting = True
                    self.pending = bytearray()
                else:
                    self.repeat_until = 0.0

            if self.awaiting:
                self.pending += ch

                # Keep collecting while what we have could still become a bound
                # sequence, otherwise a byte-at-a-time match never sees arrows.
                if any(s.startswith(self.pending) and s != self.pending
                       for s in PREFIX_SEQS):
                    if len(self.pending) < MAX_SEQ:
                        continue

                seq = bytes(self.pending)
                self.pending = bytearray()
                self.awaiting = False

                action = PREFIX_SEQS.get(seq) or (
                    PREFIX_KEYS.get(seq) if len(seq) == 1 else None)

                if seq in (b"d", b"D"):
                    # Detaching ends this client: hand back whatever was typed
                    # BEFORE the prefix — those keys are real input — and stop.
                    actions.append(DETACH)
                    return Result(bytes(out), actions)

                if seq == PREFIX:
                    # Ctrl-B Ctrl-B sends a literal Ctrl-B, as in real tmux.
                    out += PREFIX
                    continue

                if action:
                    actions.append(action)
                    # Keep the window open for repeatable actions so the next
                    # arrow (etc.) fires without another prefix.
                    self.repeat_until = (self._now() + REPEAT_TIME
                                         if action in REPEATABLE else 0.0)
                    continue

                # Unbound key: real tmux beeps and drops it. Don't pass either
                # the prefix or the key through, or we'd inject junk into the
                # app (e.g. `Ctrl-B c` would type a stray "c").
                self.repeat_until = 0.0
                continue

            if ch == PREFIX:
                self.awaiting = True
                self.pending = bytearray()
                continue

            out += ch

        return Result(bytes(out), actions)

    def _may_repeat(self, ch: bytes) -> bool:
        """Could `ch` begin a repeatable prefix key?

        Decides whether an open repeat window should accept a bare key (no
        prefix). Arrows start with ESC; the rest are single bytes bound to a
        repeatable action.
        """
        if ch == b"\x1b":
            return True                       # start of an arrow sequence
        return PREFIX_KEYS.get(ch) in REPEATABLE
