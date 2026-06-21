import os
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from repoactive.config import Config, Job, JobDefaults
from repoactive.runner import (
    CommandError,
    JobResult,
    RunMode,
    UnknownJobsError,
    _compute_parents,
    _prepare_repo,
    _run_command,
    _select_jobs,
    _select_run_jobs,
    _topological_sort,
    apply_plan,
    run_all,
    run_job,
)
from repoactive.updates import BookmarkPush, JobUpdate, MRUpdate, UpdatePlan


def _job(  # noqa: PLR0913
    name: str,
    *,
    depends_on: list[str] | None = None,
    base_branch: str | None = None,
    description: str | None = None,
    labels: list[str] | None = None,
    branch_prefix: str = "repoactive/",
    mr_title_prefix: str = "",
    commit_title_prefix: str = "",
) -> Job:
    return Job(
        name=name,
        command=f"cmd-{name}",
        title=f"Change {name}",
        depends_on=depends_on or [],
        base_branch=base_branch,
        description=description,
        labels=labels or [],
        branch_prefix=branch_prefix,
        mr_title_prefix=mr_title_prefix,
        commit_title_prefix=commit_title_prefix,
    )


def _result(job: Job, *, revsets: list[str], produced: bool = True) -> JobResult:
    return JobResult(job=job, effective_revsets=revsets, produced_output=produced)


def _mock_popen(mock_popen: MagicMock, *, output: str = "", returncode: int = 0) -> MagicMock:
    """Configure a patched subprocess.Popen to behave like a finished command."""
    proc = mock_popen.return_value
    proc.communicate.return_value = (output, None)
    proc.returncode = returncode
    return proc


def _mock_jj(mock_jj_cls: MagicMock) -> MagicMock:
    """Return the JJ mock, with temp_workspace yielding that same mock.

    run_job runs its jj operations on the workspace yielded by
    repo.temp_workspace(); making the context manager yield the repo mock lets a
    single mock stand in for both, so assertions can target one object.
    """
    mock_jj = mock_jj_cls.return_value
    mock_jj.temp_workspace.return_value.__enter__.return_value = mock_jj
    return mock_jj


def _config(*jobs: Job) -> Config:
    return Config.model_validate(
        {
            "platform": [{"url": "https://gitlab.com", "type": "gitlab", "token_env": "T"}],
            "jobs": [
                {
                    "name": j.name,
                    "command": j.command,
                    "title": j.title,
                    "depends_on": j.depends_on,
                    "disabled": j.disabled,
                    "tags": j.tags,
                }
                for j in jobs
            ],
        }
    )


REPO = Path("/repo")


class TestTopologicalSort:
    def test_no_deps_preserves_order(self) -> None:
        jobs = [_job("a"), _job("b"), _job("c")]
        assert [j.name for j in _topological_sort(jobs)] == ["a", "b", "c"]

    def test_linear_chain(self) -> None:
        a, b, c = _job("a"), _job("b", depends_on=["a"]), _job("c", depends_on=["b"])
        result = [c.name for c in _topological_sort([c, b, a])]
        assert result.index("a") < result.index("b") < result.index("c")

    def test_diamond(self) -> None:
        a = _job("a")
        b = _job("b", depends_on=["a"])
        c = _job("c", depends_on=["a"])
        d = _job("d", depends_on=["b", "c"])
        names = [x.name for x in _topological_sort([d, b, c, a])]
        assert names.index("a") < names.index("b")
        assert names.index("a") < names.index("c")
        assert names.index("b") < names.index("d")
        assert names.index("c") < names.index("d")


def _djob(
    name: str,
    *,
    disabled: bool = False,
    tags: list[str] | None = None,
    depends_on: list[str] | None = None,
) -> Job:
    return Job(
        name=name,
        command="cmd",
        title=name,
        disabled=disabled,
        tags=tags or [],
        depends_on=depends_on or [],
    )


def _names(jobs: list[Job]) -> list[str]:
    return [j.name for j in jobs]


class TestSelectJobs:
    def test_no_filter_returns_all(self) -> None:
        jobs = _config(_job("a"), _job("b")).jobs
        assert _names(_select_jobs(jobs=jobs, requested_jobs=set())) == ["a", "b"]

    def test_requested_subset(self) -> None:
        config = _config(_job("a"), _job("b"), _job("c"))
        assert _names(_select_jobs(jobs=config.jobs, requested_jobs={"a"})) == ["a"]

    def test_requested_includes_transitive_deps(self) -> None:
        config = _config(_job("a"), _job("b", depends_on=["a"]), _job("c", depends_on=["b"]))
        assert _names(_select_jobs(jobs=config.jobs, requested_jobs={"c"})) == ["a", "b", "c"]

    def test_unknown_job_raises(self) -> None:
        with pytest.raises(UnknownJobsError, match="Unknown job"):
            _select_jobs(jobs=_config(_job("a")).jobs, requested_jobs={"nonexistent"})

    def test_no_disabled_jobs(self) -> None:
        config = _config(_djob("a"), _djob("b"))
        assert _names(_select_jobs(jobs=config.jobs, requested_jobs=set())) == ["a", "b"]

    def test_explicitly_disabled_excluded(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b"))
        assert _names(_select_jobs(jobs=config.jobs, requested_jobs=set())) == ["b"]

    def test_direct_dependent_excluded(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b", depends_on=["a"]))
        assert _names(_select_jobs(jobs=config.jobs, requested_jobs=set())) == []

    def test_transitive_propagation(self) -> None:
        config = _config(
            _djob("a", disabled=True),
            _djob("b", depends_on=["a"]),
            _djob("c", depends_on=["b"]),
        )
        assert _names(_select_jobs(jobs=config.jobs, requested_jobs=set())) == []

    def test_unrelated_job_not_excluded(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b", depends_on=["a"]), _djob("c"))
        assert _names(_select_jobs(jobs=config.jobs, requested_jobs=set())) == ["c"]

    def test_multiple_disabled_roots(self) -> None:
        config = _config(
            _djob("a", disabled=True),
            _djob("b", disabled=True),
            _djob("c", depends_on=["a"]),
            _djob("d", depends_on=["b"]),
        )
        assert _names(_select_jobs(jobs=config.jobs, requested_jobs=set())) == []

    def test_diamond_propagation(self) -> None:
        config = _config(
            _djob("a", disabled=True),
            _djob("b", depends_on=["a"]),
            _djob("c", depends_on=["a"]),
            _djob("d", depends_on=["b", "c"]),
        )
        assert _names(_select_jobs(jobs=config.jobs, requested_jobs=set())) == []

    def test_only_one_dep_disabled(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b"), _djob("c", depends_on=["a", "b"]))
        assert _names(_select_jobs(jobs=config.jobs, requested_jobs=set())) == ["b"]

    def test_disabled_job_depends_on_disabled_job(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b", disabled=True, depends_on=["a"]))
        assert _names(_select_jobs(jobs=config.jobs, requested_jobs=set())) == []

    def test_requesting_disabled_job_runs_it(self) -> None:
        config = _config(_djob("a", disabled=True))
        assert _names(_select_jobs(jobs=config.jobs, requested_jobs={"a"})) == ["a"]

    def test_requesting_job_pulls_in_disabled_dependency(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b", depends_on=["a"]))
        assert _names(_select_jobs(jobs=config.jobs, requested_jobs={"b"})) == ["a", "b"]

    def test_tagged_job_excluded_from_default_run(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        assert _names(_select_jobs(jobs=config.jobs, requested_jobs=set())) == ["a"]

    def test_tag_selects_matching_jobs(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"]), _djob("c", tags=["weekly"]))
        assert _names(
            _select_jobs(jobs=config.jobs, requested_jobs=set(), requested_tags={"weekly"})
        ) == ["b", "c"]

    def test_tag_does_not_imply_enabled(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        assert _names(
            _select_jobs(jobs=config.jobs, requested_jobs=set(), requested_tags={"weekly"})
        ) == ["b"]

    def test_explicit_enabled_tag_keeps_job_in_both(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["enabled", "weekly"]))
        assert _names(_select_jobs(jobs=config.jobs, requested_jobs=set())) == ["a", "b"]
        assert _names(
            _select_jobs(jobs=config.jobs, requested_jobs=set(), requested_tags={"weekly"})
        ) == ["b"]

    def test_multiple_tags_are_ored(self) -> None:
        config = _config(
            _djob("a", tags=["weekly"]), _djob("b", tags=["monthly"]), _djob("c", tags=["daily"])
        )
        assert _names(
            _select_jobs(
                jobs=config.jobs, requested_jobs=set(), requested_tags={"weekly", "monthly"}
            )
        ) == ["a", "b"]

    def test_tag_selection_overrides_disabled(self) -> None:
        # disabled is sugar for the 'disabled' tag, so --tag disabled runs them.
        config = _config(_djob("a", disabled=True), _djob("b"))
        assert _names(
            _select_jobs(jobs=config.jobs, requested_jobs=set(), requested_tags={"disabled"})
        ) == ["a"]

    def test_names_and_tags_are_unioned(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"]), _djob("c"))
        assert _names(
            _select_jobs(jobs=config.jobs, requested_jobs={"a"}, requested_tags={"weekly"})
        ) == ["a", "b"]

    def test_tag_selection_force_includes_dependencies(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"], depends_on=["a"]))
        assert _names(
            _select_jobs(jobs=config.jobs, requested_jobs=set(), requested_tags={"weekly"})
        ) == ["a", "b"]

    def test_tagged_dependency_dropped_from_default_run(self) -> None:
        # b is out of the default run (tagged weekly); its dependent c is dropped too.
        config = _config(_djob("a"), _djob("b", tags=["weekly"]), _djob("c", depends_on=["b"]))
        assert _names(_select_jobs(jobs=config.jobs, requested_jobs=set())) == ["a"]

    def test_refresh_job_pulled_into_default_run(self) -> None:
        # A weekly job with an unmerged branch is refreshed by the default run.
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        assert _names(
            _select_jobs(jobs=config.jobs, requested_jobs=set(), refresh_jobs={"b"})
        ) == ["a", "b"]

    def test_refresh_includes_dependencies(self) -> None:
        config = _config(
            _djob("a", tags=["weekly"]), _djob("b", tags=["weekly"], depends_on=["a"])
        )
        assert _names(
            _select_jobs(jobs=config.jobs, requested_jobs=set(), refresh_jobs={"b"})
        ) == ["a", "b"]

    def test_refresh_includes_disabled_job(self) -> None:
        # An unmerged branch for a disabled job (likely from an explicit run) is refreshed.
        config = _config(_djob("a"), _djob("b", disabled=True))
        assert _names(
            _select_jobs(jobs=config.jobs, requested_jobs=set(), refresh_jobs={"b"})
        ) == ["a", "b"]

    def test_refresh_ignores_unknown_names(self) -> None:
        # A trailer for a removed/renamed job must not blow up selection.
        config = _config(_djob("a"))
        assert _names(
            _select_jobs(jobs=config.jobs, requested_jobs=set(), refresh_jobs={"gone"})
        ) == ["a"]


def _mock_repo(unmerged: set[str] | None = None) -> MagicMock:
    """A JJ stub whose unmerged_job_names returns the given set."""
    repo = MagicMock()
    repo.unmerged_job_names.return_value = unmerged or set()
    return repo


class TestSelectRunJobs:
    def test_bare_run_returns_default_jobs(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        repo = _mock_repo()
        result = _select_run_jobs(
            config=config, repo=repo, requested_jobs=None, requested_tags=None
        )
        assert _names(result) == ["a"]

    def test_bare_run_refreshes_unmerged_branches(self) -> None:
        # A weekly job (out of the default run) with an unmerged branch is pulled in.
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        repo = _mock_repo({"b"})
        result = _select_run_jobs(
            config=config, repo=repo, requested_jobs=None, requested_tags=None
        )
        assert _names(result) == ["a", "b"]

    def test_bare_run_ignores_unmerged_names_not_in_config(self) -> None:
        # A trailer for a removed/renamed job must not affect selection.
        config = _config(_djob("a"))
        repo = _mock_repo({"gone"})
        result = _select_run_jobs(
            config=config, repo=repo, requested_jobs=None, requested_tags=None
        )
        assert _names(result) == ["a"]

    def test_requested_jobs_skip_unmerged_query(self) -> None:
        # Explicit selection does not consult unmerged branches.
        config = _config(_djob("a"), _djob("b"))
        repo = _mock_repo({"a"})
        result = _select_run_jobs(
            config=config, repo=repo, requested_jobs=["b"], requested_tags=None
        )
        assert _names(result) == ["b"]
        repo.unmerged_job_names.assert_not_called()

    def test_requested_tags_skip_unmerged_query(self) -> None:
        config = _config(_djob("a", tags=["weekly"]), _djob("b"))
        repo = _mock_repo({"b"})
        result = _select_run_jobs(
            config=config, repo=repo, requested_jobs=None, requested_tags=["weekly"]
        )
        assert _names(result) == ["a"]
        repo.unmerged_job_names.assert_not_called()

    def test_unknown_requested_job_raises(self) -> None:
        config = _config(_djob("a"))
        repo = _mock_repo()
        with pytest.raises(UnknownJobsError):
            _select_run_jobs(
                config=config, repo=repo, requested_jobs=["nope"], requested_tags=None
            )


class TestComputeParents:
    def test_no_deps_uses_trunk(self) -> None:
        assert _compute_parents(_job("a"), {}) == ["trunk()"]

    def test_no_deps_uses_base_branch(self) -> None:
        assert _compute_parents(_job("a", base_branch="main"), {}) == ["main"]

    def test_dep_with_output(self) -> None:
        a = _job("a")
        results = {"a": _result(a, revsets=["repoactive/a"])}
        assert _compute_parents(_job("b", depends_on=["a"]), results) == ["repoactive/a"]

    def test_dep_with_no_output_propagates_its_parents(self) -> None:
        a = _job("a")
        results = {"a": _result(a, revsets=["trunk()"], produced=False)}
        assert _compute_parents(_job("b", depends_on=["a"]), results) == ["trunk()"]

    def test_multiple_deps_deduplicates(self) -> None:
        a, b = _job("a"), _job("b")
        results = {
            "a": _result(a, revsets=["trunk()"], produced=False),
            "b": _result(b, revsets=["trunk()"], produced=False),
        }
        assert _compute_parents(_job("c", depends_on=["a", "b"]), results) == ["trunk()"]

    def test_multiple_deps_distinct_revsets(self) -> None:
        a, b = _job("a"), _job("b")
        results = {
            "a": _result(a, revsets=["repoactive/a"]),
            "b": _result(b, revsets=["repoactive/b"]),
        }
        parents = _compute_parents(_job("c", depends_on=["a", "b"]), results)
        assert parents == ["repoactive/a", "repoactive/b"]


class TestJobResolve:
    def test_branch_prefix_falls_back_to_defaults(self) -> None:
        job = Job(name="x", command="cmd", title="X")
        resolved = job.resolve(JobDefaults(branch_prefix="bot/"))
        assert resolved.branch_prefix == "bot/"

    def test_branch_prefix_per_job_overrides_defaults(self) -> None:
        job = Job(name="x", command="cmd", title="X", branch_prefix="custom/")
        resolved = job.resolve(JobDefaults(branch_prefix="bot/"))
        assert resolved.branch_prefix == "custom/"

    def test_mr_title_prefix_falls_back_to_defaults(self) -> None:
        job = Job(name="x", command="cmd", title="X")
        resolved = job.resolve(JobDefaults(mr_title_prefix="[bot] "))
        assert resolved.mr_title_prefix == "[bot] "

    def test_mr_title_prefix_per_job_overrides_defaults(self) -> None:
        job = Job(name="x", command="cmd", title="X", mr_title_prefix="[job] ")
        resolved = job.resolve(JobDefaults(mr_title_prefix="[bot] "))
        assert resolved.mr_title_prefix == "[job] "

    def test_commit_title_prefix_falls_back_to_defaults(self) -> None:
        job = Job(name="x", command="cmd", title="X")
        resolved = job.resolve(JobDefaults(commit_title_prefix="[bot] "))
        assert resolved.commit_title_prefix == "[bot] "

    def test_commit_title_prefix_per_job_overrides_defaults(self) -> None:
        job = Job(name="x", command="cmd", title="X", commit_title_prefix="[job] ")
        resolved = job.resolve(JobDefaults(commit_title_prefix="[bot] "))
        assert resolved.commit_title_prefix == "[job] "

    def test_labels_merged_with_defaults(self) -> None:
        job = Job(name="x", command="cmd", title="X", labels=["feat"])
        resolved = job.resolve(JobDefaults(labels=["auto"]))
        assert resolved.labels == ["auto", "feat"]

    def test_labels_deduplicated(self) -> None:
        job = Job(name="x", command="cmd", title="X", labels=["auto"])
        resolved = job.resolve(JobDefaults(labels=["auto"]))
        assert resolved.labels == ["auto"]

    def test_empty_prefix_string_not_overridden_by_defaults(self) -> None:
        job = Job(name="x", command="cmd", title="X", mr_title_prefix="")
        resolved = job.resolve(JobDefaults(mr_title_prefix="[bot] "))
        assert resolved.mr_title_prefix == ""

    def test_base_branch_falls_back_to_defaults(self) -> None:
        job = Job(name="x", command="cmd", title="X")
        resolved = job.resolve(JobDefaults(base_branch="main"))
        assert resolved.base_branch == "main"

    def test_base_branch_per_job_overrides_defaults(self) -> None:
        job = Job(name="x", command="cmd", title="X", base_branch="dev")
        resolved = job.resolve(JobDefaults(base_branch="main"))
        assert resolved.base_branch == "dev"

    def test_base_branch_stays_none_when_not_set(self) -> None:
        job = Job(name="x", command="cmd", title="X")
        resolved = job.resolve(JobDefaults())
        assert resolved.base_branch is None


def _alive(pid: int) -> bool:
    """Whether ``pid`` still names a live (non-reaped) process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class TestRunCommand:
    @pytest.mark.slow
    def test_timeout_kills_whole_process_group(self, tmp_path: Path) -> None:
        # The command backgrounds a long sleep, records its PID, then waits. The
        # sleep shares the command's process group, so the timeout must kill it
        # too - not just the top-level shell.
        pidfile = tmp_path / "child.pid"
        job = Job(
            name="foo",
            command=f"sleep 30 & echo $! > {pidfile}; wait",
            title="t",
            timeout="1s",
            branch_prefix="repoactive/",
            commit_title_prefix="",
        )
        with pytest.raises(CommandError, match="timed out after 1s"):
            _run_command(job, tmp_path)

        child_pid = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(child_pid), "backgrounded child survived the timeout kill"

    def test_non_utf8_output_does_not_crash(self, tmp_path: Path) -> None:
        # A command may emit arbitrary bytes; an undecodable byte must be
        # replaced rather than raising UnicodeDecodeError and crashing the run.
        job = Job(
            # \377 is octal for 0xff: POSIX printf supports octal escapes
            # everywhere, but \xHH hex escapes are not portable (dash omits them).
            name="foo",
            command=r"printf '\377'",
            title="t",
            branch_prefix="repoactive/",
            commit_title_prefix="",
        )
        result = _run_command(job, tmp_path)

        assert result.output == "�"  # U+FFFD REPLACEMENT CHARACTER


class TestRunJob:
    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.Popen")
    def test_produces_output(self, mock_sub: MagicMock, mock_jj_cls: MagicMock) -> None:
        mock_jj = _mock_jj(mock_jj_cls)
        _mock_popen(mock_sub)
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False
        job = _job("foo")

        result = run_job(job=job, parents=["trunk()"], repo_path=REPO)

        mock_jj.new.assert_called_once_with("trunk()")
        mock_jj.bookmark_set.assert_called_once_with("repoactive/foo")
        mock_jj.describe.assert_called_once_with("Change foo\n\nRepoactive-Job: foo")
        # The push is recorded for the apply phase, not performed during the run.
        mock_jj.git_push_bookmarks.assert_not_called()
        mock_jj.abandon.assert_not_called()
        assert result.produced_output is True
        assert result.effective_revsets == ["repoactive/foo"]
        assert result.update is not None
        assert result.update.push == BookmarkPush(bookmark="repoactive/foo")

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.Popen")
    def test_describe_includes_body(self, mock_sub: MagicMock, mock_jj_cls: MagicMock) -> None:
        mock_jj = _mock_jj(mock_jj_cls)
        _mock_popen(mock_sub)
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False
        job = _job("foo", description="Body text.")

        run_job(job=job, parents=["trunk()"], repo_path=REPO)

        mock_jj.describe.assert_called_once_with("Change foo\n\nBody text.\n\nRepoactive-Job: foo")

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.Popen")
    def test_output_appended_to_commit_message(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = _mock_jj(mock_jj_cls)
        _mock_popen(mock_sub, output="did stuff\n")
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False
        job = _job("foo")

        run_job(job=job, parents=["trunk()"], repo_path=REPO)

        mock_jj.describe.assert_called_once_with(
            "Change foo\n\n  $ cmd-foo\n  did stuff\n\nRepoactive-Job: foo"
        )

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.Popen")
    def test_output_in_commit_false_suppresses_output(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = _mock_jj(mock_jj_cls)
        _mock_popen(mock_sub, output="did stuff\n")
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False
        job = Job(
            name="foo",
            command="cmd-foo",
            title="Change foo",
            output_in_commit=False,
            branch_prefix="repoactive/",
            commit_title_prefix="",
        )

        run_job(job=job, parents=["trunk()"], repo_path=REPO)

        mock_jj.describe.assert_called_once_with("Change foo\n\nRepoactive-Job: foo")

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.Popen")
    def test_commit_title_prefix_applied(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = _mock_jj(mock_jj_cls)
        _mock_popen(mock_sub)
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False

        run_job(
            job=_job("foo", commit_title_prefix="[bot] "),
            parents=["trunk()"],
            repo_path=REPO,
        )

        mock_jj.describe.assert_called_once_with("[bot] Change foo\n\nRepoactive-Job: foo")

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.Popen")
    def test_no_output_no_existing_bookmark(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = _mock_jj(mock_jj_cls)
        _mock_popen(mock_sub)
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = True

        result = run_job(job=_job("foo"), parents=["trunk()"], repo_path=REPO)

        mock_jj.abandon.assert_called_once_with()
        mock_jj.bookmark_set.assert_not_called()
        mock_jj.bookmark_delete.assert_not_called()
        mock_jj.git_push_bookmarks.assert_not_called()
        assert result.produced_output is False
        assert result.effective_revsets == ["trunk()"]

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.Popen")
    def test_no_output_existing_bookmark_deleted(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = _mock_jj(mock_jj_cls)
        _mock_popen(mock_sub)
        mock_jj.bookmark_exists.return_value = True
        mock_jj.is_empty.return_value = True

        result = run_job(job=_job("foo"), parents=["trunk()"], repo_path=REPO)

        mock_jj.abandon.assert_called_once_with()
        mock_jj.bookmark_delete.assert_called_once_with("repoactive/foo")
        # The remote deletion is deferred: recorded as a delete push, not pushed now.
        mock_jj.git_push_bookmarks.assert_not_called()
        mock_jj.bookmark_set.assert_not_called()
        assert result.produced_output is False
        assert result.effective_revsets == ["trunk()"]
        assert result.update is not None
        assert result.update.push == BookmarkPush(bookmark="repoactive/foo", delete=True)
        assert result.update.mr is None

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.Popen")
    def test_no_output_effective_revsets_are_parents(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = _mock_jj(mock_jj_cls)
        _mock_popen(mock_sub)
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = True

        result = run_job(
            job=_job("foo"),
            parents=["repoactive/a", "repoactive/b"],
            repo_path=REPO,
        )

        assert result.effective_revsets == ["repoactive/a", "repoactive/b"]

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.Popen")
    def test_command_failure_abandons_and_raises(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = _mock_jj(mock_jj_cls)
        _mock_popen(mock_sub, output="boom\n", returncode=1)
        mock_jj.bookmark_exists.return_value = False
        with pytest.raises(CommandError, match="command failed"):
            run_job(
                job=_job("foo"),
                parents=["trunk()"],
                repo_path=REPO,
            )

        mock_jj.abandon.assert_called_once_with()
        mock_jj.bookmark_set.assert_not_called()

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.Popen")
    def test_passes_timeout_to_subprocess(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = _mock_jj(mock_jj_cls)
        proc = _mock_popen(mock_sub)
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False
        job = Job(
            name="foo",
            command="cmd-foo",
            title="Change foo",
            timeout="30m",
            branch_prefix="repoactive/",
            commit_title_prefix="",
        )

        run_job(job=job, parents=["trunk()"], repo_path=REPO)

        assert mock_sub.call_args.kwargs["start_new_session"] is True
        assert proc.communicate.call_args.kwargs["timeout"] == 30 * 60

    @patch("repoactive.runner.os.getpgid", return_value=4242)
    @patch("repoactive.runner.os.killpg")
    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.Popen")
    def test_command_timeout_kills_group_abandons_and_raises(
        self,
        mock_sub: MagicMock,
        mock_jj_cls: MagicMock,
        mock_killpg: MagicMock,
        mock_getpgid: MagicMock,
    ) -> None:
        mock_jj = _mock_jj(mock_jj_cls)
        proc = _mock_popen(mock_sub)
        # First communicate (with timeout) raises; the post-kill one drains output.
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired("cmd", 1800),
            ("partial\n", None),
        ]
        mock_jj.bookmark_exists.return_value = False
        job = Job(
            name="foo",
            command="cmd-foo",
            title="Change foo",
            timeout="30m",
            branch_prefix="repoactive/",
            commit_title_prefix="",
        )
        with pytest.raises(CommandError, match="timed out after 30m"):
            run_job(job=job, parents=["trunk()"], repo_path=REPO)

        mock_killpg.assert_called_once_with(4242, signal.SIGKILL)
        mock_jj.abandon.assert_called_once_with()
        mock_jj.bookmark_set.assert_not_called()

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.Popen")
    def test_no_timeout_passes_none(self, mock_sub: MagicMock, mock_jj_cls: MagicMock) -> None:
        mock_jj = _mock_jj(mock_jj_cls)
        proc = _mock_popen(mock_sub)
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False

        run_job(job=_job("foo"), parents=["trunk()"], repo_path=REPO)

        assert proc.communicate.call_args.kwargs["timeout"] is None

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.Popen")
    def test_command_output_in_mr_update(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = _mock_jj(mock_jj_cls)
        _mock_popen(mock_sub, output="Copied file foo -> bar\n")
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False

        result = run_job(job=_job("foo"), parents=["trunk()"], repo_path=REPO)

        # The MR is not created during the run; the command output is recorded
        # for the apply phase to render.
        assert result.update is not None
        assert result.update.mr is not None
        assert result.update.mr.command == "cmd-foo"
        assert result.update.mr.command_output == "Copied file foo -> bar"

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.Popen")
    def test_records_mr_update(self, mock_sub: MagicMock, mock_jj_cls: MagicMock) -> None:
        mock_jj = _mock_jj(mock_jj_cls)
        _mock_popen(mock_sub)
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False

        result = run_job(job=_job("foo"), parents=["trunk()"], repo_path=REPO)

        # The collect phase has no platform; it always records the MR for the
        # apply phase to act on, with the target branch left unresolved.
        assert result.mr_url is None
        assert result.update is not None
        assert result.update.mr is not None
        assert result.update.mr.source_branch == "repoactive/foo"
        assert result.update.mr.target_branch is None

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.Popen")
    def test_create_mr_false_records_no_mr(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = _mock_jj(mock_jj_cls)
        _mock_popen(mock_sub)
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False
        job = Job(
            name="foo",
            command="cmd",
            title="Foo",
            create_mr=False,
            branch_prefix="repoactive/",
            commit_title_prefix="",
        )

        result = run_job(job=job, parents=["trunk()"], repo_path=REPO)

        assert result.mr_url is None
        assert result.produced_output is True
        # A push is still recorded, but with no MR.
        assert result.update is not None
        assert result.update.push == BookmarkPush(bookmark="repoactive/foo")
        assert result.update.mr is None

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.Popen")
    def test_existing_bookmark_uses_edit_restore_rebase(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = _mock_jj(mock_jj_cls)
        _mock_popen(mock_sub)
        mock_jj.bookmark_exists.return_value = True
        mock_jj.is_empty.return_value = False

        run_job(job=_job("foo"), parents=["trunk()"], repo_path=REPO)

        mock_jj.new.assert_not_called()
        mock_jj.edit.assert_called_once_with("repoactive/foo")
        mock_jj.restore.assert_called_once_with("repoactive/foo")
        mock_jj.rebase.assert_called_once_with("trunk()")

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.Popen")
    def test_existing_bookmark_multiple_parents_rebase(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = _mock_jj(mock_jj_cls)
        _mock_popen(mock_sub)
        mock_jj.bookmark_exists.return_value = True
        mock_jj.is_empty.return_value = False

        run_job(
            job=_job("foo"),
            parents=["repoactive/a", "repoactive/b"],
            repo_path=REPO,
        )

        mock_jj.new.assert_not_called()
        mock_jj.rebase.assert_called_once_with("repoactive/a", "repoactive/b")

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.Popen")
    def test_no_existing_bookmark_uses_new(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = _mock_jj(mock_jj_cls)
        _mock_popen(mock_sub)
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False

        run_job(job=_job("foo"), parents=["trunk()"], repo_path=REPO)

        mock_jj.new.assert_called_once_with("trunk()")
        mock_jj.edit.assert_not_called()
        mock_jj.restore.assert_not_called()
        mock_jj.rebase.assert_not_called()


def _push_update(name: str) -> JobUpdate:
    return JobUpdate(
        job_name=name,
        title=f"Change {name}",
        push=BookmarkPush(bookmark=f"repoactive/{name}"),
    )


def _mr_update(name: str, *, depends_on: list[str] | None = None) -> JobUpdate:
    return JobUpdate(
        job_name=name,
        title=f"Change {name}",
        push=BookmarkPush(bookmark=f"repoactive/{name}"),
        mr=MRUpdate(
            source_branch=f"repoactive/{name}",
            target_branch="main",
            title=f"[bot] Change {name}",
            description="",
            command=f"cmd-{name}",
            command_output="",
            labels=["auto"],
            draft=False,
            depends_on=depends_on or [],
        ),
    )


class TestApplyPlan:
    @patch("repoactive.runner.JJ")
    def test_empty_plan_is_noop(self, mock_jj_cls: MagicMock) -> None:
        platform = MagicMock()

        urls = apply_plan(UpdatePlan(), repo_path=REPO, platform=platform, mode=RunMode.publish)

        assert urls == {}
        mock_jj_cls.return_value.git_push_bookmarks.assert_not_called()
        platform.ensure_mr.assert_not_called()

    @patch("repoactive.runner.JJ")
    def test_pushes_bookmark_without_mr(self, mock_jj_cls: MagicMock) -> None:
        plan = UpdatePlan(updates=[_push_update("a")])

        urls = apply_plan(plan, repo_path=REPO, platform=None, mode=RunMode.push)

        mock_jj_cls.return_value.git_push_bookmarks.assert_called_once_with("repoactive/a")
        assert urls == {}

    @patch("repoactive.runner.JJ")
    def test_delete_push_propagated(self, mock_jj_cls: MagicMock) -> None:
        plan = UpdatePlan(
            updates=[
                JobUpdate(
                    job_name="a",
                    title="Change a",
                    push=BookmarkPush(bookmark="repoactive/a", delete=True),
                )
            ]
        )

        apply_plan(plan, repo_path=REPO, platform=MagicMock(), mode=RunMode.publish)

        mock_jj_cls.return_value.git_push_bookmarks.assert_called_once_with("repoactive/a")

    @patch("repoactive.runner.JJ")
    def test_creates_mr_with_params(self, mock_jj_cls: MagicMock) -> None:
        platform = MagicMock()
        platform.ensure_mr.return_value = "https://example.com/mr/1"
        plan = UpdatePlan(updates=[_mr_update("a")])

        urls = apply_plan(plan, repo_path=REPO, platform=platform, mode=RunMode.publish)

        mock_jj_cls.return_value.git_push_bookmarks.assert_called_once_with("repoactive/a")
        params = platform.ensure_mr.call_args[0][0]
        assert params.source_branch == "repoactive/a"
        assert params.target_branch == "main"
        assert params.title == "[bot] Change a"
        assert params.labels == ["auto"]
        assert params.draft is False
        assert urls == {"a": "https://example.com/mr/1"}

    @patch("repoactive.runner.JJ")
    def test_unresolved_target_branch_uses_platform_default(self, mock_jj_cls: MagicMock) -> None:
        platform = MagicMock()
        platform.default_branch.return_value = "develop"
        platform.ensure_mr.return_value = "https://example.com/mr/1"
        update = _mr_update("a")
        assert update.mr is not None
        update.mr.target_branch = None
        plan = UpdatePlan(updates=[update])

        apply_plan(plan, repo_path=REPO, platform=platform, mode=RunMode.publish)

        params = platform.ensure_mr.call_args[0][0]
        assert params.target_branch == "develop"

    @patch("repoactive.runner.JJ")
    def test_dependency_url_resolved_in_order(self, mock_jj_cls: MagicMock) -> None:
        platform = MagicMock()
        platform.ensure_mr.side_effect = [
            "https://example.com/mr/a",
            "https://example.com/mr/b",
        ]
        plan = UpdatePlan(updates=[_mr_update("a"), _mr_update("b", depends_on=["a"])])

        apply_plan(plan, repo_path=REPO, platform=platform, mode=RunMode.publish)

        b_params = platform.ensure_mr.call_args_list[1][0][0]
        assert b_params.description == "Depends on:\n- [Change a](https://example.com/mr/a)"

    @patch("repoactive.runner.JJ")
    def test_push_mode_skips_mr(self, mock_jj_cls: MagicMock) -> None:
        # In push mode the bookmark is pushed but the MR is left alone.
        plan = UpdatePlan(updates=[_mr_update("a")])

        urls = apply_plan(plan, repo_path=REPO, platform=None, mode=RunMode.push)

        mock_jj_cls.return_value.git_push_bookmarks.assert_called_once_with("repoactive/a")
        assert urls == {}


class TestRunAll:
    @pytest.fixture(autouse=True)
    def mock_jj(self) -> Iterator[MagicMock]:
        """Stub the JJ class run_all constructs (unmerged_job_names + cooldown query)."""
        with patch("repoactive.runner.JJ") as cls:
            cls.return_value.unmerged_job_names.return_value = set()
            cls.return_value.has_recent_job_commit.return_value = False
            cls.return_value.op_id.return_value = "OP-START"
            yield cls

    @patch("repoactive.runner.run_job")
    def test_independent_jobs_all_run(self, mock_run_job: MagicMock) -> None:
        a, b = _job("a"), _job("b")
        mock_run_job.return_value = _result(a, revsets=["repoactive/a"])

        summary = run_all(config=_config(a, b), repo_path=REPO)

        called_names = {c.kwargs["job"].name for c in mock_run_job.call_args_list}
        assert called_names == {"a", "b"}
        assert not summary.failed
        assert not summary.skipped

    @patch("repoactive.runner.run_job")
    def test_collected_plan_is_applied(self, mock_run_job: MagicMock, mock_jj: MagicMock) -> None:
        a = _job("a")
        result = _result(a, revsets=["repoactive/a"])
        result.update = _mr_update("a")
        mock_run_job.return_value = result
        platform = MagicMock()
        platform.ensure_mr.return_value = "https://example.com/mr/a"

        summary = run_all(
            config=_config(a), repo_path=REPO, platform=platform, mode=RunMode.publish
        )

        mock_jj.return_value.git_push_bookmarks.assert_called_once_with("repoactive/a")
        platform.ensure_mr.assert_called_once()
        # The MR URL is written back into the summary by the apply phase.
        assert summary.results["a"].mr_url == "https://example.com/mr/a"

    @patch("repoactive.runner.run_job")
    def test_failed_job_skips_dependents(self, mock_run_job: MagicMock) -> None:
        a = _job("a")
        b = _job("b", depends_on=["a"])
        c = _job("c")
        mock_run_job.side_effect = [RuntimeError("boom"), _result(c, revsets=["repoactive/c"])]

        summary = run_all(config=_config(a, b, c), repo_path=REPO)

        called_names = {c.kwargs["job"].name for c in mock_run_job.call_args_list}
        assert called_names == {"a", "c"}
        assert "a" in summary.failed
        assert "b" in summary.skipped
        assert "c" in summary.results

    @patch("repoactive.runner.run_job")
    def test_skipped_transitively(self, mock_run_job: MagicMock) -> None:
        a = _job("a")
        b = _job("b", depends_on=["a"])
        c = _job("c", depends_on=["b"])
        mock_run_job.side_effect = RuntimeError("boom")

        summary = run_all(config=_config(a, b, c), repo_path=REPO)

        assert "a" in summary.failed
        assert "b" in summary.skipped
        assert "c" in summary.skipped

    @patch("repoactive.runner.run_job")
    def test_ok_false_when_failures(self, mock_run_job: MagicMock) -> None:
        mock_run_job.side_effect = RuntimeError("boom")
        summary = run_all(config=_config(_job("a")), repo_path=REPO)
        assert not summary.ok

    @patch("repoactive.runner.run_job")
    def test_ok_true_when_all_succeed(self, mock_run_job: MagicMock) -> None:
        a = _job("a")
        mock_run_job.return_value = _result(a, revsets=["repoactive/a"])
        summary = run_all(config=_config(a), repo_path=REPO)
        assert summary.ok

    @patch("repoactive.runner.run_job")
    def test_job_filter(self, mock_run_job: MagicMock) -> None:
        a, b, c = _job("a"), _job("b"), _job("c")
        mock_run_job.return_value = _result(b, revsets=["repoactive/b"])

        run_all(config=_config(a, b, c), repo_path=REPO, requested_jobs=["b"])

        called_names = {c.kwargs["job"].name for c in mock_run_job.call_args_list}
        assert called_names == {"b"}

    @patch("repoactive.runner.run_job")
    def test_disabled_job_not_run(self, mock_run_job: MagicMock) -> None:
        a = Job(name="a", command="cmd", title="A", disabled=True)
        b = _job("b")
        mock_run_job.return_value = _result(b, revsets=["repoactive/b"])

        run_all(config=_config(a, b), repo_path=REPO)

        called_names = {c.kwargs["job"].name for c in mock_run_job.call_args_list}
        assert called_names == {"b"}

    @patch("repoactive.runner.run_job")
    def test_disabled_dependency_disables_dependent(self, mock_run_job: MagicMock) -> None:
        a = Job(name="a", command="cmd", title="A", disabled=True)
        b = _job("b", depends_on=["a"])
        c = _job("c")
        mock_run_job.return_value = _result(c, revsets=["repoactive/c"])

        run_all(config=_config(a, b, c), repo_path=REPO)

        called_names = {c.kwargs["job"].name for c in mock_run_job.call_args_list}
        assert called_names == {"c"}

    @patch("repoactive.runner.run_job")
    def test_disabled_propagates_transitively(self, mock_run_job: MagicMock) -> None:
        a = Job(name="a", command="cmd", title="A", disabled=True)
        b = _job("b", depends_on=["a"])
        c = _job("c", depends_on=["b"])
        mock_run_job.return_value = _result(c, revsets=["repoactive/c"])

        run_all(config=_config(a, b, c), repo_path=REPO)

        assert mock_run_job.call_count == 0

    @patch("repoactive.runner.apply_plan", return_value={})
    @patch("repoactive.runner.run_job")
    def test_local_run_does_not_apply_plan(
        self, mock_run_job: MagicMock, mock_apply_plan: MagicMock
    ) -> None:
        a = _job("a")
        mock_run_job.return_value = _result(a, revsets=["repoactive/a"])

        run_all(config=_config(a), repo_path=REPO, mode=RunMode.local)

        # The collect phase still runs, but a local run never applies the plan.
        mock_run_job.assert_called_once()
        mock_apply_plan.assert_not_called()

    @patch("repoactive.runner.apply_plan", return_value={})
    @patch("repoactive.runner.run_job")
    def test_non_local_run_applies_plan(
        self, mock_run_job: MagicMock, mock_apply_plan: MagicMock
    ) -> None:
        a = _job("a")
        mock_run_job.return_value = _result(a, revsets=["repoactive/a"])

        run_all(config=_config(a), repo_path=REPO, mode=RunMode.push)

        mock_apply_plan.assert_called_once()

    def test_publish_without_platform_is_rejected(self) -> None:
        a = _job("a")
        with pytest.raises(AssertionError):
            run_all(config=_config(a), repo_path=REPO, mode=RunMode.publish)

    def test_push_with_platform_is_rejected(self) -> None:
        a = _job("a")
        with pytest.raises(AssertionError):
            run_all(config=_config(a), repo_path=REPO, platform=MagicMock(), mode=RunMode.push)

    @patch("repoactive.runner.run_job")
    def test_requesting_disabled_job_runs_it(self, mock_run_job: MagicMock) -> None:
        a = Job(name="a", command="cmd", title="A", disabled=True)
        b = _job("b")
        mock_run_job.return_value = _result(a, revsets=["repoactive/a"])

        run_all(config=_config(a, b), repo_path=REPO, requested_jobs=["a"])

        called_names = {c.kwargs["job"].name for c in mock_run_job.call_args_list}
        assert called_names == {"a"}

    @patch("repoactive.runner.run_job")
    def test_requesting_job_pulls_in_disabled_dependency(self, mock_run_job: MagicMock) -> None:
        a = Job(name="a", command="cmd", title="A", disabled=True)
        b = _job("b", depends_on=["a"])
        mock_run_job.return_value = _result(b, revsets=["repoactive/b"])

        run_all(config=_config(a, b), repo_path=REPO, requested_jobs=["b"])

        called_names = {c.kwargs["job"].name for c in mock_run_job.call_args_list}
        assert called_names == {"a", "b"}

    @staticmethod
    def _cooldown_config(name: str, interval: str, **fields: object) -> Config:
        return Config.model_validate(
            {
                "platform": [{"url": "https://gitlab.com", "type": "gitlab", "token_env": "T"}],
                "jobs": [
                    {"name": name, "command": "cmd", "title": name, "cooldown_period": interval}
                    | fields
                ],
            }
        )

    @patch("repoactive.runner.run_job")
    def test_cooldown_skips_job(self, mock_run_job: MagicMock, mock_jj: MagicMock) -> None:
        mock_jj.return_value.has_recent_job_commit.return_value = True

        summary = run_all(config=self._cooldown_config("a", "7d"), repo_path=REPO)

        mock_run_job.assert_not_called()
        assert summary.cooldown == {"a"}
        assert summary.results["a"].produced_output is False
        assert summary.ok  # cooldown is not a failure

    @patch("repoactive.runner.run_job")
    def test_cooldown_queries_base_branch(
        self, mock_run_job: MagicMock, mock_jj: MagicMock
    ) -> None:
        mock_jj.return_value.has_recent_job_commit.return_value = True

        run_all(config=self._cooldown_config("a", "7d"), repo_path=REPO)

        kwargs = mock_jj.return_value.has_recent_job_commit.call_args.kwargs
        assert kwargs["job_name"] == "a"
        assert kwargs["base"] == "trunk()"

    @patch("repoactive.runner.run_job")
    def test_no_recent_commit_runs_job(self, mock_run_job: MagicMock, mock_jj: MagicMock) -> None:
        mock_jj.return_value.has_recent_job_commit.return_value = False
        a = _job("a")
        mock_run_job.return_value = _result(a, revsets=["repoactive/a"])

        summary = run_all(config=self._cooldown_config("a", "7d"), repo_path=REPO)

        mock_run_job.assert_called_once()
        assert not summary.cooldown

    @patch("repoactive.runner.run_job")
    def test_cooldown_dependent_falls_back_to_base(
        self, mock_run_job: MagicMock, mock_jj: MagicMock
    ) -> None:
        mock_jj.return_value.has_recent_job_commit.return_value = True
        b = _job("b", depends_on=["a"])
        mock_run_job.return_value = _result(b, revsets=["repoactive/b"])
        config = Config.model_validate(
            {
                "platform": [{"url": "https://gitlab.com", "type": "gitlab", "token_env": "T"}],
                "jobs": [
                    {"name": "a", "command": "cmd", "title": "a", "cooldown_period": "7d"},
                    {"name": "b", "command": "cmd", "title": "b", "depends_on": ["a"]},
                ],
            }
        )

        summary = run_all(config=config, repo_path=REPO)

        assert summary.cooldown == {"a"}
        # b still runs, parented on the base branch since a was a no-op this run.
        b_call = next(c for c in mock_run_job.call_args_list if c.kwargs["job"].name == "b")
        assert b_call.kwargs["parents"] == ["trunk()"]

    @patch("repoactive.runner.run_job")
    def test_no_cooldown_period_never_queries(
        self, mock_run_job: MagicMock, mock_jj: MagicMock
    ) -> None:
        a = _job("a")
        mock_run_job.return_value = _result(a, revsets=["repoactive/a"])

        run_all(config=_config(a), repo_path=REPO)

        mock_jj.return_value.has_recent_job_commit.assert_not_called()

    @patch("repoactive.runner.run_job")
    def test_run_all_resolves_jobs_with_defaults(self, mock_run_job: MagicMock) -> None:
        a = _job("a")
        mock_run_job.return_value = _result(a, revsets=["repoactive/a"])

        run_all(config=_config(a), repo_path=REPO)

        passed_job = mock_run_job.call_args.kwargs["job"]
        assert passed_job.branch_prefix == "repoactive/"
        assert passed_job.mr_title_prefix == "[repoactive] "
        assert passed_job.commit_title_prefix == "[repoactive] "

    @patch("repoactive.runner.run_job")
    def test_unmerged_branch_refreshes_tagged_job_in_default_run(
        self, mock_run_job: MagicMock, mock_jj: MagicMock
    ) -> None:
        # b is weekly (not in the default run) but has an unmerged branch, so it runs.
        mock_jj.return_value.unmerged_job_names.return_value = {"b"}
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        mock_run_job.return_value = _result(_job("x"), revsets=["repoactive/x"])

        run_all(config=config, repo_path=REPO)

        called_names = {c.kwargs["job"].name for c in mock_run_job.call_args_list}
        assert called_names == {"a", "b"}

    @patch("repoactive.runner.run_job")
    def test_unmerged_branches_not_queried_for_explicit_selection(
        self, mock_run_job: MagicMock, mock_jj: MagicMock
    ) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        mock_run_job.return_value = _result(_job("x"), revsets=["repoactive/x"])

        run_all(config=config, repo_path=REPO, requested_jobs=["a"])

        mock_jj.return_value.unmerged_job_names.assert_not_called()
        called_names = {c.kwargs["job"].name for c in mock_run_job.call_args_list}
        assert called_names == {"a"}

    @patch("repoactive.runner.run_job")
    def test_local_run_prints_restore_hint_before_and_after(
        self, mock_run_job: MagicMock, mock_jj: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        a = _job("a")
        mock_run_job.return_value = _result(a, revsets=["repoactive/a"])

        run_all(config=_config(a), repo_path=REPO, mode=RunMode.local)

        mock_jj.return_value.op_id.assert_called_once_with()
        out = capsys.readouterr().out
        assert out.count("jj op restore OP-START") == 2  # noqa: PLR2004

    @patch("repoactive.runner.run_job")
    def test_non_local_run_omits_restore_hint(
        self, mock_run_job: MagicMock, mock_jj: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A pushing run can't be undone with jj op restore, so don't suggest it.
        a = _job("a")
        mock_run_job.return_value = _result(a, revsets=["repoactive/a"])

        run_all(config=_config(a), repo_path=REPO, mode=RunMode.push)

        # op_id is still computed for the debug log, but no restore hint is printed.
        mock_jj.return_value.op_id.assert_called_once_with()
        assert "jj op restore" not in capsys.readouterr().out


class TestPrepareRepo:
    @pytest.fixture
    def mock_jj(self) -> Iterator[MagicMock]:
        with patch("repoactive.runner.JJ") as cls:
            cls.return_value.op_id.return_value = "OP-START"
            yield cls

    def test_yields_the_repo(self, mock_jj: MagicMock) -> None:
        with _prepare_repo(REPO, RunMode.local) as repo:
            assert repo is mock_jj.return_value
        mock_jj.assert_called_once_with(REPO)

    def test_forgets_stale_workspaces_before_yield(self, mock_jj: MagicMock) -> None:
        # Stale workspaces must be dropped before the caller starts adding fresh
        # ones, so the cleanup has to happen by the time the body runs.
        repo = mock_jj.return_value
        with _prepare_repo(REPO, RunMode.local):
            repo.forget_stale_workspaces.assert_called_once_with()

    def test_local_prints_restore_hint_before_and_after(
        self, mock_jj: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _prepare_repo(REPO, RunMode.local):
            # Printed once on entry; the body's own output sits between the two.
            assert capsys.readouterr().out.count("jj op restore OP-START") == 1
        assert capsys.readouterr().out.count("jj op restore OP-START") == 1

    @pytest.mark.parametrize("mode", [RunMode.push, RunMode.publish])
    def test_non_local_omits_restore_hint(
        self, mode: RunMode, mock_jj: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A pushed branch or created MR can't be undone with jj op restore.
        with _prepare_repo(REPO, mode):
            pass
        assert "jj op restore" not in capsys.readouterr().out

    def test_restore_hint_printed_even_when_body_raises(
        self, mock_jj: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The finally block still hands the user their undo hint on a crash.
        with pytest.raises(RuntimeError, match="boom"), _prepare_repo(REPO, RunMode.local):
            raise RuntimeError("boom")
        assert "jj op restore OP-START" in capsys.readouterr().out
