import json
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from repoactive.cli import app
from repoactive.jj import JobCommit
from repoactive.runner import RunMode, RunSummary

runner = CliRunner()


def _write_job(path: Path, name: str) -> None:
    path.write_text(f'[[job]]\nname = "{name}"\ncommand = "echo"\ntitle = "{name}"\n')


def _make_repo(tmp_path: Path) -> Path:
    """Create a directory that passes the colocated-repo check."""
    (tmp_path / ".jj").mkdir()
    (tmp_path / ".git").mkdir()
    return tmp_path


class TestVersion:
    def test_version_flag_prints_version_and_exits(self) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert result.stdout.strip() == version("repoactive")


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
        cfg.write_text('[[job]]\nname = "a"\nbogus = true\n')
        result = runner.invoke(app, ["validate-config", "--config", str(cfg)])
        assert result.exit_code == 1
        assert f"Invalid config in {cfg}:" in result.output

    def test_missing_config_reports_error_and_names_file(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.toml"
        result = runner.invoke(app, ["validate-config", "--config", str(missing)])
        assert result.exit_code == 1
        assert f"Invalid config in {missing}:" in result.output


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
        assert kwargs["requested_jobs"] is None
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
        assert kwargs["requested_jobs"] == ["a"]
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
            result = runner.invoke(app, ["run", "--repo", str(repo), "--config", str(cfg)])
        assert result.exit_code == 0
        jj.git_init_colocate.assert_called_once()
        assert "jj git init --colocate" in result.output
        assert "To undo" in result.output

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
        job_name=name,
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
