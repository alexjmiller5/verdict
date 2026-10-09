"""Settings from the environment (VERDICT_*), resolved when a command starts."""

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

APP = "verdict"


def default_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / APP


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VERDICT_")

    # client side: where the agent's CLI sends reviews
    url: str = "http://127.0.0.1:7071"
    # server side
    bind: str = "127.0.0.1"
    port: int = Field(default=7071, ge=1, le=65535)
    state_dir: Path = Field(default_factory=default_state_dir)
    # lifecycle (also the CLI defaults)
    stale_days: int = 7
    purge_days: int = 30
