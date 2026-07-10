"""Tests for job selection: filtering, dependency inclusion, and stack expansion."""

from unittest.mock import MagicMock

import pytest

from repoactive.selection import JobSelector, UnknownJobsError, UnknownTagsError
from tests.builders import _config, _djob, _job, _names


def _mock_repo(unmerged: set[str] | None = None, successors: set[str] | None = None) -> MagicMock:
    """JJ stub: the no-revset call yields the unmerged-branch refresh set, a revset call the successors."""
    repo = MagicMock()
    unmerged_set = unmerged or set()
    successor_set = successors or set()

    def _side_effect(revset: str | None = None) -> set[str]:
        return successor_set if revset is not None else unmerged_set

    repo.pending_job_names.side_effect = _side_effect
    return repo


class TestSelectJobs:
    def test_no_filter_returns_all(self) -> None:
        config = _config(_job("a"), _job("b"))
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == ["a", "b"]

    def test_requested_subset(self) -> None:
        config = _config(_job("a"), _job("b"), _job("c"))
        result = JobSelector(
            config=config, requested_names=frozenset({"a"}), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == ["a"]

    def test_requested_includes_transitive_deps(self) -> None:
        config = _config(_job("a"), _job("b", depends_on=["a"]), _job("c", depends_on=["b"]))
        result = JobSelector(
            config=config, requested_names=frozenset({"c"}), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == ["a", "b", "c"]

    def test_implicit_enabled_and_disabled_tags_are_known(self) -> None:
        # effective tags include the implicit enabled/disabled, so requesting
        # them is valid even though no job lists them explicitly.
        config = _config(_djob("a"), _djob("b", disabled=True))
        enabled = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset({"enabled"})
        ).select_run_jobs(_mock_repo())
        assert _names(enabled.jobs) == ["a"]
        disabled = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset({"disabled"})
        ).select_run_jobs(_mock_repo())
        assert _names(disabled.jobs) == ["b"]

    def test_no_disabled_jobs(self) -> None:
        config = _config(_djob("a"), _djob("b"))
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == ["a", "b"]

    def test_explicitly_disabled_excluded(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b"))
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == ["b"]

    def test_direct_dependent_excluded(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b", depends_on=["a"]))
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == []

    def test_transitive_propagation(self) -> None:
        config = _config(
            _djob("a", disabled=True),
            _djob("b", depends_on=["a"]),
            _djob("c", depends_on=["b"]),
        )
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == []

    def test_unrelated_job_not_excluded(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b", depends_on=["a"]), _djob("c"))
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == ["c"]

    def test_multiple_disabled_roots(self) -> None:
        config = _config(
            _djob("a", disabled=True),
            _djob("b", disabled=True),
            _djob("c", depends_on=["a"]),
            _djob("d", depends_on=["b"]),
        )
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == []

    def test_diamond_propagation(self) -> None:
        config = _config(
            _djob("a", disabled=True),
            _djob("b", depends_on=["a"]),
            _djob("c", depends_on=["a"]),
            _djob("d", depends_on=["b", "c"]),
        )
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == []

    def test_only_one_dep_disabled(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b"), _djob("c", depends_on=["a", "b"]))
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == ["b"]

    def test_disabled_job_depends_on_disabled_job(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b", disabled=True, depends_on=["a"]))
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == []

    def test_requesting_disabled_job_runs_it(self) -> None:
        config = _config(_djob("a", disabled=True))
        result = JobSelector(
            config=config, requested_names=frozenset({"a"}), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == ["a"]

    def test_requesting_job_pulls_in_disabled_dependency(self) -> None:
        config = _config(_djob("a", disabled=True), _djob("b", depends_on=["a"]))
        result = JobSelector(
            config=config, requested_names=frozenset({"b"}), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == ["a", "b"]

    def test_tagged_job_excluded_from_default_run(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == ["a"]

    def test_tag_selects_matching_jobs(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"]), _djob("c", tags=["weekly"]))
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset({"weekly"})
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == ["b", "c"]

    def test_tag_does_not_imply_enabled(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset({"weekly"})
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == ["b"]

    def test_explicit_enabled_tag_keeps_job_in_both(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["enabled", "weekly"]))
        default = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo())
        assert _names(default.jobs) == ["a", "b"]
        weekly = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset({"weekly"})
        ).select_run_jobs(_mock_repo())
        assert _names(weekly.jobs) == ["b"]

    def test_multiple_tags_are_ored(self) -> None:
        config = _config(
            _djob("a", tags=["weekly"]), _djob("b", tags=["monthly"]), _djob("c", tags=["daily"])
        )
        result = JobSelector(
            config=config,
            requested_names=frozenset(),
            requested_tags=frozenset({"weekly", "monthly"}),
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == ["a", "b"]

    def test_tag_selection_overrides_disabled(self) -> None:
        # disabled is sugar for the 'disabled' tag, so --tag disabled runs them.
        config = _config(_djob("a", disabled=True), _djob("b"))
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset({"disabled"})
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == ["a"]

    def test_names_and_tags_are_unioned(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"]), _djob("c"))
        result = JobSelector(
            config=config, requested_names=frozenset({"a"}), requested_tags=frozenset({"weekly"})
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == ["a", "b"]

    def test_tag_selection_force_includes_dependencies(self) -> None:
        config = _config(_djob("a"), _djob("b", tags=["weekly"], depends_on=["a"]))
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset({"weekly"})
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == ["a", "b"]

    def test_tagged_dependency_dropped_from_default_run(self) -> None:
        # b is out of the default run (tagged weekly); its dependent c is dropped too.
        config = _config(_djob("a"), _djob("b", tags=["weekly"]), _djob("c", depends_on=["b"]))
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == ["a"]

    def test_refresh_job_pulled_into_default_run(self) -> None:
        # A weekly job with an unmerged branch is refreshed by the default run.
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo(unmerged={"b"}))
        assert _names(result.jobs) == ["a", "b"]
        # The refreshed subset is reported so the run can bypass cooldown for it.
        assert result.refreshed == frozenset({"b"})

    def test_refresh_includes_dependencies(self) -> None:
        config = _config(
            _djob("a", tags=["weekly"]), _djob("b", tags=["weekly"], depends_on=["a"])
        )
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo(unmerged={"b"}))
        assert _names(result.jobs) == ["a", "b"]

    def test_refresh_includes_disabled_job(self) -> None:
        # An unmerged branch for a disabled job (likely from an explicit run) is refreshed.
        config = _config(_djob("a"), _djob("b", disabled=True))
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo(unmerged={"b"}))
        assert _names(result.jobs) == ["a", "b"]

    def test_refresh_ignores_unknown_names(self) -> None:
        # A trailer for a removed/renamed job must not blow up selection.
        config = _config(_djob("a"))
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo(unmerged={"gone"}))
        assert _names(result.jobs) == ["a"]


class TestExpandSuccessors:
    def test_no_successors_returns_selected_unchanged(self) -> None:
        config = _config(_djob("a"), _djob("b"))
        result = JobSelector(
            config=config, requested_names=frozenset({"a"}), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo())
        assert _names(result.jobs) == ["a"]

    def test_direct_successor_is_added(self) -> None:
        # b's last commit sits on a's bookmark, so b is pulled in.
        config = _config(_djob("a"), _djob("b"))
        result = JobSelector(
            config=config, requested_names=frozenset({"a"}), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo(successors={"b"}))
        assert _names(result.jobs) == ["a", "b"]

    def test_successor_recorded_separately_from_refreshed(self) -> None:
        # Successors are tracked in their own subset: they bypass their own
        # cooldown at dispatch time, but unlike refresh jobs they are skipped
        # when nothing below them in the stack ran (see _dispatch_job).
        config = _config(_djob("a"), _djob("b"))
        result = JobSelector(
            config=config, requested_names=frozenset({"a"}), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo(successors={"b"}))
        assert result.successors == frozenset({"b"})
        assert result.refreshed == frozenset()

    def test_refreshed_subset_is_preserved(self) -> None:
        # A default run's refresh subset must survive successor expansion:
        # refresh jobs bypass cooldown unconditionally, successors do not.
        config = _config(_djob("a"), _djob("b", tags=["weekly"]))
        result = JobSelector(
            config=config, requested_names=frozenset(), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo(unmerged={"a"}, successors={"b"}))
        assert result.refreshed == frozenset({"a"})
        assert result.successors == frozenset({"b"})

    def test_deep_stack_expanded_in_one_query(self) -> None:
        # descendants() finds b and c above a's bookmark in a single call.
        config = _config(_djob("a"), _djob("b"), _djob("c"))
        repo = _mock_repo(successors={"b", "c"})
        result = JobSelector(
            config=config, requested_names=frozenset({"a"}), requested_tags=frozenset()
        ).select_run_jobs(repo)
        assert _names(result.jobs) == ["a", "b", "c"]
        repo.pending_job_names.assert_called_once()

    def test_unknown_successor_is_ignored(self) -> None:
        # pending_job_names may return names not present in config (e.g. removed jobs).
        config = _config(_djob("a"))
        result = JobSelector(
            config=config, requested_names=frozenset({"a"}), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo(successors={"gone"}))
        assert _names(result.jobs) == ["a"]

    def test_already_selected_successor_is_a_noop(self) -> None:
        config = _config(_djob("a"), _djob("b"))
        result = JobSelector(
            config=config, requested_names=frozenset({"a", "b"}), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo(successors={"b"}))
        assert _names(result.jobs) == ["a", "b"]

    def test_bookmarks_passed_as_revset(self) -> None:
        config = _config(_djob("a"), _djob("b"))
        repo = _mock_repo()
        JobSelector(
            config=config, requested_names=frozenset({"a", "b"}), requested_tags=frozenset()
        ).select_run_jobs(repo)
        [call_args] = repo.pending_job_names.call_args_list
        revset = call_args.kwargs["revset"]
        assert "present(repoactive/a)" in revset
        assert "present(repoactive/b)" in revset

    def test_successor_pulls_in_its_dependencies(self) -> None:
        # c depends on b; when c is a successor of a, b must be included too.
        config = _config(_djob("a"), _djob("b"), _djob("c", depends_on=["b"]))
        result = JobSelector(
            config=config, requested_names=frozenset({"a"}), requested_tags=frozenset()
        ).select_run_jobs(_mock_repo(successors={"c"}))
        assert _names(result.jobs) == ["a", "b", "c"]


class TestJobSelector:
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
