"""Build the jobs a generator (emits_jobs) command produces from its fragments."""

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from repoactive.config import (
    Job,
    expand_config_paths,
    merge_jobs,
)
from repoactive.graph import CircularDependencyError, detect_dependency_cycle


class FragmentShape(BaseModel):
    """Structural shape of a generator-emitted job fragment.

    Generators may only emit [job.<name>] tables: the generator job itself
    acts as the scoped job-defaults for its emitted jobs (ADR 0004), so a
    [job-defaults] or [platform] in a fragment would never apply and
    is rejected instead of silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    job: dict[str, dict] = Field(default_factory=dict)


# Fields an emitted job inherits from its generator when the emitted entry does
# not set them itself (tags and depends_on are handled separately because
# their defaults are not a plain copy). See docs/adr/0004-job-generators.md.
_INHERITED_FIELDS = (
    "cooldown_period",
    "base_branch",
    "timeout",
    "labels",
    "branch_prefix",
    "mr_title_prefix",
    "commit_title_prefix",
    "draft",
    "create_mr",
    "auto_merge",
)


class GeneratedJobError(ValueError):
    """Raised when a generator emits an invalid job set.

    Invalid means collision, recursion, unknown dependency, or a job that fails validation.
    """

    def __init__(self, generator: str, message: str) -> None:
        super().__init__(f"generator {generator!r}: {message}")


def load_job_specs(jobs_dir: Path) -> dict[str, dict]:
    """Parse the *.toml fragments a generator wrote into jobs_dir.

    Files are read in sorted order and their [job.<name>] tables merged by
    name (later files win), the same machinery used for the .repoactive.d
    directory. Fragments may only contain [job.<name>] tables (see
    FragmentShape). Returns the raw job-spec table keyed by name, before
    inheritance/validation.
    """
    specs: dict[str, dict] = {}
    for path in expand_config_paths([jobs_dir]):
        data = tomllib.loads(path.read_text())
        specs = merge_jobs(base=specs, override=FragmentShape.model_validate(data).job)
    return specs


def _build_generated_job(  # noqa: PLR0913
    *,
    generator: Job,
    name: str,
    spec: dict,
    run_names: set[str],
    all_config_names: set[str],
    marked_secret_names: frozenset[str],
) -> Job:
    """Build one emitted Job from its raw spec, applying inheritance.

    name is the spec's table key. The job inherits the (resolved) generator's
    tags, depends_on and the _INHERITED_FIELDS unless the spec overrides
    them, and records the generator in generated_by. Raises GeneratedJobError
    on a name colliding with an existing job, a nested generator, a job that
    fails validation, or a secret_env naming a secret the static config did
    not already mark (marked_secret_names).
    """
    if name in run_names or name in all_config_names:
        raise GeneratedJobError(
            generator.name, f"emitted job {name!r} collides with an existing job"
        )
    if spec.get("emits_jobs"):
        raise GeneratedJobError(
            generator.name, f"emitted job {name!r} may not itself be a generator (no recursion)"
        )
    merged = {**spec, "name": name}
    if "tags" not in merged and "disabled" not in merged:
        merged["tags"] = sorted(generator.effective_tags())
    if "depends_on" not in merged:
        merged["depends_on"] = [generator.name]
    for f in _INHERITED_FIELDS:
        merged.setdefault(f, getattr(generator, f))
    merged["generated_by"] = generator.name
    # The generator's fragments live in a throwaway temp dir, so an emitted job's
    # meaningful config location is the generator's own config source.
    merged["config_source_dir"] = generator.config_source_dir
    try:
        job = Job.model_validate(merged)
    except ValidationError as e:
        raise GeneratedJobError(generator.name, f"emitted job {name!r} is invalid: {e}") from e
    # A generated job may only grant secrets the static config already marked. The
    # env strip set is derived from the static config (RunContext.stripped_env_names),
    # so a secret first introduced by an emitted job would not be stripped from the
    # other jobs' environments; requiring it be marked up front (e.g. in
    # [job-defaults].secret_env or on the generator) keeps a secret out of every job
    # that did not grant it. See docs/adr/0017-secret-env-redaction.md.
    unmarked = sorted(set(job.secret_env) - marked_secret_names)
    if unmarked:
        raise GeneratedJobError(
            generator.name,
            f"emitted job {name!r} grants secret(s) not marked in the static config: "
            f"{unmarked}; add them to [job-defaults].secret_env or the generator's secret_env",
        )
    return job


def build_generated_jobs(
    *,
    generator: Job,
    specs: dict[str, dict],
    run_names: set[str],
    all_config_names: set[str],
    marked_secret_names: frozenset[str] = frozenset(),
) -> list[Job]:
    """Turn a generator's raw specs into validated Job objects.

    Validates each spec (see _build_generated_job), that every
    depends_on target is within this run (the existing jobs or a sibling
    emitted job), and that the emitted jobs are acyclic — a cycle would
    otherwise silently mis-order the topological sort and crash the run.
    generator must be resolved (its inherited fields filled in).
    """
    emitted = [
        _build_generated_job(
            generator=generator,
            name=name,
            spec=spec,
            run_names=run_names,
            all_config_names=all_config_names,
            marked_secret_names=marked_secret_names,
        )
        for name, spec in specs.items()
    ]
    allowed = run_names | {j.name for j in emitted}
    for j in emitted:
        unknown = set(j.depends_on) - allowed
        if unknown:
            raise GeneratedJobError(
                generator.name,
                f"emitted job {j.name!r} depends_on jobs not in this run: {sorted(unknown)}",
            )
    # A cycle can only run through emitted jobs: the existing jobs were
    # validated acyclic and cannot depend on emitted names.
    try:
        detect_dependency_cycle(emitted)
    except CircularDependencyError as e:
        raise GeneratedJobError(generator.name, str(e)) from e
    return emitted
