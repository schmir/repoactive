#!/usr/bin/env -S uv run nox --noxfile
"""Nox sessions for CI: tests, type checking, config validation, and schema checks."""

import tempfile
from pathlib import Path

import nox
import nox_uv

nox.options.default_venv_backend = "uv"


@nox_uv.session(python=["3.11", "3.12", "3.13", "3.14", "3.15"], uv_groups=["dev"])
def tests(session: nox.Session) -> None:
    """Run tests."""
    session.run("pytest", *session.posargs)


@nox_uv.session(uv_groups=["dev"])
def ty(session: nox.Session) -> None:
    """Type check with ty."""
    session.run("ty", "check")


@nox_uv.session
def validate_config(session: nox.Session) -> None:
    """Validate repoactive's own config."""
    session.run("repoactive", "validate-config")


@nox_uv.session
def check_schema(session: nox.Session) -> None:
    """Check that config-schema.json is up-to-date."""
    committed = Path("config-schema.json")
    if not committed.exists():
        session.error("config-schema.json is missing; run 'just dump-schema'")
        return
    with tempfile.NamedTemporaryFile(
        mode="r", prefix="config-schema-", suffix=".json", delete=False
    ) as f:
        tmpfile = f.name
        session.run("repoactive", "dump-schema", "-o", tmpfile)
        if Path(tmpfile).read_text() != committed.read_text():
            session.error("config-schema.json is out of date; run 'just dump-schema'")
