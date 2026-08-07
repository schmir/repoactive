"""End-to-end tests driving the full run_all pipeline against a real jj repository."""

import subprocess
from pathlib import Path

import pytest

from repoactive.config import Config
from repoactive.jj import JJ
from repoactive.runner import RunMode, run_all

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
            # jj's default list rendering joins mapped elements with a space,
            # which would prefix every parent after the first; .join("")
            # keeps each change-id on its own clean line.
            'parents.map(|c| c.change_id() ++ "\\n").join("")',
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


def _file_content(jj: JJ, rev: str, path: str) -> str:
    return subprocess.run(
        ["jj", "--no-pager", "file", "show", "-r", rev, path],
        cwd=jj.cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _commit_id(jj: JJ, rev: str) -> str:
    return subprocess.run(
        ["jj", "--no-pager", "log", "-r", rev, "--no-graph", "-T", "commit_id"],
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
    """Return a colocated repo with an origin remote and a pushed main trunk.

    The idempotency skip (ADR 0020) only fires in push mode against a real remote
    (run_idempotency_check compares the local tip to the remote-tracking
    bookmark), so these tests must actually push (RunMode.push).
    """
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    local = _init_repo(tmp_path / "local")
    subprocess.run(
        ["jj", "--no-pager", "git", "remote", "add", "origin", str(remote)],
        cwd=local.cwd,
        check=True,
        capture_output=True,
    )
    local.describe("root")
    local.bookmark_set("main")
    local.new("main")  # move @ off main so the pushed bookmark is not the working copy
    local.git_push_bookmarks("main")
    return local


def _single_job_config(command: str) -> Config:
    return Config.model_validate(
        {
            "jobs": {
                "j": {"command": command, "title": "J", "branch_prefix": "", "create_mr": "never"}
            }
        }
    )


def _stacked_config() -> Config:
    """Two jobs, each writing its own file: "bookmark_b" depends on "bookmark_a"."""
    return Config.model_validate(
        {
            "jobs": {
                "bookmark_a": {"command": "echo A > a.txt", "title": "A", "branch_prefix": ""},
                "bookmark_b": {
                    "command": "echo B > b.txt",
                    "title": "B",
                    "branch_prefix": "",
                    "depends_on": ["bookmark_a"],
                },
            }
        }
    )


def test_new_dependent_stacks_on_previously_run_dependency(repo: JJ) -> None:
    """ "bookmark_a" ran in a previous invocation; "bookmark_b" has never run.

    Running both together must make "bookmark_b" a direct child of "bookmark_a",
    with neither commit empty, catching a bug that drops "bookmark_a"'s diff or
    produces an empty commit.
    """
    config = _stacked_config()

    # Simulate "a has run before, b never has": run only "bookmark_a" first.
    run_all(config=config, repo_path=repo.cwd, requested_names=frozenset({"bookmark_a"}))

    # Now run both jobs, as a normal run would.
    run_all(config=config, repo_path=repo.cwd)

    assert not _is_empty(repo, "bookmark_a")
    assert not _is_empty(repo, "bookmark_b")

    a_change_id = _change_id(repo, "bookmark_a")
    assert _parent_change_ids(repo, "bookmark_b") == {a_change_id}


def test_first_run_producing_a_diff_prints_committed(
    repo: JJ, capsys: pytest.CaptureFixture[str]
) -> None:
    """A job's very first run that produces a diff must report committed.

    This is the NoBranch/AlreadyMerged non-empty path in _run_job_build_result:
    every other outcome prints a status line, so a silent commit here leaves the
    user with no feedback that the job actually did anything.
    """
    config = Config.model_validate(
        {"jobs": {"bookmark_a": {"command": "echo A > a.txt", "title": "A", "branch_prefix": ""}}}
    )

    run_all(config=config, repo_path=repo.cwd)

    assert not _is_empty(repo, "bookmark_a")
    assert "==> [bookmark_a] committed" in capsys.readouterr().out


def _merged_job_config(command: str) -> Config:
    """Build a single job "release", based on a real "main" bookmark rather than trunk().

    Using a real bookmark (rather than trunk(), which a fresh test repo has no
    remote to resolve) lets the test move "main" onto "release"'s commit to
    simulate a human merging the branch (ADR 0019's AlreadyMerged: the bookmark
    tip becomes an ancestor of the run's parents).
    """
    return Config.model_validate(
        {
            "jobs": {
                "release": {
                    "command": command,
                    "title": "Release",
                    "branch_prefix": "",
                    "base_branch": "main",
                }
            }
        }
    )


def test_merged_branch_bookmark_is_deleted_on_unchanged_rerun(repo: JJ) -> None:
    """A stale, merged-and-undeleted branch is cleaned up on the next run.

    A human merges "release" into "main" (e.g. on GitHub) but leaves the
    "release" bookmark pointing at the old, now-ancestor commit (ADR 0019's
    AlreadyMerged). Rerunning the same command produces no diff, and the stale
    bookmark must be deleted rather than left dangling forever.
    """
    config = _merged_job_config("echo unchanged > release.txt")

    repo.describe("root")
    repo.bookmark_set("main")
    run_all(config=config, repo_path=repo.cwd)
    assert repo.bookmark_exists("release")

    # Simulate the human merge: move "main" onto "release"'s commit, leaving
    # "release" exactly where it was; "release" is now an ancestor of (equal
    # to) "main".
    repo.bookmark_set("main", _change_id(repo, "release"))

    run_all(config=config, repo_path=repo.cwd)

    assert not repo.bookmark_exists("release")


def _prereq_job_config(command: str) -> Config:
    """Single job "release", based on a real "main" bookmark, for prerequisite tests."""
    return Config.model_validate(
        {
            "jobs": {
                "release": {
                    "command": command,
                    "title": "Release",
                    "branch_prefix": "",
                    "base_branch": "main",
                }
            }
        }
    )


def _add_prerequisite_below(repo: JJ, *, command_commit: str, base: str) -> str:
    """Simulate a human placing a prerequisite below command_commit (ADR 0019).

    Commits a new change on base, then rebases command_commit onto it,
    the native "rebase the branch onto your commit" workflow. Returns the
    prerequisite's change-id.
    """
    repo.new(base)
    repo.describe("human prerequisite")
    prereq_id = _change_id(repo, "@")
    repo.rebase_revision(command_commit, prereq_id)
    return prereq_id


def test_prerequisite_survives_unchanged_rerun(repo: JJ) -> None:
    """A prerequisite committed below the command commit is not stranded by a rerun.

    ADR 0019 ("Prerequisites: merged with trunk, never rebased"): while the
    prerequisite is still based on current trunk, the merge collapses to just the
    prerequisite tip, so the regenerated command commit's only parent is the
    prerequisite. A naive rebase onto the run's parents would instead drop the
    prerequisite from the branch's ancestry entirely.
    """
    config = _prereq_job_config("echo unchanged > release.txt")

    repo.describe("root")
    repo.bookmark_set("main")
    run_all(config=config, repo_path=repo.cwd)
    command_commit = _change_id(repo, "release")

    prereq_id = _add_prerequisite_below(repo, command_commit=command_commit, base="main")

    run_all(config=config, repo_path=repo.cwd)

    assert _parent_change_ids(repo, "release") == {prereq_id}
    assert _file_content(repo, "release", "release.txt") == "unchanged\n"


def test_prerequisite_survives_rerun_with_new_output(repo: JJ) -> None:
    """A prerequisite below the command commit survives a rerun whose output changes.

    Unlike the unchanged-rerun case, the command emits different content (v1 ->
    v2), so the command commit is genuinely regenerated. The prerequisite must
    still anchor that regenerated commit; its content being rebuilt from
    scratch must not drop the prerequisite from the branch's ancestry.
    """
    repo.describe("root")
    repo.bookmark_set("main")
    run_all(config=_prereq_job_config("echo v1 > release.txt"), repo_path=repo.cwd)
    command_commit = _change_id(repo, "release")

    prereq_id = _add_prerequisite_below(repo, command_commit=command_commit, base="main")

    run_all(config=_prereq_job_config("echo v2 > release.txt"), repo_path=repo.cwd)

    assert _parent_change_ids(repo, "release") == {prereq_id}
    assert _file_content(repo, "release", "release.txt") == "v2\n"


def test_prerequisite_is_merged_with_moved_trunk_without_being_rebased(repo: JJ) -> None:
    """Once trunk moves past the prerequisite's base, the in-place rewrite merges it in.

    ADR 0019: "once trunk moves, a real two-parent merge pulls trunk in
    without moving the prerequisite. repoactive never rebases the
    prerequisite." The regenerated command commit gets both the prerequisite
    tip and the new trunk as parents; the prerequisite commit itself keeps its
    original parent.
    """
    repo.describe("root")
    repo.bookmark_set("main")
    old_main = _change_id(repo, "main")
    run_all(config=_prereq_job_config("echo v1 > release.txt"), repo_path=repo.cwd)
    command_commit = _change_id(repo, "release")

    prereq_id = _add_prerequisite_below(repo, command_commit=command_commit, base="main")

    # trunk moves past the prerequisite's base, e.g. another MR landed.
    repo.new("main")
    repo.describe("unrelated trunk change")
    repo.bookmark_set("main")
    new_main = _change_id(repo, "main")

    run_all(config=_prereq_job_config("echo v2 > release.txt"), repo_path=repo.cwd)

    assert _parent_change_ids(repo, "release") == {prereq_id, new_main}
    assert _file_content(repo, "release", "release.txt") == "v2\n"
    # The prerequisite itself must be untouched, still parented on the old
    # "main", not rebased onto the new one.
    assert _parent_change_ids(repo, prereq_id) == {old_main}


def test_prerequisite_seeded_before_first_run_is_merged_with_trunk(repo: JJ) -> None:
    """A human-seeded prerequisite branch is picked up on the job's very first run.

    The job has never run (no command commit, no Repoactive-Job trailer), and
    the human has seeded "release" with a lone prerequisite forked off trunk()'s
    parent (so "main" is *not* an ancestor of it).

    ADR 0019's "No trailer match in a non-empty P..R" degenerate case: all of
    P..R is human commits, so it is all treated as prerequisites. The regenerated
    command commit takes [R, *P] as parents, so "release" ends up parented on
    both the prerequisite tip and trunk(), with repoactive's output on top.
    """
    # Build "main" with a parent: base <- main. The prerequisite will fork off
    # `base`, the parent of trunk(), so trunk() is not an ancestor of it.
    repo.describe("base")
    base_id = _change_id(repo, "@")
    repo.new()
    repo.describe("trunk")
    repo.bookmark_set("main")
    main_id = _change_id(repo, "main")

    # The human seeds the branch: a lone prerequisite forked off `base`, with the
    # "release" bookmark pointing at it. No repoactive commit exists yet.
    repo.new(base_id)
    (repo.cwd / "prereq.txt").write_text("human prerequisite\n")
    repo.describe("human prerequisite")
    prereq_id = _change_id(repo, "@")
    repo.bookmark_set("release", prereq_id)

    run_all(config=_prereq_job_config("echo output > release.txt"), repo_path=repo.cwd)

    # "release" is now a command commit merging the prerequisite tip with trunk().
    assert _parent_change_ids(repo, "release") == {prereq_id, main_id}
    assert _file_content(repo, "release", "release.txt") == "output\n"
    # The prerequisite rides along beneath the command output, untouched: still
    # forked off `base`, not rebased onto trunk().
    assert _file_content(repo, "release", "prereq.txt") == "human prerequisite\n"
    assert _parent_change_ids(repo, prereq_id) == {base_id}


def _add_fixup_above(repo: JJ, *, bookmark: str, command_commit: str) -> str:
    """Simulate a human committing a fixup on top of command_commit (ADR 0019).

    A fixup reacts to the command's output and is simply committed on top. Moves
    bookmark to the new commit, mimicking the human pushing it. Returns the
    fixup's change-id.
    """
    repo.new(command_commit)
    (repo.cwd / "fixup.txt").write_text("human fixup\n")
    repo.describe("human fixup")
    fixup_id = _change_id(repo, "@")
    repo.bookmark_set(bookmark, fixup_id)
    return fixup_id


def test_fixup_survives_unchanged_rerun(repo: JJ) -> None:
    """A fixup committed on top of the command commit is not clobbered by a rerun.

    ADR 0019 ("Fixups: reapplied on top"): the command commit is mutated in
    place and its descendants (the fixup) are carried along, not
    overwritten by the regenerated output. A naive fold would instead anchor
    on the branch tip, which *is* the fixup commit once one exists,
    and blindly restores its whole tree to the regenerated output, deleting
    the fixup's own change.
    """
    config = _prereq_job_config("echo unchanged > release.txt")

    repo.describe("root")
    repo.bookmark_set("main")
    run_all(config=config, repo_path=repo.cwd)
    command_commit = _change_id(repo, "release")

    fixup_id = _add_fixup_above(repo, bookmark="release", command_commit=command_commit)

    run_all(config=config, repo_path=repo.cwd)

    assert _file_content(repo, "release", "release.txt") == "unchanged\n"
    assert _file_content(repo, "release", "fixup.txt") == "human fixup\n"
    # Change-id continuity: the fixup itself keeps its identity across the rerun.
    assert _change_id(repo, "release") == fixup_id


def test_fixup_survives_rerun_with_new_output(repo: JJ) -> None:
    """A fixup on top of the command commit survives a rerun whose output changes.

    Unlike the unchanged-rerun case, the command emits different content (v1 ->
    v2), so the command commit beneath the fixup is genuinely regenerated. The
    fixup must still ride on top of that regenerated commit, not be discarded
    just because the content beneath it changed.
    """
    repo.describe("root")
    repo.bookmark_set("main")
    run_all(config=_prereq_job_config("echo v1 > release.txt"), repo_path=repo.cwd)
    command_commit = _change_id(repo, "release")

    fixup_id = _add_fixup_above(repo, bookmark="release", command_commit=command_commit)

    run_all(config=_prereq_job_config("echo v2 > release.txt"), repo_path=repo.cwd)

    assert _file_content(repo, "release", "release.txt") == "v2\n"
    assert _file_content(repo, "release", "fixup.txt") == "human fixup\n"
    assert _change_id(repo, "release") == fixup_id
    # The fixup rides directly on the regenerated command commit, which keeps
    # its own identity too.
    assert _parent_change_ids(repo, "release") == {command_commit}


def test_fixup_survives_empty_command_rerun(repo: JJ) -> None:
    """A fixup is preserved when the command produces no output on a rerun.

    ADR 0019 simplification: an empty command commit is kept whenever the branch
    carries human commits. Abandoning it would reparent the fixup onto the command
    commit's parent; the next run would misclassify the fixup as a prerequisite,
    and the no-diff path would then delete the branch outright, destroying the
    human's work. The empty command commit stays to anchor the layering.
    """
    repo.describe("root")
    repo.bookmark_set("main")
    run_all(config=_prereq_job_config("echo v1 > release.txt"), repo_path=repo.cwd)
    command_commit = _change_id(repo, "release")

    fixup_id = _add_fixup_above(repo, bookmark="release", command_commit=command_commit)

    # Rerun with a command that produces nothing: the command commit is emptied
    # and regenerates no content, so it ends up empty.
    run_all(config=_prereq_job_config("true"), repo_path=repo.cwd)

    # The branch and the fixup survive: the bookmark still exists and its tip is
    # the fixup, with its own change and identity intact.
    assert repo.bookmark_exists("release")
    assert _change_id(repo, "release") == fixup_id
    assert _file_content(repo, "release", "fixup.txt") == "human fixup\n"

    # The empty command commit is kept in place (identity preserved), so the fixup
    # still rides directly on it; the layering is intact for the next run. The
    # command produced nothing this run, so its own output is correctly gone.
    assert _parent_change_ids(repo, "release") == {command_commit}
    assert _is_empty(repo, command_commit)
    with pytest.raises(subprocess.CalledProcessError):
        _file_content(repo, "release", "release.txt")


def test_prerequisite_trunk_merge_conflict_freezes_the_branch(
    repo: JJ, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unresolved prerequisite/trunk merge conflict freezes the branch (ADR 0019).

    A prerequisite and a later trunk change edit the same file in conflicting
    ways, so the rebuilt command commit - which merges the prerequisite with the
    run's parents - conflicts. The command writes only release.txt and cannot
    resolve it, so the post-command check freezes: nothing is pushed and the
    conflict is left materialized on the branch. The conflict lands in the
    command commit itself (@), not in its parents (@-), so only the post-command
    check catches it.
    """
    repo.describe("root")
    (repo.cwd / "shared.txt").write_text("base\n")
    repo.bookmark_set("main")

    run_all(config=_prereq_job_config("echo v1 > release.txt"), repo_path=repo.cwd)
    command_commit = _change_id(repo, "release")

    # Human prerequisite below the command commit edits shared.txt.
    repo.new("main")
    (repo.cwd / "shared.txt").write_text("from prerequisite\n")
    repo.describe("human prerequisite")
    prereq_id = _change_id(repo, "@")
    repo.rebase_revision(command_commit, prereq_id)

    # Trunk moves and edits shared.txt differently, conflicting with the prereq.
    repo.new("main")
    (repo.cwd / "shared.txt").write_text("from trunk\n")
    repo.describe("trunk change")
    repo.bookmark_set("main")

    summary = run_all(config=_prereq_job_config("echo v2 > release.txt"), repo_path=repo.cwd)

    assert summary.frozen == {"release"}
    assert summary.results["release"].frozen is True
    assert "release" not in summary.failed
    assert "==> [release] frozen" in capsys.readouterr().out
    # The conflict is left on the branch locally for a human to resolve.
    assert repo.has_conflict("release")


def test_fixup_conflicting_with_new_output_freezes_the_branch(
    repo: JJ, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fixup that no longer applies to regenerated output freezes the branch (ADR 0019).

    The command commit itself is clean, but a human fixup that edited the same
    file the command owns no longer applies once the command regenerates it, so
    reapplying the fixup conflicts. The conflict sits *above* the command commit,
    exercising the fixup half of the post-command check's revset.
    """
    repo.describe("root")
    repo.bookmark_set("main")
    run_all(config=_prereq_job_config("echo v1 > release.txt"), repo_path=repo.cwd)
    command_commit = _change_id(repo, "release")

    # Human fixup reacts to v1 by editing the file the command owns.
    repo.new(command_commit)
    (repo.cwd / "release.txt").write_text("v1 with human edit\n")
    repo.describe("human fixup")
    repo.bookmark_set("release", _change_id(repo, "@"))

    # Rerun regenerates release.txt to v2; the fixup's v1-based edit no longer
    # applies, so reapplying it conflicts.
    summary = run_all(config=_prereq_job_config("echo v2 > release.txt"), repo_path=repo.cwd)

    assert summary.frozen == {"release"}
    assert summary.results["release"].frozen is True
    assert "release" not in summary.failed
    assert "==> [release] frozen" in capsys.readouterr().out
    assert repo.has_conflict("release")


def _versioned_stacked_config(a_command: str) -> Config:
    """Build the same shape as _stacked_config, with "bookmark_a"'s command parametrized for reruns."""
    return Config.model_validate(
        {
            "jobs": {
                "bookmark_a": {"command": a_command, "title": "A", "branch_prefix": ""},
                "bookmark_b": {
                    "command": "echo B > b.txt",
                    "title": "B",
                    "branch_prefix": "",
                    "depends_on": ["bookmark_a"],
                },
            }
        }
    )


def test_dependency_output_change_propagates_to_dependent(repo: JJ) -> None:
    """When a dependency's output changes on a rerun, its dependent inherits it.

    "bookmark_b" depends on "bookmark_a" but writes its own file, so "bookmark_b"'s own diff is unchanged
    across the rerun. The in-place rewrite of "bookmark_a"'s command commit must not
    corrupt "bookmark_b": after the rerun "bookmark_b" is still a direct child of "bookmark_a" and its tree
    carries both "bookmark_a"'s new output and "bookmark_b"'s own file.
    """
    run_all(config=_versioned_stacked_config("echo v1 > a.txt"), repo_path=repo.cwd)

    run_all(config=_versioned_stacked_config("echo v2 > a.txt"), repo_path=repo.cwd)

    assert _file_content(repo, "bookmark_a", "a.txt") == "v2\n"
    assert _parent_change_ids(repo, "bookmark_b") == {_change_id(repo, "bookmark_a")}
    assert _file_content(repo, "bookmark_b", "a.txt") == "v2\n"
    assert _file_content(repo, "bookmark_b", "b.txt") == "B\n"


def test_fixup_on_dependency_is_inherited_by_stacked_dependent(repo: JJ) -> None:
    """A fixup on "bookmark_a" survives, and "bookmark_b" forks from "bookmark_a"'s actual branch tip.

    "bookmark_b" depends on "bookmark_a", so it stacks on whatever "bookmark_a"'s branch currently is,
    human fixups included, the same way a dependent MR in git/jj naturally
    picks up a fix pushed to the branch it's based on. A dependent that
    silently dropped its dependency's fixup would build on stale content.
    """
    run_all(config=_versioned_stacked_config("echo v1 > a.txt"), repo_path=repo.cwd)
    a_command_commit = _change_id(repo, "bookmark_a")

    fixup_id = _add_fixup_above(repo, bookmark="bookmark_a", command_commit=a_command_commit)

    run_all(config=_versioned_stacked_config("echo v2 > a.txt"), repo_path=repo.cwd)

    # "bookmark_a"'s own branch: fixup preserved, content regenerated, identity kept.
    assert _file_content(repo, "bookmark_a", "a.txt") == "v2\n"
    assert _file_content(repo, "bookmark_a", "fixup.txt") == "human fixup\n"
    assert _change_id(repo, "bookmark_a") == fixup_id

    # "bookmark_b" forks from "bookmark_a"'s actual branch tip (the fixup) and inherits it.
    assert _parent_change_ids(repo, "bookmark_b") == {fixup_id}
    assert _file_content(repo, "bookmark_b", "a.txt") == "v2\n"
    assert _file_content(repo, "bookmark_b", "fixup.txt") == "human fixup\n"
    assert _file_content(repo, "bookmark_b", "b.txt") == "B\n"


def test_dependent_is_restacked_onto_dependencys_fixup_on_unchanged_rerun(repo: JJ) -> None:
    """A fixup on "bookmark_a" pulls "bookmark_b" onto it even when nothing else changes.

    After a first run, the human commits a fixup on "bookmark_a" but does *not*
    rebase "bookmark_b", which still forks from "bookmark_a"'s bare command commit.
    The next run's config is unchanged, so "bookmark_a"'s output regenerates
    identically and its command commit keeps its identity; only "bookmark_b" must
    move, restacked onto "bookmark_a"'s true tip (the fixup). A run that re-parents
    a dependent only when its dependency's *output* changed would strand
    "bookmark_b" below the fixup.
    """
    config = _stacked_config()

    run_all(config=config, repo_path=repo.cwd)
    a_command_commit = _change_id(repo, "bookmark_a")

    fixup_id = _add_fixup_above(repo, bookmark="bookmark_a", command_commit=a_command_commit)

    # The human left "bookmark_b" where it was: still forked from "bookmark_a"'s
    # bare command commit, not the fixup.
    assert _parent_change_ids(repo, "bookmark_b") == {a_command_commit}

    run_all(config=config, repo_path=repo.cwd)

    # "bookmark_a"'s branch tip is still the fixup, unchanged.
    assert _change_id(repo, "bookmark_a") == fixup_id
    assert _file_content(repo, "bookmark_a", "fixup.txt") == "human fixup\n"

    # "bookmark_b" now lives on the fixup and inherits its content.
    assert _parent_change_ids(repo, "bookmark_b") == {fixup_id}
    assert _file_content(repo, "bookmark_b", "a.txt") == "A\n"
    assert _file_content(repo, "bookmark_b", "fixup.txt") == "human fixup\n"
    assert _file_content(repo, "bookmark_b", "b.txt") == "B\n"


def test_dependent_is_restacked_when_prerequisite_rebase_leaves_it_behind(repo: JJ) -> None:
    """A prerequisite added to "bookmark_a" with a plain -r rebase strands "bookmark_b".

    After a first run, the human inserts a prerequisite below "bookmark_a" the
    native but wrong way, jj rebase --onto PREREQ -r bookmark_a. In a stack,
    -r moves only "bookmark_a", so jj gap-fills its orphaned child "bookmark_b"
    back onto "bookmark_a"'s *old* parent (trunk), diverging it and losing
    "bookmark_a"'s output. The next run must repair this: keep the prerequisite
    beneath a regenerated "bookmark_a" and restack "bookmark_b" onto its tip, so it
    inherits both the prerequisite and "bookmark_a"'s output again.
    """
    config = _stacked_config()

    run_all(config=config, repo_path=repo.cwd)
    a_command_commit = _change_id(repo, "bookmark_a")
    b_command_commit = _change_id(repo, "bookmark_b")
    (base,) = _parent_change_ids(repo, "bookmark_a")

    # Human inserts a prerequisite below "bookmark_a" with the wrong command:
    # `jj rebase --onto PREREQ -r bookmark_a` moves only "bookmark_a" itself.
    repo.new(base)
    (repo.cwd / "prereq.txt").write_text("human prerequisite\n")
    repo.describe("human prerequisite")
    prereq_id = _change_id(repo, "@")
    repo.rebase_revision(a_command_commit, prereq_id)

    # The stack is now broken: "bookmark_b" was gap-filled back onto trunk, not
    # carried onto "bookmark_a", so it has diverged and lost "bookmark_a"'s output.
    assert _parent_change_ids(repo, "bookmark_b") == {base}
    with pytest.raises(subprocess.CalledProcessError):
        _file_content(repo, "bookmark_b", "a.txt")

    run_all(config=config, repo_path=repo.cwd)

    # "bookmark_a": prerequisite preserved beneath it, identity kept.
    assert _change_id(repo, "bookmark_a") == a_command_commit
    assert _parent_change_ids(repo, "bookmark_a") == {prereq_id}
    assert _file_content(repo, "bookmark_a", "prereq.txt") == "human prerequisite\n"

    # "bookmark_b" is restacked onto "bookmark_a"'s tip and inherits the whole stack.
    assert _change_id(repo, "bookmark_b") == b_command_commit
    assert _parent_change_ids(repo, "bookmark_b") == {a_command_commit}
    assert _file_content(repo, "bookmark_b", "a.txt") == "A\n"
    assert _file_content(repo, "bookmark_b", "b.txt") == "B\n"
    assert _file_content(repo, "bookmark_b", "prereq.txt") == "human prerequisite\n"


def _fixup_reading_stacked_config() -> Config:
    """Stack where "bookmark_b"'s command *reads* a file only "bookmark_a"'s fixup creates."""
    return Config.model_validate(
        {
            "jobs": {
                "bookmark_a": {"command": "echo v2 > a.txt", "title": "A", "branch_prefix": ""},
                "bookmark_b": {
                    "command": "cat fixup.txt > b.txt",
                    "title": "B",
                    "branch_prefix": "",
                    "depends_on": ["bookmark_a"],
                },
            }
        }
    )


def test_dependent_command_runs_against_dependencys_fixup_included_tip(repo: JJ) -> None:
    """A dependent's *command* executes against its dependency's fixup, not stale output."""
    run_all(
        config=_versioned_stacked_config("echo v1 > a.txt"),
        repo_path=repo.cwd,
        requested_names=frozenset({"bookmark_a"}),
    )
    _add_fixup_above(repo, bookmark="bookmark_a", command_commit=_change_id(repo, "bookmark_a"))

    summary = run_all(config=_fixup_reading_stacked_config(), repo_path=repo.cwd)

    # "bookmark_b"'s command succeeded (fixup.txt was present in its workspace) and its
    # output is the fixup's content, proving it ran on "bookmark_a"'s rewritten tip.
    assert not summary.failed
    assert _file_content(repo, "bookmark_b", "b.txt") == "human fixup\n"


def test_merged_branch_gets_fresh_commit_when_rerun_produces_new_output(repo: JJ) -> None:
    """A merged-and-undeleted branch that still produces new output gets a fresh commit."""
    repo.describe("root")
    repo.bookmark_set("main")
    run_all(config=_merged_job_config("echo v1 > release.txt"), repo_path=repo.cwd)

    old_change_id = _change_id(repo, "release")
    repo.bookmark_set("main", old_change_id)

    run_all(config=_merged_job_config("echo v2 > release.txt"), repo_path=repo.cwd)

    assert _change_id(repo, "release") != old_change_id
    assert _file_content(repo, "release", "release.txt") == "v2\n"
    assert _parent_change_ids(repo, "release") == {_change_id(repo, "main")}
    # "main" itself must be untouched, still exactly the old, merged commit.
    assert _change_id(repo, "main") == old_change_id


def test_unchanged_rerun_keeps_command_commit_id_stable(repo_with_remote: JJ) -> None:
    """ADR 0020 idempotency skip: an unchanged rerun neither rewrites nor re-pushes.

    The regenerated tree and message match the pushed command commit and no
    rebase is needed, so run_job rolls the in-place rewrite back to the original
    commit. Its git commit id (and thus the remote bookmark) is unchanged, so
    jj pushes nothing and CI is not retriggered.
    """
    repo = repo_with_remote
    run_all(
        config=_single_job_config("echo hello > out.txt"), repo_path=repo.cwd, mode=RunMode.push
    )
    first = _commit_id(repo, "j")

    run_all(
        config=_single_job_config("echo hello > out.txt"), repo_path=repo.cwd, mode=RunMode.push
    )

    assert _commit_id(repo, "j") == first
    # nothing was pushed: the remote still points at the original commit
    assert repo.remote_bookmark_commit_id("j") == first


def test_changed_output_rewrites_command_commit(repo_with_remote: JJ) -> None:
    """When the command's output changes, the skip must not fire.

    The command commit is rewritten (new id) and pushed.
    """
    repo = repo_with_remote
    run_all(config=_single_job_config("echo v1 > out.txt"), repo_path=repo.cwd, mode=RunMode.push)
    first = _commit_id(repo, "j")

    run_all(config=_single_job_config("echo v2 > out.txt"), repo_path=repo.cwd, mode=RunMode.push)

    assert _commit_id(repo, "j") != first
    assert _file_content(repo, "j", "out.txt") == "v2\n"


def test_moved_trunk_rewrites_command_commit_even_with_identical_output(
    repo_with_remote: JJ,
) -> None:
    """A real rebase disables the skip even when the output is identical.

    Trunk advances past the command commit's parent between runs, so the rewrite
    genuinely moves the commit onto the new trunk; it will be pushed regardless,
    so run_idempotency_check is False and the commit id changes.
    """
    repo = repo_with_remote
    run_all(
        config=_single_job_config("echo hello > out.txt"), repo_path=repo.cwd, mode=RunMode.push
    )
    first = _commit_id(repo, "j")

    # advance trunk past the command commit's parent, e.g. another MR landed.
    # trunk() resolves to the remote's main, so the move must be pushed.
    repo.new("main")
    repo.describe("unrelated trunk change")
    repo.bookmark_set("main")
    repo.git_push_bookmarks("main")

    run_all(
        config=_single_job_config("echo hello > out.txt"), repo_path=repo.cwd, mode=RunMode.push
    )

    assert _commit_id(repo, "j") != first
    assert _file_content(repo, "j", "out.txt") == "hello\n"
