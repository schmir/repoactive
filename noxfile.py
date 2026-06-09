#!/usr/bin/env -S uv run nox --noxfile

import nox
import nox_uv

nox.options.default_venv_backend = "uv"


@nox_uv.session(python=["3.11", "3.12", "3.13", "3.14"], uv_groups=["dev"])
def tests(session: nox.Session) -> None:
    """Run tests."""
    session.run("pytest", *session.posargs)
