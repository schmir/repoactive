"""Integration test reproducing the stacked-absorb content-corruption bug.

Runs ``_absorb_results`` against a real jj repository with a genuine 2-level
dependency stack (job "a" -> job "b"), hand-building the pre-existing and
fresh phase-1 commits with the JJ wrapper to simulate phase 1 having already
run.

Regression covered: when "a" (a dependency) produces a diff and has a
pre-existing bookmark, absorbing it must not corrupt "b"'s (a stacked
dependent, not yet absorbed) own phase-1 fresh commit — see
docs/adr/0012-two-phase-commit-run-then-absorb.md.
"""

import subprocess
from pathlib import Path

import pytest

from repoactive.jj import JJ
from repoactive.runner import JobResult, RunContext, RunSummary, _absorb_results
from repoactive.selection import JobSelection
from repoactive.updates import UpdatePlan
from tests.builders import _config, _job

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _init_repo(path: Path) -> JJ:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["jj", "git", "init", "--colocate", str(path)], check=True, capture_output=True)
    (path / ".jj" / "repo" / "config.toml").write_text(
        '[user]\nname = "Test User"\nemail = "test@test.com"\n'
    )
    return JJ(path)


def _file_content(jj: JJ, rev: str, path: str) -> str:
    return subprocess.run(
        ["jj", "--no-pager", "file", "show", "-r", rev, path],
        cwd=jj.cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> JJ:
    return _init_repo(tmp_path / "repo")


def test_absorb_preserves_ancestors_diff_in_unprocessed_dependent(repo: JJ) -> None:
    """A stacked dependent must not lose its ancestor's diff during absorb.

    Job "a" is a dependency whose command output changed since the last run
    (its diff differs). Job "b" depends on "a" and its own command output is
    identical to last run (its diff relative to "a" is unchanged). Absorbing
    "a" must not corrupt "b"'s not-yet-absorbed phase-1 fresh commit: after
    absorb, "b"'s final content must contain both "a"'s new change and "b"'s
    own change.
    """
    job_a = _job("a", branch_prefix="")
    job_b = _job("b", depends_on=["a"], branch_prefix="")

    # --- trunk ---
    repo.describe("root")
    repo.bookmark_set("trunk")

    # --- previous run's committed state: old_a -> old_b ---
    repo.new("trunk")
    (repo.cwd / "file1.txt").write_text("line1\n")
    repo.describe("old a")
    repo.bookmark_set("a")
    old_a = repo.change_id()

    repo.new(old_a)
    (repo.cwd / "file2.txt").write_text("lineB\n")
    repo.describe("old b")
    repo.bookmark_set("b")
    old_b = repo.change_id()

    # --- phase 1 (simulated): fresh commits, "a"'s diff changed, "b"'s diff
    # is identical to its previous run ---
    repo.new("trunk")
    (repo.cwd / "file1.txt").write_text("line1\nline1x\n")
    repo.describe("new a")
    new_a = repo.change_id()

    repo.new(new_a)
    (repo.cwd / "file2.txt").write_text("lineB\n")  # identical to old b's diff
    repo.describe("new b")
    new_b = repo.change_id()

    result_a = JobResult(
        job=job_a,
        effective_revsets=[new_a],
        produced_diff=True,
        parents=["trunk"],
        new_change_id=new_a,
        old_change_id=old_a,
        command_output="out-a",
    )
    result_b = JobResult(
        job=job_b,
        effective_revsets=[new_b],
        produced_diff=True,
        parents=[new_a],
        new_change_id=new_b,
        old_change_id=old_b,
        command_output="out-b",
    )

    ctx = RunContext(
        config=_config(job_a, job_b),
        repo_path=repo.cwd,
        repo=repo,
        summary=RunSummary(results={"a": result_a, "b": result_b}),
        blocked=set(),
        selection=JobSelection(jobs=[job_a, job_b], refreshed=frozenset()),
        plan=UpdatePlan(),
    )

    _absorb_results(ctx)

    # "a"'s own bookmark must reflect the new content.
    assert _file_content(repo, "a", "file1.txt") == "line1\nline1x\n"

    # "b" must contain BOTH its own diff and its ancestor's diff. Before the
    # fix, "a"'s change (file1.txt content) is silently dropped from "b" here.
    assert _file_content(repo, "b", "file1.txt") == "line1\nline1x\n"
    assert _file_content(repo, "b", "file2.txt") == "lineB\n"


def test_absorb_stacked_dependent_with_changed_diff_still_gets_ancestors_change(
    repo: JJ,
) -> None:
    """Same stack, but "b"'s own diff also changed between runs.

    Confirms the legitimate restore path still folds in "b"'s new content
    without losing "a"'s contribution.
    """
    job_a = _job("a", branch_prefix="")
    job_b = _job("b", depends_on=["a"], branch_prefix="")

    repo.describe("root")
    repo.bookmark_set("trunk")

    repo.new("trunk")
    (repo.cwd / "file1.txt").write_text("line1\n")
    repo.describe("old a")
    repo.bookmark_set("a")
    old_a = repo.change_id()

    repo.new(old_a)
    (repo.cwd / "file2.txt").write_text("lineB old\n")
    repo.describe("old b")
    repo.bookmark_set("b")
    old_b = repo.change_id()

    repo.new("trunk")
    (repo.cwd / "file1.txt").write_text("line1\nline1x\n")
    repo.describe("new a")
    new_a = repo.change_id()

    repo.new(new_a)
    (repo.cwd / "file2.txt").write_text("lineB new\n")
    repo.describe("new b")
    new_b = repo.change_id()

    result_a = JobResult(
        job=job_a,
        effective_revsets=[new_a],
        produced_diff=True,
        parents=["trunk"],
        new_change_id=new_a,
        old_change_id=old_a,
        command_output="out-a",
    )
    result_b = JobResult(
        job=job_b,
        effective_revsets=[new_b],
        produced_diff=True,
        parents=[new_a],
        new_change_id=new_b,
        old_change_id=old_b,
        command_output="out-b",
    )

    ctx = RunContext(
        config=_config(job_a, job_b),
        repo_path=repo.cwd,
        repo=repo,
        summary=RunSummary(results={"a": result_a, "b": result_b}),
        blocked=set(),
        selection=JobSelection(jobs=[job_a, job_b], refreshed=frozenset()),
        plan=UpdatePlan(),
    )

    _absorb_results(ctx)

    assert _file_content(repo, "a", "file1.txt") == "line1\nline1x\n"
    assert _file_content(repo, "b", "file1.txt") == "line1\nline1x\n"
    assert _file_content(repo, "b", "file2.txt") == "lineB new\n"
