"""Tests for the job generator."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from repoactive.generator import (
    FragmentShape,
    GeneratedJobError,
    build_generated_jobs,
    load_job_specs,
)
from tests.builders import _gen


class TestFragmentShape:
    def test_accepts_job_tables(self) -> None:
        shape = FragmentShape.model_validate({"job": {"a": {"command": "cmd"}}})
        assert shape.job == {"a": {"command": "cmd"}}

    def test_empty_fragment_yields_no_jobs(self) -> None:
        assert FragmentShape.model_validate({}).job == {}

    @pytest.mark.parametrize("key", ["job-defaults", "platform", "unknown"])
    def test_other_top_level_keys_rejected(self, key: str) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            FragmentShape.model_validate({key: {}, "job": {"a": {"command": "cmd"}}})

    def test_error_names_each_unexpected_key(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            FragmentShape.model_validate({"platform": {}, "job-defaults": {}})
        assert {e["loc"] for e in exc_info.value.errors()} == {("platform",), ("job-defaults",)}

    def test_non_table_job_entry_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            FragmentShape.model_validate({"job": {"foo": "hello"}})
        assert exc_info.value.errors()[0]["loc"] == ("job", "foo")


class TestBuildGeneratedJobs:
    def test_inherits_tags_depends_on_and_records_generator(self) -> None:
        gen = _gen(tags=["weekly"])
        specs = {"child": {"command": "c", "title": "Child"}}
        [job] = build_generated_jobs(
            generator=gen, specs=specs, run_names={"gen"}, all_config_names=set()
        )
        assert job.tags == ["weekly"]
        assert job.depends_on == ["gen"]
        assert job.generated_by == "gen"

    def test_inherits_config_source_dir(self) -> None:
        gen = _gen().model_copy(update={"config_source_dir": "/cfg"})
        [job] = build_generated_jobs(
            generator=gen,
            specs={"child": {"command": "c", "title": "Child"}},
            run_names={"gen"},
            all_config_names=set(),
        )
        assert job.config_source_dir == "/cfg"

    def test_plain_generator_children_inherit_enabled(self) -> None:
        # A plain generator carries the implicit 'enabled' tag; children do too.
        [job] = build_generated_jobs(
            generator=_gen(),
            specs={"child": {"command": "c", "title": "Child"}},
            run_names={"gen"},
            all_config_names=set(),
        )
        assert job.tags == ["enabled"]

    def test_spec_tags_override_inheritance(self) -> None:
        [job] = build_generated_jobs(
            generator=_gen(tags=["weekly"]),
            specs={"child": {"command": "c", "title": "Child", "tags": ["daily"]}},
            run_names={"gen"},
            all_config_names=set(),
        )
        assert job.tags == ["daily"]

    def test_disabled_spec_keeps_no_tags(self) -> None:
        # 'disabled' and 'tags' are mutually exclusive, so an emitted job that
        # sets disabled does not also inherit the generator's tags.
        [job] = build_generated_jobs(
            generator=_gen(tags=["weekly"]),
            specs={"child": {"command": "c", "title": "Child", "disabled": True}},
            run_names={"gen"},
            all_config_names=set(),
        )
        assert job.tags == []
        assert job.disabled is True

    def test_inherits_cooldown_period(self) -> None:
        [job] = build_generated_jobs(
            generator=_gen(cooldown_period="7d"),
            specs={"child": {"command": "c", "title": "Child"}},
            run_names={"gen"},
            all_config_names=set(),
        )
        assert job.cooldown_period == "7d"

    def test_spec_cooldown_overrides_inheritance(self) -> None:
        [job] = build_generated_jobs(
            generator=_gen(cooldown_period="7d"),
            specs={"child": {"command": "c", "title": "Child", "cooldown_period": "1d"}},
            run_names={"gen"},
            all_config_names=set(),
        )
        assert job.cooldown_period == "1d"

    def test_sibling_depends_on_allowed(self) -> None:
        specs = {
            "a": {"command": "c", "title": "A"},
            "b": {"command": "c", "title": "B", "depends_on": ["a"]},
        }
        jobs = build_generated_jobs(
            generator=_gen(), specs=specs, run_names={"gen"}, all_config_names=set()
        )
        assert jobs[1].depends_on == ["a"]

    def test_name_collision_with_run_job_raises(self) -> None:
        with pytest.raises(GeneratedJobError, match="collides"):
            build_generated_jobs(
                generator=_gen(),
                specs={"taken": {"command": "c", "title": "T"}},
                run_names={"gen", "taken"},
                all_config_names=set(),
            )

    def test_name_collision_with_unselected_config_job_raises(self) -> None:
        with pytest.raises(GeneratedJobError, match="collides"):
            build_generated_jobs(
                generator=_gen(),
                specs={"disabled-job": {"command": "c", "title": "T"}},
                run_names={"gen"},
                all_config_names={"disabled-job"},
            )

    def test_nested_generator_raises(self) -> None:
        with pytest.raises(GeneratedJobError, match="no recursion"):
            build_generated_jobs(
                generator=_gen(),
                specs={"child": {"command": "c", "title": "T", "emits_jobs": True}},
                run_names={"gen"},
                all_config_names=set(),
            )

    def test_sibling_cycle_raises(self) -> None:
        specs = {
            "a": {"command": "c", "title": "A", "depends_on": ["b"]},
            "b": {"command": "c", "title": "B", "depends_on": ["a"]},
        }
        with pytest.raises(GeneratedJobError, match="circular dependency"):
            build_generated_jobs(
                generator=_gen(), specs=specs, run_names={"gen"}, all_config_names=set()
            )

    def test_self_dependency_raises(self) -> None:
        specs = {"a": {"command": "c", "title": "A", "depends_on": ["a"]}}
        with pytest.raises(GeneratedJobError, match="circular dependency"):
            build_generated_jobs(
                generator=_gen(), specs=specs, run_names={"gen"}, all_config_names=set()
            )

    def test_unknown_dependency_raises(self) -> None:
        with pytest.raises(GeneratedJobError, match="not in this run"):
            build_generated_jobs(
                generator=_gen(),
                specs={"child": {"command": "c", "title": "T", "depends_on": ["ghost"]}},
                run_names={"gen"},
                all_config_names=set(),
            )

    def test_invalid_spec_raises(self) -> None:
        with pytest.raises(GeneratedJobError, match="invalid"):
            build_generated_jobs(
                generator=_gen(),
                specs={"child": {"command": "c", "title": "T", "bogus": 1}},
                run_names={"gen"},
                all_config_names=set(),
            )

    def test_secret_env_marked_in_static_config_allowed(self) -> None:
        # An emitted job may grant a secret the static config already marked.
        [job] = build_generated_jobs(
            generator=_gen(),
            specs={"child": {"command": "c", "title": "Child", "secret_env": ["FOO"]}},
            run_names={"gen"},
            all_config_names=set(),
            marked_secret_names=frozenset({"FOO"}),
        )
        assert job.secret_env == ["FOO"]

    def test_secret_env_not_marked_in_static_config_raises(self) -> None:
        # A secret first introduced by an emitted job would not be stripped from
        # the other jobs' environments (the strip set is static-config derived),
        # so it is rejected (ADR 0017).
        with pytest.raises(GeneratedJobError, match="not marked in the static config"):
            build_generated_jobs(
                generator=_gen(),
                specs={"child": {"command": "c", "title": "Child", "secret_env": ["FOO"]}},
                run_names={"gen"},
                all_config_names=set(),
                marked_secret_names=frozenset(),
            )


class TestLoadJobSpecs:
    def test_merges_sorted_toml_fragments(self, tmp_path: Path) -> None:
        (tmp_path / "01.toml").write_text('[job.a]\ncommand = "c"\ntitle = "A"\n')
        (tmp_path / "02.toml").write_text('[job.b]\ncommand = "c"\ntitle = "B"\n')
        specs = load_job_specs(tmp_path)
        assert list(specs) == ["a", "b"]

    def test_empty_directory_yields_nothing(self, tmp_path: Path) -> None:
        assert load_job_specs(tmp_path) == {}

    def test_non_table_job_entry_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "01.toml").write_text('[job]\nfoo = "hello"\n')
        with pytest.raises(ValidationError, match=r"Input should be a valid dictionary"):
            load_job_specs(tmp_path)

    def test_job_defaults_in_fragment_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "01.toml").write_text(
            '[job-defaults]\ntimeout = "5m"\n[job.a]\ncommand = "c"\ntitle = "A"\n'
        )
        with pytest.raises(ValidationError, match=r"job-defaults"):
            load_job_specs(tmp_path)
