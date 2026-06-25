#!/usr/bin/env -S uv run python
"""repoactive generator: emit one dependency-upgrade job per project dependency.

Reads ``pyproject.toml`` from the current directory and, for every dependency D
in ``[project.dependencies]``, emits a job named ``upgrade-D`` whose command is
``uv lock -P D`` (bump only that one dependency in the lockfile).

repoactive runs this as a generator (a ``[[job]]`` with ``emits_jobs = true``):
it points the ``REPOACTIVE_JOBS_DIR`` environment variable at a directory this
script writes ``*.toml`` job fragments into, and runs the emitted jobs in the
same invocation. See docs/adr/0004-job-generators.md.

Register it by adding to your repoactive config::

    [[job]]
    name = "upgrade-deps"
    command = "./scripts/emit-upgrade-jobs.py"
    title = "discover per-dependency upgrade jobs"
    emits_jobs = true
    # Inherited by every emitted job unless it overrides them:
    tags = ["weekly"]
    cooldown_period = "14d"
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

# Leading distribution-name token of a PEP 508 requirement string (everything
# before the version specifier, extras, or environment marker).
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def dependency_names(pyproject: dict) -> list[str]:
    """Return the distribution name of each entry in ``[project.dependencies]``.

    Order is preserved and duplicates dropped; the version specifier, extras and
    markers are stripped, leaving just the name (e.g. ``"pydantic>=2,<3"`` ->
    ``"pydantic"``).
    """
    names: list[str] = []
    for requirement in pyproject.get("project", {}).get("dependencies", []):
        match = _NAME_RE.match(requirement.strip())
        if match and match.group() not in names:
            names.append(match.group())
    return names


def job_name(dependency: str) -> str:
    """Map a dependency name to a valid repoactive job name ``upgrade-<dep>``.

    Job names allow only letters, digits, ``-`` and ``_``, so every other run of
    characters (e.g. the dot in ``ruamel.yaml``) collapses to a single hyphen.
    """
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", dependency).strip("-")
    return f"upgrade-{slug}"


def _toml_str(value: str) -> str:
    """Render ``value`` as a TOML basic string, escaping backslash and quote."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_jobs(names: list[str]) -> str:
    """Render a TOML fragment with one ``[[job]]`` per dependency name."""
    blocks = [
        "[[job]]\n"
        f"name = {_toml_str(job_name(name))}\n"
        f"command = {_toml_str(f'uv lock -P {name}')}\n"
        f"title = {_toml_str(f'build: upgrade {name}')}\n"
        for name in names
    ]
    return "\n".join(blocks)


def main() -> int:
    jobs_dir = os.environ.get("REPOACTIVE_JOBS_DIR")
    if not jobs_dir:
        print(
            "REPOACTIVE_JOBS_DIR is not set; run this as a repoactive generator "
            "(a [[job]] with emits_jobs = true).",
            file=sys.stderr,
        )
        return 1
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    names = dependency_names(pyproject)
    (Path(jobs_dir) / "upgrade-deps.toml").write_text(render_jobs(names))
    print(f"emitted {len(names)} upgrade job(s): {', '.join(names) or '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
