from pathlib import Path

from typer.testing import CliRunner

from repoactive.cli import app

runner = CliRunner()


def _write_job(path: Path, name: str) -> None:
    path.write_text(f'[[job]]\nname = "{name}"\ncommand = "echo"\ntitle = "{name}"\n')


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
