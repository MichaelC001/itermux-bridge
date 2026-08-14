# tmux Basics Not Yet Implemented

Checked item by item against tmux 3.7b's default prefix binding table
(`tmux list-keys -T prefix`) and the commands people actually use.

## Implemented

| Binding | Command | Notes |
|---|---|---|
| `d` | `detach-client` | Detach |
| `z` | `resize-pane -Z` | Zoom/unzoom a pane |
| `o` | `select-pane -t :.+` | Next pane |
| `←↑↓→` / `hjk` | `select-pane -LDUR` | Directional pane select (with `bind -r` repeat) |
| `"` / `%` | `split-window` / `-h` | Split |
| `x` | `kill-pane` | Close pane |
| `n` / `p` | `next-window` / `previous-window` | Switch window |
| `[` | `copy-mode` | Enter copy mode |
| `PgUp`/`PgDn`, `u`/`e`, `C-u`/`C-d` | — | Page through scrollback |
| `m` | `set -g mouse` | Toggle mouse |
| `C-b` | `send-prefix` | Send a literal prefix |

Commands: `attach` `ls` `list-windows` `list-panes` `send-keys` `has-session`
`new-session` (`-d` / `-s <name>`) `detach-client`
`select-window`/`next-window`/`previous-window` `display-message`

**`new-session` used to be a bug**: it sat in `ATTACH_CMDS`, so it neither
created anything nor errored — it just silently attached to an existing pane.
It now really does create a new iTerm2 window (`Window.async_create`). Note
that the **asymmetry** with `kill-*` is deliberate: creating is something the
user explicitly asked for, while destroying would tear down terminals the
bridge does not own.

---

## 1. Added in this round ✅

| Binding | Action | Verified on real hardware |
|---|---|---|
| `c` | `new-window` (create an iTerm2 tab and follow it) | ✅ 8→9 tabs |
| `0`–`9` | Jump to window by index | ✅ |
| `l` | `last-window` (back to the previous window) | ✅ |
| `;` | `last-pane` (previous pane) | ✅ state is tracked |
| `C-←↑↓→` | `resize-pane` (repeatable) | ✅ 15→17 rows |

**Note the semantic change to `l`**: it used to be the vi-style "select the
pane to the right"; it is now back to tmux's `last-window`. To select the pane
to the right, use the **arrow keys** (`Ctrl-B →`). This is a deliberate
alignment with tmux, so long-time users' muscle memory doesn't misfire.

`resize-pane` is implemented by setting `Session.preferred_size` and then
calling `async_update_layout()`. It works in practice, but the **rebound is
imprecise** (15→17→14): iTerm2's layout constraints redistribute the leftover
space, so it isn't exactly reversible the way tmux is. That's a backend
difference, not a bug.

### Still not done

**`,` — `rename-window`**: the iTerm2 API is all there (`async_set_name`), but
this needs a "command prompt" input UI (see §3). For now the key prints a hint
to rename from an external command line.

**`!` — `break-pane`**: iTerm2 has no API for "move a session into a new tab",
so it can't be implemented faithfully. For now the key says explicitly that
it's unsupported.

**`space` — `next-layout` / `M-1`…`M-5` — preset layouts**: `async_update_layout`
makes this theoretically possible, but we'd have to compute the geometry for
even-horizontal / tiled and friends ourselves. Medium effort, mediocre payoff.

---

## 2. Doable, but the semantics need thought

### `&` — `kill-window` / `kill-session`
**Deliberately refused today**: the bridge doesn't own the lifecycle of iTerm2
terminals, and killing one would destroy the user's real work, so it detaches
and explains instead. That's a design trade-off, not an oversight.

### `t` — clock, `?` — `list-keys`, `~` — `show-messages`
Pure UI; doable but low value. `?` listing the key bindings would help new
users.

### `(` `)` `L` — `switch-client` (switch the session a client is attached to)
In this project "session = iTerm2 window", so switching sessions is roughly
switching windows. It could be mapped, but the semantics would differ subtly
from tmux.

---

## 3. Structural gaps (not single commands)

### 1. Command prompt (`:` — `command-prompt`)
tmux's `Ctrl-B :` opens a command line, and it's the entry point for a whole
family of operations: `rename-window`, `find-window`, `move-window`, and more.
**Without it, those commands can only be invoked externally via
`tmux -S ... <cmd>`, not while attached.**

Implementing it requires: drawing an input line at the bottom of the client
screen, handling the editing keys ourselves, and dispatching through the
existing `commands.dispatch` on Enter. This is the **leverage point** for
filling in a large swath of functionality.

### 2. paste buffers (`]` `#` `=` `-`)
tmux has its own stack of clipboard buffers. This project's copy-mode goes
straight to the **system clipboard via OSC 52** (see README), which is a
deliberate choice — a buffer living inside the bridge would have nowhere to be
pasted. So the buffer commands aren't planned.

### 3. `choose-*` interactive pickers (`s` `w` `D` `=`)
`Ctrl-B s` to pick a session and `Ctrl-B w` to pick a window are both common
operations, but they need a full interactive list UI (up/down selection, Enter
to confirm). Fairly large piece of work.

### 4. `.tmux.conf` parsing
Custom bindings and `set -g` options. Today every binding is hard-coded in
`keys.py`. If we do this, we should start with `bind-key` and a small set of
`set-option`s.

---

## 4. Explicitly not planned

| Item | Reason |
|---|---|
| control mode (`-CC`) | The goal is to let **standard tmux clients** connect, not to hand rendering over to a higher-level program |
| `respawn-pane` / `respawn-window` | The bridge doesn't own process lifecycles |
| `display-menu` / `customize-mode` | Heavy UI, at odds with what the bridge is for |
| Cross-machine session migration | A fringe feature of the tmux ecosystem |

---

## Remaining priorities

1. **Command prompt `Ctrl-B :`** — the most leverage; one change unlocks a
   whole set of commands (`rename-window`, `find-window`, `move-window`…)
2. `choose-session` / `choose-window` (`Ctrl-B s` / `w`) — large effort, do it
   if demand appears
3. `.tmux.conf` parsing — makes bindings customizable
4. Preset layouts (`space`, `M-1`…`M-5`) — geometry must be computed by hand
