"""Config in ~/.itermux/config.toml (design §9)."""

import logging
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib  # 3.11+
except ModuleNotFoundError:
    tomllib = None

log = logging.getLogger(__name__)

ITMUX_DIR = Path.home() / ".itermux"
CONFIG_PATH = ITMUX_DIR / "config.toml"

DEFAULT_CONFIG = """\
[socket]
# Connect with: tmux -S <path> attach
path = "~/.itermux/default.sock"

[logging]
level = "info"
path = "~/.itermux/logs/bridge.log"
"""


@dataclass
class Config:
    socket_path: Path = ITMUX_DIR / "default.sock"
    state_path: Path = ITMUX_DIR / "state.json"
    log_path: Path = ITMUX_DIR / "logs" / "bridge.log"
    log_level: str = "info"

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        cfg = cls()
        path = Path(path).expanduser()
        if not path.exists() or tomllib is None:
            if tomllib is None:
                log.debug("no tomllib (py<3.11); using defaults")
            return cfg
        try:
            data = tomllib.loads(path.read_text())
        except (OSError, ValueError) as e:
            log.warning("bad config %s (%s); using defaults", path, e)
            return cfg

        sock = data.get("socket", {}).get("path")
        if sock:
            cfg.socket_path = Path(sock).expanduser()
        logging_cfg = data.get("logging", {})
        if logging_cfg.get("path"):
            cfg.log_path = Path(logging_cfg["path"]).expanduser()
        cfg.log_level = logging_cfg.get("level", cfg.log_level)
        return cfg

    @staticmethod
    def write_default(path: Path = CONFIG_PATH) -> Path:
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(DEFAULT_CONFIG)
        return path
