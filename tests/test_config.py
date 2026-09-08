"""Tests for config loading, merging, and validation."""

from datetime import timedelta
from pathlib import Path

import pydantic
import pytest

from repoactive.config import (
    _AD_HOC_NAME_MAX_LEN,
    _DEFAULTED_FIELDS,
    AdHocJob,
    AdHocNameCollisionError,
    Config,
    ConfigError,
    ConfigNotFoundError,
    ConfigShape,
    CreateMR,
    EmptyAdHocCommandError,
    InvalidDurationError,
    Job,
    JobDefaults,
    MissingSecretError,
    ad_hoc_job_name,
    default_config_paths,
    load_config,
    parse_duration,
)
from repoactive.constants import (
    JOB_TRAILER_KEY,
    RA_CONFIG_SOURCE_DIR_ENV,
    RA_JOB_BASE_BRANCH_ENV,
    RA_JOB_BRANCH_ENV,
    RA_JOB_NAME_ENV,
)


def _platform(**kwargs: object) -> dict[str, object]:
    return {"url": "https://gitlab.com", "type": "gitlab", "token_env": "TOKEN", **kwargs}


def _job(name: str, **kwargs: object) -> dict[str, object]:
    return {"name": name, "command": "cmd", "title": f"Job {name}", **kwargs}


def _config(**kwargs: object) -> Config:
    data: dict[str, object] = {"platform": [_platform()], "jobs": [], **kwargs}
    return Config.model_validate(data)


class TestJobNameValidation:
    @pytest.mark.parametrize("name", ["foo", "foo-bar", "foo_bar", "Foo123", "A-B_c9"])
    def test_valid_names_accepted(self, name: str) -> None:
        job = Job(name=name, command="cmd", title="T")
        assert job.name == name

    @pytest.mark.parametrize("name", ["foo bar", "foo/bar", "foo.bar", "", "foo@bar"])
    def test_invalid_names_rejected(self, name: str) -> None:
        with pytest.raises(ValueError, match="invalid job name"):
            Job(name=name, command="cmd", title="T")


class TestBranchPrefixValidation:
    def test_valid_prefix_accepted(self) -> None:
        Job(name="x", command="cmd", title="T", branch_prefix="bot/")

    def test_nested_prefix_accepted(self) -> None:
        Job(name="x", command="cmd", title="T", branch_prefix="org/team/")

    def test_none_accepted(self) -> None:
        Job(name="x", command="cmd", title="T", branch_prefix=None)

    def test_empty_prefix_accepted(self) -> None:
        job = Job(name="x", command="cmd", title="T", branch_prefix="")
        assert job.branch_name() == "x"

    def test_leading_slash_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid branch_prefix"):
            Job(name="x", command="cmd", title="T", branch_prefix="/bot/")

    def test_consecutive_slashes_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid branch_prefix"):
            Job(name="x", command="cmd", title="T", branch_prefix="bot//sub/")

    def test_invalid_char_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid branch_prefix"):
            Job(name="x", command="cmd", title="T", branch_prefix="bot prefix/")

    def test_defaults_valid_prefix_accepted(self) -> None:
        JobDefaults(branch_prefix="custom/")

    def test_defaults_leading_slash_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid branch_prefix"):
            JobDefaults(branch_prefix="/bad/")

    def test_defaults_consecutive_slashes_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid branch_prefix"):
            JobDefaults(branch_prefix="bad//prefix/")


class TestBranchName:
    def test_default_prefix(self) -> None:
        job = Job(name="foo", command="cmd", title="Foo", branch_prefix="repoactive/")
        assert job.branch_name() == "repoactive/foo"

    def test_custom_prefix(self) -> None:
        job = Job(name="bar", command="cmd", title="Bar", branch_prefix="bot/")
        assert job.branch_name() == "bot/bar"


class TestCommitTrailers:
    def test_single_trailer_for_plain_job(self) -> None:
        job = Job(name="foo", command="cmd", title="Foo")
        assert job.commit_trailers() == [f"{JOB_TRAILER_KEY}: foo"]

    def test_second_trailer_for_generated_job(self) -> None:
        job = Job(name="foo", command="cmd", title="Foo", generated_by="gen")
        assert job.commit_trailers() == [
            f"{JOB_TRAILER_KEY}: foo",
            f"{JOB_TRAILER_KEY}: gen",
        ]


class TestCreateMrValidation:
    @staticmethod
    def _job_with(*, create_mr: object) -> Job:
        return Job.model_validate(
            {"name": "x", "command": "cmd", "title": "T", "create_mr": create_mr}
        )

    def test_default_is_always(self) -> None:
        assert Job(name="x", command="cmd", title="T").create_mr is CreateMR.always

    # The boolean TOML form predates the enum and stays supported.
    def test_true_means_always(self) -> None:
        assert self._job_with(create_mr=True).create_mr is CreateMR.always

    def test_false_means_never(self) -> None:
        assert self._job_with(create_mr=False).create_mr is CreateMR.never

    @pytest.mark.parametrize("value", list(CreateMR))
    def test_enum_values_accepted(self, value: CreateMR) -> None:
        assert self._job_with(create_mr=str(value)).create_mr is value

    def test_other_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="create_mr"):
            self._job_with(create_mr="sometimes")


class TestBookmarkNames:
    def test_empty_config(self) -> None:
        assert _config().bookmark_names() == set()

    def test_uses_default_prefix(self) -> None:
        cfg = _config(jobs=[_job("a"), _job("b")])
        assert cfg.bookmark_names() == {"repoactive/a", "repoactive/b"}

    def test_default_prefix_from_job_defaults(self) -> None:
        cfg = _config(jobs=[_job("a")], **{"job-defaults": {"branch_prefix": "bot/"}})
        assert cfg.bookmark_names() == {"bot/a"}

    def test_per_job_prefix_overrides_default(self) -> None:
        cfg = _config(
            jobs=[_job("a", branch_prefix="custom/"), _job("b")],
            **{"job-defaults": {"branch_prefix": "bot/"}},
        )
        assert cfg.bookmark_names() == {"custom/a", "bot/b"}


class TestBaseBranches:
    def test_empty_config(self) -> None:
        assert _config().base_branches() == set()

    def test_jobs_without_base_branch_yield_empty(self) -> None:
        cfg = _config(jobs=[_job("a"), _job("b")])
        assert cfg.base_branches() == set()

    def test_collects_per_job_base_branches(self) -> None:
        cfg = _config(jobs=[_job("a", base_branch="main"), _job("b", base_branch="develop")])
        assert cfg.base_branches() == {"main", "develop"}

    def test_deduplicates_shared_base_branch(self) -> None:
        cfg = _config(jobs=[_job("a", base_branch="main"), _job("b", base_branch="main")])
        assert cfg.base_branches() == {"main"}

    def test_falls_back_to_job_defaults(self) -> None:
        cfg = _config(jobs=[_job("a")], **{"job-defaults": {"base_branch": "main"}})
        assert cfg.base_branches() == {"main"}

    def test_per_job_overrides_default(self) -> None:
        cfg = _config(
            jobs=[_job("a", base_branch="custom"), _job("b")],
            **{"job-defaults": {"base_branch": "main"}},
        )
        assert cfg.base_branches() == {"custom", "main"}

    def test_mix_of_set_and_unset_without_default(self) -> None:
        cfg = _config(jobs=[_job("a", base_branch="main"), _job("b")])
        assert cfg.base_branches() == {"main"}

    def test_excludes_revset_function_calls(self) -> None:
        # trunk()/root() and user-defined revset aliases are not bookmarks.
        cfg = _config(
            jobs=[
                _job("a", base_branch="trunk()"),
                _job("b", base_branch="root()"),
                _job("c", base_branch="my_alias()"),
            ]
        )
        assert cfg.base_branches() == set()

    def test_keeps_bookmarks_alongside_revset_function_calls(self) -> None:
        cfg = _config(jobs=[_job("a", base_branch="main"), _job("b", base_branch="trunk()")])
        assert cfg.base_branches() == {"main"}


class TestDependsOnValidation:
    def test_valid_depends_on(self) -> None:
        cfg = _config(
            jobs=[
                _job("a"),
                _job("b", depends_on=["a"]),
            ]
        )
        assert cfg.jobs[1].depends_on == ["a"]

    def test_unknown_dependency_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown jobs"):
            _config(jobs=[_job("a", depends_on=["nonexistent"])])

    def test_multiple_unknown_dependencies_reported(self) -> None:
        with pytest.raises(ValueError, match="unknown jobs"):
            _config(jobs=[_job("a", depends_on=["x", "y"])])

    def test_self_dependency_raises(self) -> None:
        with pytest.raises(ValueError, match="circular dependency"):
            _config(jobs=[_job("a", depends_on=["a"])])

    def test_direct_cycle_raises(self) -> None:
        with pytest.raises(ValueError, match="circular dependency"):
            _config(
                jobs=[
                    _job("a", depends_on=["b"]),
                    _job("b", depends_on=["a"]),
                ]
            )

    def test_transitive_cycle_raises(self) -> None:
        with pytest.raises(ValueError, match="circular dependency"):
            _config(
                jobs=[
                    _job("a", depends_on=["c"]),
                    _job("b", depends_on=["a"]),
                    _job("c", depends_on=["b"]),
                ]
            )


class TestRunOnlyIfChangedValidation:
    def test_valid_run_only_if_changed(self) -> None:
        cfg = _config(
            jobs=[
                _job("a"),
                _job("b", run_only_if_changed=["a"]),
            ]
        )
        assert cfg.jobs[1].run_only_if_changed == ["a"]

    def test_unknown_name_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown job"):
            _config(jobs=[_job("a", run_only_if_changed=["nonexistent"])])

    def test_multiple_unknown_names_reported(self) -> None:
        with pytest.raises(ValueError, match="unknown job"):
            _config(jobs=[_job("a", run_only_if_changed=["x", "y"])])

    def test_does_not_require_depends_on(self) -> None:
        # run_only_if_changed names need not be in depends_on
        cfg = _config(
            jobs=[
                _job("a"),
                _job("b", run_only_if_changed=["a"]),
            ]
        )
        assert cfg.jobs[1].depends_on == []
        assert cfg.jobs[1].run_only_if_changed == ["a"]

    def test_watched_job_ordered_after_rejected(self) -> None:
        # 'a' is listed in config after 'b', so it runs after b: the gate would
        # see it as "no diff" and fire wrongly. Rejected at load.
        with pytest.raises(ValueError, match="not ordered before it"):
            _config(
                jobs=[
                    _job("b", run_only_if_changed=["a"]),
                    _job("a"),
                ]
            )

    def test_watched_job_ordered_before_via_depends_on(self) -> None:
        # 'a' comes later in config order but is a depends_on ancestor, so it is
        # ordered before 'b' topologically and is accepted.
        cfg = _config(
            jobs=[
                _job("b", depends_on=["a"], run_only_if_changed=["a"]),
                _job("a"),
            ]
        )
        assert cfg.jobs[0].run_only_if_changed == ["a"]

    def test_watching_self_rejected(self) -> None:
        with pytest.raises(ValueError, match="not ordered before it"):
            _config(jobs=[_job("a", run_only_if_changed=["a"])])


class TestUniqueBranchNameValidation:
    def test_distinct_bookmarks_accepted(self) -> None:
        cfg = _config(jobs=[_job("a"), _job("b")])
        assert {j.branch_name() for j in cfg._resolved_jobs()} == {
            "repoactive/a",
            "repoactive/b",
        }

    def test_colliding_prefix_and_name_rejected(self) -> None:
        # a/ + b-c and a/b- + c both resolve to a/b-c
        with pytest.raises(ValueError, match="both resolve to bookmark 'a/b-c'"):
            _config(
                jobs=[
                    _job("b-c", branch_prefix="a/"),
                    _job("c", branch_prefix="a/b-"),
                ]
            )

    def test_bookmark_equal_to_other_jobs_base_branch_rejected(self) -> None:
        with pytest.raises(ValueError, match="uses as its base_branch"):
            _config(
                jobs=[
                    _job("shared"),  # bookmark: repoactive/shared
                    _job("downstream", base_branch="repoactive/shared"),
                ]
            )

    def test_bookmark_matching_own_base_branch_allowed(self) -> None:
        # a revset-style base such as trunk() never collides with a bookmark name.
        cfg = _config(jobs=[_job("a", base_branch="trunk()")])
        assert cfg.jobs[0].base_branch == "trunk()"


class TestCooldownOnValidation:
    def test_valid_cooldown_on(self) -> None:
        cfg = _config(
            jobs=[
                _job("full-lock", cooldown_period="7d"),
                _job("dev-lock", cooldown_period="7d", cooldown_on=["full-lock"]),
            ]
        )
        assert cfg.jobs[1].cooldown_on == ["full-lock"]

    def test_cooldown_period_may_come_from_job_defaults(self) -> None:
        cfg = _config(
            jobs=[
                _job("full-lock"),
                _job("dev-lock", cooldown_on=["full-lock"]),
            ],
            **{"job-defaults": {"cooldown_period": "7d"}},
        )
        assert cfg.jobs[1].cooldown_on == ["full-lock"]

    def test_unknown_target_is_allowed(self) -> None:
        # The named job need not exist in the config; the trailers it matches may
        # come from jobs since removed or renamed.
        cfg = _config(jobs=[_job("a", cooldown_period="7d", cooldown_on=["removed-job"])])
        assert cfg.jobs[0].cooldown_on == ["removed-job"]

    def test_invalid_target_name_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid job name"):
            _config(jobs=[_job("a", cooldown_period="7d", cooldown_on=["bad name"])])

    def test_self_reference_raises(self) -> None:
        with pytest.raises(ValueError, match="cooldown_on lists itself"):
            _config(jobs=[_job("a", cooldown_period="7d", cooldown_on=["a"])])

    def test_without_cooldown_raises(self) -> None:
        with pytest.raises(ValueError, match="no cooldown_period"):
            _config(
                jobs=[
                    _job("full-lock"),
                    _job("dev-lock", cooldown_on=["full-lock"]),
                ]
            )


class TestJobDefaults:
    def test_branch_prefix_default(self) -> None:
        cfg = _config()
        assert cfg.job_defaults.branch_prefix == "repoactive/"

    def test_labels_default_empty(self) -> None:
        cfg = _config()
        assert cfg.job_defaults.labels == []

    def test_defaulted_fields_cover_job_defaults(self) -> None:
        # Every JobDefaults field must be wired into Job.resolve: either as a
        # fallback field in _DEFAULTED_FIELDS or as the special-cased labels
        # merge. Fails loudly when a new JobDefaults field is added without
        # updating resolve. secret_env is deliberately excluded: it marks names
        # config-wide but is never inherited into a job (ADR 0017), so resolve
        # does not touch it.
        assert set(_DEFAULTED_FIELDS) | {"labels", "secret_env"} == set(JobDefaults.model_fields)
        assert set(_DEFAULTED_FIELDS) <= set(Job.model_fields)


class TestSecretEnv:
    @pytest.mark.parametrize("name", ["FOO", "OPENAI_API_KEY", "_private", "a1", "X"])
    def test_valid_names_accepted(self, name: str) -> None:
        job = Job(name="x", command="cmd", title="T", secret_env=[name])
        assert job.secret_env == [name]

    @pytest.mark.parametrize("name", ["1FOO", "foo-bar", "foo bar", "foo.bar", ""])
    def test_invalid_names_rejected(self, name: str) -> None:
        with pytest.raises(ValueError, match="invalid secret_env name"):
            Job(name="x", command="cmd", title="T", secret_env=[name])

    @pytest.mark.parametrize("name", ["RA_FOO", "REPOACTIVE_UI"])
    def test_reserved_prefixes_rejected(self, name: str) -> None:
        with pytest.raises(ValueError, match="reserved for repoactive"):
            Job(name="x", command="cmd", title="T", secret_env=[name])

    def test_reserved_prefixes_rejected_in_job_defaults(self) -> None:
        with pytest.raises(ValueError, match="reserved for repoactive"):
            JobDefaults(secret_env=["RA_JOBS_DIR"])

    def test_defaults_to_empty(self) -> None:
        assert Job(name="x", command="cmd", title="T").secret_env == []
        assert JobDefaults().secret_env == []

    def test_marked_secret_names_unions_defaults_and_jobs(self) -> None:
        cfg = _config(
            jobs=[_job("a", secret_env=["FOO"]), _job("b", secret_env=["BAR", "FOO"])],
            **{"job-defaults": {"secret_env": ["BAZ"]}},
        )
        assert cfg.marked_secret_names() == {"FOO", "BAR", "BAZ"}

    def test_marked_secret_names_empty_by_default(self) -> None:
        assert _config(jobs=[_job("a")]).marked_secret_names() == set()

    def test_job_defaults_secret_env_marks_but_is_not_inherited(self) -> None:
        # [job-defaults].secret_env marks names config-wide (so marked_secret_names
        # includes them) but is never granted/inherited into a job (ADR 0017).
        cfg = _config(jobs=[_job("a")], **{"job-defaults": {"secret_env": ["FOO"]}})
        assert cfg.marked_secret_names() == {"FOO"}
        assert cfg._resolved_jobs()[0].secret_env == []


class TestParseInterval:
    def test_days(self) -> None:
        assert parse_duration("7d") == timedelta(days=7)

    def test_weeks(self) -> None:
        assert parse_duration("2w") == timedelta(weeks=2)

    def test_hours(self) -> None:
        assert parse_duration("12h") == timedelta(hours=12)

    def test_minutes(self) -> None:
        assert parse_duration("30m") == timedelta(minutes=30)

    def test_seconds(self) -> None:
        assert parse_duration("45s") == timedelta(seconds=45)

    def test_surrounding_whitespace_ignored(self) -> None:
        assert parse_duration("  7d  ") == timedelta(days=7)

    def test_unknown_unit_raises(self) -> None:
        with pytest.raises(InvalidDurationError, match="invalid duration"):
            parse_duration("7y")

    def test_missing_unit_raises(self) -> None:
        with pytest.raises(InvalidDurationError, match="invalid duration"):
            parse_duration("7")

    def test_empty_raises(self) -> None:
        with pytest.raises(InvalidDurationError, match="invalid duration"):
            parse_duration("")


class TestCooldownPeriod:
    def test_valid_value_accepted(self) -> None:
        job = Job(name="x", command="cmd", title="X", cooldown_period="7d")
        assert job.cooldown_period == "7d"

    def test_invalid_value_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid duration"):
            Job(name="x", command="cmd", title="X", cooldown_period="nope")

    def test_invalid_value_rejected_in_defaults(self) -> None:
        with pytest.raises(ValueError, match="invalid duration"):
            JobDefaults(cooldown_period="nope")

    def test_delta_none_when_unset(self) -> None:
        job = Job(name="x", command="cmd", title="X")
        assert job.cooldown_timedelta() is None

    def test_delta_parsed_when_set(self) -> None:
        job = Job(name="x", command="cmd", title="X", cooldown_period="7d")
        assert job.cooldown_timedelta() == timedelta(days=7)

    def test_falls_back_to_defaults(self) -> None:
        job = Job(name="x", command="cmd", title="X")
        resolved = job.resolve(JobDefaults(cooldown_period="3d"))
        assert resolved.cooldown_period == "3d"

    def test_per_job_overrides_defaults(self) -> None:
        job = Job(name="x", command="cmd", title="X", cooldown_period="1d")
        resolved = job.resolve(JobDefaults(cooldown_period="3d"))
        assert resolved.cooldown_period == "1d"

    def test_stays_none_when_neither_set(self) -> None:
        job = Job(name="x", command="cmd", title="X")
        assert job.resolve(JobDefaults()).cooldown_period is None


class TestTimeout:
    def test_valid_value_accepted(self) -> None:
        job = Job(name="x", command="cmd", title="X", timeout="30m")
        assert job.timeout == "30m"

    def test_invalid_value_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid duration"):
            Job(name="x", command="cmd", title="X", timeout="nope")

    def test_invalid_value_rejected_in_defaults(self) -> None:
        with pytest.raises(ValueError, match="invalid duration"):
            JobDefaults(timeout="nope")

    def test_seconds_none_when_unset(self) -> None:
        job = Job(name="x", command="cmd", title="X")
        assert job.timeout_seconds() is None

    def test_seconds_parsed_when_set(self) -> None:
        job = Job(name="x", command="cmd", title="X", timeout="30m")
        assert job.timeout_seconds() == 30 * 60

    def test_seconds_none_when_zero(self) -> None:
        # "0s" disables the timeout: TOML cannot express null, so a zero
        # duration is how a job opts out of a timeout set in job-defaults.
        job = Job(name="x", command="cmd", title="X", timeout="0s")
        assert job.timeout_seconds() is None

    def test_falls_back_to_defaults(self) -> None:
        job = Job(name="x", command="cmd", title="X")
        resolved = job.resolve(JobDefaults(timeout="1h"))
        assert resolved.timeout == "1h"

    def test_per_job_overrides_defaults(self) -> None:
        job = Job(name="x", command="cmd", title="X", timeout="10m")
        resolved = job.resolve(JobDefaults(timeout="1h"))
        assert resolved.timeout == "10m"

    def test_defaults_to_two_minutes_when_neither_set(self) -> None:
        job = Job(name="x", command="cmd", title="X")
        assert job.resolve(JobDefaults()).timeout == "2m"


class TestRequiredApprovals:
    def test_valid_value_accepted(self) -> None:
        approvals = 2
        job = Job(name="x", command="cmd", title="X", required_approvals=approvals)
        assert job.required_approvals == approvals

    def test_zero_accepted(self) -> None:
        # Zero is meaningful: it overrides a job-defaults value back to
        # "no approvals required" (TOML cannot express null).
        job = Job(name="x", command="cmd", title="X", required_approvals=0)
        assert job.required_approvals == 0

    def test_negative_value_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError, match="greater than or equal to 0"):
            Job(name="x", command="cmd", title="X", required_approvals=-1)

    def test_negative_value_rejected_in_defaults(self) -> None:
        with pytest.raises(pydantic.ValidationError, match="greater than or equal to 0"):
            JobDefaults(required_approvals=-1)

    def test_none_when_unset(self) -> None:
        job = Job(name="x", command="cmd", title="X")
        assert job.required_approvals is None

    def test_falls_back_to_defaults(self) -> None:
        approvals = 3
        job = Job(name="x", command="cmd", title="X")
        resolved = job.resolve(JobDefaults(required_approvals=approvals))
        assert resolved.required_approvals == approvals

    def test_per_job_overrides_defaults(self) -> None:
        job = Job(name="x", command="cmd", title="X", required_approvals=1)
        resolved = job.resolve(JobDefaults(required_approvals=3))
        assert resolved.required_approvals == 1


class TestShell:
    def test_bare_name_accepted(self) -> None:
        job = Job(name="x", command="cmd", title="X", shell="bash")
        assert job.shell == "bash"

    def test_absolute_path_accepted(self) -> None:
        job = Job(name="x", command="cmd", title="X", shell="/bin/zsh")
        assert job.shell == "/bin/zsh"

    def test_none_when_unset(self) -> None:
        job = Job(name="x", command="cmd", title="X")
        assert job.shell is None

    @pytest.mark.parametrize("value", ["bash -e", "bash ", " bash", "a\tb", ""])
    def test_whitespace_rejected(self, value: str) -> None:
        with pytest.raises(pydantic.ValidationError, match="invalid shell"):
            Job(name="x", command="cmd", title="X", shell=value)

    def test_rejected_in_defaults(self) -> None:
        with pytest.raises(pydantic.ValidationError, match="invalid shell"):
            JobDefaults(shell="bash -e")

    def test_falls_back_to_defaults(self) -> None:
        job = Job(name="x", command="cmd", title="X")
        resolved = job.resolve(JobDefaults(shell="bash"))
        assert resolved.shell == "bash"

    def test_per_job_overrides_defaults(self) -> None:
        job = Job(name="x", command="cmd", title="X", shell="zsh")
        resolved = job.resolve(JobDefaults(shell="bash"))
        assert resolved.shell == "zsh"

    def test_none_when_neither_set(self) -> None:
        job = Job(name="x", command="cmd", title="X")
        assert job.resolve(JobDefaults()).shell is None


class TestConfigShape:
    def test_missing_tables_default_to_empty(self) -> None:
        shape = ConfigShape.model_validate({})
        assert shape.job == {}
        assert shape.platform == {}
        assert shape.job_defaults == {}

    def test_passes_through_tables(self) -> None:
        shape = ConfigShape.model_validate(
            {
                "job": {"a": {"command": "cmd"}},
                "platform": {"gl": {"url": "u"}},
                "job-defaults": {"branch_prefix": "bot/"},
            }
        )
        assert shape.job == {"a": {"command": "cmd"}}
        assert shape.platform == {"gl": {"url": "u"}}
        assert shape.job_defaults == {"branch_prefix": "bot/"}

    @pytest.mark.parametrize("body", ["hello", 5, ["x"], True])
    def test_non_table_job_entry_rejected(self, body: object) -> None:
        with pytest.raises(pydantic.ValidationError) as exc_info:
            ConfigShape.model_validate({"job": {"foo": body}})
        assert exc_info.value.errors()[0]["loc"] == ("job", "foo")

    @pytest.mark.parametrize("body", ["hello", 5, ["x"], True])
    def test_non_table_platform_entry_rejected(self, body: object) -> None:
        with pytest.raises(pydantic.ValidationError) as exc_info:
            ConfigShape.model_validate({"platform": {"gl": body}})
        assert exc_info.value.errors()[0]["loc"] == ("platform", "gl")

    @pytest.mark.parametrize("body", ["hello", 5, ["x"], True])
    def test_non_table_job_defaults_rejected(self, body: object) -> None:
        with pytest.raises(pydantic.ValidationError, match=r"job-defaults"):
            ConfigShape.model_validate({"job-defaults": body})

    def test_job_array_form_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError) as exc_info:
            ConfigShape.model_validate({"job": [{"name": "a"}]})
        assert exc_info.value.errors()[0]["loc"] == ("job",)

    def test_platform_array_form_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError) as exc_info:
            ConfigShape.model_validate({"platform": [{"url": "u"}]})
        assert exc_info.value.errors()[0]["loc"] == ("platform",)


class TestLoadConfig:
    def test_minimal_config(self, tmp_path: Path) -> None:
        f = tmp_path / ".repoactive.toml"
        f.write_text(
            '[platform.github]\nurl = "https://github.com"\ntype = "github"\ntoken_env = "GH_TOKEN"\n'
            '[job.x]\ncommand = "echo"\ntitle = "X"\n'
        )
        cfg = load_config([f])
        assert cfg.platforms[0].url == "https://github.com"
        assert cfg.jobs[0].name == "x"

    def test_file_not_found(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.toml"
        with pytest.raises(ConfigError, match=str(missing)):
            load_config([missing])

    def test_multiple_platforms(self, tmp_path: Path) -> None:
        f = tmp_path / ".repoactive.toml"
        f.write_text(
            '[platform.github]\nurl = "https://github.com"\ntype = "github"\ntoken_env = "GH_TOKEN"\n'
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "GL_TOKEN"\n'
        )
        cfg = load_config([f])
        assert [p.url for p in cfg.platforms] == ["https://github.com", "https://gitlab.com"]

    def test_merge_later_scalar_wins(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "A"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "B"\n'
        )
        cfg = load_config([base, override])
        gitlab = next(p for p in cfg.platforms if p.url == "https://gitlab.com")
        assert gitlab.token_env == "B"

    def test_merge_later_nested_scalar_wins(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job-defaults]\nbranch_prefix = "old/"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text('[job-defaults]\nbranch_prefix = "new/"\n')
        cfg = load_config([base, override])
        assert cfg.job_defaults.branch_prefix == "new/"

    def test_merge_unset_key_preserved(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job-defaults]\nbranch_prefix = "base/"\nmr_title_prefix = "kept"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text('[job-defaults]\nbranch_prefix = "new/"\n')
        cfg = load_config([base, override])
        assert cfg.job_defaults.branch_prefix == "new/"
        assert cfg.job_defaults.mr_title_prefix == "kept"

    def test_merge_platform_new_entry_appended(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.github]\nurl = "https://github.com"\ntype = "github"\ntoken_env = "GH"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "GL"\n'
        )
        cfg = load_config([base, override])
        assert [p.url for p in cfg.platforms] == ["https://github.com", "https://gitlab.com"]

    def test_duplicate_platform_host_rejected(self, tmp_path: Path) -> None:
        # two differently-named platforms resolving to the same host are
        # ambiguous (only the first would ever be matched).
        f = tmp_path / "dup.toml"
        f.write_text(
            '[platform.gh]\nurl = "https://github.com"\ntype = "github"\ntoken_env = "A"\n'
            '[platform.gh2]\nurl = "git@github.com:org/repo.git"\ntype = "github"\ntoken_env = "B"\n'
        )
        with pytest.raises(ConfigError, match=r"same host 'github\.com'"):
            load_config([f])

    def test_merge_jobs_new_name_appended(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job.a]\ncommand = "cmd-a"\ntitle = "A"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text('[job.b]\ncommand = "cmd-b"\ntitle = "B"\n')
        cfg = load_config([base, override])
        assert [j.name for j in cfg.jobs] == ["a", "b"]

    def test_merge_jobs_existing_name_updated(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job.a]\ncommand = "old-cmd"\ntitle = "Old"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text('[job.a]\ncommand = "new-cmd"\ntitle = "New"\n')
        cfg = load_config([base, override])
        assert len(cfg.jobs) == 1
        assert cfg.jobs[0].command == "new-cmd"
        assert cfg.jobs[0].title == "New"

    def test_merge_jobs_partial_field_override(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job.a]\ncommand = "cmd"\ntitle = "A"\ndraft = false\n'
        )
        override = tmp_path / "override.toml"
        override.write_text('[job.a]\ncommand = "cmd"\ntitle = "A"\ndraft = true\n')
        cfg = load_config([base, override])
        assert cfg.jobs[0].draft is True

    def test_merge_jobs_disabled_overrides_tags(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job.a]\ncommand = "cmd"\ntitle = "A"\ntags = ["weekly"]\n'
        )
        override = tmp_path / "override.toml"
        override.write_text("[job.a]\ndisabled = true\n")
        cfg = load_config([base, override])
        assert cfg.jobs[0].disabled is True
        assert cfg.jobs[0].tags == []

    def test_merge_jobs_tags_override_disabled(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job.a]\ncommand = "cmd"\ntitle = "A"\ndisabled = true\n'
        )
        override = tmp_path / "override.toml"
        override.write_text('[job.a]\ntags = ["weekly"]\n')
        cfg = load_config([base, override])
        assert cfg.jobs[0].disabled is False
        assert cfg.jobs[0].tags == ["weekly"]

    def test_merge_jobs_override_with_both_disabled_and_tags_rejected(
        self, tmp_path: Path
    ) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job.a]\ncommand = "cmd"\ntitle = "A"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text('[job.a]\ndisabled = true\ntags = ["weekly"]\n')
        with pytest.raises(ConfigError, match="both 'disabled' and 'tags'"):
            load_config([base, override])

    def test_platform_always_includes_defaults(self, tmp_path: Path) -> None:
        f = tmp_path / ".repoactive.toml"
        f.write_text('[job-defaults]\nbranch_prefix = "x/"\n')
        cfg = load_config([f])
        assert {p.url for p in cfg.platforms} >= {"https://github.com", "https://gitlab.com"}

    def test_second_config_may_be_partial(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
        )
        partial = tmp_path / "partial.toml"
        partial.write_text('[job-defaults]\nbranch_prefix = "x/"\n')
        cfg = load_config([base, partial])
        assert cfg.job_defaults.branch_prefix == "x/"

    def test_second_config_invalid_depends_on_raises(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job.a]\ncommand = "cmd"\ntitle = "A"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text(
            '[job.b]\ncommand = "cmd"\ntitle = "B"\ndepends_on = ["nonexistent"]\n'
        )
        with pytest.raises(ConfigError, match="unknown jobs"):
            load_config([base, override])

    def test_toml_parse_error_names_file(self, tmp_path: Path) -> None:
        bad = tmp_path / "broken.toml"
        bad.write_text("this is not = valid = toml\n")
        with pytest.raises(ConfigError, match=str(bad)):
            load_config([bad])

    def test_validation_error_names_offending_file(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job.a]\ncommand = "cmd"\ntitle = "A"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text('[job.b]\ncommand = "cmd"\ntitle = "B"\ntimeout = "nope"\n')
        # the error points at override.toml, the file that introduced the bad value
        with pytest.raises(ConfigError, match=r"override\.toml") as exc_info:
            load_config([base, override])
        assert "base.toml" not in str(exc_info.value)
        assert "invalid duration" in str(exc_info.value)

    def test_override_scalar_wins(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job-defaults]\ncooldown_period = "1h"\n'
        )
        cfg = load_config([base], overrides=['job-defaults.cooldown_period = "24h"'])
        assert cfg.job_defaults.cooldown_period == "24h"

    def test_override_dotted_key(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job-defaults]\nbranch_prefix = "old/"\n'
        )
        cfg = load_config([base], overrides=['job-defaults.branch_prefix = "new/"'])
        assert cfg.job_defaults.branch_prefix == "new/"

    def test_override_job_field_merges(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job.a]\ncommand = "cmd"\ntitle = "A"\ndraft = false\n'
        )
        cfg = load_config([base], overrides=["job.a.draft = true"])
        assert cfg.jobs[0].command == "cmd"
        assert cfg.jobs[0].draft is True

    def test_override_platform_field_via_dotted_key(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.github]\nurl = "https://github.com"\ntype = "github"\ntoken_env = "GITHUB_TOKEN"\n'
        )
        cfg = load_config([base], overrides=['platform.github.token_env = "MY_TOKEN"'])
        github = next(p for p in cfg.platforms if p.url == "https://github.com")
        assert github.token_env == "MY_TOKEN"

    def test_old_platform_array_form_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "old.toml"
        bad.write_text(
            '[[platform]]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
        )
        with pytest.raises(ConfigError, match=r"Input should be a valid dictionary"):
            load_config([bad])

    def test_override_invalid_toml_names_set_label(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
        )
        # cooldown = 24h is not valid TOML (bareword value)
        with pytest.raises(ConfigError, match=r"--set"):
            load_config([base], overrides=["cooldown = 24h"])

    def test_override_unknown_key_rejected(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
        )
        with pytest.raises(ConfigError, match=r"--set"):
            load_config([base], overrides=["nonexistent = true"])

    def test_name_in_job_body_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "redundant.toml"
        f.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job.a]\nname = "a"\ncommand = "cmd"\ntitle = "A"\n'
        )
        with pytest.raises(ConfigError, match=str(f)) as exc_info:
            load_config([f])
        assert "must not set a 'name' field" in str(exc_info.value)

    def test_generated_by_in_job_body_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "generated_by.toml"
        f.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job.a]\ngenerated_by = "gen"\ncommand = "cmd"\ntitle = "A"\n'
        )
        with pytest.raises(ConfigError, match=str(f)) as exc_info:
            load_config([f])
        assert "must not set 'generated_by'" in str(exc_info.value)

    def test_non_table_job_entry_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "odd.toml"
        f.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job]\nfoo = "hello"\n'
        )
        with pytest.raises(ConfigError, match=str(f)) as exc_info:
            load_config([f])
        assert "job.foo" in str(exc_info.value)
        assert "Input should be a valid dictionary" in str(exc_info.value)

    def test_non_table_job_entry_in_second_source_names_that_source(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job.a]\ncommand = "cmd"\ntitle = "A"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text('[job]\na = "hello"\n')
        with pytest.raises(ConfigError, match=str(override)) as exc_info:
            load_config([base, override])
        assert "job.a" in str(exc_info.value)
        assert "Input should be a valid dictionary" in str(exc_info.value)

    def test_non_table_platform_entry_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "odd.toml"
        f.write_text('[platform]\ngitlab = "https://gitlab.com"\n')
        with pytest.raises(ConfigError, match=str(f)) as exc_info:
            load_config([f])
        assert "platform.gitlab" in str(exc_info.value)
        assert "Input should be a valid dictionary" in str(exc_info.value)

    def test_non_table_job_entry_in_set_override_rejected(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job.a]\ncommand = "cmd"\ntitle = "A"\n'
        )
        with pytest.raises(ConfigError, match=r"--set") as exc_info:
            load_config([base], overrides=['job.a = "hello"'])
        assert "job.a" in str(exc_info.value)
        assert "Input should be a valid dictionary" in str(exc_info.value)

    def test_non_table_job_defaults_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "odd.toml"
        f.write_text(
            'job-defaults = "hello"\n'
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
        )
        with pytest.raises(ConfigError, match=str(f)) as exc_info:
            load_config([f])
        assert "job-defaults" in str(exc_info.value)

    def test_old_job_array_form_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "legacy.toml"
        f.write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[[job]]\nname = "a"\ncommand = "cmd"\ntitle = "A"\n'
        )
        with pytest.raises(ConfigError, match=str(f)) as exc_info:
            load_config([f])
        assert "Input should be a valid dictionary" in str(exc_info.value)

    def test_directory_reads_toml_files_sorted(self, tmp_path: Path) -> None:
        conf_dir = tmp_path / "conf.d"
        conf_dir.mkdir()
        (conf_dir / "02-override.toml").write_text('[job-defaults]\nbranch_prefix = "second/"\n')
        (conf_dir / "01-base.toml").write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job-defaults]\nbranch_prefix = "first/"\n'
            '[job.a]\ncommand = "cmd"\ntitle = "A"\n'
        )
        cfg = load_config([conf_dir])
        # 02-override.toml is applied after 01-base.toml because entries are sorted
        assert cfg.job_defaults.branch_prefix == "second/"
        assert cfg.jobs[0].name == "a"

    def test_directory_ignores_non_toml_files(self, tmp_path: Path) -> None:
        conf_dir = tmp_path / "conf.d"
        conf_dir.mkdir()
        (conf_dir / "a.toml").write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job.x]\ncommand = "cmd"\ntitle = "X"\n'
        )
        (conf_dir / "README.md").write_text("not a config\n")
        cfg = load_config([conf_dir])
        assert [j.name for j in cfg.jobs] == ["x"]

    def test_directory_ignores_subdirectories_named_toml(self, tmp_path: Path) -> None:
        conf_dir = tmp_path / "conf.d"
        conf_dir.mkdir()
        (conf_dir / "nested.toml").mkdir()
        (conf_dir / "a.toml").write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job.x]\ncommand = "cmd"\ntitle = "X"\n'
        )
        cfg = load_config([conf_dir])
        assert [j.name for j in cfg.jobs] == ["x"]

    def test_directory_mixed_with_files(self, tmp_path: Path) -> None:
        conf_dir = tmp_path / "conf.d"
        conf_dir.mkdir()
        (conf_dir / "base.toml").write_text(
            '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job-defaults]\nbranch_prefix = "dir/"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text('[job-defaults]\nbranch_prefix = "file/"\n')
        cfg = load_config([conf_dir, override])
        assert cfg.job_defaults.branch_prefix == "file/"


class TestConfigSourceDir:
    """config_source_dir: the directory of the config source that set a job's command."""

    _PLATFORM = '[platform.gitlab]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'

    def test_file_job_gets_the_files_directory(self, tmp_path: Path) -> None:
        f = tmp_path / ".repoactive.toml"
        f.write_text(self._PLATFORM + '[job.a]\ncommand = "cmd"\ntitle = "A"\n')
        cfg = load_config([f])
        assert cfg.jobs[0].config_source_dir == str(tmp_path)

    def test_config_dir_job_gets_that_directory(self, tmp_path: Path) -> None:
        conf_dir = tmp_path / "conf.d"
        conf_dir.mkdir()
        (conf_dir / "a.toml").write_text(
            self._PLATFORM + '[job.a]\ncommand = "cmd"\ntitle = "A"\n'
        )
        # Whether the directory is expanded or the file is named directly, the
        # job's command lives in conf.d/a.toml, so it gets conf.d itself.
        via_dir = load_config([conf_dir]).jobs[0]
        via_file = load_config([conf_dir / "a.toml"]).jobs[0]
        assert via_dir.config_source_dir == str(conf_dir)
        assert via_file.config_source_dir == str(conf_dir)

    def test_absolute_even_for_relative_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".repoactive.toml").write_text(
            self._PLATFORM + '[job.a]\ncommand = "cmd"\ntitle = "A"\n'
        )
        monkeypatch.chdir(tmp_path)
        cfg = load_config([Path(".repoactive.toml")])
        assert cfg.jobs[0].config_source_dir == str(tmp_path.resolve())

    def test_override_of_other_field_keeps_command_source(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(self._PLATFORM + '[job.a]\ncommand = "cmd"\ntitle = "A"\n')
        other = tmp_path / "conf.d" / "over.toml"
        other.parent.mkdir()
        other.write_text('[job.a]\ntitle = "A2"\n')
        cfg = load_config([base, other])
        # over.toml sets only the title; the command still comes from base.toml.
        assert cfg.jobs[0].config_source_dir == str(tmp_path)

    def test_later_source_setting_command_moves_source_dir(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(self._PLATFORM + '[job.a]\ncommand = "cmd"\ntitle = "A"\n')
        other = tmp_path / "conf.d" / "over.toml"
        other.parent.mkdir()
        other.write_text('[job.a]\ncommand = "cmd2"\n')
        cfg = load_config([base, other])
        assert cfg.jobs[0].config_source_dir == str(other.parent)

    def test_set_override_of_command_clears_source_dir(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(self._PLATFORM + '[job.a]\ncommand = "cmd"\ntitle = "A"\n')
        cfg = load_config([base], overrides=['job.a.command = "cmd2"'])
        # A --set source has no file, so the command dir is cleared.
        assert cfg.jobs[0].config_source_dir is None

    def test_config_source_dir_in_job_body_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "csd.toml"
        f.write_text(
            self._PLATFORM + '[job.a]\nconfig_source_dir = "/evil"\ncommand = "cmd"\ntitle = "A"\n'
        )
        with pytest.raises(ConfigError, match=str(f)) as exc_info:
            load_config([f])
        assert "must not set 'config_source_dir'" in str(exc_info.value)


class TestDefaultConfigPaths:
    def test_picks_up_file_and_directory(self, tmp_path: Path) -> None:
        (tmp_path / ".repoactive.toml").write_text("")
        (tmp_path / ".repoactive.d").mkdir()
        assert default_config_paths(tmp_path) == [
            tmp_path / ".repoactive.d",
            tmp_path / ".repoactive.toml",
        ]

    def test_only_file(self, tmp_path: Path) -> None:
        (tmp_path / ".repoactive.toml").write_text("")
        assert default_config_paths(tmp_path) == [tmp_path / ".repoactive.toml"]

    def test_only_directory(self, tmp_path: Path) -> None:
        (tmp_path / ".repoactive.d").mkdir()
        assert default_config_paths(tmp_path) == [tmp_path / ".repoactive.d"]

    def test_raises_when_neither_exists(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigNotFoundError, match="no configuration found"):
            default_config_paths(tmp_path)

    def test_directory_path_must_be_a_directory(self, tmp_path: Path) -> None:
        # a plain file named .repoactive.d is ignored, so nothing is found
        (tmp_path / ".repoactive.d").write_text("")
        with pytest.raises(ConfigNotFoundError, match="no configuration found"):
            default_config_paths(tmp_path)


class TestNewlineValidation:
    def test_job_title_with_newline_rejected(self) -> None:
        with pytest.raises(ValueError, match="newline"):
            Job(name="x", command="cmd", title="bad\ntitle")

    def test_job_title_without_newline_accepted(self) -> None:
        job = Job(name="x", command="cmd", title="good title")
        assert job.title == "good title"

    def test_job_commit_title_prefix_with_newline_rejected(self) -> None:
        with pytest.raises(ValueError, match="newline"):
            Job(name="x", command="cmd", title="T", commit_title_prefix="bad\nprefix")

    def test_job_commit_title_prefix_without_newline_accepted(self) -> None:
        job = Job(name="x", command="cmd", title="T", commit_title_prefix="[ok] ")
        assert job.commit_title_prefix == "[ok] "

    def test_job_mr_title_prefix_with_newline_rejected(self) -> None:
        with pytest.raises(ValueError, match="newline"):
            Job(name="x", command="cmd", title="T", mr_title_prefix="bad\nprefix")

    def test_job_mr_title_prefix_without_newline_accepted(self) -> None:
        job = Job(name="x", command="cmd", title="T", mr_title_prefix="[mr] ")
        assert job.mr_title_prefix == "[mr] "

    def test_defaults_mr_title_prefix_with_newline_rejected(self) -> None:
        with pytest.raises(ValueError, match="newline"):
            JobDefaults(mr_title_prefix="bad\nprefix")

    def test_defaults_mr_title_prefix_without_newline_accepted(self) -> None:
        defaults = JobDefaults(mr_title_prefix="[mr] ")
        assert defaults.mr_title_prefix == "[mr] "

    def test_defaults_commit_title_prefix_with_newline_rejected(self) -> None:
        with pytest.raises(ValueError, match="newline"):
            JobDefaults(commit_title_prefix="bad\nprefix")

    def test_defaults_commit_title_prefix_without_newline_accepted(self) -> None:
        defaults = JobDefaults(commit_title_prefix="[ok] ")
        assert defaults.commit_title_prefix == "[ok] "


class TestTags:
    @pytest.mark.parametrize("tag", ["weekly", "nightly-build", "tier_1", "Weekly2"])
    def test_valid_tags_accepted(self, tag: str) -> None:
        job = Job(name="j", command="cmd", title="T", tags=[tag])
        assert job.tags == [tag]

    @pytest.mark.parametrize("tag", ["has space", "comma,tag", "dot.tag", ""])
    def test_invalid_tags_rejected(self, tag: str) -> None:
        with pytest.raises(ValueError, match="invalid tag"):
            Job(name="j", command="cmd", title="T", tags=[tag])

    def test_disabled_and_tags_together_rejected(self) -> None:
        with pytest.raises(ValueError, match="both 'disabled' and 'tags'"):
            Job(name="j", command="cmd", title="T", disabled=True, tags=["weekly"])

    def test_plain_job_is_enabled(self) -> None:
        assert Job(name="j", command="cmd", title="T").effective_tags() == {"enabled"}

    def test_disabled_job_is_disabled_tag(self) -> None:
        job = Job(name="j", command="cmd", title="T", disabled=True)
        assert job.effective_tags() == {"disabled"}

    def test_explicit_tags_replace_default(self) -> None:
        job = Job(name="j", command="cmd", title="T", tags=["weekly"])
        assert job.effective_tags() == {"weekly"}

    def test_explicit_enabled_keeps_job_in_default_run(self) -> None:
        job = Job(name="j", command="cmd", title="T", tags=["enabled", "weekly"])
        assert job.effective_tags() == {"enabled", "weekly"}


class TestJobInjectedEnv:
    def test_always_adds_name_and_branches(self) -> None:
        # Every job command gets RA_JOB_NAME, RA_JOB_BRANCH, and RA_JOB_BASE_BRANCH,
        # even with nothing else to add. base_branch is unset, so it defaults to
        # trunk().
        job = Job(name="foo", command="c", title="t", branch_prefix="repoactive/")
        assert job.injected_env() == {
            RA_JOB_NAME_ENV: "foo",
            RA_JOB_BRANCH_ENV: "repoactive/foo",
            RA_JOB_BASE_BRANCH_ENV: "trunk()",
        }

    def test_base_branch_uses_job_override(self) -> None:
        # A configured base_branch is exposed verbatim instead of trunk().
        job = Job(
            name="foo", command="c", title="t", branch_prefix="repoactive/", base_branch="release"
        )
        assert job.injected_env()[RA_JOB_BASE_BRANCH_ENV] == "release"

    def test_adds_config_source_dir(self) -> None:
        job = Job(
            name="foo",
            command="c",
            title="t",
            branch_prefix="repoactive/",
            config_source_dir="/cfg",
        )
        assert job.injected_env() == {
            RA_JOB_NAME_ENV: "foo",
            RA_JOB_BRANCH_ENV: "repoactive/foo",
            RA_JOB_BASE_BRANCH_ENV: "trunk()",
            RA_CONFIG_SOURCE_DIR_ENV: "/cfg",
        }


class TestResolveGrantedSecrets:
    def test_reads_granted_values_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A_SECRET", "one")
        monkeypatch.setenv("B_SECRET", "two")
        job = Job(name="foo", command="c", title="t", secret_env=["A_SECRET", "B_SECRET"])
        assert job.resolve_granted_secrets() == {"A_SECRET": "one", "B_SECRET": "two"}

    def test_empty_without_secret_env(self) -> None:
        assert Job(name="foo", command="c", title="t").resolve_granted_secrets() == {}

    def test_raises_on_first_unset_granted_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("A_SECRET", raising=False)
        job = Job(name="foo", command="c", title="t", secret_env=["A_SECRET"])
        with pytest.raises(
            MissingSecretError, match="requires secret A_SECRET, not set"
        ) as excinfo:
            job.resolve_granted_secrets()
        assert excinfo.value.name == "A_SECRET"


class TestAdHocJobName:
    def test_spaces_become_dashes(self) -> None:
        assert ad_hoc_job_name("just update-flake") == "just-update-flake"

    def test_punctuation_collapses_into_single_dashes(self) -> None:
        assert ad_hoc_job_name("sed -i 's/a/b/' x.txt") == "sed-i-s-a-b-x-txt"

    def test_surrounding_separators_are_dropped(self) -> None:
        assert ad_hoc_job_name("  ./build.sh  ") == "build-sh"

    def test_long_command_is_truncated_without_a_trailing_dash(self) -> None:
        name = ad_hoc_job_name("uv run " + "x" * 60 + " last")
        assert len(name) == _AD_HOC_NAME_MAX_LEN
        assert not name.endswith("-")

    def test_command_without_usable_characters_falls_back(self) -> None:
        assert ad_hoc_job_name("!!! ???") == "ad-hoc"

    def test_name_is_stable_across_calls(self) -> None:
        # The branch of an ad-hoc job is reused across runs, so the same
        # command must always yield the same name.
        assert ad_hoc_job_name("uv lock --upgrade") == ad_hoc_job_name("uv lock --upgrade")


class TestAdHocJob:
    def test_derives_the_name_from_the_command(self) -> None:
        assert AdHocJob(command="just update-flake").job_name == "just-update-flake"

    def test_explicit_name_wins(self) -> None:
        assert AdHocJob(command="just update-flake", name="flake").job_name == "flake"

    def test_empty_name_is_not_replaced_by_the_derived_one(self) -> None:
        assert AdHocJob(command="just update-flake", name="").job_name == ""


class TestLoadAdHocConfig:
    def test_builds_the_job_without_any_config_file(self) -> None:
        cfg = load_config([], ad_hoc=AdHocJob(command="just update-flake"))
        assert [j.name for j in cfg.jobs] == ["just-update-flake"]
        job = cfg.jobs[0]
        assert job.command == "just update-flake"
        assert job.title == "Run 'just update-flake'"
        assert job.effective_tags() == {"enabled"}

    def test_commit_subject_carries_no_prefix(self) -> None:
        cfg = load_config([], ad_hoc=AdHocJob(command="just update-flake"))
        job = cfg.jobs[0].resolve(cfg.job_defaults)
        assert job.commit_title_prefix == ""
        assert job.mr_title_prefix == "[repoactive] "

    def test_command_containing_a_quote_is_quoted_with_the_other_one(self) -> None:
        cfg = load_config([], ad_hoc=AdHocJob(command="sed -i 's/a/b/' x.txt"))
        assert cfg.jobs[0].title == "Run \"sed -i 's/a/b/' x.txt\""

    def test_command_belongs_to_no_config_directory(self) -> None:
        cfg = load_config([], ad_hoc=AdHocJob(command="echo hi"))
        assert cfg.jobs[0].config_source_dir is None

    def test_multi_line_command_title_is_marked_as_cut_off(self) -> None:
        cfg = load_config([], ad_hoc=AdHocJob(command="set -e\nmake all\n"))
        assert cfg.jobs[0].title == "Run 'set -e ...'"

    def test_empty_command_is_rejected(self) -> None:
        with pytest.raises(EmptyAdHocCommandError):
            load_config([], ad_hoc=AdHocJob(command="   "))

    def test_explicit_name_names_the_job(self) -> None:
        cfg = load_config([], ad_hoc=AdHocJob(command="just update-flake", name="flake"))
        assert [j.name for j in cfg.jobs] == ["flake"]

    def test_empty_explicit_name_is_rejected(self) -> None:
        # An empty name is a given name, not a missing one: it must fail
        # validation instead of falling back to the derived name.
        with pytest.raises(ConfigError, match="--ad-hoc") as excinfo:
            load_config([], ad_hoc=AdHocJob(command="just fmt", name=""))
        assert "invalid job name" in str(excinfo.value)

    def test_invalid_explicit_name_is_reported_against_the_option(self) -> None:
        with pytest.raises(ConfigError, match="--ad-hoc") as excinfo:
            load_config([], ad_hoc=AdHocJob(command="echo hi", name="bad name"))
        assert "invalid job name" in str(excinfo.value)

    def test_job_defaults_from_the_config_apply(self, tmp_path: Path) -> None:
        f = tmp_path / ".repoactive.toml"
        f.write_text('[job-defaults]\nbranch_prefix = "tmp/"\ntimeout = "9m"\n')
        cfg = load_config([f], ad_hoc=AdHocJob(command="echo hi"))
        job = cfg.jobs[0].resolve(cfg.job_defaults)
        assert job.branch_name() == "tmp/echo-hi"
        assert job.timeout == "9m"

    def test_configured_jobs_are_kept(self, tmp_path: Path) -> None:
        f = tmp_path / ".repoactive.toml"
        f.write_text('[job.x]\ncommand = "echo"\ntitle = "X"\n')
        cfg = load_config([f], ad_hoc=AdHocJob(command="echo hi"))
        assert [j.name for j in cfg.jobs] == ["x", "echo-hi"]

    def test_set_override_wins_over_the_ad_hoc_job(self) -> None:
        cfg = load_config(
            [], overrides=['job.echo-hi.timeout = "5m"'], ad_hoc=AdHocJob(command="echo hi")
        )
        assert cfg.jobs[0].timeout == "5m"

    def test_name_taken_by_a_configured_job_is_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / ".repoactive.toml"
        f.write_text('[job.echo-hi]\ncommand = "echo"\ntitle = "X"\ntags = ["nightly"]\n')
        with pytest.raises(AdHocNameCollisionError, match="--ad-hoc-name"):
            load_config([f], ad_hoc=AdHocJob(command="echo hi"))
