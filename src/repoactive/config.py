from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)


class PlatformConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["gitlab", "github"]
    url: str | None = None
    token_env: str
    # Project path ("namespace/repo"). Auto-detected from remote URL if omitted.
    repo: str | None = None


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch_prefix: str = "repoactive/"
    mr_title_prefix: str = "[repoactive] "
    commit_title_prefix: str = "[repoactive] "
    labels: list[str] = Field(default_factory=list)


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    command: str
    title: str
    description: str | None = None
    labels: list[str] = Field(default_factory=list)
    base_branch: str | None = None
    draft: bool = False
    create_mr: bool = True
    disabled: bool = False
    depends_on: list[str] = Field(default_factory=list)
    output_in_commit: bool = True

    def branch_name(self, prefix: str) -> str:
        return f"{prefix}{self.name}"


class Config(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    platform: PlatformConfig
    defaults: Defaults = Field(default_factory=Defaults)
    jobs: list[Job] = Field(default_factory=list, alias="job")

    @model_validator(mode="after")
    def validate_depends_on(self) -> Config:
        names = {j.name for j in self.jobs}
        for job in self.jobs:
            unknown = set(job.depends_on) - names
            if unknown:
                raise ValueError(f"Job '{job.name}' depends_on unknown jobs: {sorted(unknown)}")
        by_name = {j.name: j for j in self.jobs}
        visiting: set[str] = set()
        visited: set[str] = set()

        def detect_cycle(name: str) -> None:
            if name in visiting:
                raise ValueError(f"Circular dependency involving '{name}'")
            if name in visited:
                return
            visiting.add(name)
            for dep in by_name[name].depends_on:
                detect_cycle(dep)
            visiting.discard(name)
            visited.add(name)

        for job in self.jobs:
            detect_cycle(job.name)
        return self


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _merge_jobs(base: list[dict], override: list[dict]) -> list[dict]:
    """Merge two job lists by name: order is preserved, override fields win, new names appended."""
    job_by_name: dict[str, dict] = {}
    result: list[dict] = []
    for job in base + override:
        name = job["name"]
        if name in job_by_name:
            job_by_name[name].update(job)
        else:
            job_by_name[name] = dict(job)
            result.append(job_by_name[name])

    return result


def load_config(paths: list[Path]) -> Config:
    assert paths
    configs = [tomllib.loads(path.read_text()) for path in paths]
    merged = {}
    for data in configs:
        jobs = _merge_jobs(merged.get("job", []), data.get("job", []))
        merged = _deep_merge(merged, data)
        merged["job"] = jobs
        config = Config.model_validate(merged)  # ensure it's valid after each merge
    logger.debug("loaded config: %s", config.model_dump_json(indent=2))
    return config
