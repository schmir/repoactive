from datetime import timedelta
from pathlib import Path

import pytest

from repoactive.config import (
    Config,
    ConfigNotFoundError,
    Job,
    JobDefaults,
    default_config_paths,
    load_config,
    parse_duration,
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
        with pytest.raises(ValueError, match="Circular dependency"):
            _config(jobs=[_job("a", depends_on=["a"])])

    def test_direct_cycle_raises(self) -> None:
        with pytest.raises(ValueError, match="Circular dependency"):
            _config(
                jobs=[
                    _job("a", depends_on=["b"]),
                    _job("b", depends_on=["a"]),
                ]
            )

    def test_transitive_cycle_raises(self) -> None:
        with pytest.raises(ValueError, match="Circular dependency"):
            _config(
                jobs=[
                    _job("a", depends_on=["c"]),
                    _job("b", depends_on=["a"]),
                    _job("c", depends_on=["b"]),
                ]
            )


class TestJobDefaults:
    def test_branch_prefix_default(self) -> None:
        cfg = _config()
        assert cfg.job_defaults.branch_prefix == "repoactive/"

    def test_labels_default_empty(self) -> None:
        cfg = _config()
        assert cfg.job_defaults.labels == []


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
        with pytest.raises(ValueError, match="invalid duration"):
            parse_duration("7y")

    def test_missing_unit_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid duration"):
            parse_duration("7")

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid duration"):
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

    def test_falls_back_to_defaults(self) -> None:
        job = Job(name="x", command="cmd", title="X")
        resolved = job.resolve(JobDefaults(timeout="1h"))
        assert resolved.timeout == "1h"

    def test_per_job_overrides_defaults(self) -> None:
        job = Job(name="x", command="cmd", title="X", timeout="10m")
        resolved = job.resolve(JobDefaults(timeout="1h"))
        assert resolved.timeout == "10m"

    def test_stays_none_when_neither_set(self) -> None:
        job = Job(name="x", command="cmd", title="X")
        assert job.resolve(JobDefaults()).timeout is None


class TestLoadConfig:
    def test_minimal_config(self, tmp_path: Path) -> None:
        f = tmp_path / ".repoactive.toml"
        f.write_text(
            '[[platform]]\nurl = "https://github.com"\ntype = "github"\ntoken_env = "GH_TOKEN"\n'
            '[[job]]\nname = "x"\ncommand = "echo"\ntitle = "X"\n'
        )
        cfg = load_config([f])
        assert cfg.platforms[0].url == "https://github.com"
        assert cfg.jobs[0].name == "x"

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_config([tmp_path / "missing.toml"])

    def test_multiple_platforms(self, tmp_path: Path) -> None:
        f = tmp_path / ".repoactive.toml"
        f.write_text(
            '[[platform]]\nurl = "https://github.com"\ntype = "github"\ntoken_env = "GH_TOKEN"\n'
            '[[platform]]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "GL_TOKEN"\n'
        )
        cfg = load_config([f])
        assert [p.url for p in cfg.platforms] == ["https://github.com", "https://gitlab.com"]

    def test_merge_later_scalar_wins(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[[platform]]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "A"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text(
            '[[platform]]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "B"\n'
        )
        cfg = load_config([base, override])
        gitlab = next(p for p in cfg.platforms if p.url == "https://gitlab.com")
        assert gitlab.token_env == "B"

    def test_merge_later_nested_scalar_wins(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[[platform]]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job-defaults]\nbranch_prefix = "old/"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text('[job-defaults]\nbranch_prefix = "new/"\n')
        cfg = load_config([base, override])
        assert cfg.job_defaults.branch_prefix == "new/"

    def test_merge_unset_key_preserved(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[[platform]]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
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
            '[[platform]]\nurl = "https://github.com"\ntype = "github"\ntoken_env = "GH"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text(
            '[[platform]]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "GL"\n'
        )
        cfg = load_config([base, override])
        assert [p.url for p in cfg.platforms] == ["https://github.com", "https://gitlab.com"]

    def test_merge_jobs_new_name_appended(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[[platform]]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[[job]]\nname = "a"\ncommand = "cmd-a"\ntitle = "A"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text('[[job]]\nname = "b"\ncommand = "cmd-b"\ntitle = "B"\n')
        cfg = load_config([base, override])
        assert [j.name for j in cfg.jobs] == ["a", "b"]

    def test_merge_jobs_existing_name_updated(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[[platform]]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[[job]]\nname = "a"\ncommand = "old-cmd"\ntitle = "Old"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text('[[job]]\nname = "a"\ncommand = "new-cmd"\ntitle = "New"\n')
        cfg = load_config([base, override])
        assert len(cfg.jobs) == 1
        assert cfg.jobs[0].command == "new-cmd"
        assert cfg.jobs[0].title == "New"

    def test_merge_jobs_partial_field_override(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[[platform]]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[[job]]\nname = "a"\ncommand = "cmd"\ntitle = "A"\ndraft = false\n'
        )
        override = tmp_path / "override.toml"
        override.write_text('[[job]]\nname = "a"\ncommand = "cmd"\ntitle = "A"\ndraft = true\n')
        cfg = load_config([base, override])
        assert cfg.jobs[0].draft is True

    def test_platform_always_includes_defaults(self, tmp_path: Path) -> None:
        f = tmp_path / ".repoactive.toml"
        f.write_text('[job-defaults]\nbranch_prefix = "x/"\n')
        cfg = load_config([f])
        assert {p.url for p in cfg.platforms} >= {"https://github.com", "https://gitlab.com"}

    def test_second_config_may_be_partial(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[[platform]]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
        )
        partial = tmp_path / "partial.toml"
        partial.write_text('[job-defaults]\nbranch_prefix = "x/"\n')
        cfg = load_config([base, partial])
        assert cfg.job_defaults.branch_prefix == "x/"

    def test_second_config_invalid_depends_on_raises(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[[platform]]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[[job]]\nname = "a"\ncommand = "cmd"\ntitle = "A"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text(
            '[[job]]\nname = "b"\ncommand = "cmd"\ntitle = "B"\ndepends_on = ["nonexistent"]\n'
        )
        with pytest.raises(ValueError, match="unknown jobs"):
            load_config([base, override])

    def test_directory_reads_toml_files_sorted(self, tmp_path: Path) -> None:
        conf_dir = tmp_path / "conf.d"
        conf_dir.mkdir()
        (conf_dir / "02-override.toml").write_text('[job-defaults]\nbranch_prefix = "second/"\n')
        (conf_dir / "01-base.toml").write_text(
            '[[platform]]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job-defaults]\nbranch_prefix = "first/"\n'
            '[[job]]\nname = "a"\ncommand = "cmd"\ntitle = "A"\n'
        )
        cfg = load_config([conf_dir])
        # 02-override.toml is applied after 01-base.toml because entries are sorted
        assert cfg.job_defaults.branch_prefix == "second/"
        assert cfg.jobs[0].name == "a"

    def test_directory_ignores_non_toml_files(self, tmp_path: Path) -> None:
        conf_dir = tmp_path / "conf.d"
        conf_dir.mkdir()
        (conf_dir / "a.toml").write_text(
            '[[platform]]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[[job]]\nname = "x"\ncommand = "cmd"\ntitle = "X"\n'
        )
        (conf_dir / "README.md").write_text("not a config\n")
        cfg = load_config([conf_dir])
        assert [j.name for j in cfg.jobs] == ["x"]

    def test_directory_ignores_subdirectories_named_toml(self, tmp_path: Path) -> None:
        conf_dir = tmp_path / "conf.d"
        conf_dir.mkdir()
        (conf_dir / "nested.toml").mkdir()
        (conf_dir / "a.toml").write_text(
            '[[platform]]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[[job]]\nname = "x"\ncommand = "cmd"\ntitle = "X"\n'
        )
        cfg = load_config([conf_dir])
        assert [j.name for j in cfg.jobs] == ["x"]

    def test_directory_mixed_with_files(self, tmp_path: Path) -> None:
        conf_dir = tmp_path / "conf.d"
        conf_dir.mkdir()
        (conf_dir / "base.toml").write_text(
            '[[platform]]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[job-defaults]\nbranch_prefix = "dir/"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text('[job-defaults]\nbranch_prefix = "file/"\n')
        cfg = load_config([conf_dir, override])
        assert cfg.job_defaults.branch_prefix == "file/"


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
