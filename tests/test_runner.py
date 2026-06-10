import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from repoactive.config import Config, Defaults, Job
from repoactive.runner import (
    JobResult,
    _compute_parents,
    _mr_params,
    _propagate_disabled,
    _resolve_jobs,
    _topological_sort,
    run_all,
    run_job,
)


def _job(
    name: str,
    *,
    depends_on: list[str] | None = None,
    base_branch: str | None = None,
    description: str | None = None,
    labels: list[str] | None = None,
) -> Job:
    return Job(
        name=name,
        command=f"cmd-{name}",
        title=f"Change {name}",
        depends_on=depends_on or [],
        base_branch=base_branch,
        description=description,
        labels=labels or [],
    )


def _defaults(
    prefix: str = "repoactive/",
    labels: list[str] | None = None,
    mr_title_prefix: str = "",
    commit_title_prefix: str = "",
) -> Defaults:
    return Defaults(
        branch_prefix=prefix,
        labels=labels or [],
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


class TestResolveJobs:
    def test_all_when_no_filter(self) -> None:
        jobs = [_job("a"), _job("b")]
        assert _resolve_jobs(jobs, ["a", "b"]) == jobs

    def test_subset(self) -> None:
        a, b, c = _job("a"), _job("b"), _job("c")
        result = _resolve_jobs([a, b, c], ["a"])
        assert [x.name for x in result] == ["a"]

    def test_includes_transitive_deps(self) -> None:
        a = _job("a")
        b = _job("b", depends_on=["a"])
        c = _job("c", depends_on=["b"])
        result = {x.name for x in _resolve_jobs([a, b, c], ["c"])}
        assert result == {"a", "b", "c"}

    def test_unknown_name_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown job"):
            _resolve_jobs([_job("a")], ["nonexistent"])

    def test_multiple_unknown_names_in_error(self) -> None:
        with pytest.raises(ValueError, match="x"):
            _resolve_jobs([_job("a")], ["x", "y"])


class TestPropagateDisabled:
    def _job(
        self, name: str, *, disabled: bool = False, depends_on: list[str] | None = None
    ) -> Job:
        return Job(
            name=name, command="cmd", title=name, disabled=disabled, depends_on=depends_on or []
        )

    def test_no_disabled_jobs(self) -> None:
        jobs = [self._job("a"), self._job("b")]
        assert _propagate_disabled(jobs) == set()

    def test_explicitly_disabled(self) -> None:
        jobs = [self._job("a", disabled=True), self._job("b")]
        assert _propagate_disabled(jobs) == {"a"}

    def test_direct_dependent_disabled(self) -> None:
        jobs = [self._job("a", disabled=True), self._job("b", depends_on=["a"])]
        assert _propagate_disabled(jobs) == {"a", "b"}

    def test_transitive_propagation(self) -> None:
        jobs = [
            self._job("a", disabled=True),
            self._job("b", depends_on=["a"]),
            self._job("c", depends_on=["b"]),
        ]
        assert _propagate_disabled(jobs) == {"a", "b", "c"}

    def test_unrelated_job_not_disabled(self) -> None:
        jobs = [
            self._job("a", disabled=True),
            self._job("b", depends_on=["a"]),
            self._job("c"),
        ]
        assert _propagate_disabled(jobs) == {"a", "b"}

    def test_multiple_disabled_roots(self) -> None:
        jobs = [
            self._job("a", disabled=True),
            self._job("b", disabled=True),
            self._job("c", depends_on=["a"]),
            self._job("d", depends_on=["b"]),
        ]
        assert _propagate_disabled(jobs) == {"a", "b", "c", "d"}

    def test_diamond_propagation(self) -> None:
        jobs = [
            self._job("a", disabled=True),
            self._job("b", depends_on=["a"]),
            self._job("c", depends_on=["a"]),
            self._job("d", depends_on=["b", "c"]),
        ]
        assert _propagate_disabled(jobs) == {"a", "b", "c", "d"}

    def test_only_one_dep_disabled_propagates(self) -> None:
        jobs = [
            self._job("a", disabled=True),
            self._job("b"),
            self._job("c", depends_on=["a", "b"]),
        ]
        assert _propagate_disabled(jobs) == {"a", "c"}


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


_BM = "repoactive/x"
_BASE_BRANCH = "main"


class TestMrParams:
    def test_labels_merged(self) -> None:
        job = _job("x", labels=["feat"])
        params = _mr_params(
            job=job,
            defaults=_defaults(labels=["auto"]),
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
        )
        assert params.labels == ["auto", "feat"]

    def test_labels_deduplicated(self) -> None:
        job = _job("x", labels=["auto"])
        params = _mr_params(
            job=job,
            defaults=_defaults(labels=["auto"]),
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
        )
        assert params.labels == ["auto"]

    def test_description_falls_back_to_empty(self) -> None:
        params = _mr_params(
            job=_job("x"), defaults=_defaults(), bookmark=_BM, base_branch=_BASE_BRANCH
        )
        assert params.description == ""

    def test_description_used_when_set(self) -> None:
        job = _job("x", description="Details.")
        params = _mr_params(job=job, defaults=_defaults(), bookmark=_BM, base_branch=_BASE_BRANCH)
        assert params.description == "Details."

    def test_command_output_appended(self) -> None:
        params = _mr_params(
            job=_job("x"),
            defaults=_defaults(),
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
            command_output="some output",
        )
        assert params.description == "```\n$ cmd-x\nsome output\n```"

    def test_command_output_appended_after_description(self) -> None:
        params = _mr_params(
            job=_job("x", description="Details."),
            defaults=_defaults(),
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
            command_output="some output",
        )
        assert params.description == "Details.\n\n```\n$ cmd-x\nsome output\n```"

    def test_empty_command_output_not_appended(self) -> None:
        job = _job("x", description="Details.")
        params = _mr_params(
            job=job,
            defaults=_defaults(),
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
            command_output="",
        )
        assert params.description == "Details."

    def test_dep_outputs_included_before_own_output(self) -> None:
        dep_outputs = [("dep-cmd", "dep output")]
        params = _mr_params(
            job=_job("x"),
            defaults=_defaults(),
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
            command_output="own output",
            dep_outputs=dep_outputs,
        )
        assert params.description == "```\n$ dep-cmd\ndep output\n\n$ cmd-x\nown output\n```"

    def test_dep_outputs_without_own_output(self) -> None:
        dep_outputs = [("dep-cmd", "dep output")]
        params = _mr_params(
            job=_job("x"),
            defaults=_defaults(),
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
            command_output="",
            dep_outputs=dep_outputs,
        )
        assert params.description == "```\n$ dep-cmd\ndep output\n```"

    def test_dep_outputs_with_empty_output_skipped(self) -> None:
        dep_outputs = [("dep-cmd", ""), ("dep2-cmd", "dep2 output")]
        params = _mr_params(
            job=_job("x"),
            defaults=_defaults(),
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
            command_output="",
            dep_outputs=dep_outputs,
        )
        assert params.description == "```\n$ dep2-cmd\ndep2 output\n```"

    def test_title_prefix_applied(self) -> None:
        params = _mr_params(
            job=_job("x"),
            defaults=_defaults(mr_title_prefix="[bot] "),
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
        )
        assert params.title == "[bot] Change x"

    def test_empty_title_prefix(self) -> None:
        params = _mr_params(
            job=_job("x"),
            defaults=_defaults(mr_title_prefix=""),
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
        )
        assert params.title == "Change x"

    def test_draft_forwarded(self) -> None:
        job = Job(name="x", command="cmd", title="X", draft=True)
        params = _mr_params(job=job, defaults=_defaults(), bookmark=_BM, base_branch=_BASE_BRANCH)
        assert params.draft is True

    def test_dep_mr_urls_included(self) -> None:
        params = _mr_params(
            job=_job("x"),
            defaults=_defaults(),
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
            dep_mr_urls=[("Dep A", "https://example.com/mr/1")],
        )
        assert params.description == "Depends on:\n- [Dep A](https://example.com/mr/1)"

    def test_dep_mr_urls_multiple(self) -> None:
        params = _mr_params(
            job=_job("x"),
            defaults=_defaults(),
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
            defaults=_defaults(),
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
            defaults=_defaults(),
            bookmark=_BM,
            base_branch=_BASE_BRANCH,
            command_output="some output",
            dep_mr_urls=[("Dep A", "https://example.com/mr/1")],
        )
        assert params.description == (
            "Depends on:\n- [Dep A](https://example.com/mr/1)\n\n```\n$ cmd-x\nsome output\n```"
        )


class TestRunJob:
    @patch("repoactive.runner.jj")
    @patch("repoactive.runner.subprocess.run")
    def test_produces_output(self, mock_sub: MagicMock, mock_jj: MagicMock) -> None:
        mock_sub.return_value.stdout = ""
        mock_jj.is_empty.return_value = False
        job = _job("foo")

        result = run_job(
            job=job, defaults=_defaults(), parents=["trunk()"], repo_path=REPO, platform=None
        )

        mock_jj.new.assert_called_once_with("trunk()", cwd=REPO)
        mock_jj.bookmark_set.assert_called_once_with("repoactive/foo", cwd=REPO)
        mock_jj.describe.assert_called_once_with("Change foo", cwd=REPO)
        mock_jj.git_push.assert_called_once_with("repoactive/foo", cwd=REPO)
        mock_jj.abandon.assert_not_called()
        assert result.produced_output is True
        assert result.effective_revsets == ["repoactive/foo"]

    @patch("repoactive.runner.jj")
    @patch("repoactive.runner.subprocess.run")
    def test_describe_includes_body(self, mock_sub: MagicMock, mock_jj: MagicMock) -> None:
        mock_sub.return_value.stdout = ""
        mock_jj.is_empty.return_value = False
        job = _job("foo", description="Body text.")

        run_job(job=job, defaults=_defaults(), parents=["trunk()"], repo_path=REPO, platform=None)

        mock_jj.describe.assert_called_once_with("Change foo\n\nBody text.", cwd=REPO)

    @patch("repoactive.runner.jj")
    @patch("repoactive.runner.subprocess.run")
    def test_output_appended_to_commit_message(
        self, mock_sub: MagicMock, mock_jj: MagicMock
    ) -> None:
        mock_sub.return_value.stdout = "did stuff\n"
        mock_jj.is_empty.return_value = False
        job = _job("foo")

        run_job(job=job, defaults=_defaults(), parents=["trunk()"], repo_path=REPO, platform=None)

        mock_jj.describe.assert_called_once_with(
            "Change foo\n\n  $ cmd-foo\n  did stuff", cwd=REPO
        )

    @patch("repoactive.runner.jj")
    @patch("repoactive.runner.subprocess.run")
    def test_output_in_commit_false_suppresses_output(
        self, mock_sub: MagicMock, mock_jj: MagicMock
    ) -> None:
        mock_sub.return_value.stdout = "did stuff\n"
        mock_jj.is_empty.return_value = False
        job = Job(name="foo", command="cmd-foo", title="Change foo", output_in_commit=False)

        run_job(job=job, defaults=_defaults(), parents=["trunk()"], repo_path=REPO, platform=None)

        mock_jj.describe.assert_called_once_with("Change foo", cwd=REPO)

    @patch("repoactive.runner.jj")
    @patch("repoactive.runner.subprocess.run")
    def test_commit_title_prefix_applied(self, mock_sub: MagicMock, mock_jj: MagicMock) -> None:
        mock_sub.return_value.stdout = ""
        mock_jj.is_empty.return_value = False

        defaults = _defaults(commit_title_prefix="[bot] ")
        run_job(
            job=_job("foo"),
            defaults=defaults,
            parents=["trunk()"],
            repo_path=REPO,
            platform=None,
        )

        mock_jj.describe.assert_called_once_with("[bot] Change foo", cwd=REPO)

    @patch("repoactive.runner.jj")
    @patch("repoactive.runner.subprocess.run")
    def test_no_output_single_parent_sets_bookmark_to_parent(
        self, mock_sub: MagicMock, mock_jj: MagicMock
    ) -> None:
        mock_sub.return_value.stdout = ""
        mock_jj.is_empty.return_value = True
        job = _job("foo")

        result = run_job(
            job=job, defaults=_defaults(), parents=["trunk()"], repo_path=REPO, platform=None
        )

        mock_jj.abandon.assert_called_once_with(cwd=REPO)
        mock_jj.bookmark_set.assert_called_once_with(
            "repoactive/foo", revision="trunk()", cwd=REPO
        )
        mock_jj.git_push.assert_called_once_with("repoactive/foo", cwd=REPO)
        assert result.produced_output is False
        assert result.effective_revsets == ["repoactive/foo"]

    @patch("repoactive.runner.jj")
    @patch("repoactive.runner.subprocess.run")
    def test_no_output_multiple_parents_sets_bookmark_to_merge_commit(
        self, mock_sub: MagicMock, mock_jj: MagicMock
    ) -> None:
        mock_sub.return_value.stdout = ""
        mock_jj.is_empty.return_value = True
        job = _job("foo")

        result = run_job(
            job=job,
            defaults=_defaults(),
            parents=["repoactive/a", "repoactive/b"],
            repo_path=REPO,
            platform=None,
        )

        mock_jj.abandon.assert_not_called()
        mock_jj.bookmark_set.assert_called_once_with("repoactive/foo", cwd=REPO)
        mock_jj.git_push.assert_called_once_with("repoactive/foo", cwd=REPO)
        assert result.produced_output is False
        assert result.effective_revsets == ["repoactive/foo"]

    @patch("repoactive.runner.jj")
    @patch("repoactive.runner.subprocess.run", side_effect=subprocess.CalledProcessError(1, "cmd"))
    def test_command_failure_abandons_and_raises(
        self, mock_sub: MagicMock, mock_jj: MagicMock
    ) -> None:
        with pytest.raises(RuntimeError, match="command failed"):
            run_job(
                job=_job("foo"),
                defaults=_defaults(),
                parents=["trunk()"],
                repo_path=REPO,
                platform=None,
            )

        mock_jj.abandon.assert_called_once_with(cwd=REPO)
        mock_jj.bookmark_set.assert_not_called()

    @patch("repoactive.runner.jj")
    @patch("repoactive.runner.subprocess.run")
    def test_command_output_in_mr_description(
        self, mock_sub: MagicMock, mock_jj: MagicMock
    ) -> None:
        mock_sub.return_value.stdout = "Copied file foo -> bar\n"
        mock_jj.is_empty.return_value = False
        platform = MagicMock()
        platform.default_branch.return_value = "main"
        platform.ensure_mr.return_value = "https://gitlab.example.com/mr/1"

        run_job(
            job=_job("foo"),
            defaults=_defaults(),
            parents=["trunk()"],
            repo_path=REPO,
            platform=platform,
        )

        params = platform.ensure_mr.call_args[0][0]
        assert "```\n$ cmd-foo\nCopied file foo -> bar\n```" in params.description

    @patch("repoactive.runner.jj")
    @patch("repoactive.runner.subprocess.run")
    def test_calls_platform_ensure_mr(self, mock_sub: MagicMock, mock_jj: MagicMock) -> None:
        mock_sub.return_value.stdout = ""
        mock_jj.is_empty.return_value = False
        platform = MagicMock()
        platform.default_branch.return_value = "main"
        platform.ensure_mr.return_value = "https://gitlab.example.com/mr/1"

        result = run_job(
            job=_job("foo"),
            defaults=_defaults(),
            parents=["trunk()"],
            repo_path=REPO,
            platform=platform,
        )

        platform.ensure_mr.assert_called_once()
        assert result.mr_url == "https://gitlab.example.com/mr/1"

    @patch("repoactive.runner.jj")
    @patch("repoactive.runner.subprocess.run")
    def test_create_mr_false_skips_ensure_mr(
        self, mock_sub: MagicMock, mock_jj: MagicMock
    ) -> None:
        mock_sub.return_value.stdout = ""
        mock_jj.is_empty.return_value = False
        platform = MagicMock()
        job = Job(name="foo", command="cmd", title="Foo", create_mr=False)

        result = run_job(
            job=job,
            defaults=_defaults(),
            parents=["trunk()"],
            repo_path=REPO,
            platform=platform,
        )

        platform.ensure_mr.assert_not_called()
        assert result.mr_url is None
        assert result.produced_output is True

    @patch("repoactive.runner.jj")
    @patch("repoactive.runner.subprocess.run")
    def test_local_skips_push_and_mr(self, mock_sub: MagicMock, mock_jj: MagicMock) -> None:
        mock_sub.return_value.stdout = ""
        mock_jj.is_empty.return_value = False
        platform = MagicMock()

        result = run_job(
            job=_job("foo"),
            defaults=_defaults(),
            parents=["trunk()"],
            repo_path=REPO,
            platform=platform,
            local=True,
        )

        mock_jj.git_push.assert_not_called()
        platform.ensure_mr.assert_not_called()
        assert result.produced_output is True
        assert result.effective_revsets == ["repoactive/foo"]

    @patch("repoactive.runner.jj")
    @patch("repoactive.runner.subprocess.run")
    def test_local_no_output_skips_push(self, mock_sub: MagicMock, mock_jj: MagicMock) -> None:
        mock_sub.return_value.stdout = ""
        mock_jj.is_empty.return_value = True

        result = run_job(
            job=_job("foo"),
            defaults=_defaults(),
            parents=["trunk()"],
            repo_path=REPO,
            platform=None,
            local=True,
        )

        mock_jj.abandon.assert_called_once_with(cwd=REPO)
        mock_jj.bookmark_set.assert_called_once_with(
            "repoactive/foo", revision="trunk()", cwd=REPO
        )
        mock_jj.git_push.assert_not_called()
        assert result.produced_output is False


class TestRunAll:
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

    def test_requesting_disabled_job_raises(self) -> None:
        a = Job(name="a", command="cmd", title="A", disabled=True)
        b = _job("b")
        with pytest.raises(ValueError, match="Cannot run disabled job"):
            run_all(config=_config(a, b), repo_path=REPO, requested_jobs=["a"])

    def test_requesting_transitively_disabled_job_raises(self) -> None:
        a = Job(name="a", command="cmd", title="A", disabled=True)
        b = _job("b", depends_on=["a"])
        with pytest.raises(ValueError, match="Cannot run disabled job"):
            run_all(config=_config(a, b), repo_path=REPO, requested_jobs=["b"])
