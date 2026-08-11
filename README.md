# itermux-bridge

Speak the **real tmux protocol** to **iTerm2**. Attach a stock `tmux` client to a
live iTerm2 session, or drive iTerm2 with `tmux send-keys` / `list-panes`.

```bash
tmux -S ~/.itermux/default.sock attach        # see & type into an iTerm2 session
tmux -S ~/.itermux/default.sock list-panes    # enumerate iTerm2 sessions as panes
tmux -S ~/.itermux/default.sock send-keys -t %3 'ls' Enter
```

It is not tmux and does not wrap tmux. It's a Python server that implements
tmux's client↔server wire protocol (imsg framing over a Unix socket, with
`SCM_RIGHTS` fd passing) and maps it onto iTerm2's Python API.

    tmux client  ──imsg/SCM_RIGHTS──▶  itermux-bridge  ──WebSocket──▶  iTerm2
                 ◀──── your tty fd ───┘  (renders + forwards keys)

| tmux         | iTerm2  |
|--------------|---------|
| session `$N` | window  |
| window `@N`  | tab     |
| pane `%N`    | session (a split within a tab) |

```
$ tmux -S ~/.itermux/default.sock ls
$0: 9 windows (attached)

$ tmux -S ~/.itermux/default.sock list-windows
0: zsh (1 panes) [162x59] @0
7: claude* (2 panes) [162x59] @7 (active)

$ tmux -S ~/.itermux/default.sock list-panes -a
0:7.0: [162x29] [vim]    %7
0:7.1: [162x29] [claude] %8 (active)
```

`-t` accepts every form tmux does — `%8` (pane id), `@7` (window id → its active
pane), `7.1` (window.pane index), `$0:7.1`, or a bare `8`. A target that names
nothing is an **error**, never a silent fallback to the focused pane.

**What you attach to depends on what you target:**

```bash
tmux -S ~/.itermux/default.sock a -t @0    # the whole TAB — every pane, with dividers
tmux -S ~/.itermux/default.sock a -t %2    # one PANE, full-screen
tmux -S ~/.itermux/default.sock a          # the focused pane
```

Targeting a window (`@N`, or `$0:1`) composites the tab's whole split layout onto
your terminal — the bridge walks iTerm2's split tree (`tab.root`), scales each
pane's rectangle to your grid in proportion to its real size, and draws box
dividers between them. A 6-pane tab (2 columns × 3 rows) renders as:

```
 pane A            │ pane D
───────────────────┼───────────────────
 pane B            │ pane E     ← active: its border is green
───────────────────┼───────────────────
 pane C            │ pane F
```

The **active pane's border is highlighted green** (tmux's own
`pane-active-border-style` default), so you can see where your keystrokes will
land. It follows `Ctrl-B o` / the arrow keys as you move between panes.

Targeting a pane (`%N`) renders just that one full-screen.

Without `-t`, the bridge renders whatever tab is focused — but never the terminal
the client itself is running in. Attaching a pane to itself is a feedback loop
(the repaint changes the pane, which triggers another repaint), so it is skipped
automatically, and refused with an error if you name it explicitly with `-t`.

IDs (`$N`/`@N`/`%N`) are allocated on first sight and persisted to
`~/.itermux/state.json`, so a given iTerm2 tab keeps its number across restarts.
Indices (the `0:` / `7.1` columns) are positional, like tmux's.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install iterm2
.venv/bin/python -m itermux_bridge.cli install
.venv/bin/python -m itermux_bridge.cli doctor
```

Enable **iTerm2 → Settings → General → Magic → Python API**, then restart iTerm2.
The bridge runs as an AutoLaunch script, so it lives exactly as long as iTerm2 does.

```
itermux-bridge install | uninstall | status | logs [-f] | doctor
```

## Supported

Attach/detach, live screen streaming, keyboard input, mouse (wheel/click/drag),
prefix bindings, `send-keys`, `list-panes`, `list-windows`, `list-sessions`,
`has-session`, `detach-client`, `select-window` / `next-window` /
`previous-window`, `display-message`.

**Prefix bindings** (`Ctrl-B`), mapped onto the equivalent iTerm2 operation:

| key | action | iTerm2 |
|---|---|---|
| `d` | detach | — |
| `c` | new window | new iTerm2 tab (and follow it) |
| `z` | zoom/unzoom the pane | Maximize Active Pane (a real toggle) |
| `o` | next pane | activate the next split |
| arrows, `h` `j` `k` | select pane by direction | `select_pane_in_direction` |
| `Ctrl`+arrows | resize the pane | `preferred_size` + `update_layout` |
| `0`–`9` | jump to window N | activate that tab |
| `l` / `;` | last window / last pane | — |
| `"` / `%` | split horizontally / vertically | `async_split_pane` |
| `x` | kill pane | close the split |
| `PgUp` / `PgDn` | page through the pane's scrollback | — |
| `n` / `p` | next / previous window (tab) | activate the neighbouring tab |
| `[` | copy-mode (select text) | — |
| `m` | toggle mouse reporting (`set -g mouse`) | — |
| `Ctrl-B` | send a literal `Ctrl-B` | — |

**Scrolling a pane's history.** Laptop keyboards mostly have no PgUp/PgDn, so
there are several ways in:

| | |
|---|---|
| `Ctrl-B u` / `Ctrl-B e` | page up / down — the shortest, no modifier needed |
| `Ctrl-B Ctrl-U` / `Ctrl-B Ctrl-D` | same, vi-style |
| `Ctrl-B PgUp` / `Ctrl-B PgDn` | if you do have the keys |

Inside copy-mode (`Ctrl-B [`): `Ctrl-U`/`Ctrl-D` half a page, `b`/`Ctrl-F` a full
page, `g`/`G` to the very top/bottom — and holding an arrow key at the screen
edge keeps pulling in history.

In window mode only the *active* pane scrolls — scrollback is per-pane — and the
split layout stays put around it. Typing anything jumps back to the live screen.

(Your terminal's own scrollbar won't help: it holds our repaint frames, not the
pane's history. The history lives in iTerm2 and is fetched on demand.)

**Selecting text.** Normally you don't need copy-mode at all: with the mouse left
to your terminal (the default), just double-click a word or drag — native
selection, native clipboard.

copy-mode is there for keyboard-driven selection: `Ctrl-B [`, then arrows/`hjkl`
to move, `v`/Space to start a selection, `y`/Enter to copy, `q`/Escape to leave;
`0`/`$` for line ends, `g`/`G` for top/bottom. A selection is confined to one
pane, as in tmux. Copied text goes to your **system clipboard** via OSC 52, not
to a paste buffer inside the bridge.

With `Ctrl-B m` (mouse on), press-drag-release selects and copies as well.

iTerm2 has no zoom API, but it exposes the **menu item** `Maximize Active Pane`,
and its `checked` state makes `Ctrl-B z` a true toggle rather than a one-way trip.

**Mouse — off by default, exactly like tmux.** The bridge does *not* request
mouse reporting on attach, so **your terminal keeps the mouse**: double-click to
select a word, drag-select, right-click, copy to the system clipboard — all the
native behaviour you get outside tmux, unchanged.

This is the whole reason real tmux feels normal to select in: its default is
`mouse off`, so mouse events never reach it. Asking the terminal for
`\033[?1000h` takes the mouse *away* from it, and then a hand-rolled copy-mode
has to reimplement selection — badly (no double-click-to-select-a-word, no
right-click menu).

`Ctrl-B m` toggles reporting **on** when you do want it — the wheel then pages
through iTerm2's scrollback, and a TUI (vim, the Claude CLI) receives its clicks.
The trade is the terminal's own selection, same as `set -g mouse on` in tmux.

Careful, if you turn it on: iTerm2 reports `mouseReportingMode = **-1**` when
reporting is off, not `0` — a truthiness check treats that as "enabled" and
silently breaks scrollback.

Not implemented: control mode (`-CC`), `.tmux.conf` parsing, `resize-pane`,
`rename-window`. Killing sessions is deliberately refused — the bridge doesn't
own those terminals' lifetimes, so `kill-session` detaches instead. The client's
terminal does the drawing (normal mode, not control mode), so any tmux version
on any terminal works.

## Protocol notes

Things the wire format demands that are easy to get wrong — all verified against
tmux 3.7b source and a live client:

- **`IMSG_FD_MARK`** — the high bit of the imsg `len` field (`0x80000000`) flags
  "an fd rides along via SCM_RIGHTS". Real length is `len & ~IMSG_FD_MARK`. Miss
  it and every fd-bearing frame decodes as a ~2 GB length.
- **`peerid` low byte carries `PROTOCOL_VERSION`** (8 for tmux 3.x). Mismatch →
  the server must reply `MSG_VERSION` and hang up.
- **The server owns the client's tty.** `client.c` only calls `cfmakeraw()` for
  control mode; on a normal attach the *server* does `tcsetattr()` on the fd it
  received. Skip that and the line discipline eats every keystroke.
- **`MSG_COMMAND` payload is `struct msg_command { int argc; }` + packed argv**,
  not a bare NUL-separated list — splitting the whole payload yields a bogus
  leading `\x01` argument.
- **`MSG_EXIT` must carry a 4-byte exit status.** An empty payload leaves the
  client at its default of 1, so a clean detach looks like a failure to `$?`.
- **Only send `MSG_READY` to clients that actually attach** (a real tty). Send it
  to a one-shot command client and it takes the attached code path, appending a
  stray `[exited]` to the command's output.
- **iTerm2 returns `\0` for uninitialized cells.** Writing those raw desyncs the
  client's parser and drops the connection; render them as spaces.
- **`cursor_coord.y` is an absolute line number in the session's whole history**
  (we saw `y=705` on a 59-row grid). Rebase it against
  `windowed_coord_range.start.y`, which is the absolute line number of the first
  line you were handed — *not* `number_of_lines_above_screen`, which is often 0
  and leaves the cursor clamped to the bottom row. Get this wrong and TUIs render
  fine but put the cursor in the wrong place.
- **`get_screen_contents()` can return more lines than the client has rows** —
  render the *tail* (what the user is looking at), not the head.
- **`line.string` is indexed by character, but the terminal advances by cell.** A
  CJK glyph or emoji is one index and *two* columns (live: a 146-character line
  was 187 cells wide). Clipping with `text[:cols]` overruns the width, the line
  wraps, and the active background smears down the screen. Clip by display width.
- **`\033[2K` erases using the current background colour**, so reset SGR before
  erasing each row or the previous line's background paints the whole next row.
- **Never erase the whole screen (`\033[2J`) to repaint.** It flashes the
  terminal blank before the new frame lands — visible flicker on every keystroke.
  Write every row instead (erase each one immediately before filling it), so
  stale content from a taller previous frame is overwritten without the screen
  ever going empty.
- **Wrap each frame in a synchronized update** (`\033[?2026h` … `\033[?2026l`).
  A full frame is tens of KB, more than a pty accepts in one `write()`, so it is
  split across several writes and the terminal would otherwise render it
  half-drawn. Emit the closing sequence on *every* return path — an unclosed sync
  block freezes the client's display.

## Tests

```bash
.venv/bin/python tests/test_handshake.py    # real tmux binary: handshake, fds, detach
.venv/bin/python tests/test_command.py      # one-shot command clients
.venv/bin/python tests/test_ansi.py         # CellStyle -> SGR re-encoding
.venv/bin/python tests/test_live_iterm.py   # live attach (needs iTerm2 + bridge)
```

The first two drive the **actual `tmux` binary** against the bridge over a PTY —
they fail if the wire format is wrong.
