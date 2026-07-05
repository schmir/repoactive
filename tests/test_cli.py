"""Tests for the CLI commands."""

import json
import logging
import re
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.logging import RichHandler
from typer.testing import CliRunner

from repoactive.cli import LOCK_HELD_EXIT_CODE, _setup_logging, app
from repoactive.jj import CommandFailedError, JobCommit
from repoactive.lock import RunLockHeldError
from repoactive.platforms import PlatformTokenNotSetError
from repoactive.platforms.base import PlatformError
from repoactive.runner import RunMode, RunSummary, UnknownJobsError, UnknownTagsError

runner = CliRunner()


def _write_job(path: Path, name: str) -> None:
    path.write_text(f'[job.{name}]\ncommand = "echo"\ntitle = "{name}"\n')


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
    the styling splits option names like ``--debug`` across escape sequences.
    """
    return _ANSI_RE.sub("", output)


class TestDebugOption:
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
    def test_debug_flag_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REPOACTIVE_LOG_LEVEL", "warning")
        with patch("logging.basicConfig") as basic_config:
            _setup_logging(debug=True)
        basic_config.assert_called_once()
        assert basic_config.call_args.kwargs["level"] == logging.DEBUG

    def test_env_sets_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REPOACTIVE_LOG_LEVEL", "info")
        with patch("logging.basicConfig") as basic_config:
            _setup_logging(debug=False)
        basic_config.assert_called_once()
        assert basic_config.call_args.kwargs["level"] == "INFO"

    def test_logs_go_through_rich_handler(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("REPOACTIVE_LOG_HANDLER", raising=False)
        monkeypatch.delenv("REPOACTIVE_UI", raising=False)
        with patch("logging.basicConfig") as basic_config:
            _setup_logging(debug=True)
        (handler,) = basic_config.call_args.kwargs["handlers"]
        assert isinstance(handler, RichHandler)

    def test_plain_handler_uses_stdlib_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REPOACTIVE_LOG_HANDLER", "plain")
        with patch("logging.basicConfig") as basic_config:
            _setup_logging(debug=True)
        basic_config.assert_called_once_with(level=logging.DEBUG)

    def test_unset_leaves_logging_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("REPOACTIVE_LOG_LEVEL", raising=False)
        with patch("logging.basicConfig") as basic_config:
            _setup_logging(debug=False)
        basic_config.assert_not_called()


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
    def test_runs_jobs_and_succeeds(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        cfg = repo / "config.toml"
        _write_job(cfg, "a")
        with patch("repoactive.cli.run_all", return_value=RunSummary()) as run_all:
            result = runner.invoke(app, ["run", "--repo", str(repo), "--config", str(cfg)])
        assert result.exit_code == 0
        kwargs = run_all.call_args.kwargs
        assert kwargs["repo_path"] == repo
        assert kwargs["mode"] is RunMode.local
        assert kwargs["requested_names"] is None
        assert kwargs["requested_tags"] is None
        assert kwargs["platform"] is None

    def test_failed_summary_exits_nonzero(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        cfg = repo / "config.toml"
        _write_job(cfg, "a")
        summary = RunSummary(failed={"a": RuntimeError("boom")})
        with patch("repoactive.cli.run_all", return_value=summary):
            result = runner.invoke(app, ["run", "--repo", str(repo), "--config", str(cfg)])
        assert result.exit_code == 1

    def test_lock_held_exits_with_distinct_code(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        cfg = repo / "config.toml"
        _write_job(cfg, "a")
        err = RunLockHeldError(repo / ".jj" / "repoactive.lock", "pid=999")
        with patch("repoactive.cli.run_all", side_effect=err):
            result = runner.invoke(app, ["run", "--repo", str(repo), "--config", str(cfg)])
        assert result.exit_code == LOCK_HELD_EXIT_CODE
        assert "Error: another repoactive run is in progress" in result.output

    def test_unknown_job_reports_error_without_traceback(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        cfg = repo / "config.toml"
        _write_job(cfg, "a")
        err = UnknownJobsError({"nope"})
        with patch("repoactive.cli.run_all", side_effect=err):
            result = runner.invoke(app, ["run", "--repo", str(repo), "--config", str(cfg), "nope"])
        assert result.exit_code == 1
        assert "Error: unknown job(s): nope" in result.output
        assert "Traceback" not in result.output

    def test_unknown_tag_reports_error_without_traceback(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        cfg = repo / "config.toml"
        _write_job(cfg, "a")
        err = UnknownTagsError({"weekley"})
        with patch("repoactive.cli.run_all", side_effect=err):
            result = runner.invoke(
                app, ["run", "--repo", str(repo), "--config", str(cfg), "--tag", "weekley"]
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

    def test_passes_jobs_and_tags(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        cfg = repo / "config.toml"
        _write_job(cfg, "a")
        with patch("repoactive.cli.run_all", return_value=RunSummary()) as run_all:
            result = runner.invoke(
                app,
                ["run", "--repo", str(repo), "--config", str(cfg), "--tag", "x", "a"],
            )
        assert result.exit_code == 0
        kwargs = run_all.call_args.kwargs
        assert kwargs["requested_names"] == ["a"]
        assert kwargs["requested_tags"] == ["x"]

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

    def test_non_publish_mode_skips_platform(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        cfg = repo / "config.toml"
        _write_job(cfg, "a")
        with (
            patch("repoactive.cli.run_all", return_value=RunSummary()),
            patch("repoactive.cli.get_platform") as get_platform,
        ):
            result = runner.invoke(
                app,
                ["run", "--repo", str(repo), "--config", str(cfg), "--mode", "push"],
            )
        assert result.exit_code == 0
        get_platform.assert_not_called()

    def test_non_colocated_repo_exits_nonzero(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["run", "--repo", str(tmp_path)])
        assert result.exit_code == 1

    def test_plain_git_repo_is_colocated_in_place(self, tmp_path: Path) -> None:
        repo = tmp_path
        (repo / ".git").mkdir()
        cfg = repo / "config.toml"
        _write_job(cfg, "a")
        jj = MagicMock()

        def fake_init() -> None:
            (repo / ".jj").mkdir()

        jj.git_init_colocate.side_effect = fake_init
        with (
            patch("repoactive.cli.JJ", return_value=jj),
            patch("repoactive.cli.run_all", return_value=RunSummary()),
        ):
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
        jj.git_init_colocate.assert_called_once()
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
        (repo / ".git").mkdir()
        jj = MagicMock()
        with patch("repoactive.cli.JJ", return_value=jj):
            result = runner.invoke(app, ["run", "--repo", str(repo)])
        assert result.exit_code == 1
        jj.git_init_colocate.assert_not_called()

    def test_missing_config_exits_nonzero(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        result = runner.invoke(app, ["run", "--repo", str(repo)])
        assert result.exit_code == 1

    def test_missing_jj_points_to_install_docs(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        with patch("repoactive.jj.shutil.which", return_value=None):
            result = runner.invoke(app, ["run", "--repo", str(repo)])
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


def _commit(name: str, age: str = "1 day ago") -> JobCommit:
    return JobCommit(
        commit_id=f"abc{name}",
        change_id=f"chg{name}",
        job_names={name},
        subject=f"subject {name}",
        relative_age=age,
    )


class TestRecentCommits:
    def test_non_colocated_repo_exits_nonzero(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["recent-commits", "--repo", str(tmp_path)])
        assert result.exit_code == 1

    def test_invalid_duration_exits_nonzero(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        result = runner.invoke(app, ["recent-commits", "--repo", str(repo), "--within", "nope"])
        assert result.exit_code == 1

    def test_jj_failure_reports_error_without_traceback(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        jj = MagicMock()
        jj.recent_job_commits.side_effect = CommandFailedError("jj", ("log",), "no trunk()")
        with patch("repoactive.cli.JJ", return_value=jj):
            result = runner.invoke(app, ["recent-commits", "--repo", str(repo)])
        assert result.exit_code == 1
        assert "Error: jj log failed" in result.output
        assert "Traceback" not in result.output

    def test_no_commits_reports_message(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        jj = MagicMock()
        jj.recent_job_commits.return_value = []
        with patch("repoactive.cli.JJ", return_value=jj):
            result = runner.invoke(app, ["recent-commits", "--repo", str(repo)])
        assert result.exit_code == 0
        assert "No matching commits found." in result.stdout

    def test_lists_commits(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        jj = MagicMock()
        jj.recent_job_commits.return_value = [_commit("alpha"), _commit("beta")]
        with patch("repoactive.cli.JJ", return_value=jj):
            result = runner.invoke(app, ["recent-commits", "--repo", str(repo)])
        assert result.exit_code == 0
        assert "alpha" in result.stdout
        assert "beta" in result.stdout
        assert "subject alpha" in result.stdout

    def test_filters_by_job_name(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        jj = MagicMock()
        jj.recent_job_commits.return_value = [_commit("alpha"), _commit("beta")]
        with patch("repoactive.cli.JJ", return_value=jj):
            result = runner.invoke(app, ["recent-commits", "--repo", str(repo), "alpha"])
        assert result.exit_code == 0
        assert "subject alpha" in result.stdout
        assert "subject beta" not in result.stdout

    def test_status_merged_uses_trunk_revset(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        jj = MagicMock()
        jj.recent_job_commits.return_value = [_commit("alpha")]
        with patch("repoactive.cli.JJ", return_value=jj):
            result = runner.invoke(
                app, ["recent-commits", "--repo", str(repo), "--status", "merged"]
            )
        assert result.exit_code == 0
        assert jj.recent_job_commits.call_args.kwargs["revset"] == "::trunk()"

    def test_status_unmerged_uses_negated_trunk_revset(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        jj = MagicMock()
        jj.recent_job_commits.return_value = [_commit("alpha")]
        with patch("repoactive.cli.JJ", return_value=jj):
            result = runner.invoke(
                app, ["recent-commits", "--repo", str(repo), "--status", "unmerged"]
            )
        assert result.exit_code == 0
        assert jj.recent_job_commits.call_args.kwargs["revset"] == "~(::trunk())"

    def test_status_all_uses_all_revset_and_passes_cutoff(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        jj = MagicMock()
        jj.recent_job_commits.return_value = [_commit("alpha")]
        with patch("repoactive.cli.JJ", return_value=jj):
            result = runner.invoke(app, ["recent-commits", "--repo", str(repo)])
        assert result.exit_code == 0
        args, kwargs = jj.recent_job_commits.call_args
        assert kwargs["revset"] == "all()"
        assert isinstance(args[0], datetime)
