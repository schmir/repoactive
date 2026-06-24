from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

logger = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"^(\d+)([smhdw])$")
_JOB_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_BRANCH_PREFIX_RE = re.compile(r"^(?!/)(?!.*//)[a-zA-Z0-9_\-/]+$")
_DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}

# Reserved tags driving job selection. A plain job carries DEFAULT_TAG, which is
# what the bare ``repoactive run`` selects; ``disabled = true`` is sugar for
# DISABLED_TAG. See docs/adr/0002-tag-based-job-selection.md.
DEFAULT_TAG = "enabled"
DISABLED_TAG = "disabled"


class InvalidDurationError(ValueError):
    """Raised when a duration string cannot be parsed."""

    def __init__(self, value: str) -> None:
        super().__init__(
            f"invalid duration {value!r}: expected <number><unit> "
            "with unit one of s, m, h, d, w (e.g. '7d')"
        )


class InvalidBranchPrefixError(ValueError):
    """Raised when a branch_prefix contains disallowed characters."""

    def __init__(self, value: str) -> None:
        super().__init__(
            f"invalid branch_prefix {value!r}: only alphanumerics, hyphens, underscores, and "
            "slashes are allowed; must not start with '/' or contain '//'"
        )


class InvalidJobNameError(ValueError):
    """Raised when a job name contains disallowed characters."""

    def __init__(self, value: str) -> None:
        super().__init__(
            f"invalid job name {value!r}: only letters, digits, '-', and '_' are allowed"
        )


class InvalidTagError(ValueError):
    """Raised when a tag contains disallowed characters."""

    def __init__(self, tag: str) -> None:
        super().__init__(f"invalid tag {tag!r}: only letters, digits, '-', and '_' are allowed")


class DisabledAndTagsError(ValueError):
    """Raised when a job sets both 'disabled' and 'tags'."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"job {name!r} sets both 'disabled' and 'tags'; "
            f"set one or the other (disabled is sugar for tags = ['{DISABLED_TAG}'])"
        )


class UnknownDependencyError(ValueError):
    """Raised when a job depends_on a job that does not exist."""

    def __init__(self, name: str, unknown: list[str]) -> None:
        super().__init__(f"Job '{name}' depends_on unknown jobs: {unknown}")


class CircularDependencyError(ValueError):
    """Raised when jobs form a dependency cycle."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Circular dependency involving '{name}'")


class MissingJobNameError(ValueError):
    """Raised when a [[job]] entry has no 'name' field."""

    def __init__(self) -> None:
        super().__init__("Each [[job]] entry must have a 'name' field")


class ConfigError(Exception):
    """Wraps a parse or validation error with the config source it came from."""

    def __init__(self, source: str, error: Exception) -> None:
        self.source = source
        self.error = error
        super().__init__(f"in {source}:\n  {error}")


def parse_duration(value: str) -> timedelta:
    """Parse a duration like ``"7d"`` or ``"12h"`` into a timedelta.

    The unit is one of s (seconds), m (minutes), h (hours), d (days), w (weeks).
    Raises ValueError on anything else.
    """
    match = _DURATION_RE.match(value.strip())
    if not match:
        raise InvalidDurationError(value)
    amount, unit = int(match.group(1)), match.group(2)
    return timedelta(**{_DURATION_UNITS[unit]: amount})


class PlatformConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    type: Literal["gitlab", "github"]
    token_env: str


def _validate_branch_prefix(value: str) -> None:
    if not _BRANCH_PREFIX_RE.match(value):
        raise InvalidBranchPrefixError(value)


class JobDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch_prefix: str = "repoactive/"
    mr_title_prefix: str = "[repoactive] "
    commit_title_prefix: str = "[repoactive] "
    labels: list[str] = Field(default_factory=list)
    base_branch: str | None = None
    cooldown_period: str | None = None
    timeout: str | None = "2m"

    @field_validator("branch_prefix")
    @classmethod
    def _check_branch_prefix(cls, value: str) -> str:
        _validate_branch_prefix(value)
        return value

    @field_validator("cooldown_period", "timeout")
    @classmethod
    def _check_duration(cls, value: str | None) -> str | None:
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
    tags: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    output_in_commit: bool = True

    # the following fields will be resolved from the defaults
    branch_prefix: str | None = None
    mr_title_prefix: str | None = None
    commit_title_prefix: str | None = None
    labels: list[str] = Field(default_factory=list)
    cooldown_period: str | None = None
    timeout: str | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not _JOB_NAME_RE.match(value):
            raise InvalidJobNameError(value)
        return value

    @field_validator("branch_prefix")
    @classmethod
    def _check_branch_prefix(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_branch_prefix(value)
        return value

    @field_validator("cooldown_period", "timeout")
    @classmethod
    def _check_duration(cls, value: str | None) -> str | None:
        if value is not None:
            parse_duration(value)
        return value

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, value: list[str]) -> list[str]:
        for tag in value:
            if not _TAG_RE.match(tag):
                raise InvalidTagError(tag)
        return value

    @model_validator(mode="after")
    def _check_disabled_xor_tags(self) -> Job:
        if self.disabled and self.tags:
            raise DisabledAndTagsError(self.name)
        return self

    def effective_tags(self) -> set[str]:
        """Tags driving selection: explicit tags, else DISABLED_TAG if disabled,
        else DEFAULT_TAG. ``disabled`` and ``tags`` are mutually exclusive."""
        if self.disabled:
            return {DISABLED_TAG}
        if self.tags:
            return set(self.tags)
        return {DEFAULT_TAG}

    def branch_name(self) -> str:
        assert self.branch_prefix is not None, "job must be resolved before calling branch_name()"
        return f"{self.branch_prefix}{self.name}"

    def cooldown_timedelta(self) -> timedelta | None:
        return parse_duration(self.cooldown_period) if self.cooldown_period is not None else None

    def timeout_seconds(self) -> float | None:
        return parse_duration(self.timeout).total_seconds() if self.timeout is not None else None

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
                "timeout": self.timeout if self.timeout is not None else defaults.timeout,
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
                raise UnknownDependencyError(job.name, sorted(unknown))
        by_name = {j.name: j for j in self.jobs}
        visiting: set[str] = set()
        visited: set[str] = set()

        def detect_cycle(name: str) -> None:
            if name in visiting:
                raise CircularDependencyError(name)
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

    def _resolved_jobs(self) -> list[Job]:
        return [job.resolve(self.job_defaults) for job in self.jobs]

    def bookmark_names(self) -> set[str]:
        """The branch/bookmark names repoactive manages, one per job.

        Each is the job's resolved ``branch_prefix`` followed by its name (see
        ``Job.branch_name``). When we start working on a repository these are the
        bookmarks to track with ``jj bookmark track`` so that branches already
        pushed by an earlier run are recognised instead of being recreated.
        """
        return {job.branch_name() for job in self._resolved_jobs()}

    def base_branches(self) -> set[str]:
        """All branches a job uses as base_branch"""
        return {job.base_branch for job in self._resolved_jobs() if job.base_branch}


def _deep_merge(*, base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(base=result[key], override=value)
        else:
            result[key] = value
    return result


def _merge_jobs(*, base: list[dict], override: list[dict]) -> list[dict]:
    """Merge two job lists by name: order is preserved, override fields win, new names appended."""
    job_by_name: dict[str, dict] = {}
    result: list[dict] = []
    for job in base + override:
        name = job.get("name")
        if name is None:
            raise MissingJobNameError

        if name in job_by_name:
            job_by_name[name].update(job)
        else:
            job_by_name[name] = dict(job)
            result.append(job_by_name[name])
    return result


def _merge_platforms(*, base: list[dict], override: list[dict]) -> list[dict]:
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


_DEFAULT_CONFIG_FILE = Path(".repoactive.toml")
_DEFAULT_CONFIG_DIR = Path(".repoactive.d")


class ConfigNotFoundError(Exception):
    """Raised when no configuration is given and no default config exists."""

    def __init__(self, config_file: Path, config_dir: Path) -> None:
        super().__init__(f"no configuration found: neither {config_file} nor {config_dir}/ exists")


def default_config_paths(repo: Path) -> list[Path]:
    """Config paths to use when none are passed on the command line.

    Looks inside ``repo`` for the ``.repoactive.d`` directory and the
    ``.repoactive.toml`` file; the file is applied last so it overrides the
    directory. Raises ConfigNotFoundError when neither exists.
    """
    config_dir = repo / _DEFAULT_CONFIG_DIR
    config_file = repo / _DEFAULT_CONFIG_FILE
    paths: list[Path] = []
    if config_dir.is_dir():
        paths.append(config_dir)
    if config_file.is_file():
        paths.append(config_file)
    if not paths:
        raise ConfigNotFoundError(config_file, config_dir)
    return paths


def expand_config_paths(paths: list[Path]) -> list[Path]:
    """Expand any directory into its sorted ``*.toml`` files; files pass through unchanged."""
    expanded: list[Path] = []
    for path in paths:
        if path.is_dir():
            expanded.extend(sorted(p for p in path.glob("*.toml") if p.is_file()))
        else:
            expanded.append(path)
    return expanded


@dataclass(frozen=True)
class _ConfigSource:
    """A parsed config along with the label naming where it came from."""

    label: str
    data: dict


def load_config(paths: list[Path]) -> Config:
    assert paths
    sources = [_ConfigSource("<built-in defaults>", tomllib.loads(_default_platforms))]
    for path in expand_config_paths(paths):
        try:
            data = tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(str(path), e) from e
        sources.append(_ConfigSource(str(path), data))

    merged = {}
    for source in sources:
        data = source.data
        try:
            jobs = _merge_jobs(base=merged.get("job", []), override=data.get("job", []))
            platforms = _merge_platforms(
                base=merged.get("platform", []), override=data.get("platform", [])
            )
            merged = _deep_merge(base=merged, override=data)
            merged["job"] = jobs
            merged["platform"] = platforms
            merged.pop("$schema", None)
            Config.model_validate(merged)  # ensure it's valid after each merge
        except (ValueError, ValidationError) as e:
            raise ConfigError(source.label, e) from e
    config = Config.model_validate(merged)
    logger.debug("loaded config: %s", config.model_dump_json(indent=2))
    return config
