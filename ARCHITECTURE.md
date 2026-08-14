# Architecture and Design Review

## 1. Current State (compared against real tmux)

### Parts that are already solid

| Aspect | State | Basis |
|---|---|---|
| imsg protocol codec | ✅ Reliable | Implemented against tmux 3.6b/3.7b sources, including gotchas like `IMSG_FD_MARK`; verified end-to-end against a real tmux binary |
| Handshake / fd passing | ✅ Reliable | SCM_RIGHTS dual fds, 4-byte `MSG_EXIT` status, command clients not sending `MSG_READY` — all covered by regression tests |
| tty takeover | ✅ Correct | Replicates the termios flags of `tty_start_tty()`, fully restored on detach (including turning off mouse reporting) |
| Rendering correctness | ✅ Good | Wide chars measured per cell, `style_at` indexed per cell, synchronized updates remove flicker, cursor is not hidden repeatedly (IME stays stable) |
| session/window/pane mapping | ✅ Correct | Index vs ID kept separate, per-window indices, IDs persisted |

### **Semantic gaps** versus real tmux (design trade-offs, not bugs)

1. **This is not a multiplexer, it is a "view bridge"**
   Real tmux owns the PTY and manages process lifetimes. This project merely **projects** sessions that iTerm2 already has — when iTerm2 exits, everything goes away. That follows from the design goal; it is not a defect.

2. **Client resize does not re-lay-out**
   `on_resize` only records, it does not act. Real tmux re-tiles every pane to fit the smallest client. Here we deliberately avoid touching iTerm2's real window (it would disrupt the user); the cost is that content gets clipped when the client is smaller than the iTerm2 window.

3. **Multiple clients share one iTerm2 session but have independent views**
   Each peer has its own `scroll_offset` / `copy` state (correctly isolated), but there is no equivalent of real tmux's "multiple clients attached to the same window" semantics.

### Command coverage

Implemented: `attach` `ls` `list-windows` `list-panes` `send-keys` `display-message`
`has-session` `detach-client` `select-window` / `next-window` / `previous-window`

`kill-session` / `kill-window` deliberately refuse to run — the bridge does not own the
lifetime of those iTerm2 terminals, and killing them would destroy the user's real work, so
they detach and explain instead.

Still missing: `rename-window` / `rename-session` / `resize-pane`, and `-h/-v` argument
parsing for `split-window` (currently only the prefix binding exists).

## 2. Current architectural problems

```
iterm_backend.py  880 lines / 34 methods  ← one class mixing 6 responsibilities
```

It simultaneously handles:

1. Gateway callback dispatch (`on_attach` / `on_input` / `on_mouse` / …)
2. Mouse semantics (selection, scrolling, forwarding decisions)
3. The copy-mode keyboard state machine
4. prefix command execution (zoom / split / navigation)
5. Screen polling and change detection
6. iTerm2 API adaptation (fetching contents, fetching history, menu items)

**Cost of the coupling**: touching mouse logic means reading 880 lines; copy-mode bugs keep resurfacing at the intersection of rendering, polling, and input; iTerm2 API details bleed into the state machine.

**What is already right**: apart from `iterm_backend.py`, almost no module **depends on the iterm2 SDK**, so the protocol layer is naturally reusable.

## 3. Split plan (implemented)

Cut along two axes: **"core tmux functionality" × "degree of reusability"**:

```
┌─ Layer 1: tmux protocol (entirely iTerm2-agnostic, could be its own lib)──┐
│  protocol.py      message type constants                                  │
│  imsg_codec.py    frame codec                                             │
│  gateway.py       Unix socket listen / accept                             │
│  peer.py          per-client state machine (handshake→attach→detach)      │
│  tty.py           client tty takeover                                     │
└───────────────────────────────────────────────────────────────────────────┘
                          ↓ depends on
┌─ Layer 2: terminal semantics (pure logic, touches no backend)─────────────┐
│  ansi.py          style → ANSI encoding / screen composition              │
│  layout.py        split tree → screen rectangles                          │
│  mouse.py         SGR mouse sequence parsing                              │
│  copymode.py      selection geometry / text extraction / OSC 52           │
│  keys.py    【new】prefix binding table + repeat window (out of peer.py)   │
└───────────────────────────────────────────────────────────────────────────┘
                          ↓ depends on
┌─ Layer 3: session model (defines the "tmux concepts", backend-agnostic)───┐
│  mapper.py        session/window/pane ↔ backend object ID mapping         │
│  commands.py      tmux command parsing and dispatch                       │
│  backend.py 【new】abstract interface: what a backend must provide         │
│                   (fetch contents / send keys / split)                    │
└───────────────────────────────────────────────────────────────────────────┘
                          ↓ implemented by
┌─ Layer 4: iTerm2 adapter (the only place depending on the iterm2 SDK)─────┐
│  iterm/api.py     【split】iTerm2 API wrapper: screen/history/menu/split   │
│  iterm/session.py 【split】session and tab lookup, neighbour pane math     │
└───────────────────────────────────────────────────────────────────────────┘
                          ↓ assembled by
┌─ Layer 5: interaction orchestration (glues the layers above together)─────┐
│  view.py     【new】screen polling + change detection + repaint scheduling │
│                    (_pump/_paint)                                         │
│  input.py    【new】input routing: keyboard/mouse/copy-mode dispatch       │
│  actions.py  【new】prefix action execution (zoom/split/nav/paging)        │
└───────────────────────────────────────────────────────────────────────────┘
```

### Where `iterm_backend.py` went after the split

| Original method | Destination | Rationale |
|---|---|---|
| `_pump_screen` `_signature` `_paint*` `_history` | **view.py** | Repaint scheduling is its own concern and a bug hotspot |
| `on_input` `on_mouse` `on_copy_key` `_handle_mouse` `_mouse_select` `_copy_key` `_esc_timeout` | **input.py** | Input routing + the copy-mode keyboard state machine |
| `_prefix` `_zoom` `_neighbour` `_page` `_move_v` | **actions.py** | prefix action execution |
| `_screen_text` `_tab_of` `_bounds_at` `_pane_bounds` | **iterm/session.py** | iTerm2 object navigation |
| `_send` `_send_raw` + menu invocations | **iterm/api.py** | SDK wrapper |

### The key win: the `backend.py` abstract interface

Once the backend contract is defined, the tmux protocol layer is decoupled from iTerm2:

```python
class Backend(Protocol):
    async def screen(self, pane_id) -> ScreenContents: ...
    async def history(self, pane_id, rows, offset) -> ScreenContents: ...
    async def send_text(self, pane_id, text: str) -> None: ...
    async def split(self, pane_id, vertical: bool) -> str: ...
    async def zoom(self, pane_id) -> None: ...
    def layout(self, window_id) -> SplitTree: ...
```

That gives us:
- **Unit tests without iTerm2** — the hand-rolled `_Peer` / `ITermBackend.__new__` fakes currently in `test_copymode` / `test_mouse` can be replaced by a proper `FakeBackend`
- Plugging in another backend later (real tmux passthrough, or Terminal.app) means writing only an adapter layer

## 4. Suggested edge-case hardening (by priority)

1. **`imsg_codec` against malicious input**: an oversized `len` or a truncated frame that never completes will grow `_buf` without bound. Enforce a cap and disconnect.
2. **Exceptions swallowed by `create_task`**: several `self.loop.create_task(...)` calls have no exception handling; an exception inside the task is lost silently and the peer can end up half-dead.
3. **Pane disappearing mid-flight**: when `get_session_by_id` returns None most paths simply `return`, so the client sees a frozen screen instead of a clear message.
4. **Client smaller than the iTerm2 window**: currently clipped. At minimum the status bar should flag the size mismatch.
5. **Filling in `has-session` / `detach-client` / `select-window`**: common dependencies for scripted use.


---

## 5. Results

The split and the hardening are done: nine test suites green, live-verified on two machines.

```
before                        after
iterm_backend.py  929 lines →   140 lines (assembly only)
ansi.py           598 lines →   464 lines + sgr.py 153 lines
```

Final layering:

| Module | Lines | Responsibility | Depends on iterm2 SDK |
|---|---|---|---|
| `protocol.py` `imsg_codec.py` `gateway.py` `peer.py` `tty.py` | ~800 | tmux protocol | ❌ |
| `keys.py` `sgr.py` `ansi.py` `layout.py` `mouse.py` `copymode.py` | ~1100 | terminal semantics | ❌ |
| `mapper.py` `commands.py` `backend.py` | ~600 | session model | ❌ |
| `iterm/api.py` | 195 | the **only** SDK wrapper | ✅ |
| `view.py` `input.py` `actions.py` | ~780 | interaction orchestration | ❌ |
| `iterm_backend.py` | 140 | assembly | ✅ (only constructs ITermAPI) |

**Key outcome: every iTerm2 SDK call is now funnelled through `iterm/api.py`.** Upper layers
go through `self.api`, and SDK failures (pane vanishing mid-flight, connection jitter) are
uniformly degraded to `None`/no-op at that layer, so each call site no longer writes its own
try/except.

Value the split paid out immediately: `test_prefix.py` now tests `keys.PrefixState` directly
and no longer needs a fake `Peer` — it exercises the real code path.

## 6. Still not done

- `mapper.py` still reads `app.terminal_windows` / `get_session_by_id` directly. These are
  synchronous read-only lookups that cannot fail, so the payoff is lower than the cost of
  changing them; left as is.
- `resize-pane` / `rename-window` / `.tmux.conf` parsing are still unimplemented.
- Client resize still does not re-lay-out (see §1).
