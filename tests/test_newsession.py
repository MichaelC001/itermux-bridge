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

    async def new_terminal_window(self):
        self.created += 1
        return self.pane

    async def set_name(self, pane, name):
        self.named = (pane.session_id, name)

    async def refresh(self):
        self.refreshed += 1

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

    def inventory(self, app):
        return [{
            "id": 4, "windows": [{
                "id": 9, "panes": [{"id": 12, "iterm_session_id": "new-pane"}],
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


class FakeBackend:
    def __init__(self, api):
        self.api, self.app, self.mapper = api, object(), FakeMapper()
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
    check("-P prints the new pane id", peer.text.strip() == "%12",
          f"({peer.text.strip()!r})")
    peer, api = await run(["split-window", "-t", "%1"])
    check("no -P prints nothing", peer.text == "", f"({peer.text!r})")

    print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES ABOVE") + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
