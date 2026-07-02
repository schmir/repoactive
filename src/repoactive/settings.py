"""Process-level settings, read from ``REPOACTIVE_*`` environment variables.

These tune how repoactive presents itself in the current environment (as opposed
to ``config.py``, which describes the jobs to run). Instantiate settings via
``load_settings()`` at the point of use so the environment is read at call time,
not import time. The CLI callback also calls it once at startup so a
misconfigured environment fails immediately with a clean error instead of
mid-run.
"""

from typing import Literal

from pydantic import ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "REPOACTIVE_"


class SettingsError(Exception):
    """A ``REPOACTIVE_*`` environment variable failed validation."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX)

    # Set REPOACTIVE_UI=noninteractive where nobody is at the keyboard (e.g. a
    # CI job); it suppresses the "how to undo" panels. This is an explicit
    # switch rather than CI auto-detection because a CI container someone has
    # logged in to *is* interactive.
    ui: Literal["interactive", "noninteractive"] = "interactive"

    # Set REPOACTIVE_LOG_LEVEL to enable logging at that level without passing
    # --debug on every invocation. The --debug flag takes precedence.
    log_level: Literal["debug", "info", "warning", "error", "critical"] | None = None

    # Set REPOACTIVE_LOG_HANDLER to choose how log records are rendered:
    # "rich" (colourised column layout) or "plain" (the stdlib's default
    # stream handler, e.g. when the output is collected by a log aggregator).
    # When unset, it follows REPOACTIVE_UI: plain for noninteractive, rich
    # otherwise.
    log_handler: Literal["rich", "plain"] | None = None

    # Set REPOACTIVE_PROGRESS_LINES to change how many output lines the live
    # tail shows while a job command runs; a value <= 0 disables the live
    # block entirely.
    progress_lines: int = 8

    @field_validator("ui", "log_level", "log_handler", mode="before")
    @classmethod
    def _lowercase(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _default_log_handler_from_ui(self) -> "Settings":
        if self.log_handler is None:
            self.log_handler = "plain" if self.ui == "noninteractive" else "rich"
        return self


def load_settings() -> Settings:
    """Read ``Settings`` from the environment.

    Raises ``SettingsError`` with a one-line message naming the offending
    environment variable(s), instead of pydantic's multi-line ``ValidationError``.
    """
    try:
        return Settings()
    except ValidationError as e:
        problems = "; ".join(
            f"{ENV_PREFIX}{'_'.join(str(loc) for loc in err['loc']).upper()}: {err['msg']}"
            for err in e.errors()
        )
        raise SettingsError(problems) from e
