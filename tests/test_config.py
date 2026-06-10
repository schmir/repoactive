from pathlib import Path

import pytest

from repoactive.config import Config, Job, load_config


def _platform(**kwargs: object) -> dict[str, object]:
    return {"url": "https://gitlab.com", "type": "gitlab", "token_env": "TOKEN", **kwargs}


def _job(name: str, **kwargs: object) -> dict[str, object]:
    return {"name": name, "command": "cmd", "title": f"Job {name}", **kwargs}


def _config(**kwargs: object) -> Config:
    data: dict[str, object] = {"platform": [_platform()], "jobs": [], **kwargs}
    return Config.model_validate(data)


class TestBranchName:
    def test_default_prefix(self) -> None:
        job = Job(name="foo", command="cmd", title="Foo")
        assert job.branch_name("repoactive/") == "repoactive/foo"

    def test_custom_prefix(self) -> None:
        job = Job(name="bar", command="cmd", title="Bar")
        assert job.branch_name("bot/") == "bot/bar"


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


class TestDefaults:
    def test_branch_prefix_default(self) -> None:
        cfg = _config()
        assert cfg.defaults.branch_prefix == "repoactive/"

    def test_labels_default_empty(self) -> None:
        cfg = _config()
        assert cfg.defaults.labels == []


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
            '[defaults]\nbranch_prefix = "old/"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text('[defaults]\nbranch_prefix = "new/"\n')
        cfg = load_config([base, override])
        assert cfg.defaults.branch_prefix == "new/"

    def test_merge_unset_key_preserved(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[[platform]]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
            '[defaults]\nbranch_prefix = "base/"\nmr_title_prefix = "kept"\n'
        )
        override = tmp_path / "override.toml"
        override.write_text('[defaults]\nbranch_prefix = "new/"\n')
        cfg = load_config([base, override])
        assert cfg.defaults.branch_prefix == "new/"
        assert cfg.defaults.mr_title_prefix == "kept"

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
        f.write_text('[defaults]\nbranch_prefix = "x/"\n')
        cfg = load_config([f])
        assert {p.url for p in cfg.platforms} >= {"https://github.com", "https://gitlab.com"}

    def test_second_config_may_be_partial(self, tmp_path: Path) -> None:
        base = tmp_path / "base.toml"
        base.write_text(
            '[[platform]]\nurl = "https://gitlab.com"\ntype = "gitlab"\ntoken_env = "T"\n'
        )
        partial = tmp_path / "partial.toml"
        partial.write_text('[defaults]\nbranch_prefix = "x/"\n')
        cfg = load_config([base, partial])
        assert cfg.defaults.branch_prefix == "x/"

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
