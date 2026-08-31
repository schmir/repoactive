"""Integration tests for human_commits.classify_branch — runs against a real jj repository."""

import subprocess
from pathlib import Path

import pytest

from repoactive.human_commits import (
    AllPrerequisites,
    AlreadyMerged,
    NoBranch,
    NormalLayers,
    UnexpectedLayers,
    classify_branch,
)
from repoactive.jj import JJ

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _init_repo(path: Path) -> JJ:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["jj", "git", "init", "--colocate", str(path)], check=True, capture_output=True)
    return JJ(path)


def _change_id(jj: JJ, rev: str = "@") -> str:
    """Full (not short) change id — matches what bookmark_change_id returns."""
    return subprocess.run(
        ["jj", "--no-pager", "log", "-r", rev, "--no-graph", "-T", "change_id"],
        cwd=jj.cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> JJ:
    return _init_repo(tmp_path / "repo")


@pytest.fixture
def repo_with_remote(tmp_path: Path) -> JJ:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    local = _init_repo(tmp_path / "local")
    subprocess.run(
        ["jj", "--no-pager", "git", "remote", "add", "origin", str(remote)],
        cwd=local.cwd,
        check=True,
        capture_output=True,
    )
    return local


class TestClassifyBranch:
    def test_no_branch_when_bookmark_does_not_exist(self, repo: JJ) -> None:
        repo.describe("root")
        repo.bookmark_set("trunk")

        result = classify_branch(
            repo=repo, bookmark="feature", parents=["trunk"], job_name="my-job"
        )

        assert result == NoBranch()

    def test_already_merged_when_tip_is_ancestor_of_parents(self, repo: JJ) -> None:
        repo.describe("root")
        repo.bookmark_set("trunk")

        repo.new("trunk")
        repo.describe("upgrade deps\n\nRepoactive-Job: my-job")
        repo.bookmark_set("feature")
        feature_id = _change_id(repo)

        # Simulate a manual merge that advanced trunk past the branch, leaving
        # the now-stale "feature" bookmark un-deleted (ADR 0019's "empty P..R").
        repo.new("feature")
        repo.describe("merge feature into trunk")
        repo.bookmark_set("trunk")

        result = classify_branch(
            repo=repo, bookmark="feature", parents=["trunk"], job_name="my-job"
        )

        assert result == AlreadyMerged(bookmark_change_id=feature_id)

    def test_all_prerequisites_when_branch_has_no_trailer(self, repo: JJ) -> None:
        repo.describe("root")
        repo.bookmark_set("trunk")

        repo.new("trunk")
        repo.describe("manual change, no job ever ran here")
        repo.bookmark_set("feature")
        feature_id = _change_id(repo)

        result = classify_branch(
            repo=repo, bookmark="feature", parents=["trunk"], job_name="my-job"
        )

        assert isinstance(result, AllPrerequisites)
        assert result.bookmark_change_id == feature_id
        assert len(result.prereq_heads) == 1
        assert feature_id.startswith(result.prereq_heads[0])
        assert result.has_human is True

    def test_ignores_commits_carrying_a_different_jobs_trailer(self, repo: JJ) -> None:
        repo.describe("root")
        repo.bookmark_set("trunk")

        repo.new("trunk")
        repo.describe("other job's output\n\nRepoactive-Job: other-job")
        repo.bookmark_set("feature")

        result = classify_branch(
            repo=repo, bookmark="feature", parents=["trunk"], job_name="my-job"
        )

        assert isinstance(result, AllPrerequisites)

    def test_normal_layers_with_no_human_commits(self, repo: JJ) -> None:
        repo.describe("root")
        repo.bookmark_set("trunk")

        repo.new("trunk")
        repo.describe("upgrade deps\n\nRepoactive-Job: my-job")
        repo.bookmark_set("feature")
        command_id = _change_id(repo)

        result = classify_branch(
            repo=repo, bookmark="feature", parents=["trunk"], job_name="my-job"
        )

        assert isinstance(result, NormalLayers)
        assert result.bookmark_change_id == command_id
        assert command_id.startswith(result.command_commit.change_id)
        assert result.prereq_heads == []
        assert result.fixup_roots == []
        assert result.has_human is False

    def test_detects_prerequisite_below_command_commit(self, repo: JJ) -> None:
        repo.describe("root")
        repo.bookmark_set("trunk")

        repo.new("trunk")
        repo.describe("human prerequisite")
        prereq_id = _change_id(repo)

        repo.new(prereq_id)
        repo.describe("upgrade deps\n\nRepoactive-Job: my-job")
        command_id = _change_id(repo)
        repo.bookmark_set("feature")

        result = classify_branch(
            repo=repo, bookmark="feature", parents=["trunk"], job_name="my-job"
        )

        assert isinstance(result, NormalLayers)
        assert command_id.startswith(result.command_commit.change_id)
        assert len(result.prereq_heads) == 1
        assert prereq_id.startswith(result.prereq_heads[0])
        assert result.fixup_roots == []
        assert result.has_human is True

    def test_detects_fixup_above_command_commit(self, repo: JJ) -> None:
        repo.describe("root")
        repo.bookmark_set("trunk")

        repo.new("trunk")
        repo.describe("upgrade deps\n\nRepoactive-Job: my-job")
        command_id = _change_id(repo)

        repo.new(command_id)
        repo.describe("human fixup")
        fixup_id = _change_id(repo)
        repo.bookmark_set("feature")

        result = classify_branch(
            repo=repo, bookmark="feature", parents=["trunk"], job_name="my-job"
        )

        assert isinstance(result, NormalLayers)
        assert command_id.startswith(result.command_commit.change_id)
        assert result.prereq_heads == []
        assert len(result.fixup_roots) == 1
        assert fixup_id.startswith(result.fixup_roots[0])
        assert result.has_human is True

    def test_detects_prerequisite_and_fixup_together(self, repo: JJ) -> None:
        repo.describe("root")
        repo.bookmark_set("trunk")

        repo.new("trunk")
        repo.describe("human prerequisite")
        prereq_id = _change_id(repo)

        repo.new(prereq_id)
        repo.describe("upgrade deps\n\nRepoactive-Job: my-job")
        command_id = _change_id(repo)

        repo.new(command_id)
        repo.describe("human fixup")
        fixup_id = _change_id(repo)
        repo.bookmark_set("feature")

        result = classify_branch(
            repo=repo, bookmark="feature", parents=["trunk"], job_name="my-job"
        )

        assert isinstance(result, NormalLayers)
        assert command_id.startswith(result.command_commit.change_id)
        assert len(result.prereq_heads) == 1
        assert prereq_id.startswith(result.prereq_heads[0])
        assert len(result.fixup_roots) == 1
        assert fixup_id.startswith(result.fixup_roots[0])
        assert result.has_human is True

    def test_unexpected_layers_for_duplicate_command_commits(self, repo: JJ) -> None:
        repo.describe("root")
        repo.bookmark_set("trunk")

        repo.new("trunk")
        repo.describe("first\n\nRepoactive-Job: my-job")
        first_id = _change_id(repo)

        repo.new(first_id)
        repo.describe("second\n\nRepoactive-Job: my-job")
        repo.bookmark_set("feature")
        second_id = _change_id(repo)
        feature_id = second_id

        result = classify_branch(
            repo=repo, bookmark="feature", parents=["trunk"], job_name="my-job"
        )

        assert isinstance(result, UnexpectedLayers)
        assert result.bookmark_change_id == feature_id
        command_commits = [jc.change_id for jc in result.command_commits]
        assert len(command_commits) == 2  # noqa: PLR2004
        assert any(first_id.startswith(c) for c in command_commits)
        assert any(second_id.startswith(c) for c in command_commits)

    def test_supports_multiple_parents_for_stacked_jobs(self, repo: JJ) -> None:
        repo.describe("root")
        repo.bookmark_set("root")

        repo.new("root")
        repo.describe("dep a")
        repo.bookmark_set("dep-a")

        repo.new("root")
        repo.describe("dep b")
        repo.bookmark_set("dep-b")

        repo.new("dep-a", "dep-b")
        repo.describe("upgrade deps\n\nRepoactive-Job: my-job")
        command_id = _change_id(repo)
        repo.bookmark_set("feature")

        result = classify_branch(
            repo=repo, bookmark="feature", parents=["dep-a", "dep-b"], job_name="my-job"
        )

        assert isinstance(result, NormalLayers)
        assert command_id.startswith(result.command_commit.change_id)
        assert result.prereq_heads == []
        assert result.fixup_roots == []


class TestRunIdempotencyCheck:
    """NormalLayers.run_idempotency_check gates ADR 0020's idempotency skip.

    It is True only when the in-place rewrite would be a no-op *and* the branch
    still matches what was last pushed; otherwise a push is inevitable, so the
    skip must not fire.
    """

    def _seed_pushed_branch(self, repo: JJ) -> str:
        """Create trunk + a pushed command commit on "feature"; return its change id."""
        repo.describe("root")
        repo.bookmark_set("trunk")
        repo.new("trunk")
        repo.describe("upgrade deps\n\nRepoactive-Job: my-job")
        repo.bookmark_set("feature")
        command_id = _change_id(repo)
        repo.git_push_bookmarks("feature")
        return command_id

    def test_true_for_pushed_unchanged_branch(self, repo_with_remote: JJ) -> None:
        repo = repo_with_remote
        self._seed_pushed_branch(repo)

        result = classify_branch(
            repo=repo, bookmark="feature", parents=["trunk"], job_name="my-job"
        )

        assert isinstance(result, NormalLayers)
        assert result.run_idempotency_check is True

    def test_false_when_branch_never_pushed(self, repo_with_remote: JJ) -> None:
        repo = repo_with_remote
        repo.describe("root")
        repo.bookmark_set("trunk")
        repo.new("trunk")
        repo.describe("upgrade deps\n\nRepoactive-Job: my-job")
        repo.bookmark_set("feature")
        # deliberately not pushed: no remote-tracking bookmark exists

        result = classify_branch(
            repo=repo, bookmark="feature", parents=["trunk"], job_name="my-job"
        )

        assert isinstance(result, NormalLayers)
        assert result.run_idempotency_check is False

    def test_false_when_a_rebase_is_needed(self, repo_with_remote: JJ) -> None:
        repo = repo_with_remote
        self._seed_pushed_branch(repo)

        # trunk advances past the command commit's parent, so the rewrite would
        # rebase it and the skip must not fire even though the branch still
        # matches what was pushed.
        repo.new("trunk")
        repo.describe("unrelated trunk change")
        repo.bookmark_set("trunk")

        result = classify_branch(
            repo=repo, bookmark="feature", parents=["trunk"], job_name="my-job"
        )

        assert isinstance(result, NormalLayers)
        assert result.run_idempotency_check is False

    def test_false_when_branch_already_displaced_from_remote(self, repo_with_remote: JJ) -> None:
        repo = repo_with_remote
        command_id = self._seed_pushed_branch(repo)

        # A sibling trunk position; move the command commit onto it, mimicking
        # jj's auto-rebase after a dependency changed earlier this run. The change
        # id is preserved but the commit id (the pushed state) diverges, so the
        # rebase looks like a no-op yet the branch no longer matches its remote.
        repo.new("trunk")
        repo.describe("dependency moved")
        repo.bookmark_set("trunk2")
        repo.rebase_source(command_id, "trunk2")

        result = classify_branch(
            repo=repo, bookmark="feature", parents=["trunk2"], job_name="my-job"
        )

        assert isinstance(result, NormalLayers)
        assert result.run_idempotency_check is False
