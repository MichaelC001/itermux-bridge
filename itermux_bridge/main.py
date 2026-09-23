"""Entry point — runs inside iTerm2 as an AutoLaunch script.

iTerm2 hands us a live connection and keeps this coroutine alive, so the gateway
lives exactly as long as iTerm2 does (design §2.1).
"""

import asyncio
import logging
from pathlib import Path

from .config import Config
from .gateway import Gateway
from .logging_setup import setup_logging

log = logging.getLogger(__name__)


async def serve(connection, config: Config) -> None:
    import iterm2
    from .iterm_backend import ITermBackend
    from .mapper import SessionMapper

    app = await iterm2.async_get_app(connection)
    mapper = SessionMapper(config.state_path)
    backend = ITermBackend(connection, app, mapper)

    gw = Gateway(config.socket_path, backend)
    gw.start(asyncio.get_running_loop())

    log.info("itermux-bridge ready — connect with: tmux -S %s attach",
             config.socket_path)
    try:
        # Live exactly as long as the iTerm2 connection. When iTerm2 quits or
        # restarts, the websocket closes but nothing kills this process: waiting
        # on a bare Event() kept an orphan alive with a dead connection, still
        # holding the socket — so every client attached to a bridge that could
        # neither read screens nor send keys, and every new bridge iTerm2
        # launched died on "socket already in use".
        wait_closed = getattr(getattr(connection, "websocket", None),
                              "wait_closed", None)
        if wait_closed is None:
            # Older iterm2/websockets runtime: can't watch for the disconnect,
            # so keep the old live-forever behaviour rather than fail to start.
            log.warning("cannot watch iTerm2 connection; running without "
                        "exit-on-disconnect")
            await asyncio.Event().wait()
        await wait_closed()
        log.warning("iTerm2 connection closed; exiting")
    finally:
        gw.stop()


def main() -> None:
    import iterm2

    config = Config.load()
    setup_logging(config.log_path, config.log_level)
    log.info("starting itermux-bridge")

    async def _main(connection):
        try:
            await serve(connection, config)
        except Exception:
            # iterm2 prints this to the Script Console only; without logging
            # here a failed start shows up in bridge.log as "starting" and
            # then silence.
            log.exception("itermux-bridge failed")
            raise

    iterm2.run_forever(_main)


if __name__ == "__main__":
    main()
