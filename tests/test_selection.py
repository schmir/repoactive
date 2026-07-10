"""Tests for job selection: filtering, dependency inclusion, and stack expansion."""

from unittest.mock import MagicMock

import pytest

from repoactive.config import Job
from repoactive.selection import (
    JobSelection,
    JobSelector,
    UnknownJobsError,
    UnknownTagsError,
    _expand_successors,
    _select_jobs,
)
from tests.builders import _config, _djob, _job, _names


class TestSelectJobs:
    def test_no_filter_returns_all(self) -> None:
        jobs = _config(_job("a"), _job("b")).jobs
        assert _names(_select_jobs(jobs=jobs, requested_names=frozenset())) == ["a", "b"]

    def test_requested_subset(self) -> None:
        config = _config(_job("a"), _job("b"), _job("c"))
        assert _names(_select_jobs(jobs=config.jobs, requested_names=frozenset({"a"}))) == ["a"]

    def test_requested_includes_transitive_deps(self) -> None:
        config = _config(_job("a"), _job("b", depends_on=["a"]), _job("c", depends_on=["b"]))
        assert _names(_select_jobs(jobs=config.jobs, requested_names=frozenset({"c"}))) == [
            "a",
            "b",
            "c",
        ]

    def test_implicit_enabled_and_disabled_tags_are_known(self) -> None:
        # effective tags include the implicit enabled/disabled, so requesting
        # them is valid even though no job lists them explicitly.
        jobs = _config(_djob("a"), _djob("b", disabled=True)).jobs
        assert _names(
            _select_jobs(
                jobs=jobs, requested_names=frozenset(), requested_tags=frozenset({"enabled"})
            )
        ) == ["a"]
        assert _names(
            _select_jobs(
                jobs=jobs, requested_names=frozenset(), requested_tags=frozenset({"disabled"})
            )
        ) == ["b"]

    def test_no_disabled_jobs(self) -> None:
        config = _config(_djob("a"), _djob("b"))
        assert _names(_select_jobs(jobs=config.jobs, requested_names=frozenset())) == ["a", "b"]

    def test_explicitly_disabled_excluded(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b"))
        assert _names(_select_jobs(jobs=config.jobs, requested_names=frozenset())) == ["b"]

    def test_direct_dependent_excluded(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b", depends_on=["a"]))
        assert _names(_select_jobs(jobs=config.jobs, requested_names=frozenset())) == []

    def test_transitive_propagation(self) -> None:
        config = _config(
            _djob("a", disabled=True),
            _djob("b", depends_on=["a"]),
            _djob("c", depends_on=["b"]),
        )
        assert _names(_select_jobs(jobs=config.jobs, requested_names=frozenset())) == []

    def test_unrelated_job_not_excluded(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b", depends_on=["a"]), _djob("c"))
        assert _names(_select_jobs(jobs=config.jobs, requested_names=frozenset())) == ["c"]

    def test_multiple_disabled_roots(self) -> None:
        config = _config(
            _djob("a", disabled=True),
            _djob("b", disabled=True),
            _djob("c", depends_on=["a"]),
            _djob("d", depends_on=["b"]),
        )
        assert _names(_select_jobs(jobs=config.jobs, requested_names=frozenset())) == []

    def test_diamond_propagation(self) -> None:
        config = _config(
            _djob("a", disabled=True),
            _djob("b", depends_on=["a"]),
            _djob("c", depends_on=["a"]),
            _djob("d", depends_on=["b", "c"]),
        )
        assert _names(_select_jobs(jobs=config.jobs, requested_names=frozenset())) == []

    def test_only_one_dep_disabled(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b"), _djob("c", depends_on=["a", "b"]))
        assert _names(_select_jobs(jobs=config.jobs, requested_names=frozenset())) == ["b"]

    def test_disabled_job_depends_on_disabled_job(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b", disabled=True, depends_on=["a"]))
        assert _names(_select_jobs(jobs=config.jobs, requested_names=frozenset())) == []

    def test_requesting_disabled_job_runs_it(self) -> None:
        config = _config(_djob("a", disabled=True))
        assert _names(_select_jobs(jobs=config.jobs, requested_names=frozenset({"a"}))) == ["a"]

    def test_requesting_job_pulls_in_disabled_dependency(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b", depends_on=["a"]))
        assert _names(_select_jobs(jobs=config.jobs, requested_names=frozenset({"b"}))) == [
            "a",
            "b",
        ]

    def test_tagged_job_excluded_from_default_run(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        assert _names(_select_jobs(jobs=config.jobs, requested_names=frozenset())) == ["a"]

    def test_tag_selects_matching_jobs(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"]), _djob("c", tags=["weekly"]))
        assert _names(
            _select_jobs(
                jobs=config.jobs, requested_names=frozenset(), requested_tags=frozenset({"weekly"})
            )
        ) == ["b", "c"]

    def test_tag_does_not_imply_enabled(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        assert _names(
            _select_jobs(
                jobs=config.jobs, requested_names=frozenset(), requested_tags=frozenset({"weekly"})
            )
        ) == ["b"]

    def test_explicit_enabled_tag_keeps_job_in_both(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["enabled", "weekly"]))
        assert _names(_select_jobs(jobs=config.jobs, requested_names=frozenset())) == ["a", "b"]
        assert _names(
            _select_jobs(
                jobs=config.jobs, requested_names=frozenset(), requested_tags=frozenset({"weekly"})
            )
        ) == ["b"]

    def test_multiple_tags_are_ored(self) -> None:
        config = _config(
            _djob("a", tags=["weekly"]), _djob("b", tags=["monthly"]), _djob("c", tags=["daily"])
        )
        assert _names(
            _select_jobs(
                jobs=config.jobs,
                requested_names=frozenset(),
                requested_tags=frozenset({"weekly", "monthly"}),
            )
        ) == ["a", "b"]

    def test_tag_selection_overrides_disabled(self) -> None:
        # disabled is sugar for the 'disabled' tag, so --tag disabled runs them.
        config = _config(_djob("a", disabled=True), _djob("b"))
        assert _names(
            _select_jobs(
                jobs=config.jobs,
                requested_names=frozenset(),
                requested_tags=frozenset({"disabled"}),
            )
        ) == ["a"]

    def test_names_and_tags_are_unioned(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"]), _djob("c"))
        assert _names(
            _select_jobs(
                jobs=config.jobs,
                requested_names=frozenset({"a"}),
                requested_tags=frozenset({"weekly"}),
            )
        ) == ["a", "b"]

    def test_tag_selection_force_includes_dependencies(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"], depends_on=["a"]))
        assert _names(
            _select_jobs(
                jobs=config.jobs, requested_names=frozenset(), requested_tags=frozenset({"weekly"})
            )
        ) == ["a", "b"]

    def test_tagged_dependency_dropped_from_default_run(self) -> None:
        # b is out of the default run (tagged weekly); its dependent c is dropped too.
        config = _config(_djob("a"), _djob("b", tags=["weekly"]), _djob("c", depends_on=["b"]))
        assert _names(_select_jobs(jobs=config.jobs, requested_names=frozenset())) == ["a"]

    def test_refresh_job_pulled_into_default_run(self) -> None:
        # A weekly job with an unmerged branch is refreshed by the default run.
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        assert _names(
            _select_jobs(
                jobs=config.jobs, requested_names=frozenset(), refresh_names=frozenset({"b"})
            )
        ) == ["a", "b"]

    def test_refresh_includes_dependencies(self) -> None:
        config = _config(
            _djob("a", tags=["weekly"]), _djob("b", tags=["weekly"], depends_on=["a"])
        )
        assert _names(
            _select_jobs(
                jobs=config.jobs, requested_names=frozenset(), refresh_names=frozenset({"b"})
            )
        ) == ["a", "b"]

    def test_refresh_includes_disabled_job(self) -> None:
        # An unmerged branch for a disabled job (likely from an explicit run) is refreshed.
        config = _config(_djob("a"), _djob("b", disabled=True))
        assert _names(
            _select_jobs(
                jobs=config.jobs, requested_names=frozenset(), refresh_names=frozenset({"b"})
            )
        ) == ["a", "b"]

    def test_refresh_ignores_unknown_names(self) -> None:
        # A trailer for a removed/renamed job must not blow up selection.
        config = _config(_djob("a"))
        assert _names(
            _select_jobs(
                jobs=config.jobs, requested_names=frozenset(), refresh_names=frozenset({"gone"})
            )
        ) == ["a"]


def _mock_repo(unmerged: set[str] | None = None) -> MagicMock:
    """Return a JJ stub differentiating unmerged-branch refresh (no revset) from successor expansion (with revset)."""
    repo = MagicMock()
    unmerged_set = unmerged or set()

    def _side_effect(revset: str | None = None) -> set[str]:
        return set() if revset is not None else unmerged_set

    repo.pending_job_names.side_effect = _side_effect
    return repo


def _mock_repo_with_successors(successors: set[str]) -> MagicMock:
    """Return a JJ stub whose successor expansion (revset call) returns the given set."""
    repo = MagicMock()

    def _side_effect(revset: str | None = None) -> set[str]:
        return successors if revset is not None else set()

    repo.pending_job_names.side_effect = _side_effect
    return repo


class TestExpandSuccessors:
    def _sel(self, jobs: list[Job]) -> JobSelection:
        return JobSelection(jobs=jobs, refreshed=frozenset())

    def test_no_successors_returns_selected_unchanged(self) -> None:
        config = _config(_djob("a"), _djob("b"))
        selected = _select_jobs(jobs=config.jobs, requested_names=frozenset({"a"}))
        repo = _mock_repo_with_successors(set())
        result = _expand_successors(selection=self._sel(selected), config=config, repo=repo)
        assert _names(result.jobs) == ["a"]

    def test_direct_successor_is_added(self) -> None:
        # b's last commit sits on a's bookmark, so b is pulled in.
        config = _config(_djob("a"), _djob("b"))
        selected = _select_jobs(jobs=config.jobs, requested_names=frozenset({"a"}))
        repo = _mock_repo_with_successors({"b"})
        result = _expand_successors(selection=self._sel(selected), config=config, repo=repo)
        assert _names(result.jobs) == ["a", "b"]

    def test_successor_recorded_separately_from_refreshed(self) -> None:
        # Successors are tracked in their own subset: they bypass their own
        # cooldown at dispatch time, but unlike refresh jobs they are skipped
        # when nothing below them in the stack ran (see _dispatch_job).
        config = _config(_djob("a"), _djob("b"))
        selected = _select_jobs(jobs=config.jobs, requested_names=frozenset({"a"}))
        repo = _mock_repo_with_successors({"b"})
        result = _expand_successors(selection=self._sel(selected), config=config, repo=repo)
        assert result.successors == frozenset({"b"})
        assert result.refreshed == frozenset()

    def test_refreshed_subset_is_preserved(self) -> None:
        # A default run's refresh subset must survive successor expansion:
        # refresh jobs bypass cooldown unconditionally, successors do not.
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        selected = _select_jobs(
            jobs=config.jobs, requested_names=frozenset(), refresh_names=frozenset({"a"})
        )
        repo = _mock_repo_with_successors({"b"})
        result = _expand_successors(
            selection=JobSelection(jobs=selected, refreshed=frozenset({"a"})),
            config=config,
            repo=repo,
        )
        assert result.refreshed == frozenset({"a"})
        assert result.successors == frozenset({"b"})

    def test_deep_stack_expanded_in_one_query(self) -> None:
        # descendants() finds b and c above a's bookmark in a single call.
        config = _config(_djob("a"), _djob("b"), _djob("c"))
        selected = _select_jobs(jobs=config.jobs, requested_names=frozenset({"a"}))
        repo = _mock_repo_with_successors({"b", "c"})
        result = _expand_successors(selection=self._sel(selected), config=config, repo=repo)
        assert _names(result.jobs) == ["a", "b", "c"]
        repo.pending_job_names.assert_called_once()

    def test_unknown_successor_is_ignored(self) -> None:
        # pending_job_names may return names not present in config (e.g. removed jobs).
        config = _config(_djob("a"))
        selected = _select_jobs(jobs=config.jobs, requested_names=frozenset({"a"}))
        repo = _mock_repo_with_successors({"gone"})
        result = _expand_successors(selection=self._sel(selected), config=config, repo=repo)
        assert _names(result.jobs) == ["a"]

    def test_already_selected_successor_is_a_noop(self) -> None:
        config = _config(_djob("a"), _djob("b"))
        selected = _select_jobs(jobs=config.jobs, requested_names=frozenset({"a", "b"}))
        repo = _mock_repo_with_successors({"b"})
        result = _expand_successors(selection=self._sel(selected), config=config, repo=repo)
        assert _names(result.jobs) == ["a", "b"]

    def test_bookmarks_passed_as_revset(self) -> None:
        config = _config(_djob("a"), _djob("b"))
        selected = _select_jobs(jobs=config.jobs, requested_names=frozenset({"a", "b"}))
        repo = _mock_repo_with_successors(set())
        _expand_successors(selection=self._sel(selected), config=config, repo=repo)
        [call_args] = repo.pending_job_names.call_args_list
        revset = call_args.kwargs["revset"]
        assert "present(repoactive/a)" in revset
        assert "present(repoactive/b)" in revset

    def test_successor_pulls_in_its_dependencies(self) -> None:
        # c depends on b; when c is a successor of a, b must be included too.
        config = _config(_djob("a"), _djob("b"), _djob("c", depends_on=["b"]))
        selected = _select_jobs(jobs=config.jobs, requested_names=frozenset({"a"}))
        repo = _mock_repo_with_successors({"c"})
        result = _expand_successors(selection=self._sel(selected), config=config, repo=repo)
        assert _names(result.jobs) == ["a", "b", "c"]


class TestJobSelector:
    def test_bare_run_returns_default_jobs(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        repo = _mock_repo()
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(repo)
        assert _names(result.jobs) == ["a"]

    def test_bare_run_refreshes_unmerged_branches(self) -> None:
        # A weekly job (out of the default run) with an unmerged branch is pulled in.
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        repo = _mock_repo({"b"})
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(repo)
        assert _names(result.jobs) == ["a", "b"]
        # The refreshed subset is reported so the run can bypass cooldown for it.
        assert result.refreshed == frozenset({"b"})

    def test_bare_run_ignores_unmerged_names_not_in_config(self) -> None:
        # A trailer for a removed/renamed job must not affect selection.
        config = _config(_djob("a"))
        repo = _mock_repo({"gone"})
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(repo)
        assert _names(result.jobs) == ["a"]

    def test_requested_jobs_skip_unmerged_query(self) -> None:
        # Explicit selection does not consult unmerged branches.
        config = _config(_djob("a"), _djob("b"))
        repo = _mock_repo({"a"})
        result = JobSelector(
            config=config, requested_names=frozenset({"b"}), requested_tags=frozenset()
        ).select_run_jobs(repo)
        assert _names(result.jobs) == ["b"]
        assert result.refreshed == frozenset()
        # Unmerged branch refresh was skipped — only successor expansion calls (with revset) were made.
        assert all(
            c.kwargs.get("revset") is not None for c in repo.pending_job_names.call_args_list
        )

    def test_requested_tags_skip_unmerged_query(self) -> None:
        config = _config(_djob("a", tags=["weekly"]), _djob("b"))
        repo = _mock_repo({"b"})
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset({"weekly"})
        ).select_run_jobs(repo)
        assert _names(result.jobs) == ["a"]
        assert all(
            c.kwargs.get("revset") is not None for c in repo.pending_job_names.call_args_list
        )

    def test_unknown_requested_job_raises(self) -> None:
        # The request is validated at construction, before any repo work.
        config = _config(_djob("a"))
        with pytest.raises(UnknownJobsError, match="unknown job"):
            JobSelector(
                config=config, requested_names=frozenset({"nope"}), requested_tags=frozenset()
            )

    def test_unknown_requested_tag_raises(self) -> None:
        config = _config(_djob("a", tags=["weekly"]))
        with pytest.raises(UnknownTagsError, match="unknown tag"):
            JobSelector(
                config=config, requested_names=frozenset(), requested_tags=frozenset({"weekley"})
            )
