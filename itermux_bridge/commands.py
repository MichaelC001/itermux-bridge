"""tmux command subset (design §5 stage 2).

A one-shot client (`tmux -S sock list-panes`) hands us the same stdout fd as an
attaching client, sends MSG_COMMAND, and expects output on that fd followed by
MSG_EXIT. So "run a command" is: write text to peer.stdout_fd, then detach.
"""

import logging
import shlex

log = logging.getLogger(__name__)

# tmux key names -> the bytes a terminal actually sends.
KEYS = {
    "Enter": b"\r", "C-m": b"\r", "Return": b"\r",
    "Tab": b"\t", "Escape": b"\033", "Space": b" ",
    "BSpace": b"\177", "Up": b"\033[A", "Down": b"\033[B",
    "Right": b"\033[C", "Left": b"\033[D",
    "Home": b"\033[H", "End": b"\033[F",
    "PageUp": b"\033[5~", "PageDown": b"\033[6~",
}


def _key_bytes(token: str) -> bytes:
    if token in KEYS:
        return KEYS[token]
    # C-a .. C-z  ->  control codes
    if len(token) == 3 and token.startswith("C-"):
        ch = token[2].lower()
        if "a" <= ch <= "z":
            return bytes([ord(ch) - ord("a") + 1])
    if len(token) == 3 and token.startswith("M-"):
        return b"\033" + token[2].encode()
    return token.encode()


def _reply(peer, text: str, status: int = 0) -> None:
    """Send command output and end this client."""
    if text:
        peer.write_out(text.replace("\n", "\r\n").encode())
    peer.detach(status=status)


def _target_spec(backend, pane) -> str:
    """tmux's default -P format: `session:window.pane` (verified against 3.7b).

    NOT the raw %N id — scripts feed this straight back in as a -t target.
    """
    for sess, win, p in backend.mapper.flat_panes(backend.app):
        if p["iterm_session_id"] == pane.session_id:
            return f"{sess['id']}:{win['index']}.{p['index']}"
    return ""


def _strip_flags(argv, c_value):
    """Drop split-window's own flags, leaving the trailing shell-command.

    `-t` is already gone (_target_pane removed it); without this the rest of
    the flags get TYPED INTO the new pane as if they were a command.
    """
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in ("-h", "-v", "-P", "-d", "-b", "-f", "-I"):
            continue
        if a in ("-c", "-n"):
            skip = True                   # its value follows
            continue
        if c_value and a == f"-c{c_value}":
            continue                      # glued form
        out.append(a)
    return out


def _flag_value(argv, flag: str):
    """The value following `flag` in argv, or None if absent/trailing."""
    for i, a in enumerate(argv):
        if a == flag:
            return argv[i + 1] if i + 1 < len(argv) else None
        if a.startswith(flag) and len(a) > len(flag):
            return a[len(flag):]          # tmux also accepts -sname
    return None


#: `-t` was not given at all (distinct from "given but unresolvable").
NO_TARGET = object()


def _target_pane(argv):
    """Pull `-t <target>` out of argv.

    Returns (target, remaining_args) where target is:
      NO_TARGET  -- no -t flag; caller may fall back to the focused session
      int        -- a resolved pane number
      None       -- a -t was given but we can't resolve it -> must be an error,
                    never a silent fallback, or `send-keys -t %main` would type
                    into whatever pane happens to be focused.
    """
    rest, target = [], None
    i = 0
    while i < len(argv):
        if argv[i] == "-t" and i + 1 < len(argv):
            target = argv[i + 1]
            i += 2
            continue
        rest.append(argv[i])
        i += 1

    return (NO_TARGET if target is None else target), rest


def resolve_target(mapper, app, target: str):
    """Resolve a tmux target to an iTerm2 session, or None if it names nothing.

    Understands the forms tmux itself accepts:
        %3          pane id
        @2          window id -> that window's active pane
        1.0         window index . pane index
        $0:1.0      session : window . pane
        0           bare number -> pane id (what our list-panes prints)
    """
    t = target.strip()

    # A bare `$N` names a SESSION with no window/pane part — `new-window -t $0`
    # is the natural way to say "a tab in that iTerm2 window". Resolve it to
    # the session's active pane, which is what tmux does.
    if t.startswith("$") and ":" not in t:
        sid = _int(t[1:])
        panes = [(s, w, p) for s, w, p in mapper.flat_panes(app)
                 if s["id"] == sid]
        if not panes:
            return None
        _s, _w, chosen = next((x for x in panes if x[2]["active"]), panes[0])
        return app.get_session_by_id(chosen["iterm_session_id"])

    # $session:window.pane -- drop the session part; we only have one level of
    # window grouping and the ids are globally unique anyway.
    if ":" in t:
        t = t.split(":", 1)[1]

    if t.startswith("%"):
        return mapper.find_session(app, _int(t[1:]))

    if t.startswith("@"):
        wid = _int(t[1:])
        panes = [p for _s, w, p in mapper.flat_panes(app) if w["id"] == wid]
        if not panes:
            return None
        chosen = next((p for p in panes if p["active"]), panes[0])
        return app.get_session_by_id(chosen["iterm_session_id"])

    if "." in t:
        win_s, pane_s = t.split(".", 1)
        w_idx, p_idx = _int(win_s), _int(pane_s)
        if w_idx is None or p_idx is None:
            return None
        for _s, w, p in mapper.flat_panes(app):
            if w["index"] == w_idx and p["index"] == p_idx:
                return app.get_session_by_id(p["iterm_session_id"])
        return None

    n = _int(t)
    return None if n is None else mapper.find_session(app, n)


def _names_a_window(target: str) -> bool:
    """True if this target names a window (whole tab) rather than one pane.

        @2      -> window
        $0:2    -> window
        2       -> pane (%2), matching what list-panes prints
        %2      -> pane
        0.1     -> pane
    """
    t = target.strip()
    if t.startswith("@"):
        return True
    if ":" in t:
        return "." not in t.split(":", 1)[1]
    return False


def _window_target(backend, mapper, app, cmd: str, target):
    """Resolve select-window / next-window / previous-window to a pane.

    Windows are iTerm2 tabs; "switching window" means activating a pane in the
    neighbouring tab. Returns the iTerm2 session to activate, or None.
    """
    flat = list(mapper.flat_panes(app))
    if not flat:
        return None

    # Windows in order, with their active pane.
    windows = []
    for _s, w, p in flat:
        if not windows or windows[-1][0] != w["id"]:
            windows.append((w["id"], p))
        elif p["active"]:
            windows[-1] = (w["id"], p)
    if not windows:
        return None

    if cmd in ("select-window", "selectw") and target is not NO_TARGET:
        session = resolve_target(mapper, app, str(target))
        return session

    # next / previous relative to the currently active window.
    cur = next((i for i, (_wid, p) in enumerate(windows)
                if p["active"]), 0)
    step = 1 if cmd in ("next-window", "next") else -1
    _wid, pane = windows[(cur + step) % len(windows)]
    return app.get_session_by_id(pane["iterm_session_id"])


async def _activate(backend, peer, session):
    """Make `session` the pane this client is showing."""
    await backend.api.activate(session)
    peer.iterm_session_id = session.session_id
    peer.copy.leave()
    peer.scroll_offset = 0
    await backend._paint(peer, session)


async def _new_session(backend, peer, detached: bool, name, printed: bool):
    """new-session: open an iTerm2 window, then attach unless -d."""
    pane = await backend.api.new_terminal_window()
    if pane is None:
        _reply(peer, "itermux-bridge: could not create an iTerm2 window\n",
               status=1)
        return

    if name:
        await backend.api.set_name(pane, name)

    # Let the mapper see the new window so it gets its $N/@N/%N before we
    # report or render it.
    await backend.api.refresh()

    if detached:
        # tmux is SILENT here unless -P is given; scripts parse that output, so
        # printing unasked would break `id=$(tmux new -d -P)`. -P's default
        # format is '#{session_name}:'.
        _reply(peer, f"{name or _session_name(backend, pane)}:\n"
               if printed else "")
        return

    peer.attach(session_id=pane.session_id)


async def _new_window(backend, peer, near, name, start_dir, printed: bool,
                      argv) -> None:
    """new-window: a new iTerm2 tab in the window holding `near`."""
    pane = await backend.api.new_window(near)
    if pane is None:
        _reply(peer, "itermux-bridge: could not create a tab\n", status=1)
        return

    if name:
        await backend.api.set_name(pane, name)
    # iTerm2 can't set a start directory or command at creation, so type them.
    if start_dir:
        await backend.api.send_text(pane, f"cd {shlex.quote(start_dir)}\n")
    if argv:
        await backend.api.send_text(pane, " ".join(argv) + "\n")

    await backend.api.refresh()
    _reply(peer, f"{_target_spec(backend, pane)}\n" if printed else "")


async def _split(backend, peer, session, horizontal: bool, start_dir,
                 printed: bool, argv) -> None:
    """split-window: divide a pane, optionally cd'ing and running a command."""
    # iTerm2's `vertical` means "a vertical DIVIDER" (side by side), which is
    # tmux's -h. Same convention as the Ctrl-B % / " bindings.
    pane = await backend.api.split(session, vertical=horizontal)
    if pane is None:
        _reply(peer, "itermux-bridge: could not split the pane\n", status=1)
        return

    # -c and a trailing shell-command are what make scripted layouts useful;
    # iTerm2 gives no way to set either at creation, so type them into the new
    # pane instead. Quote the path so spaces can't split it into two words.
    if start_dir:
        await backend.api.send_text(pane, f"cd {shlex.quote(start_dir)}\n")
    if argv:
        await backend.api.send_text(pane, " ".join(argv) + "\n")

    await backend.api.refresh()
    _reply(peer, f"{_target_spec(backend, pane)}\n" if printed else "")


def _session_name(backend, pane) -> str:
    """The `$N` the mapper gave the window holding `pane` (tmux's default name)."""
    for s in backend.mapper.inventory(backend.app):
        if any(p["iterm_session_id"] == pane.session_id
               for w in s["windows"] for p in w["panes"]):
            return str(s["id"])
    return ""


def _sessions_for(tree, target):
    """The session(s) a session-scoped command should act on.

    NO_TARGET -> the active session (what the user is looking at), like tmux.
    "$1"/"1"  -> that session.
    Returns None if the target names no session.
    """
    if target is NO_TARGET:
        active = [s for s in tree if s["active"]]
        return active or tree[:1]

    t = str(target).lstrip("$")
    # `-t $0:2` targets a window; the session part is what matters here.
    t = t.split(":", 1)[0]
    n = _int(t)
    if n is None:
        return None
    match = [s for s in tree if s["id"] == n]
    return match or None


def _int(s: str):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


#: Commands that put the client on a terminal instead of printing and exiting.
#: NB: `new-session` is deliberately NOT here. A tmux session is an iTerm2
#: window, which the bridge projects rather than creates — treating it as an
#: attach silently connected the client to an existing pane instead.
ATTACH_CMDS = frozenset({
    "attach", "attach-session", "a", "at",
})


def dispatch(backend, peer, argv) -> None:
    app, mapper = backend.app, backend.mapper

    # A bare `tmux -S sock` (no args) means "attach", same as real tmux.
    if not argv:
        peer.attach()
        return

    cmd, args = argv[0], argv[1:]

    # This is where attach-vs-command is actually decided — the identify
    # sequence is identical for both, so the command is the only signal.
    if cmd in ATTACH_CMDS:
        # `-t` selects WHICH iTerm2 tab/pane to render. Without it we fall back
        # to the focused session (minus the client's own terminal — see
        # session_for_attach).
        target, _rest = _target_pane(args)
        if target is NO_TARGET:
            peer.attach()
            return

        session = resolve_target(mapper, app, target)
        if session is None:
            _reply(peer, f"can't find session: {target}\n", status=1)
            return

        # `-t @N` (or a bare `$0:2`) names a WINDOW, so show the whole tab —
        # every pane at once, with dividers, like tmux. `-t %N` names a single
        # pane and renders just that one full-screen.
        peer.window_mode = _names_a_window(str(target))
        peer.attach(session_id=session.session_id)
        return

    if cmd in ("list-panes", "lsp"):
        # tmux defaults to the CURRENT window; -a lists every pane, prefixed
        # with session:window.pane. Previously we always dumped everything.
        if "-a" in args:
            # A pane is only really "the active pane" if its window is the
            # active one too. Otherwise every tab's sole session claims it,
            # since each is trivially its own tab's current session.
            out = "".join(
                f"{s['id']}:{w['index']}.{p['index']}: "
                f"[{p['width']}x{p['height']}] [{p['name']}] %{p['id']}"
                f"{' (active)' if p['active'] and w['active'] else ''}\n"
                for s, w, p in mapper.flat_panes(app)
            )
        else:
            # -t names a window (or a pane in it); without it, the current one.
            # -t used to be ignored here, so `list-panes -t @7` quietly listed
            # whatever tab was focused on the Mac — and a script feeding that
            # into send-keys typed into the wrong session.
            target, _rest = _target_pane(args)
            want = None
            if target is not NO_TARGET:
                pane = resolve_target(mapper, app, str(target))
                want = next((w["id"] for _s, w, p in mapper.flat_panes(app)
                             if pane is not None
                             and p["iterm_session_id"] == pane.session_id),
                            None)
                if want is None:
                    _reply(peer, f"can't find window: {target}\n", status=1)
                    return
            out = ""
            for s, w, p in mapper.flat_panes(app):
                if want is None and not w["active"]:
                    continue
                if want is not None and w["id"] != want:
                    continue
                out += (f"{p['index']}: [{p['width']}x{p['height']}] "
                        f"[{p['name']}] %{p['id']}"
                        f"{' (active)' if p['active'] else ''}\n")
        _reply(peer, out)

    elif cmd in ("list-windows", "lsw"):
        # tmux lists ONE session's windows (the current one unless -t says
        # otherwise); only -a spans every session. Listing them all
        # unconditionally prints two windows numbered `0` once a second iTerm2
        # window exists, because window indices restart per session.
        tree = mapper.inventory(app)
        target, _rest = _target_pane(args)

        if "-a" in args:
            sessions = tree
        else:
            sessions = _sessions_for(tree, target)
            if sessions is None:
                _reply(peer, f"can't find session: {target}\n", status=1)
                return

        out = ""
        for sess in sessions:
            for w in sess["windows"]:
                n = len(w["panes"])
                width = w["panes"][0]["width"] if w["panes"] else 0
                height = w["panes"][0]["height"] if w["panes"] else 0
                prefix = f"{sess['id']}:" if "-a" in args else ""
                out += (f"{prefix}{w['index']}: "
                        f"{w['name']}{'*' if w['active'] else ''} "
                        f"({n} panes) [{width}x{height}] @{w['id']}"
                        f"{' (active)' if w['active'] else ''}\n")
        _reply(peer, out)

    elif cmd in ("list-sessions", "ls"):
        # tmux counts WINDOWS here, not panes.
        out = ""
        for sess in mapper.inventory(app):
            n = len(sess["windows"])
            out += (f"${sess['id']}: {n} window{'s' if n != 1 else ''}"
                    f"{' (attached)' if sess['active'] else ''}\n")
        _reply(peer, out)

    elif cmd == "send-keys":
        target, keys = _target_pane(args)

        # Without -t, send to the focused session. Unlike attach, there's no
        # feedback-loop risk here (we're injecting keys, not rendering), so we
        # don't exclude the caller's own tty.
        if target is NO_TARGET:
            session = mapper.focused_session(app)
        else:
            # A -t that names nothing must be an error, never a silent fallback
            # to the focused pane — that would type the user's keystrokes into
            # a terminal they didn't ask for.
            session = resolve_target(mapper, app, target)

        if session is None:
            _reply(peer, f"can't find pane: {target}\n", status=1)
            return
        # Don't log key contents — they may be passwords (§11.5).
        log.info("send-keys -> %s (%d tokens)", target, len(keys))
        data = b"".join(_key_bytes(k) for k in keys)
        backend._spawn(backend._send(session, data.decode("utf-8", "replace")),
                       "send-keys command")
        _reply(peer, "")

    elif cmd in ("display-message", "display"):
        _reply(peer, " ".join(args) + "\n")

    elif cmd in ("has-session", "has"):
        # Scripts use this to test for a session before attaching. Exit status
        # is the answer: 0 = exists, 1 = doesn't.
        target, _rest = _target_pane(args)
        tree = mapper.inventory(app)
        if target is NO_TARGET:
            _reply(peer, "", status=0 if tree else 1)
        else:
            found = _sessions_for(tree, target) is not None
            _reply(peer, "" if found
                   else f"can't find session: {target}\n",
                   status=0 if found else 1)

    elif cmd in ("detach-client", "detach"):
        # Detach this client (or, with -a, every attached client).
        if "-a" in args:
            for other in list(getattr(backend, "_pumps", {}).keys()):
                if other is not peer and not other.closed:
                    other.detach(status=0)
        peer.detach(status=0)

    elif cmd in ("select-window", "selectw", "next-window", "next",
                 "previous-window", "prev"):
        target, _rest = _target_pane(args)
        session = _window_target(backend, mapper, app, cmd, target)
        if session is None:
            _reply(peer, "can't find window\n", status=1)
            return
        backend._spawn(_activate(backend, peer, session), "select-window")
        _reply(peer, "")

    elif cmd in ("new", "new-session"):
        # A tmux session is an iTerm2 window, so this really does open one.
        # Unlike attach it can't be decided synchronously — the window has to
        # exist before we know which pane to attach to — so the whole thing
        # runs on the backend's loop.
        # Flags tmux takes that we can't honour. Ignoring them silently would
        # be worse than refusing: `new -c /path` looks like it worked and puts
        # you in the wrong directory.
        unsupported = [f for f in ("-c", "-e", "-x", "-y", "-n", "-A", "-E",
                                   "-D", "-X", "-f")
                       if f in args or any(a.startswith(f) and len(a) > 2
                                           for a in args if a.startswith("-"))]
        if unsupported:
            _reply(peer, f"itermux-bridge: new-session {' '.join(unsupported)}"
                         " not supported (iTerm2 opens the window with your "
                         "default profile)\n", status=1)
            return

        backend._spawn(
            _new_session(backend, peer, detached="-d" in args,
                         name=_flag_value(args, "-s"), printed="-P" in args),
            "new-session")

    elif cmd in ("new-window", "neww"):
        # A tmux window is an iTerm2 TAB, created inside the window that holds
        # the target pane — so `-t $0` lands in that session, as tmux means it.
        target, rest = _target_pane(args)
        near = (resolve_target(mapper, app, target)
                if target is not NO_TARGET else mapper.focused_session(app))
        if near is None:
            _reply(peer, f"can't find session: {target}\n", status=1)
            return
        start_dir = _flag_value(args, "-c")
        backend._spawn(
            _new_window(backend, peer, near, name=_flag_value(args, "-n"),
                        start_dir=start_dir, printed="-P" in args,
                        argv=_strip_flags(rest, start_dir)),
            "new-window")

    elif cmd in ("split-window", "splitw"):
        # tmux: -h splits left/right, -v (the default) top/bottom. iTerm2's
        # async_split_pane takes vertical=True for a LEFT/RIGHT split, which is
        # the opposite sense — mapping it wrong silently transposes layouts.
        target, rest = _target_pane(args)
        session = (resolve_target(mapper, app, target)
                   if target is not NO_TARGET else mapper.focused_session(app))
        if session is None:
            _reply(peer, f"can't find pane: {target}\n", status=1)
            return
        start_dir = _flag_value(args, "-c")
        backend._spawn(
            _split(backend, peer, session, horizontal="-h" in args,
                   start_dir=start_dir, printed="-P" in args,
                   argv=_strip_flags(rest, start_dir)),
            "split-window")

    elif cmd in ("kill-server", "kill-session", "kill-window"):
        # We don't own the iTerm2 sessions' lifetimes — killing them would
        # destroy the user's real work. Detach instead, and say so.
        _reply(peer, "itermux-bridge: not killing iTerm2 sessions; "
                     "detaching instead\n")
        peer.detach(status=0)

    else:
        _reply(peer, f"unknown command: {shlex.join(argv)}\n", status=1)
