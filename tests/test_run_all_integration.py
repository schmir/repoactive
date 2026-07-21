"""End-to-end integration tests for ``run_all`` against a real jj repository.

Unlike ``test_absorb_integration.py`` (which hand-builds phase-1 state to
exercise ``_absorb_results`` directly), these tests drive the full
``run_all`` pipeline -- real job commands, real command execution, real
selection -- against a real jj repository.
"""

import subprocess
from pathlib import Path

import pytest

from repoactive.config import Config
from repoactive.jj import JJ
from repoactive.runner import run_all

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _init_repo(path: Path) -> JJ:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["jj", "git", "init", "--colocate", str(path)], check=True, capture_output=True)
    (path / ".jj" / "repo" / "config.toml").write_text(
        '[user]\nname = "Test User"\nemail = "test@test.com"\n'
    )
    return JJ(path)


def _change_id(jj: JJ, rev: str) -> str:
    return subprocess.run(
        ["jj", "--no-pager", "log", "-r", rev, "--no-graph", "-T", "change_id"],
        cwd=jj.cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _parent_change_ids(jj: JJ, rev: str) -> set[str]:
    output = subprocess.run(
        [
            "jj",
            "--no-pager",
            "log",
            "-r",
            rev,
            "--no-graph",
            "-T",
            'parents.map(|c| c.change_id() ++ "\\n")',
        ],
        cwd=jj.cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return set(output.splitlines())


def _is_empty(jj: JJ, rev: str) -> bool:
    output = subprocess.run(
        ["jj", "--no-pager", "log", "-r", rev, "--no-graph", "-T", "self.empty()"],
        cwd=jj.cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return output == "true"


@pytest.fixture
def repo(tmp_path: Path) -> JJ:
    return _init_repo(tmp_path / "repo")


def _stacked_config() -> Config:
    """Two jobs, each writing its own file: "b" depends on "a"."""
    return Config.model_validate(
        {
            "jobs": {
                "a": {"command": "echo A > a.txt", "title": "A", "branch_prefix": ""},
                "b": {
                    "command": "echo B > b.txt",
                    "title": "B",
                    "branch_prefix": "",
                    "depends_on": ["a"],
                },
            }
        }
    )


def test_new_dependent_stacks_on_previously_run_dependency(repo: JJ) -> None:
    """ "a" already ran in a previous invocation; "b" has never run.

    Running both jobs together must produce a commit for "b" that is a
    direct child of "a"'s commit, and neither job's commit may be empty --
    both commands genuinely write a file, so a bug that dropped "a"'s diff
    from "b" (or otherwise produced an empty commit) is caught here.
    """
    config = _stacked_config()

    # Simulate "a has run before, b never has": run only "a" first.
    run_all(config=config, repo_path=repo.cwd, requested_names=frozenset({"a"}))

    # Now run both jobs, as a normal run would.
    run_all(config=config, repo_path=repo.cwd)

    assert not _is_empty(repo, "a")
    assert not _is_empty(repo, "b")

    a_change_id = _change_id(repo, "a")
    assert _parent_change_ids(repo, "b") == {a_change_id}
