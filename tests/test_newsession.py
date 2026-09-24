"""new-session: opens a real iTerm2 WINDOW, then attaches (or -d).

Drives commands.dispatch() against fakes — no iTerm2, no sockets. The point is
the branch that used to be wrong: `new-session` sat in ATTACH_CMDS, so it
silently attached to an EXISTING pane instead of creating anything.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itermux_bridge import commands

ok = True


def check(label, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label} {extra}")


class FakePane:
    def __init__(self, session_id):
        self.session_id = session_id


class FakeAPI:
    """Records what the command layer asked iTerm2 to do."""

    def __init__(self, pane=None):
        self.pane = pane
        self.created = 0
        self.named = None
        self.refreshed = 0
        self.split_vertical = None
        self.sent = []
        self.tabs = 0
        self.near = None

    async def new_terminal_window(self):
        self.created += 1
        return self.pane

    async def set_name(self, pane, name):
        self.named = (pane.session_id, name)

    async def refresh(self):
        self.refreshed += 1

    async def new_window(self, near_pane=None):
        self.tabs += 1
        self.near = near_pane
        return self.pane

    async def split(self, pane, vertical):
        self.split_vertical = vertical
        return FakePane("split-pane")

    async def send_text(self, pane, text):
        self.sent.append(text)


class FakeMapper:
    def pane_id(self, session_id):
        return 12

    def focused_session(self, app):
        return FakePane("focused")

    def find_session(self, app, pane):
        return FakePane(f"pane-{pane}")

    def flat_panes(self, app):
        for sess in self.inventory(app):
            for win in sess["windows"]:
                for pane in win["panes"]:
                    yield sess, win, pane

    def inventory(self, app):
        return [{
            "id": 4, "windows": [{
                "id": 9, "index": 1, "panes": [
                    {"id": 12, "index": 0, "active": True,
                     "iterm_session_id": "new-pane"},
                    {"id": 13, "index": 1, "active": False,
                     "iterm_session_id": "split-pane"},
                ],
            }],
        }]


class FakePeer:
    def __init__(self):
        self.out = bytearray()
        self.attached_to = "NOT-CALLED"
        self.status = None

    def write_out(self, data):
        self.out += data

    def detach(self, status=0):
        self.status = status

    def attach(self, session_id=None):
        self.attached_to = session_id

    @property
    def text(self):
        return bytes(self.out).decode()


class FakeApp:
    def get_session_by_id(self, sid):
        return FakePane(sid)


class FakeBackend:
    def __init__(self, api):
        self.api, self.app, self.mapper = api, FakeApp(), FakeMapper()
        self.spawned = []

    def _spawn(self, coro, what):
        self.spawned.append(coro)


async def run(argv, pane=FakePane("new-pane")):
    """dispatch(argv) -> (peer, api), with the spawned coroutine awaited."""
    api = FakeAPI(pane)
    backend, peer = FakeBackend(api), FakePeer()
    commands.dispatch(backend, peer, argv)
    for coro in backend.spawned:
        await coro
    return peer, api


async def main():
    # The regression itself. `new` must not be treated as an attach: before the
    # fix it never created anything and connected to an existing pane.
    check("`new` is not in ATTACH_CMDS",
          "new" not in commands.ATTACH_CMDS and
          "new-session" not in commands.ATTACH_CMDS)

    peer, api = await run(["new-session"])
    check("new-session created an iTerm2 window", api.created == 1,
          f"(created={api.created})")
    check("attached to the NEW pane, not an existing one",
          peer.attached_to == "new-pane", f"({peer.attached_to!r})")
    check("client was not detached", peer.status is None)
    check("mapper saw the window before we used it", api.refreshed == 1)

    # -d: create and leave it running on the Mac.
    peer, api = await run(["new-session", "-d"])
    check("-d still created the window", api.created == 1)
    check("-d did NOT attach", peer.attached_to == "NOT-CALLED",
          f"({peer.attached_to!r})")
    check("-d exited 0", peer.status == 0, f"(status={peer.status})")
    # Real tmux prints NOTHING without -P; scripts do `id=$(tmux new -d -P)`,
    # so unasked-for output would corrupt the value they capture.
    check("-d alone prints nothing, as tmux does", peer.text == "",
          f"({peer.text!r})")

    peer, _api = await run(["new-session", "-d", "-P"])
    check("-P prints '<name>:' (tmux's default format)",
          peer.text.strip() == "4:", f"({peer.text.strip()!r})")
    peer, _api = await run(["new-session", "-d", "-P", "-s", "build"])
    check("-P uses the -s name when given", peer.text.strip() == "build:",
          f"({peer.text.strip()!r})")

    # -s <name> names the window; tmux also accepts the value glued on.
    _peer, api = await run(["new-session", "-d", "-s", "build"])
    check("-s named the new window", api.named == ("new-pane", "build"),
          f"({api.named})")
    _peer, api = await run(["new-session", "-d", "-sbuild"])
    check("-sbuild (glued form) also works", api.named == ("new-pane", "build"),
          f"({api.named})")

    # Creation can fail (no iTerm2 window to create from, API off). Say so and
    # exit non-zero rather than attaching to something arbitrary.
    peer, api = await run(["new-session"], pane=None)
    check("failure reported to the user", "could not create" in peer.text,
          f"({peer.text.strip()!r})")
    check("failure exits non-zero", peer.status == 1, f"(status={peer.status})")
    check("failure did not attach", peer.attached_to == "NOT-CALLED")

    # The alias tmux users actually type.
    peer, api = await run(["new"])
    check("`new` alias creates too", api.created == 1 and
          peer.attached_to == "new-pane")

    # Flags tmux accepts but iTerm2 can't honour must be REFUSED, not ignored:
    # `new -c /path` that silently lands elsewhere is worse than an error.
    peer, api = await run(["new", "-c", "/tmp"])
    check("-c is refused, not ignored", peer.status == 1 and api.created == 0,
          f"(status={peer.status}, created={api.created})")
    check("refusal names the flag", "-c" in peer.text,
          f"({peer.text.strip()!r})")
    # ...but the supported flags must not trip that check.
    _peer, api = await run(["new", "-d", "-P", "-sx"])
    check("supported flags aren't mistaken for unsupported ones",
          api.created == 1, f"(created={api.created})")

    # --- split-window ---------------------------------------------------
    # tmux -h = left/right; iTerm2's async_split_pane(vertical=True) is ALSO
    # left/right, so -h must map to vertical=True. Getting this backwards
    # transposes every scripted layout.
    peer, api = await run(["split-window", "-h", "-t", "%1"])
    check("-h splits left/right (vertical=True)", api.split_vertical is True,
          f"({api.split_vertical})")
    peer, api = await run(["split-window", "-t", "%1"])
    check("no flag = tmux's -v, top/bottom", api.split_vertical is False,
          f"({api.split_vertical})")

    peer, api = await run(["split-window", "-t", "%1", "-c", "/tmp/x"])
    # Exactly one line: the cd. The flags must NOT be typed into the pane as
    # if they were a shell command — _target_pane only strips -t.
    check("-c cd's the new pane", api.sent == ["cd /tmp/x\n"], f"({api.sent})")
    peer, api = await run(["split-window", "-t", "%1", "-c", "/a b"])
    check("-c quotes paths with spaces", api.sent == ["cd '/a b'\n"],
          f"({api.sent})")
    peer, api = await run(["split-window", "-h", "-P", "-t", "%1"])
    check("bare flags are never typed into the pane", api.sent == [],
          f"({api.sent})")
    # tmux runs a trailing shell-command in the new pane.
    peer, api = await run(["split-window", "-t", "%1", "-c", "/tmp", "htop"])
    check("trailing command still runs after the cd",
          api.sent == ["cd /tmp\n", "htop\n"], f"({api.sent})")

    peer, api = await run(["split-window", "-t", "%1", "-P"])
    # Verified against tmux 3.7b: -P prints `session:window.pane`, NOT %N.
    # Scripts feed it straight back as a -t target, so the raw id is wrong.
    check("-P prints session:window.pane", peer.text.strip() == "4:1.1",
          f"({peer.text.strip()!r})")
    peer, api = await run(["split-window", "-t", "%1"])
    check("no -P prints nothing", peer.text == "", f"({peer.text!r})")

    # --- new-window -------------------------------------------------------
    peer, api = await run(["new-window", "-t", "$4"])
    check("new-window created a tab", api.tabs == 1, f"({api.tabs})")
    check("...next to the target pane", api.near is not None)
    check("no -P prints nothing", peer.text == "", f"({peer.text!r})")

    peer, api = await run(["new-window", "-P"])
    check("new-window -P prints session:window.pane",
          peer.text.strip() == "4:1.0", f"({peer.text.strip()!r})")

    peer, api = await run(["new-window", "-n", "build", "-c", "/tmp/x"])
    check("-n names the tab", api.named == ("new-pane", "build"),
          f"({api.named})")
    # -n takes a VALUE; if it isn't stripped the name gets typed into the pane.
    check("-n's value is not typed into the pane",
          api.sent == ["cd /tmp/x\n"], f"({api.sent})")

    peer, api = await run(["new-window"], pane=None)
    check("tab-creation failure is reported", "could not create" in peer.text
          and peer.status == 1, f"({peer.text.strip()!r})")

    # A bare `$N` (session, no :window.pane) has to resolve — it's the natural
    # way to say `new-window -t $0`. It previously fell through to None.
    m, app = FakeMapper(), FakeApp()
    check("bare $N resolves to the session's active pane",
          getattr(commands.resolve_target(m, app, "$4"), "session_id", None)
          == "new-pane")
    check("unknown $N still resolves to nothing",
          commands.resolve_target(m, app, "$99") is None)

    # --- list-panes -t ----------------------------------------------------
    # The focused window is @1; -t @2 must list @2, not whatever is focused.
    # It used to ignore -t, and `list-panes -t @2 | ... | send-keys` typed into
    # a session in the FOCUSED window instead.
    def pane(pid, idx, sid, active):
        return {"id": pid, "index": idx, "iterm_session_id": sid,
                "name": sid, "width": 80, "height": 24, "active": active}

    class TwoWindows(FakeMapper):
        def find_session(self, app, pane_id):
            # Like the real one: look the %id up in the inventory.
            return next((FakePane(p["iterm_session_id"])
                         for _s, _w, p in self.flat_panes(app)
                         if p["id"] == pane_id), None)

        def inventory(self, app):
            return [{"id": 0, "active": True, "windows": [
                {"id": 1, "index": 0, "active": True,
                 "panes": [pane(10, 0, "focused-a", True)]},
                {"id": 2, "index": 1, "active": False,
                 "panes": [pane(20, 0, "other-a", False),
                           pane(21, 1, "other-b", True)]},
            ]}]

    async def lsp(argv):
        backend, peer = FakeBackend(FakeAPI()), FakePeer()
        backend.mapper = TwoWindows()
        commands.dispatch(backend, peer, argv)
        return peer

    peer = await lsp(["list-panes", "-t", "@2"])
    check("list-panes -t @2 lists window @2",
          "%20" in peer.text and "%21" in peer.text, f"({peer.text.strip()!r})")
    check("...not the focused window", "%10" not in peer.text)
    peer = await lsp(["list-panes", "-t", "%20"])
    check("list-panes -t %N lists that pane's window",
          "%21" in peer.text and "%10" not in peer.text)
    peer = await lsp(["list-panes"])
    check("no -t still lists the focused window",
          "%10" in peer.text and "%20" not in peer.text)
    peer = await lsp(["list-panes", "-t", "@99"])
    check("unknown -t is an error, not a silent fallback",
          peer.status == 1 and "%10" not in peer.text,
          f"(status={peer.status}, {peer.text.strip()!r})")

    print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
