"""Pydantic config models and multi-source TOML merging for repoactive."""

from __future__ import annotations

import itertools
import logging
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from repoactive.constants import JOB_TRAILER_KEY
from repoactive.graph import detect_dependency_cycle

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


class NewlineInTitleError(ValueError):
    """Raised when a title or prefix field contains a newline character."""

    def __init__(self, field: str, value: str) -> None:
        super().__init__(f"{field} must not contain newline characters, got {value!r}")


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
        super().__init__(f"job '{name}' depends_on unknown jobs: {unknown}")


class UnknownRunOnlyIfChangedError(ValueError):
    """Raised when run_only_if_changed references a job that does not exist."""

    def __init__(self, name: str, unknown: list[str]) -> None:
        super().__init__(
            f"job '{name}' run_only_if_changed references unknown job(s): {', '.join(unknown)}"
        )


class UnknownCooldownOnError(ValueError):
    """Raised when cooldown_on references a job that does not exist."""

    def __init__(self, name: str, unknown: list[str]) -> None:
        super().__init__(
            f"job '{name}' cooldown_on references unknown job(s): {', '.join(unknown)}"
        )


class SelfCooldownOnError(ValueError):
    """Raised when a job lists itself in cooldown_on."""

    def __init__(self, name: str) -> None:
        super().__init__(f"job '{name}' cooldown_on lists itself")


class CooldownOnWithoutCooldownPeriodError(ValueError):
    """Raised when cooldown_on is set but the job has no effective cooldown_period.

    Without a cooldown window there is nothing to throttle against, so the
    widened match would silently do nothing (ADR 0015).
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"job '{name}' sets cooldown_on but has no cooldown_period")


class JobNameInBodyError(ValueError):
    """Raised when a [job.<name>] table also sets a 'name' field.

    The job's name is the table key; repeating it in the body is redundant and
    could disagree with the key.
    """

    def __init__(self, name: str) -> None:
        super().__init__(
            f"job {name!r} must not set a 'name' field; the name is the table key ([job.{name}])"
        )


class GeneratedByInBodyError(ValueError):
    """Raised when a [job.<name>] table sets a 'generated_by' field.

    'generated_by' is set by repoactive itself on jobs emitted by a generator;
    it is not a user-facing config field.
    """

    def __init__(self, name: str) -> None:
        super().__init__(
            f"job {name!r} must not set 'generated_by'; "
            "that field is set by repoactive on generator-emitted jobs"
        )


class JobNotTableError(ValueError):
    """Raised when a [job.<name>] entry is not a table."""

    def __init__(self, name: str) -> None:
        super().__init__(f"job {name!r} must be a table ([job.{name}])")


class JobsNotTableError(ValueError):
    """Raised when 'job' is not a table keyed by name (e.g. the old array form)."""

    def __init__(self) -> None:
        super().__init__(
            "jobs must be a table keyed by name ([job.<name>]); the [[job]] array form "
            "with a 'name' field is no longer supported"
        )


class PlatformNotTableError(ValueError):
    """Raised when a [platform.<name>] entry is not a table."""

    def __init__(self, name: str) -> None:
        super().__init__(f"platform {name!r} must be a table ([platform.{name}])")


class PlatformsNotTableError(ValueError):
    """Raised when 'platform' is not a table keyed by name (e.g. the old array form)."""

    def __init__(self) -> None:
        super().__init__(
            "platforms must be a table keyed by name ([platform.<name>]); the [[platform]] "
            "array form is no longer supported"
        )


class DuplicatePlatformHostError(ValueError):
    """Raised when two platforms resolve to the same host.

    Platforms are matched to a repository by host (see platforms._match_platform),
    so two entries sharing a host are ambiguous — only the first would ever be used.
    """

    def __init__(self, host: str, first_url: str, second_url: str) -> None:
        super().__init__(
            f"two platforms resolve to the same host {host!r} "
            f"({first_url!r} and {second_url!r}); platform hosts must be unique"
        )


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


class CreateMR(StrEnum):
    """When a job creates its MR/PR.

    ``always`` and ``never`` are written as ``true``/``false`` in TOML (the
    original boolean form, kept for backwards compatibility);
    ``unless-superseded`` skips the MR when a dependent job produced an MR in
    the same run (that MR is stacked on this job's branch, so it already
    contains this job's changes).
    See docs/adr/0009-unless-superseded-mr-creation.md.
    """

    never = "never"
    always = "always"
    unless_superseded = "unless-superseded"


def _coerce_create_mr(value: object) -> object:
    """Map the boolean TOML form onto the enum (true -> always, false -> never)."""
    if isinstance(value, bool):
        return CreateMR.always if value else CreateMR.never
    return value


def _validate_branch_prefix(value: str) -> str:
    if not _BRANCH_PREFIX_RE.match(value):
        raise InvalidBranchPrefixError(value)
    return value


def _validate_duration(value: str) -> str:
    parse_duration(value)
    return value


_BranchPrefix = Annotated[str, AfterValidator(_validate_branch_prefix)]
_Duration = Annotated[str, AfterValidator(_validate_duration)]
# json_schema_input_type keeps booleans valid in the published JSON schema.
_CreateMR = Annotated[
    CreateMR, BeforeValidator(_coerce_create_mr, json_schema_input_type=bool | CreateMR)
]


class JobDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch_prefix: _BranchPrefix = "repoactive/"
    mr_title_prefix: str = "[repoactive] "
    commit_title_prefix: str = "[repoactive] "
    labels: list[str] = Field(default_factory=list)
    base_branch: str | None = None
    cooldown_period: _Duration | None = None
    timeout: _Duration | None = "2m"
    auto_merge: bool = False

    @field_validator("mr_title_prefix", "commit_title_prefix")
    @classmethod
    def _check_no_newline(cls, value: str, info: ValidationInfo) -> str:
        if "\n" in value:
            assert info.field_name is not None
            raise NewlineInTitleError(info.field_name, value)
        return value


# Fields Job.resolve fills in from JobDefaults when the job does not set them
# itself. labels is absent on purpose: it is merged, not replaced (see resolve).
_DEFAULTED_FIELDS = (
    "branch_prefix",
    "mr_title_prefix",
    "commit_title_prefix",
    "base_branch",
    "cooldown_period",
    "timeout",
    "auto_merge",
)


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    command: str
    title: str
    description: str | None = None
    base_branch: str | None = None
    draft: bool = False
    create_mr: _CreateMR = CreateMR.always
    disabled: bool = False
    tags: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    run_only_if_changed: list[str] = Field(default_factory=list)
    # Names of broader jobs that subsume this one. This job's cooldown check also
    # counts a recent landing of any named job, so once a superset lands this job
    # is throttled for its cooldown_period. Requires a cooldown_period. See
    # docs/adr/0015-cooldown-on-throttles-only-new-work.md.
    cooldown_on: list[str] = Field(default_factory=list)
    output_in_commit: bool = True
    # When true, the command does not produce a diff to commit; instead it writes
    # one or more *.toml job fragments into the directory named by the
    # REPOACTIVE_JOBS_DIR environment variable, and those jobs are run in the same
    # invocation. See docs/adr/0004-job-generators.md.
    emits_jobs: bool = False
    # Set by repoactive (never written in config) on jobs produced by a generator:
    # the generator's name, recorded as a second Repoactive-Job trailer so the
    # generator gets a meaningful cooldown over the whole fan-out.
    generated_by: str | None = None

    # the following fields will be resolved from the defaults
    branch_prefix: _BranchPrefix | None = None
    mr_title_prefix: str | None = None
    commit_title_prefix: str | None = None
    labels: list[str] = Field(default_factory=list)
    cooldown_period: _Duration | None = None
    timeout: _Duration | None = None
    auto_merge: bool | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not _JOB_NAME_RE.match(value):
            raise InvalidJobNameError(value)
        return value

    @field_validator("title", "commit_title_prefix")
    @classmethod
    def _check_no_newline(cls, value: str, info: ValidationInfo) -> str:
        if "\n" in value:
            assert info.field_name is not None
            raise NewlineInTitleError(info.field_name, value)
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
        """Tags driving selection: explicit tags, else DISABLED_TAG if disabled, else DEFAULT_TAG.

        ``disabled`` and ``tags`` are mutually exclusive.
        """
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
        """Return the command timeout in seconds, or None for no timeout.

        A zero duration (e.g. ``"0s"``) also means no timeout: TOML cannot
        express null, so this is how a job opts out of a timeout set in
        ``job-defaults``.
        """
        if self.timeout is None:
            return None
        return parse_duration(self.timeout).total_seconds() or None

    def commit_trailers(self) -> list[str]:
        """Return the ``Repoactive-Job`` trailer lines recorded on this job's commit.

        A job produced by a generator records a second trailer with the
        generator's name, giving the generator a cooldown over the whole
        fan-out (ADR 0004).
        """
        lines = [f"{JOB_TRAILER_KEY}: {self.name}"]
        if self.generated_by:
            lines.append(f"{JOB_TRAILER_KEY}: {self.generated_by}")
        return lines

    def resolve(self, defaults: JobDefaults) -> Job:
        update: dict[str, object] = {
            f: getattr(self, f) if getattr(self, f) is not None else getattr(defaults, f)
            for f in _DEFAULTED_FIELDS
        }
        update["labels"] = list(dict.fromkeys(defaults.labels + self.labels))
        return self.model_copy(update=update)


class Config(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    platforms: list[PlatformConfig] = Field(alias="platform", default_factory=list)
    job_defaults: JobDefaults = Field(alias="job-defaults", default_factory=JobDefaults)
    jobs: list[Job] = Field(default_factory=list, alias="job")

    @field_validator("jobs", mode="before")
    @classmethod
    def _jobs_from_mapping(cls, value: object) -> object:
        """Coerce the ``[job.<name>]`` table into the list pydantic expects.

        TOML stores jobs as a table keyed by name; the name comes from the key
        and is injected into each job. A non-mapping value (e.g. an already-built
        ``list[Job]`` passed programmatically) passes through unchanged.
        """
        if not isinstance(value, dict):
            return value
        jobs: list[dict] = []
        for key, body in value.items():
            name = str(key)
            if not isinstance(body, dict):
                raise JobNotTableError(name)
            if "name" in body:
                raise JobNameInBodyError(name)
            if "generated_by" in body:
                raise GeneratedByInBodyError(name)
            jobs.append({**body, "name": name})
        return jobs

    @field_validator("platforms", mode="before")
    @classmethod
    def _platforms_from_mapping(cls, value: object) -> object:
        """Coerce the ``[platform.<name>]`` table into the list pydantic expects.

        TOML stores platforms as a table keyed by name; the name is only a label
        (platforms are matched by ``url``), so it is dropped here. A non-mapping
        value (e.g. an already-built ``list`` passed programmatically) passes
        through unchanged.
        """
        if not isinstance(value, dict):
            return value
        platforms: list[dict] = []
        for key, body in value.items():
            if not isinstance(body, dict):
                raise PlatformNotTableError(str(key))
            platforms.append(body)
        return platforms

    @model_validator(mode="after")
    def validate_unique_platform_hosts(self) -> Config:
        # platforms are matched to a repo by host, so two entries sharing a host
        # are ambiguous (only the first would ever be selected). Imported here,
        # not at module top, to avoid a circular import via platforms/__init__.
        from repoactive.platforms.base import extract_host  # noqa: PLC0415

        url_by_host: dict[str, str] = {}
        for platform in self.platforms:
            host = extract_host(platform.url)
            if host in url_by_host:
                raise DuplicatePlatformHostError(host, url_by_host[host], platform.url)
            url_by_host[host] = platform.url
        return self

    @model_validator(mode="after")
    def validate_depends_on(self) -> Config:
        names = {j.name for j in self.jobs}
        for job in self.jobs:
            unknown = set(job.depends_on) - names
            if unknown:
                raise UnknownDependencyError(job.name, sorted(unknown))
        detect_dependency_cycle(self.jobs)
        return self

    @model_validator(mode="after")
    def validate_run_only_if_changed(self) -> Config:
        names = {j.name for j in self.jobs}
        for job in self.jobs:
            unknown = sorted(set(job.run_only_if_changed) - names)
            if unknown:
                raise UnknownRunOnlyIfChangedError(job.name, unknown)
        return self

    @model_validator(mode="after")
    def validate_cooldown_on(self) -> Config:
        names = {j.name for j in self.jobs}
        for job in self.jobs:
            if not job.cooldown_on:
                continue
            unknown = sorted(set(job.cooldown_on) - names)
            if unknown:
                raise UnknownCooldownOnError(job.name, unknown)
            if job.name in job.cooldown_on:
                raise SelfCooldownOnError(job.name)
            # cooldown_on only throttles against a cooldown window; without one
            # (own or inherited) it would silently do nothing.
            if job.cooldown_period is None and self.job_defaults.cooldown_period is None:
                raise CooldownOnWithoutCooldownPeriodError(job.name)
        return self

    def _resolved_jobs(self) -> list[Job]:
        return [job.resolve(self.job_defaults) for job in self.jobs]

    def bookmark_names(self) -> set[str]:
        """Return the branch/bookmark names repoactive manages, one per job.

        Each is the job's resolved ``branch_prefix`` followed by its name (see
        ``Job.branch_name``). When we start working on a repository these are the
        bookmarks to track with ``jj bookmark track`` so that branches already
        pushed by an earlier run are recognised instead of being recreated.
        """
        return {job.branch_name() for job in self._resolved_jobs()}

    def base_branches(self) -> set[str]:
        """All branches a job uses as base_branch."""
        return {job.base_branch for job in self._resolved_jobs() if job.base_branch}

    def token_env_names(self) -> set[str]:
        """Names of the env vars holding platform API tokens.

        These are stripped from the environment a job command runs in so a
        command cannot read the platform credential (see runner._run_command and
        docs/adr/0006-job-commands-are-trusted.md).
        """
        return {p.token_env for p in self.platforms}


def jobs_table(value: object) -> dict:
    """Return ``value`` as a job table, rejecting the old ``[[job]]`` array form.

    TOML parses ``[job.<name>]`` tables into a dict keyed by name; an array of
    tables (the format used before) parses into a list, which is no longer
    accepted.
    """
    if not isinstance(value, dict):
        raise JobsNotTableError
    return value


def platforms_table(value: object) -> dict:
    """Return ``value`` as a platform table, rejecting the old ``[[platform]]`` array.

    TOML parses ``[platform.<name>]`` tables into a dict keyed by name; an array
    of tables (the format used before) parses into a list, which is no longer
    accepted.
    """
    if not isinstance(value, dict):
        raise PlatformsNotTableError
    return value


def _deep_merge(*, base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(base=result[key], override=value)
        else:
            result[key] = value
    return result


def _merge_named_tables(*, base: dict, override: dict) -> dict:
    """Merge two tables keyed by name.

    Order is preserved (base names first, new override names appended) and
    override fields win field-by-field, the same semantics as the per-source
    table merge.
    """
    result: dict[str, dict] = {name: dict(body) for name, body in base.items()}
    for name, body in override.items():
        if name in result:
            result[name].update(body)
        else:
            result[name] = dict(body)
    return result


def merge_jobs(*, base: dict, override: dict) -> dict:
    """Merge two job tables keyed by name (see ``_merge_named_tables``).

    When the override sets ``disabled = true``, any ``tags`` carried over from
    the base are removed, and when the override sets ``tags``, any ``disabled``
    carried over from the base is removed.  This keeps the mutual-exclusion
    invariant intact across multi-source merges.
    """
    result = _merge_named_tables(base=base, override=override)
    for name, override_body in override.items():
        merged_body = result[name]
        if override_body.get("disabled") and "tags" not in override_body:
            merged_body.pop("tags", None)
        elif override_body.get("tags") and "disabled" not in override_body:
            merged_body.pop("disabled", None)
    return result


def merge_platforms(*, base: dict, override: dict) -> dict:
    """Merge two platform tables keyed by name (see ``_merge_named_tables``)."""
    return _merge_named_tables(base=base, override=override)


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


def _built_in_defaults() -> list[_ConfigSource]:
    return [
        _ConfigSource(
            "<built-in defaults>",
            tomllib.loads("""
[platform.github]
url="https://github.com"
type="github"
token_env="GITHUB_TOKEN"

[platform.gitlab]
url="https://gitlab.com"
type="gitlab"
token_env="GITLAB_TOKEN"
"""),
        ),
    ]


def _parse_override(text: str) -> _ConfigSource:
    """Parse one ``--set NAME=VALUE`` override into a _ConfigSource.

    ``text`` is a TOML assignment line, so dotted keys (``job.lint.disabled``)
    and value expressions come straight from ``tomllib``.
    """
    label = f"--set {text!r}"
    try:
        return _ConfigSource(label, tomllib.loads(text))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(label, e) from e


def _read_toml_file(path: Path) -> _ConfigSource:
    try:
        return _ConfigSource(str(path), tomllib.loads(path.read_text()))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(str(path), e) from e


def load_config(paths: list[Path], overrides: list[str] | None = None) -> Config:
    return _merge_config(
        itertools.chain(
            _built_in_defaults(),
            (_read_toml_file(path) for path in expand_config_paths(paths)),
            (_parse_override(text) for text in overrides or []),
        )
    )


def _merge_config(sources: Iterable[_ConfigSource]) -> Config:
    merged = {}
    for source in sources:
        data = source.data
        try:
            jobs = merge_jobs(base=merged.get("job", {}), override=jobs_table(data.get("job", {})))
            platforms = merge_platforms(
                base=merged.get("platform", {}),
                override=platforms_table(data.get("platform", {})),
            )
            merged = _deep_merge(base=merged, override=data)
            merged["job"] = jobs
            merged["platform"] = platforms
            merged.pop("$schema", None)
            # Validate the cumulative merge so errors are attributed to this
            # source; forward references to later sources are rejected by
            # design (docs/adr/0010-validate-config-after-each-source.md).
            Config.model_validate(merged)
        except (ValueError, ValidationError) as e:
            raise ConfigError(source.label, e) from e
    config = Config.model_validate(merged)
    logger.debug("loaded config: %s", config.model_dump_json(indent=2))
    return config
