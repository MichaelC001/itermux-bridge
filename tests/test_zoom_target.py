"""Ctrl-B z must never toggle a tab other than the one it was aimed at.

The menu item acts on the key window's current pane. If activation hasn't
landed yet, the toggle hits whatever was focused before -- seen live, a zoom
meant for one window maximized a pane in another. Drives ITermAPI.zoom with a
fake iTerm2 whose focus lags or never arrives.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itermux_bridge.iterm import api as api_mod

ok = True


def check(label, cond, extra=""):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {label} {extra}")
    ok = ok and bool(cond)


class Sess:
    def __init__(self, sid):
        self.session_id = sid
        self.focus_after = 0            # activations before focus "lands"

    async def async_activate(self, **kw):
        App.pending = self


class Tab:
    def __init__(self, tab_id, sessions):
        self.tab_id, self.sessions = tab_id, sessions
        self.current_session = sessions[0]


class Win:
    def __init__(self, wid, tabs):
        self.window_id, self.tabs = wid, tabs
        self.current_tab = tabs[0]


class App:
    pending = None

    def __init__(self, windows, lag_polls):
        self.terminal_windows = windows
        self.current_terminal_window = windows[0]
        self.lag = lag_polls

    async def async_refresh_focus(self):
        # Focus moves to the activated pane only after `lag` refreshes.
        if App.pending is None:
            return
        if self.lag > 0:
            self.lag -= 1
            return
        for w in self.terminal_windows:
            for t in w.tabs:
                if App.pending in t.sessions:
                    self.current_terminal_window = w
                    w.current_tab = t
                    t.current_session = App.pending


def rig(lag_polls):
    mine, theirs = Sess("mine"), Sess("theirs")
    app = App([Win("w-theirs", [Tab("t-theirs", [theirs])]),
               Win("w-mine", [Tab("t-mine", [mine])])], lag_polls)
    api = api_mod.ITermAPI(connection=None, app=app)
    fired = []

    async def select(conn, item):
        w = app.current_terminal_window
        fired.append(w.current_tab.tab_id)
    api_mod.iterm2.MainMenu.async_select_menu_item = select
    return api, mine, fired


api, mine, fired = rig(lag_polls=3)
asyncio.run(api.zoom(mine))
check("focus lands late: the toggle waits for it, then hits our tab",
      fired == ["t-mine"], f"({fired})")

api, mine, fired = rig(lag_polls=10_000)
asyncio.run(api.zoom(mine))
check("focus never lands: no toggle at all (never someone else's tab)",
      fired == [], f"({fired})")

print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
sys.exit(0 if ok else 1)
