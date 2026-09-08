"""Tests for the CLI commands."""

import json
import logging
import re
import subprocess
from collections.abc import Generator
from importlib.metadata import version
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.logging import RichHandler
from typer.testing import CliRunner

from repoactive.cli import LOCK_HELD_EXIT_CODE, _setup_logging, app
from repoactive.jj import JJ, CommandFailedError
from repoactive.lock import run_lock
from repoactive.platforms import PlatformTokenNotSetError
from repoactive.platforms.base import PlatformError
from repoactive.runner import RunMode, RunSummary

runner = CliRunner()


@pytest.fixture
def root_logger() -> Generator[logging.Logger, None, None]:
    """Provide a clean root logger and restore its state after the test."""
    logger = logging.getLogger()
    previous_handlers = logger.handlers[:]
    previous_level = logger.level
    logger.handlers.clear()
    logger.setLevel(logging.WARNING)
    try:
        yield logger
    finally:
        logger.handlers[:] = previous_handlers
        logger.setLevel(previous_level)


def _setup_test_logging(logger: logging.Logger, *, debug: bool) -> None:
    """Configure logging after removing handlers added by pytest."""
    logger.handlers.clear()
    _setup_logging(debug)


def _job_toml(name: str) -> str:
    return f'[job.{name}]\ncommand = "echo"\ntitle = "{name}"\n'


def _write_job(path: Path, name: str) -> None:
    path.write_text(_job_toml(name))


def _make_repo(tmp_path: Path) -> Path:
    """Create a directory that passes the colocated-repo check."""
    (tmp_path / ".jj").mkdir()
    (tmp_path / ".git").mkdir()
    return tmp_path


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    """Strip ANSI style sequences from CLI output.

    typer forces terminal mode (and thus rich's styled help output) when
    GITHUB_ACTIONS, FORCE_COLOR, or PY_COLORS is set, even under CliRunner;
    the styling splits option names like --debug across escape sequences.
    """
    return _ANSI_RE.sub("", output)


class TestDebugOption:
    @pytest.mark.slow
    def test_all_jj_commands_expose_debug(self) -> None:
        for command in ("run", "validate-config", "recent-commits"):
            result = runner.invoke(app, [command, "--help"], env={"COLUMNS": "200"})
            assert result.exit_code == 0
            assert "--debug" in _plain(result.output), command


class TestVersion:
    def test_version_flag_prints_version_and_exits(self) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert result.stdout.strip() == version("repoactive")


class TestSetupLogging:
    def test_debug_flag_wins_over_env(
        self, monkeypatch: pytest.MonkeyPatch, root_logger: logging.Logger
    ) -> None:
        monkeypatch.setenv("REPOACTIVE_LOG_LEVEL", "warning")
        _setup_test_logging(root_logger, debug=True)
        assert root_logger.level == logging.DEBUG

    def test_env_sets_level(
        self, monkeypatch: pytest.MonkeyPatch, root_logger: logging.Logger
    ) -> None:
        monkeypatch.setenv("REPOACTIVE_LOG_LEVEL", "info")
        _setup_test_logging(root_logger, debug=False)
        assert root_logger.level == logging.INFO

    def test_logs_go_through_rich_handler(
        self, monkeypatch: pytest.MonkeyPatch, root_logger: logging.Logger
    ) -> None:
        monkeypatch.delenv("REPOACTIVE_LOG_HANDLER", raising=False)
        monkeypatch.delenv("REPOACTIVE_UI", raising=False)
        _setup_test_logging(root_logger, debug=True)
        (handler,) = root_logger.handlers
        assert isinstance(handler, RichHandler)

    def test_plain_handler_uses_stdlib_default(
        self, monkeypatch: pytest.MonkeyPatch, root_logger: logging.Logger
    ) -> None:
        monkeypatch.setenv("REPOACTIVE_LOG_HANDLER", "plain")
        _setup_test_logging(root_logger, debug=True)
        assert root_logger.level == logging.DEBUG
        assert len(root_logger.handlers) == 1
        assert type(root_logger.handlers[0]) is logging.StreamHandler

    def test_unset_leaves_logging_unconfigured(
        self, monkeypatch: pytest.MonkeyPatch, root_logger: logging.Logger
    ) -> None:
        monkeypatch.delenv("REPOACTIVE_LOG_LEVEL", raising=False)
        _setup_test_logging(root_logger, debug=False)
        assert root_logger.handlers == []
        assert root_logger.level == logging.WARNING


class TestEnvironmentValidation:
    def test_invalid_repoactive_ui_fails_before_any_command(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app, ["validate-config", "--repo", str(tmp_path)], env={"REPOACTIVE_UI": "bogus"}
        )
        assert result.exit_code == 1
        assert "REPOACTIVE_UI" in result.output


class TestValidateConfigShowsLocations:
    def test_lists_single_config_file(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        _write_job(cfg, "a")
        result = runner.invoke(app, ["validate-config", "--config", str(cfg)])
        assert result.exit_code == 0
        assert "Configuration files:" in result.stdout
        assert str(cfg) in result.stdout
        assert "Config OK: 1 job(s) defined." in result.stdout

    def test_lists_expanded_directory_files(self, tmp_path: Path) -> None:
        conf_dir = tmp_path / ".repoactive.d"
        conf_dir.mkdir()
        _write_job(conf_dir / "01-base.toml", "a")
        _write_job(conf_dir / "02-extra.toml", "b")
        result = runner.invoke(app, ["validate-config", "--config", str(conf_dir)])
        assert result.exit_code == 0
        lines = result.stdout.splitlines()
        assert lines[0] == "Configuration files:"
        assert lines[1].strip() == str(conf_dir / "01-base.toml")
        assert lines[2].strip() == str(conf_dir / "02-extra.toml")
        assert "Config OK: 2 job(s) defined." in result.stdout

    def test_invalid_config_reports_error_and_names_file(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text("[job.a]\nbogus = true\n")
        result = runner.invoke(app, ["validate-config", "--config", str(cfg)])
        assert result.exit_code == 1
        assert f"invalid config in {cfg}:" in result.output

    def test_missing_config_reports_error_and_names_file(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.toml"
        result = runner.invoke(app, ["validate-config", "--config", str(missing)])
        assert result.exit_code == 1
        assert f"invalid config in {missing}:" in result.output

    def test_set_override_applies(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        _write_job(cfg, "a")
        result = runner.invoke(
            app,
            ["info", "jobs", "--config", str(cfg), "--set", 'job.a.title = "Overridden"'],
        )
        assert result.exit_code == 0
        assert "Overridden" in result.stdout

    def test_set_override_invalid_reports_error(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        _write_job(cfg, "a")
        result = runner.invoke(
            app, ["validate-config", "--config", str(cfg), "--set", "cooldown = 24h"]
        )
        assert result.exit_code == 1
        assert "invalid config" in result.output
        assert "--set" in result.output

    def test_missing_default_config_reports_error_like_run(self, tmp_path: Path) -> None:
        # No config anywhere: the message must match `run`'s, not be wrapped
        # in "invalid config".
        result = runner.invoke(app, ["validate-config", "--repo", str(tmp_path)])
        assert result.exit_code == 1
        assert "no configuration found" in result.output
        assert "invalid config" not in result.output


class TestInfoJobs:
    def test_shows_all_jobs_as_dependency_tree(self, tmp_path: Path) -> None:
        # 'deploy' is defined before its dependencies and 'off' is disabled;
        # the tree must nest by depends_on and include every job, with title
        # and effective tags in aligned columns.
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            "[job.deploy]\n"
            'command = "echo"\n'
            'title = "Deploy to staging"\n'
            'tags = ["nightly", "risky"]\n'
            'depends_on = ["test", "docs"]\n'
            "[job.test]\n"
            'command = "echo"\n'
            'title = "Run the test suite"\n'
            'tags = ["nightly"]\n'
            'depends_on = ["build"]\n'
            "[job.docs]\n"
            'command = "echo"\n'
            'title = "Build the docs"\n'
            'depends_on = ["build"]\n'
            "[job.build]\n"
            'command = "echo"\n'
            'title = "Build the project"\n'
            "[job.off]\n"
            'command = "echo"\n'
            'title = "Disabled job"\n'
            "disabled = true\n"
        )
        result = runner.invoke(app, ["info", "jobs", "--config", str(cfg)])
        assert result.exit_code == 0
        assert result.stdout == (
            "build           Build the project   enabled\n"
            "├── test        Run the test suite  nightly\n"
            "│   └── deploy  Deploy to staging   nightly, risky\n"
            "└── docs        Build the docs      enabled\n"
            "    └── deploy  Deploy to staging   nightly, risky\n"
            "off             Disabled job        disabled\n"
        )

    def test_invalid_config_reports_error(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text("[job.a]\nbogus = true\n")
        result = runner.invoke(app, ["info", "jobs", "--config", str(cfg)])
        assert result.exit_code == 1
        assert f"invalid config in {cfg}:" in result.output

    def test_missing_config_reports_error(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["info", "jobs", "--repo", str(tmp_path)])
        assert result.exit_code == 1
        assert "no configuration found" in result.output


class TestInfoTags:
    def test_groups_jobs_by_tag_as_dependency_tree(self, tmp_path: Path) -> None:
        # nightly-b depends on nightly-a but is defined first; it must be
        # nested under nightly-a, not listed in config or name order.
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            "[job.plain]\n"
            'command = "echo"\n'
            'title = "plain"\n'
            "[job.nightly-b]\n"
            'command = "echo"\n'
            'title = "nightly-b"\n'
            'tags = ["nightly"]\n'
            'depends_on = ["nightly-a"]\n'
            "[job.nightly-a]\n"
            'command = "echo"\n'
            'title = "nightly-a"\n'
            'tags = ["nightly", "risky"]\n'
            "[job.off]\n"
            'command = "echo"\n'
            'title = "off"\n'
            "disabled = true\n"
        )
        result = runner.invoke(app, ["info", "tags", "--config", str(cfg)])
        assert result.exit_code == 0
        assert result.stdout == (
            "disabled:\n"
            "  off            off        disabled\n"
            "enabled:\n"
            "  plain          plain      enabled\n"
            "nightly:\n"
            "  nightly-a      nightly-a  nightly, risky\n"
            "  └── nightly-b  nightly-b  nightly\n"
            "risky:\n"
            "  nightly-a      nightly-a  nightly, risky\n"
        )

    def test_diamond_dependency_shows_job_under_each_parent(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        for name, deps in (("a", []), ("b", ["a"]), ("c", ["a"]), ("d", ["b", "c"])):
            with cfg.open("a") as f:
                f.write(f'[job.{name}]\ncommand = "echo"\ntitle = "{name}"\n')
                if deps:
                    f.write(f"depends_on = {json.dumps(deps)}\n")
        result = runner.invoke(app, ["info", "tags", "--config", str(cfg)])
        assert result.exit_code == 0
        assert result.stdout == (
            "enabled:\n"
            "  a          a  enabled\n"
            "  ├── b      b  enabled\n"
            "  │   └── d  d  enabled\n"
            "  └── c      c  enabled\n"
            "      └── d  d  enabled\n"
        )

    def test_cross_tag_dependency_does_not_break_sorting(self, tmp_path: Path) -> None:
        # 'child' carries a tag its dependency does not; the sort runs over all
        # jobs, so this must not fail on the missing dependency within the tag.
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            "[job.child]\n"
            'command = "echo"\n'
            'title = "child"\n'
            'tags = ["nightly"]\n'
            'depends_on = ["parent"]\n'
            "[job.parent]\n"
            'command = "echo"\n'
            'title = "parent"\n'
        )
        result = runner.invoke(app, ["info", "tags", "--config", str(cfg)])
        assert result.exit_code == 0
        assert result.stdout == (
            "enabled:\n  parent  parent  enabled\nnightly:\n  child   child   nightly\n"
        )

    def test_invalid_config_reports_error(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text("[job.a]\nbogus = true\n")
        result = runner.invoke(app, ["info", "tags", "--config", str(cfg)])
        assert result.exit_code == 1
        assert f"invalid config in {cfg}:" in result.output

    def test_missing_config_reports_error(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["info", "tags", "--repo", str(tmp_path)])
        assert result.exit_code == 1
        assert "no configuration found" in result.output


class TestRun:
    @pytest.mark.slow
    def test_runs_jobs_and_succeeds(self, tmp_path: Path) -> None:
        repo = _init_jj_repo(tmp_path)
        cfg = repo.cwd / "config.toml"
        _write_job(cfg, "a")

        result = runner.invoke(app, ["run", "--repo", str(repo.cwd), "--config", str(cfg)])

        assert result.exit_code == 0

    @pytest.mark.slow
    def test_failed_summary_exits_nonzero(self, tmp_path: Path) -> None:
        repo = _init_jj_repo(tmp_path)
        cfg = repo.cwd / "config.toml"
        cfg.write_text('[job.a]\ncommand = "false"\ntitle = "a"\n')

        result = runner.invoke(app, ["run", "--repo", str(repo.cwd), "--config", str(cfg)])

        assert result.exit_code == 1

    def test_lock_held_exits_with_distinct_code(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        cfg = repo / "config.toml"
        _write_job(cfg, "a")
        with run_lock(repo):
            result = runner.invoke(app, ["run", "--repo", str(repo), "--config", str(cfg)])
        assert result.exit_code == LOCK_HELD_EXIT_CODE
        assert "Error: another repoactive run is in progress" in result.output

    @pytest.mark.slow
    def test_unknown_job_reports_error_without_traceback(self, tmp_path: Path) -> None:
        repo = _init_jj_repo(tmp_path)
        cfg = repo.cwd / "config.toml"
        _write_job(cfg, "a")

        result = runner.invoke(app, ["run", "--repo", str(repo.cwd), "--config", str(cfg), "nope"])

        assert result.exit_code == 1
        assert "Error: unknown job(s): nope" in result.output
        assert "Traceback" not in result.output

    @pytest.mark.slow
    def test_unknown_tag_reports_error_without_traceback(self, tmp_path: Path) -> None:
        repo = _init_jj_repo(tmp_path)
        cfg = repo.cwd / "config.toml"
        _write_job(cfg, "a")

        result = runner.invoke(
            app,
            ["run", "--repo", str(repo.cwd), "--config", str(cfg), "--tag", "weekley"],
        )

        assert result.exit_code == 1
        assert "Error: unknown tag(s): weekley" in result.output
        assert "Traceback" not in result.output

    def test_jj_failure_reports_error_without_traceback(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        cfg = repo / "config.toml"
        _write_job(cfg, "a")
        err = CommandFailedError("jj", ("git", "push"), "remote rejected")
        with patch("repoactive.cli.run_all", side_effect=err):
            result = runner.invoke(app, ["run", "--repo", str(repo), "--config", str(cfg)])
        assert result.exit_code == 1
        assert "Error: jj git push failed" in result.output
        assert "Traceback" not in result.output

    def test_unset_platform_token_reports_error_without_traceback(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        cfg = repo / "config.toml"
        _write_job(cfg, "a")
        err = PlatformTokenNotSetError("GITHUB_TOKEN")
        with (
            patch("repoactive.cli.get_platform", side_effect=err),
            patch("repoactive.cli.run_all") as run_all,
        ):
            result = runner.invoke(
                app, ["run", "--repo", str(repo), "--config", str(cfg), "--mode", "publish"]
            )
        assert result.exit_code == 1
        assert "Error: platform token not set" in result.output
        assert "Traceback" not in result.output
        run_all.assert_not_called()

    def test_rejected_platform_token_reports_error_without_traceback(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        cfg = repo / "config.toml"
        _write_job(cfg, "a")
        err = PlatformError("GitHub", "o/r", RuntimeError("401 Bad credentials"))
        with patch("repoactive.cli.get_platform", side_effect=err):
            result = runner.invoke(
                app, ["run", "--repo", str(repo), "--config", str(cfg), "--mode", "publish"]
            )
        assert result.exit_code == 1
        assert "Error: GitHub: cannot access repository" in result.output
        assert "Traceback" not in result.output

    @pytest.mark.slow
    def test_selects_named_and_tagged_jobs(self, tmp_path: Path) -> None:
        repo = _init_jj_repo(tmp_path)
        cfg = repo.cwd / "config.toml"
        cfg.write_text(
            """
[job.a]
command = "touch selected-a"
title = "a"

[job.b]
command = "touch selected-b"
title = "b"
tags = ["x"]

[job.c]
command = "touch unselected-c"
title = "c"
"""
        )

        result = runner.invoke(
            app,
            ["run", "--repo", str(repo.cwd), "--config", str(cfg), "--tag", "x", "a"],
        )

        assert result.exit_code == 0
        assert repo.bookmark_exists("repoactive/a")
        assert repo.bookmark_exists("repoactive/b")
        assert not repo.bookmark_exists("repoactive/c")

    def test_publish_mode_resolves_platform(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        cfg = repo / "config.toml"
        _write_job(cfg, "a")
        platform = MagicMock()
        with (
            patch("repoactive.cli.run_all", return_value=RunSummary()) as run_all,
            patch("repoactive.cli.get_platform", return_value=platform) as get_platform,
        ):
            result = runner.invoke(
                app,
                ["run", "--repo", str(repo), "--config", str(cfg), "--mode", "publish"],
            )
        assert result.exit_code == 0
        get_platform.assert_called_once()
        assert run_all.call_args.kwargs["platform"] is platform
        assert run_all.call_args.kwargs["mode"] is RunMode.publish

    @pytest.mark.slow
    def test_push_mode_does_not_require_platform(self, tmp_path: Path) -> None:
        repo = _init_jj_repo(tmp_path)
        cfg = repo.cwd / "config.toml"
        _write_job(cfg, "a")

        result = runner.invoke(
            app,
            ["run", "--repo", str(repo.cwd), "--config", str(cfg), "--mode", "push"],
        )

        assert result.exit_code == 0

    def test_non_colocated_repo_exits_nonzero(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["run", "--repo", str(tmp_path)])
        assert result.exit_code == 1

    @pytest.mark.slow
    def test_plain_git_repo_is_colocated_in_place(self, tmp_path: Path) -> None:
        repo = tmp_path
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        cfg = repo / "config.toml"
        _write_job(cfg, "a")

        # The hint is a rich panel whose prose wraps to the console width
        # (rich reads COLUMNS); pin it wide so the asserted phrase is not
        # split across lines when the test runs in a narrow terminal. Force
        # the panel on in case the surrounding environment turned it off.
        result = runner.invoke(
            app,
            ["run", "--repo", str(repo), "--config", str(cfg)],
            env={"COLUMNS": "200", "REPOACTIVE_UI": "interactive"},
        )

        assert result.exit_code == 0
        assert (repo / ".jj").is_dir()
        assert "jj git init --colocate" in result.output
        assert "To undo" in result.output

    def test_failing_colocate_reports_error_without_traceback(self, tmp_path: Path) -> None:
        repo = tmp_path
        (repo / ".git").mkdir()
        cfg = repo / "config.toml"
        _write_job(cfg, "a")
        jj = MagicMock()
        jj.git_init_colocate.side_effect = CommandFailedError(
            "jj", ("git", "init", "--colocate"), "boom"
        )
        with patch("repoactive.cli.JJ", return_value=jj):
            result = runner.invoke(app, ["run", "--repo", str(repo), "--config", str(cfg)])
        assert result.exit_code == 1
        assert "Error: jj git init --colocate failed" in result.output
        assert "Traceback" not in result.output

    def test_plain_git_repo_without_config_is_not_colocated(self, tmp_path: Path) -> None:
        repo = tmp_path
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)

        result = runner.invoke(app, ["run", "--repo", str(repo)])

        assert result.exit_code == 1
        assert not (repo / ".jj").exists()

    def test_missing_config_exits_nonzero(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        result = runner.invoke(app, ["run", "--repo", str(repo)])
        assert result.exit_code == 1

    def test_missing_jj_points_to_install_docs(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        result = runner.invoke(app, ["run", "--repo", str(repo)], env={"PATH": ""})
        assert result.exit_code == 1
        assert "docs.jj-vcs.dev" in result.output


class TestDumpSchema:
    def test_writes_json_schema(self, tmp_path: Path) -> None:
        out = tmp_path / "schema.json"
        result = runner.invoke(app, ["dump-schema", "--output", str(out)])
        assert result.exit_code == 0
        assert f"Wrote schema to {out}" in result.stdout
        schema = json.loads(out.read_text())
        assert schema["title"] == "Config"


def _init_jj_repo(path: Path) -> JJ:
    subprocess.run(
        ["jj", "git", "init", "--colocate", str(path)],
        check=True,
        capture_output=True,
    )
    return JJ(path)


def _add_job_commit(repo: JJ, name: str) -> None:
    repo.describe(f"subject {name}\n\nRepoactive-Job: {name}")


def _repo_with_merged_and_unmerged_jobs(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo = _init_jj_repo(repo_path)
    subprocess.run(
        ["jj", "git", "remote", "add", "origin", str(remote)],
        cwd=repo.cwd,
        check=True,
        capture_output=True,
    )
    _add_job_commit(repo, "alpha")
    repo.bookmark_set("main")
    repo.git_push_bookmarks("main")
    repo.new("main")
    _add_job_commit(repo, "beta")
    return repo_path


class TestRecentCommits:
    def test_non_colocated_repo_exits_nonzero(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["recent-commits", "--repo", str(tmp_path)])
        assert result.exit_code == 1

    def test_invalid_duration_exits_nonzero(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        result = runner.invoke(app, ["recent-commits", "--repo", str(repo), "--within", "nope"])
        assert result.exit_code == 1

    def test_jj_failure_reports_error_without_traceback(self, tmp_path: Path) -> None:
        # recent-commits has its own JJError handler, separate from run's. jj
        # resolves an unusable trunk() to root() rather than failing, so the
        # failure is injected instead of produced from repository state.
        repo = _make_repo(tmp_path)
        jj = MagicMock()
        jj.recent_job_commits.side_effect = CommandFailedError("jj", ("log",), "no trunk()")
        with patch("repoactive.cli.JJ", return_value=jj):
            result = runner.invoke(app, ["recent-commits", "--repo", str(repo)])
        assert result.exit_code == 1
        assert "Error: jj log failed" in result.output
        assert "Traceback" not in result.output

    @pytest.mark.slow
    def test_no_commits_reports_message(self, tmp_path: Path) -> None:
        repo = _init_jj_repo(tmp_path)
        result = runner.invoke(app, ["recent-commits", "--repo", str(repo.cwd)])
        assert result.exit_code == 0
        assert "No matching commits found." in result.stdout

    @pytest.mark.slow
    def test_lists_commits(self, tmp_path: Path) -> None:
        repo = _repo_with_merged_and_unmerged_jobs(tmp_path)
        result = runner.invoke(app, ["recent-commits", "--repo", str(repo)])
        assert result.exit_code == 0
        assert "alpha" in result.stdout
        assert "beta" in result.stdout
        assert "subject alpha" in result.stdout

    @pytest.mark.slow
    def test_filters_by_job_name(self, tmp_path: Path) -> None:
        repo = _repo_with_merged_and_unmerged_jobs(tmp_path)
        result = runner.invoke(app, ["recent-commits", "--repo", str(repo), "alpha"])
        assert result.exit_code == 0
        assert "subject alpha" in result.stdout
        assert "subject beta" not in result.stdout

    @pytest.mark.slow
    def test_status_merged_lists_only_merged_commits(self, tmp_path: Path) -> None:
        repo = _repo_with_merged_and_unmerged_jobs(tmp_path)
        result = runner.invoke(app, ["recent-commits", "--repo", str(repo), "--status", "merged"])
        assert result.exit_code == 0
        assert "subject alpha" in result.stdout
        assert "subject beta" not in result.stdout

    @pytest.mark.slow
    def test_status_unmerged_lists_only_unmerged_commits(self, tmp_path: Path) -> None:
        repo = _repo_with_merged_and_unmerged_jobs(tmp_path)
        result = runner.invoke(
            app, ["recent-commits", "--repo", str(repo), "--status", "unmerged"]
        )
        assert result.exit_code == 0
        assert "subject alpha" not in result.stdout
        assert "subject beta" in result.stdout

    @pytest.mark.slow
    def test_status_all_lists_merged_and_unmerged_commits(self, tmp_path: Path) -> None:
        repo = _repo_with_merged_and_unmerged_jobs(tmp_path)
        result = runner.invoke(app, ["recent-commits", "--repo", str(repo)])
        assert result.exit_code == 0
        assert "subject alpha" in result.stdout
        assert "subject beta" in result.stdout


def _commit_config(repo: JJ, name: str, files: dict[str, str], parent: str = "root()") -> None:
    """Commit files on a fresh child of parent and point bookmark name at it."""
    repo.new(parent)
    for relative, text in files.items():
        path = repo.cwd / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    repo.describe(name)
    repo.bookmark_set(name)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def _file_at(repo: JJ, revision: str, path: str) -> str:
    return subprocess.run(
        ["jj", "--no-pager", "file", "show", "-r", revision, path],
        cwd=repo.cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


class TestAdHocOption:
    @pytest.mark.slow
    def test_run_exposes_it(self) -> None:
        result = runner.invoke(app, ["run", "--help"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        assert "--ad-hoc" in _plain(result.output)
        assert "--ad-hoc-name" in _plain(result.output)

    @pytest.mark.slow
    def test_other_commands_do_not_expose_it(self) -> None:
        for command in (["validate-config"], ["info", "jobs"], ["info", "tags"]):
            result = runner.invoke(app, [*command, "--help"], env={"COLUMNS": "200"})
            assert result.exit_code == 0
            assert "--ad-hoc" not in _plain(result.output), command

    def test_name_without_a_command_is_rejected(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app, ["run", "--repo", str(_make_repo(tmp_path)), "--ad-hoc-name", "x"]
        )
        assert result.exit_code == 1
        assert "--ad-hoc-name requires --ad-hoc" in result.output

    def test_empty_command_is_rejected(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["run", "--repo", str(_make_repo(tmp_path)), "--ad-hoc", " "])
        assert result.exit_code == 1
        assert "--ad-hoc needs a command to run" in result.output

    def test_empty_name_is_rejected(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "run",
                "--repo",
                str(_make_repo(tmp_path)),
                "--ad-hoc",
                "just fmt",
                "--ad-hoc-name",
                "",
            ],
        )
        assert result.exit_code == 1
        assert "invalid job name" in result.output

    def test_runs_without_any_configuration(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        with patch("repoactive.cli.run_all", return_value=RunSummary()) as run_all:
            result = runner.invoke(app, ["run", "--repo", str(repo), "--ad-hoc", "just fmt"])
        assert result.exit_code == 0
        kwargs = run_all.call_args.kwargs
        assert [j.name for j in kwargs["config"].jobs] == ["just-fmt"]
        assert kwargs["config"].jobs[0].command == "just fmt"
        assert kwargs["requested_names"] == frozenset({"just-fmt"})

    def test_ad_hoc_name_names_the_job(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        with patch("repoactive.cli.run_all", return_value=RunSummary()) as run_all:
            result = runner.invoke(
                app,
                ["run", "--repo", str(repo), "--ad-hoc", "just fmt", "--ad-hoc-name", "fmt"],
            )
        assert result.exit_code == 0
        kwargs = run_all.call_args.kwargs
        assert [j.name for j in kwargs["config"].jobs] == ["fmt"]
        assert kwargs["requested_names"] == frozenset({"fmt"})

    def test_adds_itself_to_the_discovered_configuration(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        _write_job(repo / ".repoactive.toml", "a")
        with patch("repoactive.cli.run_all", return_value=RunSummary()) as run_all:
            result = runner.invoke(app, ["run", "--repo", str(repo), "--ad-hoc", "just fmt", "a"])
        assert result.exit_code == 0
        kwargs = run_all.call_args.kwargs
        assert [j.name for j in kwargs["config"].jobs] == ["a", "just-fmt"]
        assert kwargs["requested_names"] == frozenset({"a", "just-fmt"})

    def test_tagged_jobs_are_selected_alongside_it(self, tmp_path: Path) -> None:
        # Selection stays the union of names and tags (ADR 0002): --ad-hoc adds
        # a name, it does not suppress a --tag selection.
        repo = _make_repo(tmp_path)
        (repo / ".repoactive.toml").write_text(
            '[job.a]\ncommand = "echo"\ntitle = "a"\ntags = ["weekly"]\n'
        )
        with patch("repoactive.cli.run_all", return_value=RunSummary()) as run_all:
            result = runner.invoke(
                app, ["run", "--repo", str(repo), "--ad-hoc", "just fmt", "--tag", "weekly"]
            )
        assert result.exit_code == 0
        kwargs = run_all.call_args.kwargs
        assert kwargs["requested_names"] == frozenset({"just-fmt"})
        assert kwargs["requested_tags"] == frozenset({"weekly"})

    def test_explicit_missing_config_still_fails(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        result = runner.invoke(
            app,
            ["run", "--repo", str(repo), "-c", str(repo / "missing.toml"), "--ad-hoc", "just fmt"],
        )
        assert result.exit_code == 1
        assert "invalid config" in result.output

    def test_name_taken_by_a_configured_job_is_rejected(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        (repo / ".repoactive.toml").write_text('[job.just-fmt]\ncommand = "x"\ntitle = "x"\n')
        result = runner.invoke(app, ["run", "--repo", str(repo), "--ad-hoc", "just fmt"])
        assert result.exit_code == 1
        assert "--ad-hoc-name" in result.output

    @pytest.mark.slow
    def test_commits_the_command_output_on_its_own_branch(self, tmp_path: Path) -> None:
        repo = _init_jj_repo(tmp_path)

        result = runner.invoke(app, ["run", "--repo", str(repo.cwd), "--ad-hoc", "echo hi > out"])

        assert result.exit_code == 0
        assert repo.bookmark_exists("repoactive/echo-hi-out")


_CONFIG_REVSET_COMMANDS = (
    ["run"],
    ["validate-config"],
    ["info", "jobs"],
    ["info", "tags"],
)


class TestConfigRevsetOption:
    @pytest.mark.slow
    def test_config_reading_commands_expose_it(self) -> None:
        for command in _CONFIG_REVSET_COMMANDS:
            result = runner.invoke(app, [*command, "--help"], env={"COLUMNS": "200"})
            assert result.exit_code == 0
            assert "--config-revset" in _plain(result.output), command

    def test_commands_without_config_do_not_expose_it(self) -> None:
        for command in (["recent-commits"], ["dump-schema"]):
            result = runner.invoke(app, [*command, "--help"], env={"COLUMNS": "200"})
            assert result.exit_code == 0
            assert "--config-revset" not in _plain(result.output), command

    def test_rejects_combination_with_config(self, tmp_path: Path) -> None:
        # Rejected before anything touches the repository, so no jj is needed.
        cfg = tmp_path / "config.toml"
        _write_job(cfg, "a")
        for command in _CONFIG_REVSET_COMMANDS:
            result = runner.invoke(
                app,
                [*command, "--repo", str(tmp_path), "--config-revset", "main", "-c", str(cfg)],
            )
            assert result.exit_code == 1, command
            assert "--config-revset cannot be combined with --config" in result.output, command

    def test_rejects_a_directory_that_is_neither_git_nor_jj(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app, ["info", "jobs", "--repo", str(tmp_path), "--config-revset", "main"]
        )
        assert result.exit_code == 1
        assert "not a jj repository" in result.output
        assert not (tmp_path / ".jj").exists()


@pytest.mark.slow
class TestConfigRevset:
    def test_reads_config_from_the_revset_not_the_working_copy(self, tmp_path: Path) -> None:
        repo = _init_jj_repo(tmp_path / "repo")
        _commit_config(repo, "main", {".repoactive.toml": _job_toml("committed")})
        repo.new("root()")
        (repo.cwd / ".repoactive.toml").write_text(_job_toml("working-copy"))

        result = runner.invoke(
            app, ["info", "jobs", "--repo", str(repo.cwd), "--config-revset", "main"]
        )
        assert result.exit_code == 0
        assert "committed" in result.stdout
        assert "working-copy" not in result.stdout
        assert repo.workspace_names() == {"default"}

    def test_merges_the_trees_of_a_multi_revision_revset(self, tmp_path: Path) -> None:
        repo = _init_jj_repo(tmp_path / "repo")
        _commit_config(repo, "base", {".repoactive.toml": _job_toml("shared")})
        _commit_config(repo, "left", {".repoactive.d/a.toml": _job_toml("only-left")}, "base")
        _commit_config(repo, "right", {".repoactive.d/b.toml": _job_toml("only-right")}, "base")

        result = runner.invoke(
            app, ["info", "jobs", "--repo", str(repo.cwd), "--config-revset", "left | right"]
        )
        assert result.exit_code == 0
        assert "shared" in result.stdout
        assert "only-left" in result.stdout
        assert "only-right" in result.stdout

    def test_conflicting_merge_warns_and_carries_on(self, tmp_path: Path) -> None:
        # A conflict outside the configuration does not stop the command. The
        # warning identifies the conflicted file.
        repo = _init_jj_repo(tmp_path / "repo")
        _commit_config(repo, "base", {".repoactive.toml": _job_toml("a"), "notes.txt": "base\n"})
        _commit_config(repo, "left", {"notes.txt": "left\n"}, "base")
        _commit_config(repo, "right", {"notes.txt": "right\n"}, "base")

        result = runner.invoke(
            app, ["info", "jobs", "--repo", str(repo.cwd), "--config-revset", "left | right"]
        )
        assert result.exit_code == 0, result.output
        assert "produced file conflicts" in result.output
        assert "notes.txt" in result.output
        assert "a" in result.stdout
        assert repo.workspace_names() == {"default"}

    def test_conflicting_config_file_warns_before_failing_to_parse(self, tmp_path: Path) -> None:
        repo = _init_jj_repo(tmp_path / "repo")
        _commit_config(repo, "base", {".repoactive.toml": _job_toml("a")})
        _commit_config(repo, "left", {".repoactive.toml": _job_toml("left-only")}, "base")
        _commit_config(repo, "right", {".repoactive.toml": _job_toml("right-only")}, "base")

        result = runner.invoke(
            app, ["info", "jobs", "--repo", str(repo.cwd), "--config-revset", "left | right"]
        )
        assert ".repoactive.toml" in result.output
        assert "file conflicts" in result.output
        # Conflict markers make the configuration file invalid.
        assert result.exit_code == 1
        assert "invalid config" in result.output
        assert repo.workspace_names() == {"default"}

    def test_unknown_revset_reports_a_clean_error(self, tmp_path: Path) -> None:
        repo = _init_jj_repo(tmp_path / "repo")
        _commit_config(repo, "main", {".repoactive.toml": _job_toml("a")})

        result = runner.invoke(
            app, ["info", "jobs", "--repo", str(repo.cwd), "--config-revset", "no-such-bookmark"]
        )
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert repo.workspace_names() == {"default"}

    def test_colocates_a_plain_git_repository(self, tmp_path: Path) -> None:
        # The option converts a plain git repository before it creates a workspace.
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        subprocess.run(["git", "init", str(repo_path)], check=True, capture_output=True)
        (repo_path / ".repoactive.toml").write_text(_job_toml("committed"))
        _git(repo_path, "add", "-A")
        _git(repo_path, "-c", "user.name=T", "-c", "user.email=t@t.com", "commit", "-m", "initial")

        result = runner.invoke(
            app, ["info", "jobs", "--repo", str(repo_path), "--config-revset", "@-"]
        )
        assert result.exit_code == 0, result.output
        assert "committed" in result.stdout
        assert (repo_path / ".jj").is_dir()
        assert JJ(repo_path).workspace_names() == {"default"}

    def test_validate_config_names_the_revset(self, tmp_path: Path) -> None:
        repo = _init_jj_repo(tmp_path / "repo")
        _commit_config(repo, "main", {".repoactive.toml": _job_toml("a")})

        result = runner.invoke(
            app, ["validate-config", "--repo", str(repo.cwd), "--config-revset", "main"]
        )
        assert result.exit_code == 0
        assert "Configuration files from revset 'main':" in result.stdout
        assert "Config OK: 1 job(s) defined." in result.stdout

    def test_run_keeps_the_config_workspace_alive_for_the_jobs(self, tmp_path: Path) -> None:
        # RA_CONFIG_SOURCE_DIR points into the temporary workspace, so the job
        # can only reach its script if that workspace outlives config loading.
        repo = _init_jj_repo(tmp_path / "repo")
        _commit_config(
            repo,
            "main",
            {
                ".repoactive.toml": (
                    "[job.uses-script]\n"
                    'command = "sh $RA_CONFIG_SOURCE_DIR/gen.sh"\n'
                    'title = "run the script beside the config"\n'
                ),
                "gen.sh": "echo generated > output.txt\n",
            },
        )
        repo.new("root()")

        result = runner.invoke(app, ["run", "--repo", str(repo.cwd), "--config-revset", "main"])
        assert result.exit_code == 0, result.output
        assert repo.bookmark_exists("repoactive/uses-script")
        assert _file_at(repo, "repoactive/uses-script", "output.txt") == "generated\n"
        assert repo.workspace_names() == {"default"}
