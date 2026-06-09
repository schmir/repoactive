#!/usr/bin/env -S uv run nox --noxfile

import re
import tempfile
from pathlib import Path

import nox
import nox_uv

nox.options.default_venv_backend = "uv"


@nox_uv.session(python=["3.11", "3.12", "3.13", "3.14"], uv_groups=["dev"])
def tests(session: nox.Session) -> None:
    """Run tests."""
    session.run("pytest", *session.posargs)


@nox_uv.session
def validate_config(session: nox.Session) -> None:
    """Validate the example config embedded in README.md."""
    session.run("repoactive", "validate-config", "-c", ".repoactive.toml")

    readme = Path("README.md").read_text()
    match = re.search(r"```toml\n(.*?)```", readme, re.DOTALL)
    if not match:
        session.error("No toml block found in README.md")
        return
    with tempfile.NamedTemporaryFile(
        mode="w", prefix="readme-example-config-", suffix=".toml", delete=False
    ) as f:
        f.write(match.group(1))
        tmpfile = f.name
    session.run("repoactive", "validate-config", "-c", tmpfile)
