#!/usr/bin/env -S uv run nox --noxfile
"""Nox sessions for CI: tests, type checking, config validation, and schema checks."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import nox
import nox_uv

nox.options.default_venv_backend = "uv"


@nox_uv.session(python=["3.12", "3.13", "3.14", "3.15"], uv_groups=["dev"])
def tests(session: nox.Session) -> None:
    """Run tests."""
    session.run("pytest", *session.posargs)


@nox_uv.session(uv_groups=["dev"])
def ty(session: nox.Session) -> None:
    """Type check with ty."""
    session.run("ty", "check")


@nox_uv.session(name="validate-config")
def validate_config(session: nox.Session) -> None:
    """Validate repoactive's own config."""
    session.run("repoactive", "validate-config")


@nox_uv.session(name="check-schema")
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


CONTAINER_ENGINE_ENV = "CONTAINER_ENGINE"


def container_engine(session: nox.Session) -> str:
    """Pick the container engine for the smoke test.

    CONTAINER_ENGINE wins if set; otherwise podman when it is on PATH, else
    docker. Images do not cross engines, so CI, which pre-builds with docker
    buildx, pins the variable to docker.
    """
    chosen = os.environ.get(CONTAINER_ENGINE_ENV, "").strip()
    if chosen:
        if shutil.which(chosen) is None:
            session.error(f"{CONTAINER_ENGINE_ENV}={chosen} but {chosen} is not on PATH")
        return chosen
    return "podman" if shutil.which("podman") else "docker"


@nox.session(venv_backend="none", name="smoketest")
def smoketest(session: nox.Session) -> None:
    """Build the container image and smoke-test it against a fresh clone of repoactive.

    Requires a working container engine (see container_engine) and network
    access. Not part of `just ci`; run manually with `nox -s smoketest`.
    Pass `-- --no-build` to reuse an existing `repoactive` image (CI pre-builds
    it with layer caching).
    """
    engine = container_engine(session)
    session.log(f"using container engine: {engine}")

    if "--no-build" not in session.posargs:
        # Mirrors `just build-image`.
        session.run(engine, "build", "-t", "repoactive", ".", external=True)

    # The smoke test itself lives in scripts/smoketest.sh. We read it here and
    # pass it as the `bash -c` argument.
    script = Path("scripts/smoketest.sh").read_text()
    # Run via subprocess rather than session.run: on failure nox would echo the entire `run ... -c
    # <whole script>` command line.
    result = subprocess.run(
        [engine, "run", "--rm", "--entrypoint", "bash", "repoactive", "-c", script],
        check=False,
    )
    if result.returncode != 0:
        session.error(f"{engine} smoke test failed (exit {result.returncode}); see banner above")
