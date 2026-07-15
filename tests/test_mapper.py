"""session/window/pane mapping + tmux target resolution.

    tmux session $N  <-  iTerm2 window
    tmux window  @N  <-  iTerm2 tab
    tmux pane    %N  <-  iTerm2 session

Fakes mirror the iTerm2 API surface actually used: app.terminal_windows,
window.tabs/window_id/current_tab, tab.sessions/tab_id/current_session,
session.session_id/name/grid_size.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itermux_bridge.commands import resolve_target
from itermux_bridge.mapper import SessionMapper


class Grid:
    def __init__(self, w, h): self.width, self.height = w, h


class Sess:
    def __init__(self, sid, name, w=80, h=24):
        self.session_id, self.name = sid, name
        self.grid_size = Grid(w, h)


class Tab:
    def __init__(self, tid, sessions, current=0):
        self.tab_id, self.sessions = tid, sessions
        self.current_session = sessions[current]


class Win:
    def __init__(self, wid, tabs, current=0):
        self.window_id, self.tabs = wid, tabs
        self.current_tab = tabs[current]


class App:
    def __init__(self, windows, current=0):
        self.terminal_windows = windows
        self.current_terminal_window = windows[current]

    def get_session_by_id(self, sid):
        for w in self.terminal_windows:
            for t in w.tabs:
                for s in t.sessions:
                    if s.session_id == sid:
                        return s
        return None


# One iTerm2 window; tab0 has 2 split panes, tab1 has 1. tab1 is focused.
app = App([Win("w1", [
    Tab("t1", [Sess("s1", "vim", 80, 12), Sess("s2", "zsh", 80, 11)], current=1),
    Tab("t2", [Sess("s3", "htop")], current=0),
], current=1)])

mapper = SessionMapper(Path(tempfile.mkdtemp()) / "state.json")

ok = True


def check(label, cond, extra=""):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {label} {extra}")
    ok = ok and cond


print("\n=== iTerm2 <-> tmux hierarchy ===")

tree = mapper.inventory(app)
check("one iTerm2 window -> one tmux session", len(tree) == 1)
check("two iTerm2 tabs -> two tmux windows", len(tree[0]["windows"]) == 2)

w0, w1 = tree[0]["windows"]
check("split tab -> window with 2 panes", len(w0["panes"]) == 2)
check("single tab -> window with 1 pane", len(w1["panes"]) == 1)
check("window indices are per-session (0,1)",
      [w["index"] for w in tree[0]["windows"]] == [0, 1])
check("pane indices are per-window (0,1)",
      [p["index"] for p in w0["panes"]] == [0, 1])
check("pane ids are globally unique",
      len({p["id"] for _s, _w, p in mapper.flat_panes(app)}) == 3)

# Active tracking: only the focused tab, and within it only its current session.
check("only the focused tab is the active window",
      [w["active"] for w in tree[0]["windows"]] == [False, True])
check("in a split, only the tab's current session is the active pane",
      [p["active"] for p in w0["panes"]] == [False, True])
check("window is named after its active pane", w0["name"] == "zsh",
      f"({w0['name']!r})")
check("pane size comes from the iTerm2 grid",
      (w0["panes"][0]["width"], w0["panes"][0]["height"]) == (80, 12))

print("\n=== target resolution ===")

# %N pane ids as allocated above.
ids = {p["name"]: p["id"] for _s, _w, p in mapper.flat_panes(app)}
vim, zsh, htop = ids["vim"], ids["zsh"], ids["htop"]

check("%N resolves to its pane",
      resolve_target(mapper, app, f"%{vim}").name == "vim")
check("bare N resolves like %N",
      resolve_target(mapper, app, str(htop)).name == "htop")
check("window.pane index resolves", resolve_target(mapper, app, "0.0").name == "vim")
check("window.pane picks the right pane in a split",
      resolve_target(mapper, app, "0.1").name == "zsh")
check("second window's pane resolves",
      resolve_target(mapper, app, "1.0").name == "htop")
check("session:window.pane resolves",
      resolve_target(mapper, app, "$0:0.1").name == "zsh")
check("@N resolves to that window's ACTIVE pane",
      resolve_target(mapper, app, f"@{w0['id']}").name == "zsh")
check("unknown pane id -> None", resolve_target(mapper, app, "%999") is None)
check("unknown index -> None", resolve_target(mapper, app, "9.9") is None)
check("garbage -> None (never a silent fallback)",
      resolve_target(mapper, app, "main") is None)

print("\n=== id stability ===")
again = mapper.inventory(app)
check("ids are stable across calls",
      [p["id"] for _s, _w, p in mapper.flat_panes(app)] ==
      [p["id"] for w in again[0]["windows"] for p in w["panes"]])

reloaded = SessionMapper(mapper.state_path)
check("ids persist across a bridge restart",
      reloaded.pane_id("s1") == vim and reloaded.pane_id("s3") == htop)

print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
sys.exit(0 if ok else 1)
