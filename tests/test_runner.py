import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from repoactive.config import Config, Job, JobDefaults
from repoactive.runner import (
    JobResult,
    _compute_parents,
    _mr_params,
    _select_jobs,
    _topological_sort,
    run_all,
    run_job,
)


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
        assert _names(_select_jobs(_config(_job("a"), _job("b")).jobs, set())) == ["a", "b"]

    def test_requested_subset(self) -> None:
        config = _config(_job("a"), _job("b"), _job("c"))
        assert _names(_select_jobs(config.jobs, {"a"})) == ["a"]

    def test_requested_includes_transitive_deps(self) -> None:
        config = _config(_job("a"), _job("b", depends_on=["a"]), _job("c", depends_on=["b"]))
        assert _names(_select_jobs(config.jobs, {"c"})) == ["a", "b", "c"]

    def test_unknown_job_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown job"):
            _select_jobs(_config(_job("a")).jobs, {"nonexistent"})

    def test_no_disabled_jobs(self) -> None:
        config = _config(_djob("a"), _djob("b"))
        assert _names(_select_jobs(config.jobs, set())) == ["a", "b"]

    def test_explicitly_disabled_excluded(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b"))
        assert _names(_select_jobs(config.jobs, set())) == ["b"]

    def test_direct_dependent_excluded(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b", depends_on=["a"]))
        assert _names(_select_jobs(config.jobs, set())) == []

    def test_transitive_propagation(self) -> None:
        config = _config(
            _djob("a", disabled=True),
            _djob("b", depends_on=["a"]),
            _djob("c", depends_on=["b"]),
        )
        assert _names(_select_jobs(config.jobs, set())) == []

    def test_unrelated_job_not_excluded(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b", depends_on=["a"]), _djob("c"))
        assert _names(_select_jobs(config.jobs, set())) == ["c"]

    def test_multiple_disabled_roots(self) -> None:
        config = _config(
            _djob("a", disabled=True),
            _djob("b", disabled=True),
            _djob("c", depends_on=["a"]),
            _djob("d", depends_on=["b"]),
        )
        assert _names(_select_jobs(config.jobs, set())) == []

    def test_diamond_propagation(self) -> None:
        config = _config(
            _djob("a", disabled=True),
            _djob("b", depends_on=["a"]),
            _djob("c", depends_on=["a"]),
            _djob("d", depends_on=["b", "c"]),
        )
        assert _names(_select_jobs(config.jobs, set())) == []

    def test_only_one_dep_disabled(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b"), _djob("c", depends_on=["a", "b"]))
        assert _names(_select_jobs(config.jobs, set())) == ["b"]

    def test_disabled_job_depends_on_disabled_job(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b", disabled=True, depends_on=["a"]))
        assert _names(_select_jobs(config.jobs, set())) == []

    def test_requesting_disabled_job_runs_it(self) -> None:
        config = _config(_djob("a", disabled=True))
        assert _names(_select_jobs(config.jobs, {"a"})) == ["a"]

    def test_requesting_job_pulls_in_disabled_dependency(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b", depends_on=["a"]))
        assert _names(_select_jobs(config.jobs, {"b"})) == ["a", "b"]

    def test_tagged_job_excluded_from_default_run(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        assert _names(_select_jobs(config.jobs, set())) == ["a"]

    def test_tag_selects_matching_jobs(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"]), _djob("c", tags=["weekly"]))
        assert _names(_select_jobs(config.jobs, set(), {"weekly"})) == ["b", "c"]

    def test_tag_does_not_imply_enabled(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        assert _names(_select_jobs(config.jobs, set(), {"weekly"})) == ["b"]

    def test_explicit_enabled_tag_keeps_job_in_both(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["enabled", "weekly"]))
        assert _names(_select_jobs(config.jobs, set())) == ["a", "b"]
        assert _names(_select_jobs(config.jobs, set(), {"weekly"})) == ["b"]

    def test_multiple_tags_are_ored(self) -> None:
        config = _config(
            _djob("a", tags=["weekly"]), _djob("b", tags=["monthly"]), _djob("c", tags=["daily"])
        )
        assert _names(_select_jobs(config.jobs, set(), {"weekly", "monthly"})) == ["a", "b"]

    def test_tag_selection_overrides_disabled(self) -> None:
        # disabled is sugar for the 'disabled' tag, so --tag disabled runs them.
        config = _config(_djob("a", disabled=True), _djob("b"))
        assert _names(_select_jobs(config.jobs, set(), {"disabled"})) == ["a"]

    def test_names_and_tags_are_unioned(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"]), _djob("c"))
        assert _names(_select_jobs(config.jobs, {"a"}, {"weekly"})) == ["a", "b"]

    def test_tag_selection_force_includes_dependencies(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"], depends_on=["a"]))
        assert _names(_select_jobs(config.jobs, set(), {"weekly"})) == ["a", "b"]

    def test_tagged_dependency_dropped_from_default_run(self) -> None:
        # b is out of the default run (tagged weekly); its dependent c is dropped too.
        config = _config(_djob("a"), _djob("b", tags=["weekly"]), _djob("c", depends_on=["b"]))
        assert _names(_select_jobs(config.jobs, set())) == ["a"]

    def test_refresh_job_pulled_into_default_run(self) -> None:
        # A weekly job with an unmerged branch is refreshed by the default run.
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        assert _names(_select_jobs(config.jobs, set(), refresh_jobs={"b"})) == ["a", "b"]

    def test_refresh_includes_dependencies(self) -> None:
        config = _config(
            _djob("a", tags=["weekly"]), _djob("b", tags=["weekly"], depends_on=["a"])
        )
        assert _names(_select_jobs(config.jobs, set(), refresh_jobs={"b"})) == ["a", "b"]

    def test_refresh_includes_disabled_job(self) -> None:
        # An unmerged branch for a disabled job (likely from an explicit run) is refreshed.
        config = _config(_djob("a"), _djob("b", disabled=True))
        assert _names(_select_jobs(config.jobs, set(), refresh_jobs={"b"})) == ["a", "b"]

    def test_refresh_ignores_unknown_names(self) -> None:
        # A trailer for a removed/renamed job must not blow up selection.
        config = _config(_djob("a"))
        assert _names(_select_jobs(config.jobs, set(), refresh_jobs={"gone"})) == ["a"]


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


_BM = "repoactive/x"
_BASE_BRANCH = "main"


class TestMrParams:
    def test_labels_used(self) -> None:
        job = _job("x", labels=["auto", "feat"])
        params = _mr_params(job=job, bookmark=_BM, base_branch=_BASE_BRANCH)
        assert params.labels == ["auto", "feat"]

    def test_description_falls_back_to_empty(self) -> None:
        params = _mr_params(job=_job("x"), bookmark=_BM, base_branch=_BASE_BRANCH)
        assert params.description == ""

    def test_description_used_when_set(self) -> None:
        job = _job("x", description="Details.")
        params = _mr_params(job=job, bookmark=_BM, base_branch=_BASE_BRANCH)
        assert params.description == "Details."

    def test_command_output_appended(self) -> None:
        params = _mr_params(
            job=_job("x"),
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
            command_output="some output",
        )
        assert params.description == "```\n$ cmd-x\nsome output\n```"

    def test_command_output_appended_after_description(self) -> None:
        params = _mr_params(
            job=_job("x", description="Details."),
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
            command_output="some output",
        )
        assert params.description == "Details.\n\n```\n$ cmd-x\nsome output\n```"

    def test_empty_command_output_not_appended(self) -> None:
        job = _job("x", description="Details.")
        params = _mr_params(
            job=job,
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
            command_output="",
        )
        assert params.description == "Details."

    def test_title_prefix_applied(self) -> None:
        params = _mr_params(
            job=_job("x", mr_title_prefix="[bot] "),
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
        )
        assert params.title == "[bot] Change x"

    def test_empty_title_prefix(self) -> None:
        params = _mr_params(
            job=_job("x", mr_title_prefix=""), bookmark=_BM, base_branch=_BASE_BRANCH
        )
        assert params.title == "Change x"

    def test_draft_forwarded(self) -> None:
        job = Job(name="x", command="cmd", title="X", draft=True, mr_title_prefix="")
        params = _mr_params(job=job, bookmark=_BM, base_branch=_BASE_BRANCH)
        assert params.draft is True

    def test_dep_mr_urls_included(self) -> None:
        params = _mr_params(
            job=_job("x"),
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
            dep_mr_urls=[("Dep A", "https://example.com/mr/1")],
        )
        assert params.description == "Depends on:\n- [Dep A](https://example.com/mr/1)"

    def test_dep_mr_urls_multiple(self) -> None:
        params = _mr_params(
            job=_job("x"),
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
            dep_mr_urls=[
                ("Dep A", "https://example.com/mr/1"),
                ("Dep B", "https://example.com/mr/2"),
            ],
        )
        assert params.description == (
            "Depends on:\n- [Dep A](https://example.com/mr/1)\n- [Dep B](https://example.com/mr/2)"
        )

    def test_dep_mr_urls_after_description(self) -> None:
        params = _mr_params(
            job=_job("x", description="Details."),
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
            dep_mr_urls=[("Dep A", "https://example.com/mr/1")],
        )
        assert params.description == (
            "Details.\n\nDepends on:\n- [Dep A](https://example.com/mr/1)"
        )

    def test_dep_mr_urls_before_command_output(self) -> None:
        params = _mr_params(
            job=_job("x"),
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
            command_output="some output",
            dep_mr_urls=[("Dep A", "https://example.com/mr/1")],
        )
        assert params.description == (
            "Depends on:\n- [Dep A](https://example.com/mr/1)\n\n```\n$ cmd-x\nsome output\n```"
        )


class TestRunJob:
    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.run")
    def test_produces_output(self, mock_sub: MagicMock, mock_jj_cls: MagicMock) -> None:
        mock_jj = mock_jj_cls.return_value
        mock_sub.return_value.stdout = ""
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False
        job = _job("foo")

        result = run_job(job=job, parents=["trunk()"], repo_path=REPO, platform=None)

        mock_jj.new.assert_called_once_with("trunk()")
        mock_jj.bookmark_set.assert_called_once_with("repoactive/foo")
        mock_jj.describe.assert_called_once_with("Change foo\n\nRepoactive-Job: foo")
        mock_jj.git_push_bookmarks.assert_called_once_with("repoactive/foo")
        mock_jj.abandon.assert_not_called()
        assert result.produced_output is True
        assert result.effective_revsets == ["repoactive/foo"]

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.run")
    def test_describe_includes_body(self, mock_sub: MagicMock, mock_jj_cls: MagicMock) -> None:
        mock_jj = mock_jj_cls.return_value
        mock_sub.return_value.stdout = ""
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False
        job = _job("foo", description="Body text.")

        run_job(job=job, parents=["trunk()"], repo_path=REPO, platform=None)

        mock_jj.describe.assert_called_once_with("Change foo\n\nBody text.\n\nRepoactive-Job: foo")

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.run")
    def test_output_appended_to_commit_message(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = mock_jj_cls.return_value
        mock_sub.return_value.stdout = "did stuff\n"
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False
        job = _job("foo")

        run_job(job=job, parents=["trunk()"], repo_path=REPO, platform=None)

        mock_jj.describe.assert_called_once_with(
            "Change foo\n\n  $ cmd-foo\n  did stuff\n\nRepoactive-Job: foo"
        )

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.run")
    def test_output_in_commit_false_suppresses_output(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = mock_jj_cls.return_value
        mock_sub.return_value.stdout = "did stuff\n"
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

        run_job(job=job, parents=["trunk()"], repo_path=REPO, platform=None)

        mock_jj.describe.assert_called_once_with("Change foo\n\nRepoactive-Job: foo")

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.run")
    def test_commit_title_prefix_applied(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = mock_jj_cls.return_value
        mock_sub.return_value.stdout = ""
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False

        run_job(
            job=_job("foo", commit_title_prefix="[bot] "),
            parents=["trunk()"],
            repo_path=REPO,
            platform=None,
        )

        mock_jj.describe.assert_called_once_with("[bot] Change foo\n\nRepoactive-Job: foo")

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.run")
    def test_no_output_no_existing_bookmark(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = mock_jj_cls.return_value
        mock_sub.return_value.stdout = ""
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = True

        result = run_job(job=_job("foo"), parents=["trunk()"], repo_path=REPO, platform=None)

        mock_jj.abandon.assert_called_once_with()
        mock_jj.bookmark_set.assert_not_called()
        mock_jj.bookmark_delete.assert_not_called()
        mock_jj.git_push_bookmarks.assert_not_called()
        assert result.produced_output is False
        assert result.effective_revsets == ["trunk()"]

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.run")
    def test_no_output_existing_bookmark_deleted(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = mock_jj_cls.return_value
        mock_sub.return_value.stdout = ""
        mock_jj.bookmark_exists.return_value = True
        mock_jj.is_empty.return_value = True

        result = run_job(job=_job("foo"), parents=["trunk()"], repo_path=REPO, platform=None)

        mock_jj.abandon.assert_called_once_with()
        mock_jj.bookmark_delete.assert_called_once_with("repoactive/foo")
        mock_jj.git_push_bookmarks.assert_called_once_with("repoactive/foo")
        mock_jj.bookmark_set.assert_not_called()
        assert result.produced_output is False
        assert result.effective_revsets == ["trunk()"]

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.run")
    def test_no_output_existing_bookmark_local_skips_push(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = mock_jj_cls.return_value
        mock_sub.return_value.stdout = ""
        mock_jj.bookmark_exists.return_value = True
        mock_jj.is_empty.return_value = True

        run_job(job=_job("foo"), parents=["trunk()"], repo_path=REPO, platform=None, local=True)

        mock_jj.bookmark_delete.assert_called_once_with("repoactive/foo")
        mock_jj.git_push_bookmarks.assert_not_called()

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.run")
    def test_no_output_effective_revsets_are_parents(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = mock_jj_cls.return_value
        mock_sub.return_value.stdout = ""
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = True

        result = run_job(
            job=_job("foo"),
            parents=["repoactive/a", "repoactive/b"],
            repo_path=REPO,
            platform=None,
        )

        assert result.effective_revsets == ["repoactive/a", "repoactive/b"]

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.run", side_effect=subprocess.CalledProcessError(1, "cmd"))
    def test_command_failure_abandons_and_raises(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = mock_jj_cls.return_value
        mock_jj.bookmark_exists.return_value = False
        with pytest.raises(RuntimeError, match="command failed"):
            run_job(
                job=_job("foo"),
                parents=["trunk()"],
                repo_path=REPO,
                platform=None,
            )

        mock_jj.abandon.assert_called_once_with()
        mock_jj.bookmark_set.assert_not_called()

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.run")
    def test_command_output_in_mr_description(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = mock_jj_cls.return_value
        mock_sub.return_value.stdout = "Copied file foo -> bar\n"
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False
        platform = MagicMock()
        platform.default_branch.return_value = "main"
        platform.ensure_mr.return_value = "https://gitlab.example.com/mr/1"

        run_job(
            job=_job("foo"),
            parents=["trunk()"],
            repo_path=REPO,
            platform=platform,
        )

        params = platform.ensure_mr.call_args[0][0]
        assert "```\n$ cmd-foo\nCopied file foo -> bar\n```" in params.description

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.run")
    def test_calls_platform_ensure_mr(self, mock_sub: MagicMock, mock_jj_cls: MagicMock) -> None:
        mock_jj = mock_jj_cls.return_value
        mock_sub.return_value.stdout = ""
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False
        platform = MagicMock()
        platform.default_branch.return_value = "main"
        platform.ensure_mr.return_value = "https://gitlab.example.com/mr/1"

        result = run_job(
            job=_job("foo"),
            parents=["trunk()"],
            repo_path=REPO,
            platform=platform,
        )

        platform.ensure_mr.assert_called_once()
        assert result.mr_url == "https://gitlab.example.com/mr/1"

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.run")
    def test_create_mr_false_skips_ensure_mr(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = mock_jj_cls.return_value
        mock_sub.return_value.stdout = ""
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False
        platform = MagicMock()
        job = Job(
            name="foo",
            command="cmd",
            title="Foo",
            create_mr=False,
            branch_prefix="repoactive/",
            commit_title_prefix="",
        )

        result = run_job(
            job=job,
            parents=["trunk()"],
            repo_path=REPO,
            platform=platform,
        )

        platform.ensure_mr.assert_not_called()
        assert result.mr_url is None
        assert result.produced_output is True

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.run")
    def test_local_skips_push_and_mr(self, mock_sub: MagicMock, mock_jj_cls: MagicMock) -> None:
        mock_jj = mock_jj_cls.return_value
        mock_sub.return_value.stdout = ""
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False
        platform = MagicMock()

        result = run_job(
            job=_job("foo"),
            parents=["trunk()"],
            repo_path=REPO,
            platform=platform,
            local=True,
        )

        mock_jj.git_push_bookmarks.assert_not_called()
        platform.ensure_mr.assert_not_called()
        assert result.produced_output is True
        assert result.effective_revsets == ["repoactive/foo"]

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.run")
    def test_local_no_output_skips_push(self, mock_sub: MagicMock, mock_jj_cls: MagicMock) -> None:
        mock_jj = mock_jj_cls.return_value
        mock_sub.return_value.stdout = ""
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = True

        result = run_job(
            job=_job("foo"),
            parents=["trunk()"],
            repo_path=REPO,
            platform=None,
            local=True,
        )

        mock_jj.abandon.assert_called_once_with()
        mock_jj.bookmark_set.assert_not_called()
        mock_jj.git_push_bookmarks.assert_not_called()
        assert result.produced_output is False

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.run")
    def test_existing_bookmark_uses_edit_restore_rebase(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = mock_jj_cls.return_value
        mock_sub.return_value.stdout = ""
        mock_jj.bookmark_exists.return_value = True
        mock_jj.is_empty.return_value = False

        run_job(job=_job("foo"), parents=["trunk()"], repo_path=REPO, platform=None)

        mock_jj.new.assert_not_called()
        mock_jj.edit.assert_called_once_with("repoactive/foo")
        mock_jj.restore.assert_called_once_with("repoactive/foo")
        mock_jj.rebase.assert_called_once_with("trunk()")

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.run")
    def test_existing_bookmark_multiple_parents_rebase(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = mock_jj_cls.return_value
        mock_sub.return_value.stdout = ""
        mock_jj.bookmark_exists.return_value = True
        mock_jj.is_empty.return_value = False

        run_job(
            job=_job("foo"),
            parents=["repoactive/a", "repoactive/b"],
            repo_path=REPO,
            platform=None,
        )

        mock_jj.new.assert_not_called()
        mock_jj.rebase.assert_called_once_with("repoactive/a", "repoactive/b")

    @patch("repoactive.runner.JJ")
    @patch("repoactive.runner.subprocess.run")
    def test_no_existing_bookmark_uses_new(
        self, mock_sub: MagicMock, mock_jj_cls: MagicMock
    ) -> None:
        mock_jj = mock_jj_cls.return_value
        mock_sub.return_value.stdout = ""
        mock_jj.bookmark_exists.return_value = False
        mock_jj.is_empty.return_value = False

        run_job(job=_job("foo"), parents=["trunk()"], repo_path=REPO, platform=None)

        mock_jj.new.assert_called_once_with("trunk()")
        mock_jj.edit.assert_not_called()
        mock_jj.restore.assert_not_called()
        mock_jj.rebase.assert_not_called()


class TestRunAll:
    @pytest.fixture(autouse=True)
    def mock_jj(self) -> Iterator[MagicMock]:
        """Stub the JJ class run_all constructs (unmerged_job_names + cooldown query)."""
        with patch("repoactive.runner.JJ") as cls:
            cls.return_value.unmerged_job_names.return_value = set()
            cls.return_value.has_recent_job_commit.return_value = False
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

    @patch("repoactive.runner.run_job")
    def test_local_forwarded_to_run_job(self, mock_run_job: MagicMock) -> None:
        a = _job("a")
        mock_run_job.return_value = _result(a, revsets=["repoactive/a"])

        run_all(config=_config(a), repo_path=REPO, local=True)

        assert mock_run_job.call_args.kwargs["local"] is True

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

        name, base, _since = mock_jj.return_value.has_recent_job_commit.call_args.args
        assert name == "a"
        assert base == "trunk()"

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
