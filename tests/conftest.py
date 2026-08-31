"""Shared fixtures for the test suite."""

from collections.abc import Iterator
from pathlib import Path

import pytest

_JJ_USER_CONFIG = """\
[user]
name = "Test User"
email = "test@test.com"
"""


@pytest.fixture(scope="session", autouse=True)
def jj_user_config(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Point jj at a throwaway user config for the whole session.

    Without it jj reads the developer's own ~/.config/jj/config.toml, so their
    aliases, default-command and diff settings decide what the tests see.

    This is also why test repositories carry no .jj/repo/config.toml: jj
    migrates that file to ~/.config/jj/repos/<hash>/config.toml on first use,
    which leaves a directory behind in the developer's home for every
    repository a test creates.
    """
    config = tmp_path_factory.mktemp("jj-config") / "config.toml"
    config.write_text(_JJ_USER_CONFIG)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("JJ_CONFIG", str(config))
        yield config
