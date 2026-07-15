"""tmux concepts <-> iTerm2 concepts.

    tmux session ($N) ~ iTerm2 window
    tmux window  (@N) ~ iTerm2 tab
    tmux pane    (%N) ~ iTerm2 session   <- the addressable terminal unit

IDs are assigned on first sight and persisted, so a given iTerm2 session keeps
the same %N across bridge restarts.
"""

import json
import logging
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger(__name__)


class SessionMapper:
    def __init__(self, state_path: Optional[Path] = None) -> None:
        self.state_path = Path(state_path).expanduser() if state_path else None
        self._pane: Dict[str, int] = {}    # iTerm2 session_id -> %N
        self._window: Dict[str, int] = {}  # iTerm2 tab_id     -> @N
        self._sess: Dict[str, int] = {}    # iTerm2 window_id  -> $N
        self._next = {"pane": 0, "window": 0, "sess": 0}
        self._load()

    # --- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text())
            self._pane = data.get("pane", {})
            self._window = data.get("window", {})
            self._sess = data.get("sess", {})
            self._next = data.get("next", self._next)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("could not load state (%s); starting fresh", e)

    def save(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({
                "pane": self._pane, "window": self._window,
                "sess": self._sess, "next": self._next,
            }, indent=2))
        except OSError as e:
            log.warning("could not save state: %s", e)

    # --- id allocation -----------------------------------------------------

    def _alloc(self, table: Dict[str, int], key: str, kind: str) -> int:
        if key not in table:
            table[key] = self._next[kind]
            self._next[kind] += 1
            self.save()
        return table[key]

    def pane_id(self, session_id: str) -> int:
        return self._alloc(self._pane, session_id, "pane")

    def window_id(self, tab_id: str) -> int:
        return self._alloc(self._window, tab_id, "window")

    def session_id(self, window_id: str) -> int:
        return self._alloc(self._sess, window_id, "sess")

    # --- lookups -----------------------------------------------------------

    def _all_sessions(self, app):
        return [s for w in app.terminal_windows for t in w.tabs
                for s in t.sessions]

    def focused_session(self, app):
        """The session the user is currently looking at, or None."""
        window = app.current_terminal_window
        if window is None:
            windows = app.terminal_windows
            if not windows:
                return None
            window = windows[0]
        tab = window.current_tab
        return tab.current_session if tab else None

    async def session_for_attach(self, app, exclude_tty: str = ""):
        """Which iTerm2 session a fresh `tmux attach` should land on.

        Default: whatever the user is currently looking at — EXCEPT the session
        the client itself is running in.

        Attaching a client to its own terminal is a feedback loop: we render the
        pane into the pane, which changes the pane, which triggers another
        render. The screen never settles and the client looks hung. Since the
        user necessarily types `tmux attach` in the focused session, the naive
        "use the focused session" rule hits this every time.

        The client tells us its tty in MSG_IDENTIFY_TTYNAME, and iTerm2 exposes
        each session's tty, so we can match them and skip that one.
        """
        async def tty_of(session):
            try:
                return await session.async_get_variable("tty") or ""
            except Exception:
                return ""

        first = self.focused_session(app)
        if first is not None and (not exclude_tty
                                  or await tty_of(first) != exclude_tty):
            return first

        # Focused session is the client's own terminal — pick any other one.
        for s in self._all_sessions(app):
            if await tty_of(s) != exclude_tty:
                return s
        return None

    def find_session(self, app, pane: int):
        """Resolve a tmux pane id (%N / N) back to an iTerm2 session."""
        for sid, n in self._pane.items():
            if n == pane:
                return app.get_session_by_id(sid)
        return None

    def inventory(self, app):
        """Full session/window/pane tree, shaped for the list-* commands.

        Mirrors tmux's own hierarchy, so the commands can reproduce its output
        exactly:

            session $N  (iTerm2 window)
              window @N  (iTerm2 tab)   -- has a per-session INDEX and a size
                pane %N  (iTerm2 session) -- has a per-window INDEX and a size

        Both an id (@N/%N, stable and global) and an index (0,1,2… within its
        parent) are needed: tmux prints the index in the left column and the id
        at the end of the line, and targets like `-t demo:1.0` are indices.
        """
        cur_window = app.current_terminal_window
        cur_tab = cur_window.current_tab if cur_window else None

        tree = []
        for w in app.terminal_windows:
            sess = {
                "id": self.session_id(w.window_id),
                "iterm_window_id": w.window_id,
                "active": bool(cur_window and w.window_id == cur_window.window_id),
                "windows": [],
            }
            for w_index, t in enumerate(w.tabs):
                cur_sess = t.current_session
                win = {
                    "id": self.window_id(t.tab_id),
                    "index": w_index,
                    "iterm_tab_id": t.tab_id,
                    "active": bool(cur_tab and t.tab_id == cur_tab.tab_id),
                    "panes": [],
                }
                for p_index, s in enumerate(t.sessions):
                    grid = s.grid_size
                    win["panes"].append({
                        "id": self.pane_id(s.session_id),
                        "index": p_index,
                        "iterm_session_id": s.session_id,
                        "name": s.name or "",
                        "width": int(grid.width),
                        "height": int(grid.height),
                        # Only the tab's CURRENT session is the active pane —
                        # not every pane, which is what a naive check reports
                        # when each tab holds exactly one session.
                        "active": bool(cur_sess and
                                       s.session_id == cur_sess.session_id),
                    })
                # tmux names a window after its active pane.
                active = next((p for p in win["panes"] if p["active"]),
                              win["panes"][0] if win["panes"] else None)
                win["name"] = active["name"] if active else ""
                sess["windows"].append(win)
            tree.append(sess)
        return tree

    def flat_panes(self, app):
        """Every pane, with its session/window context — for `list-panes -a`."""
        for sess in self.inventory(app):
            for win in sess["windows"]:
                for pane in win["panes"]:
                    yield sess, win, pane
