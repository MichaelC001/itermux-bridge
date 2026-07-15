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
        # Sleep forever; iTerm2 keeps the process alive.
        await asyncio.Event().wait()
    finally:
        gw.stop()


def main() -> None:
    import iterm2

    config = Config.load()
    setup_logging(config.log_path, config.log_level)
    log.info("starting itermux-bridge")

    async def _main(connection):
        await serve(connection, config)

    iterm2.run_forever(_main)


if __name__ == "__main__":
    main()
