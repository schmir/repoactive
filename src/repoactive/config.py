from __future__ import annotations

import logging
import re
import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"^(\d+)([smhdw])$")
_JOB_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_BRANCH_PREFIX_RE = re.compile(r"^(?!/)(?!.*//)[a-zA-Z0-9_\-/]+$")
_DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_duration(value: str) -> timedelta:
    """Parse a duration like ``"7d"`` or ``"12h"`` into a timedelta.

    The unit is one of s (seconds), m (minutes), h (hours), d (days), w (weeks).
    Raises ValueError on anything else.
    """
    match = _DURATION_RE.match(value.strip())
    if not match:
        raise ValueError(
            f"invalid duration {value!r}: expected <number><unit> "
            "with unit one of s, m, h, d, w (e.g. '7d')"
        )
    amount, unit = int(match.group(1)), match.group(2)
    return timedelta(**{_DURATION_UNITS[unit]: amount})


class PlatformConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    type: Literal["gitlab", "github"]
    token_env: str


def _validate_branch_prefix(value: str) -> None:
    if not _BRANCH_PREFIX_RE.match(value):
        raise ValueError(
            f"invalid branch_prefix {value!r}: only alphanumerics, hyphens, underscores, and "
            "slashes are allowed; must not start with '/' or contain '//'"
        )


class JobDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch_prefix: str = "repoactive/"
    mr_title_prefix: str = "[repoactive] "
    commit_title_prefix: str = "[repoactive] "
    labels: list[str] = Field(default_factory=list)
    base_branch: str | None = None
    cooldown_period: str | None = None

    @field_validator("branch_prefix")
    @classmethod
    def _check_branch_prefix(cls, value: str) -> str:
        _validate_branch_prefix(value)
        return value

    @field_validator("cooldown_period")
    @classmethod
    def _check_cooldown_period(cls, value: str | None) -> str | None:
        if value is not None:
            parse_duration(value)
        return value


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    command: str
    title: str
    description: str | None = None
    base_branch: str | None = None
    draft: bool = False
    create_mr: bool = True
    disabled: bool = False
    depends_on: list[str] = Field(default_factory=list)
    output_in_commit: bool = True

    # the following fields will be resolved from the defaults
    branch_prefix: str | None = None
    mr_title_prefix: str | None = None
    commit_title_prefix: str | None = None
    labels: list[str] = Field(default_factory=list)
    cooldown_period: str | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not _JOB_NAME_RE.match(value):
            raise ValueError(
                f"invalid job name {value!r}: only letters, digits, '-', and '_' are allowed"
            )
        return value

    @field_validator("branch_prefix")
    @classmethod
    def _check_branch_prefix(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_branch_prefix(value)
        return value

    @field_validator("cooldown_period")
    @classmethod
    def _check_cooldown_period(cls, value: str | None) -> str | None:
        if value is not None:
            parse_duration(value)
        return value

    def branch_name(self) -> str:
        assert self.branch_prefix is not None, "job must be resolved before calling branch_name()"
        return f"{self.branch_prefix}{self.name}"

    def cooldown_timedelta(self) -> timedelta | None:
        return parse_duration(self.cooldown_period) if self.cooldown_period is not None else None

    def resolve(self, defaults: JobDefaults) -> Job:
        return self.model_copy(
            update={
                "branch_prefix": self.branch_prefix
                if self.branch_prefix is not None
                else defaults.branch_prefix,
                "mr_title_prefix": self.mr_title_prefix
                if self.mr_title_prefix is not None
                else defaults.mr_title_prefix,
                "commit_title_prefix": self.commit_title_prefix
                if self.commit_title_prefix is not None
                else defaults.commit_title_prefix,
                "base_branch": self.base_branch
                if self.base_branch is not None
                else defaults.base_branch,
                "cooldown_period": self.cooldown_period
                if self.cooldown_period is not None
                else defaults.cooldown_period,
                "labels": list(dict.fromkeys(defaults.labels + self.labels)),
            }
        )


class Config(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    platforms: list[PlatformConfig] = Field(alias="platform", default_factory=list)
    job_defaults: JobDefaults = Field(alias="job-defaults", default_factory=JobDefaults)
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
        name = job.get("name")
        if name is None:
            raise ValueError("Each [[job]] entry must have a 'name' field")

        if name in job_by_name:
            job_by_name[name].update(job)
        else:
            job_by_name[name] = dict(job)
            result.append(job_by_name[name])
    return result


def _merge_platforms(base: list[dict], override: list[dict]) -> list[dict]:
    """Merge two platform lists by url: override fields win, new entries appended."""
    platform_by_url: dict[str, dict] = {}
    result: list[dict] = []
    for platform in base + override:
        url = platform.get("url", "")
        if url in platform_by_url:
            platform_by_url[url].update(platform)
        else:
            platform_by_url[url] = dict(platform)
            result.append(platform_by_url[url])
    return result


_default_platforms = """
[[platform]]
url="https://github.com"
type="github"
token_env="GITHUB_TOKEN"

[[platform]]
url="https://gitlab.com"
type="gitlab"
token_env="GITLAB_TOKEN"
"""


def _expand_paths(paths: list[Path]) -> list[Path]:
    """Expand any directory into its sorted ``*.toml`` files; files pass through unchanged."""
    expanded: list[Path] = []
    for path in paths:
        if path.is_dir():
            expanded.extend(sorted(p for p in path.glob("*.toml") if p.is_file()))
        else:
            expanded.append(path)
    return expanded


def load_config(paths: list[Path]) -> Config:
    assert paths
    configs = [
        tomllib.loads(_default_platforms),
        *(tomllib.loads(path.read_text()) for path in _expand_paths(paths)),
    ]

    merged = {}
    for data in configs:
        jobs = _merge_jobs(merged.get("job", []), data.get("job", []))
        platforms = _merge_platforms(merged.get("platform", []), data.get("platform", []))
        merged = _deep_merge(merged, data)
        merged["job"] = jobs
        merged["platform"] = platforms
        Config.model_validate(merged)  # ensure it's valid after each merge
    config = Config.model_validate(merged)
    logger.debug("loaded config: %s", config.model_dump_json(indent=2))
    return config
