"""Tests for the scripts/emit-upgrade-jobs.py repoactive generator."""

import importlib.util
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from repoactive.config import Job

_SCRIPT = Path(__file__).parent.parent / "scripts" / "emit-upgrade-jobs.py"


def _load_module() -> ModuleType:
    """Import the hyphen-named generator script as a module."""
    spec = importlib.util.spec_from_file_location("emit_upgrade_jobs", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def test_dependency_names_strips_specifiers() -> None:
    pyproject = {"project": {"dependencies": ["pydantic>=2.13.4,<3.0.0", "typer >= 0.26"]}}
    assert mod.dependency_names(pyproject) == ["pydantic", "typer"]


def test_dependency_names_handles_extras_and_markers() -> None:
    pyproject = {
        "project": {"dependencies": ["uvicorn[standard]>=0.30", 'tomli; python_version < "3.11"']}
    }
    assert mod.dependency_names(pyproject) == ["uvicorn", "tomli"]


def test_dependency_names_deduplicates_preserving_order() -> None:
    pyproject = {"project": {"dependencies": ["a>=1", "b>=1", "a<2"]}}
    assert mod.dependency_names(pyproject) == ["a", "b"]


def test_dependency_names_empty_when_no_project() -> None:
    assert mod.dependency_names({}) == []


def test_job_name_normalizes_invalid_characters() -> None:
    assert mod.job_name("python-gitlab") == "upgrade-python-gitlab"
    assert mod.job_name("ruamel.yaml") == "upgrade-ruamel-yaml"


def test_render_jobs_produces_valid_repoactive_jobs() -> None:
    fragment = mod.render_jobs(["pydantic", "python-gitlab"])
    parsed = tomllib.loads(fragment)

    jobs = [Job.model_validate(j) for j in parsed["job"]]
    assert [j.name for j in jobs] == ["upgrade-pydantic", "upgrade-python-gitlab"]
    assert jobs[0].command == "uv lock -P pydantic"
    assert jobs[0].title == "build: upgrade pydantic"


def test_render_jobs_empty_for_no_dependencies() -> None:
    assert mod.render_jobs([]) == ""


def test_main_writes_fragment_to_jobs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\ndependencies = ["pydantic>=2", "typer>=0.2"]\n'
    )
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("REPOACTIVE_JOBS_DIR", str(jobs_dir))

    assert mod.main() == 0

    parsed = tomllib.loads((jobs_dir / "upgrade-deps.toml").read_text())
    assert [j["name"] for j in parsed["job"]] == ["upgrade-pydantic", "upgrade-typer"]


def test_main_fails_without_jobs_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPOACTIVE_JOBS_DIR", raising=False)
    assert mod.main() == 1
