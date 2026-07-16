"""Job orchestration: select, run, commit, push, and publish MRs for each job."""

import contextlib
import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
import tomllib
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from repoactive.boxquote import boxquote, strip_boxquotes
from repoactive.config import (
    Config,
    CreateMR,
    FragmentShape,
    Job,
    expand_config_paths,
    merge_jobs,
)
from repoactive.graph import CircularDependencyError, detect_dependency_cycle, topological_sort
from repoactive.jj import JJ, workspace_name
from repoactive.jobtree import format_job_forest, print_job_table
from repoactive.lock import run_lock
from repoactive.platforms.base import MRParams, Platform
from repoactive.progress import ProgressView, format_elapsed
from repoactive.selection import JobSelection, JobSelector
from repoactive.settings import load_settings
from repoactive.trailers import strip_trailers
from repoactive.ui import print_status, print_undo_hint
from repoactive.updates import (
    BookmarkPush,
    JobUpdate,
    MRLink,
    MRUpdate,
    UpdatePlan,
    build_mr_description,
)

logger = logging.getLogger(__name__)

# Environment variable naming the directory a generator (``emits_jobs``) command
# writes its ``*.toml`` job fragments into. See docs/adr/0004-job-generators.md.
RA_JOBS_DIR_ENV = "RA_JOBS_DIR"

# Environment variable exposing to a job command the directory of the config
# source that defined the command (Job.config_source_dir), so the command can
# reach files kept beside its config. See docs/adr/0016-injected-env-var-prefix.md.
RA_CONFIG_SOURCE_DIR_ENV = "RA_CONFIG_SOURCE_DIR"

# Environment variable exposing to a job command the throwaway jj workspace
# repoactive created for it. This is always the command's working directory, but
# naming it explicitly lets a command that changes directory find its way back.
RA_WORKSPACE_DIR_ENV = "RA_WORKSPACE_DIR"

# Environment variable exposing to a job command the bookmark/branch repoactive
# uses for the job's output (Job.branch_name). The command runs on a fresh commit
# while this bookmark still points at the previous run's commit, so the command
# can inspect what it produced last time (e.g. `git diff $RA_JOB_BRANCH`). The
# bookmark may not exist yet: a first run, a run that produced no diff, or a
# generator never creates it.
RA_JOB_BRANCH_ENV = "RA_JOB_BRANCH"

# Fields an emitted job inherits from its generator when the emitted entry does
# not set them itself (``tags`` and ``depends_on`` are handled separately because
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


class RunMode(StrEnum):
    """How far a run publishes its results past the local jj repository.

    The modes form a ladder:
    - local: only changes the local jj repository
    - push: additionally pushes bookmarks/branches to the remote
    - publish: additionally updates or creates MRs/PRs.
    """

    local = "local"
    push = "push"
    publish = "publish"


@dataclass
class CommandResult:
    output: str
    elapsed: float


class CommandError(RuntimeError):
    """A job command exited non-zero.

    Carries the command's wall time so the failure can be reported with the same
    elapsed semantics as a success.
    """

    def __init__(self, message: str, elapsed: float) -> None:
        super().__init__(message)
        self.elapsed = elapsed


class GeneratedJobError(ValueError):
    """Raised when a generator emits an invalid job set.

    Invalid means collision, recursion, unknown dependency, or a job that fails validation.
    """

    def __init__(self, generator: str, message: str) -> None:
        super().__init__(f"generator {generator!r}: {message}")


@dataclass
class JobResult:
    job: Job
    # Revsets dependents should use as parents. During phase 1 this is the
    # new commit's change-id (produced_diff=True) or the job's parent revsets
    # (produced_diff=False). After absorb the bookmark name is canonical, but
    # dependents only need this during the run, before absorb.
    effective_revsets: list[str]
    produced_diff: bool
    # Parent revsets passed to repo.new() in phase 1. Carried so the absorb
    # phase knows where to rebase the old commit.
    parents: list[str] = field(default_factory=list)
    # change-id of the fresh commit created in phase 1 (None when no diff).
    new_change_id: str | None = None
    # change-id of the pre-existing bookmark commit (None for new jobs).
    old_change_id: str | None = None
    # Filled in by the apply phase once the MR has been created.
    mr_url: str | None = None
    command_output: str = ""


@dataclass
class RunSummary:
    results: dict[str, JobResult] = field(default_factory=dict)
    failed: dict[str, Exception] = field(default_factory=dict)
    skipped: set[str] = field(default_factory=set)
    on_cooldown: set[str] = field(default_factory=set)
    # Successor jobs skipped because nothing below them in the stack ran this
    # run (see _dispatch_job). Like on_cooldown, an intentional skip: the job's
    # bookmark is left alone in the absorb phase.
    successor_skipped: set[str] = field(default_factory=set)
    # Jobs whose run_only_if_changed gate fired (none of the watched deps
    # produced a diff). Like on_cooldown, an intentional skip: the job's
    # bookmark is left alone in the absorb phase.
    run_only_if_changed_skipped: set[str] = field(default_factory=set)
    # Wall time of the whole run, filled in by run_all just before print_report.
    elapsed: float | None = None

    @property
    def ok(self) -> bool:
        # cooldown and successor skips are intentional, not failures, so they
        # do not affect ok.
        return not self.failed and not self.skipped

    def print_report(self) -> None:
        # A name may sit in more than one bucket - cooldown jobs are also stored
        # in results (so dependents can read their effective_revsets), and a job
        # whose MR failed at apply time is in results and failed - so count the
        # union of names, not the sum of the buckets.
        total = len(self.results.keys() | self.failed.keys() | self.skipped)
        produced = sum(1 for r in self.results.values() if r.produced_diff)
        print(
            f"\nDone: {produced}/{total} produced changes"
            + (f", {len(self.failed)} failed" if self.failed else "")
            + (f", {len(self.skipped)} skipped" if self.skipped else "")
            + (f", {len(self.on_cooldown)} on cooldown" if self.on_cooldown else "")
            + (
                f", {len(self.successor_skipped)} successors unchanged"
                if self.successor_skipped
                else ""
            )
            + (
                f", {len(self.run_only_if_changed_skipped)} gated"
                if self.run_only_if_changed_skipped
                else ""
            )
            + "."
            + (f" ({format_elapsed(self.elapsed)})" if self.elapsed is not None else "")
        )


@dataclass
class RunContext:
    """Run-wide state shared by every job in a single run_all pass.

    Built once in ``run_all`` and threaded through ``_run_jobs`` /
    ``_dispatch_job`` to ``run_job`` / ``_run_generator_job`` so every stage has a
    single handle to the run's config, target repo, accumulating results,
    selection, and the ``plan`` the absorb phase fills in.
    ``selection`` is the live selection object (``_run_jobs`` splices
    generator-emitted jobs into ``selection.jobs`` in place), so ``selection.jobs``
    is always every job in the run.
    ``repo`` is the prepared, colocated ``JJ`` bound to ``repo_path``.
    """

    config: Config
    repo_path: Path
    repo: JJ
    summary: RunSummary
    blocked: set[str]
    selection: JobSelection
    # Bookmark pushes and MR descriptors, filled in by _absorb_results and then
    # applied by apply_plan. Empty until phase 2.
    plan: UpdatePlan = field(default_factory=UpdatePlan)

    @property
    def secret_env_names(self) -> frozenset[str]:
        # Token vars stripped from every job command's environment so a command
        # cannot read the credential repoactive uses to push/create MRs.
        return frozenset(self.config.token_env_names())


def _compute_parents(job: Job, results: dict[str, JobResult]) -> list[str]:
    if not job.depends_on:
        return [job.base_branch or "trunk()"]

    parents: list[str] = []
    seen: set[str] = set()
    for dep_name in job.depends_on:
        for revset in results[dep_name].effective_revsets:
            if revset not in seen:
                seen.add(revset)
                parents.append(revset)
    return parents


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the whole process group led by ``proc``.

    The command is started with ``start_new_session=True`` so it leads its own
    process group; killing the group reaps any children the command spawned, not
    just the top-level shell.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def _command_env(
    *, extra_env: dict[str, str] | None, secret_env_names: frozenset[str]
) -> dict[str, str]:
    """Build the environment a job command runs in.

    Starts from the inherited environment (so the command still sees PATH etc.),
    drops the platform token variables (``secret_env_names``) so a command cannot read
    the credential repoactive uses to push/create MRs, then layers on ``extra_env``
    (e.g. RA_JOBS_DIR for a generator). See
    docs/adr/0006-job-commands-are-trusted.md.
    """
    env = {k: v for k, v in os.environ.items() if k not in secret_env_names}
    if extra_env:
        env.update(extra_env)
    return env


def _job_extra_env(job: Job, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Extra environment for ``job``'s command: its branch, config dir, ``extra``.

    Always adds RA_JOB_BRANCH (the bookmark repoactive uses for the job's output).
    Adds RA_CONFIG_SOURCE_DIR when the job has a ``config_source_dir`` (the
    directory of the config source that defined its command), on top of any
    caller-supplied entries (e.g. RA_JOBS_DIR for a generator).
    """
    env = dict(extra or {})
    env[RA_JOB_BRANCH_ENV] = job.branch_name()
    if job.config_source_dir is not None:
        env[RA_CONFIG_SOURCE_DIR_ENV] = job.config_source_dir
    return env


@contextlib.contextmanager
def _watchdog(proc: subprocess.Popen[str], timeout: float | None) -> Generator[threading.Event]:
    """Kill ``proc``'s process group if it outlives ``timeout`` seconds.

    The blocking stdout read in ``_run_command`` cannot be interrupted by a
    timeout, so a background timer SIGKILLs the process group once the deadline
    passes; that closes stdout and ends the read loop. The poll() guard avoids
    flagging a false timeout when the command finishes just as the timer fires;
    the remaining race (the command exits between poll() and the kill) is closed
    by the caller, which treats only a non-zero exit as a timeout.

    Yields an event that is set iff the watchdog fired. ``timeout is None`` means
    no deadline: no timer is started and the event never fires.
    """
    timed_out = threading.Event()

    def _on_timeout() -> None:
        if proc.poll() is None:
            timed_out.set()
            _kill_process_group(proc)

    timer = threading.Timer(timeout, _on_timeout) if timeout is not None else None
    if timer is not None:
        timer.start()
    try:
        yield timed_out
    finally:
        if timer is not None:
            timer.cancel()


@contextlib.contextmanager
def _spawn(job: Job, cwd: Path, env: dict[str, str]) -> Generator[subprocess.Popen[str]]:
    """Run ``job.command`` in its own session, cleaning up on exit.

    start_new_session puts the command in its own process group so a timeout can
    kill the whole tree (see _kill_process_group). On exit, if the command is
    still running — a body that raised before it finished, not just a timeout —
    the process group is killed and reaped so nothing is orphaned or left a
    zombie; then stdout is closed.
    """
    proc = subprocess.Popen(
        job.command,
        shell=True,
        # None keeps subprocess' shell=True default of /bin/sh; a configured shell
        # runs the command as `<shell> -c <command>` (see Job.shell).
        executable=job.shell,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # Decode as UTF-8 and never raise on undecodable bytes: a job command may
        # emit arbitrary output, and a decode error must not crash the run.
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
        env=env,
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            _kill_process_group(proc)
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()


def _run_command(
    job: Job,
    cwd: Path,
    *,
    secret_env_names: frozenset[str] = frozenset(),
    extra_env: dict[str, str] | None = None,
) -> CommandResult:
    start = time.monotonic()
    # The workspace is always cwd, but expose it explicitly so a command that
    # cd's elsewhere can still find the workspace repoactive prepared for it.
    env = _command_env(
        extra_env={**(extra_env or {}), RA_WORKSPACE_DIR_ENV: str(cwd)},
        secret_env_names=secret_env_names,
    )

    # Stream the merged stdout/stderr line by line: keep the full output (needed
    # for the commit message and the success result) while feeding a live tail of
    # the last few lines (see repoactive.progress).
    output_lines: list[str] = []
    timeout = job.timeout_seconds()
    view = ProgressView(
        name=job.name,
        command=job.command,
        max_lines=load_settings().progress_lines,
        timeout=timeout,
    )
    with _spawn(job, cwd, env) as proc:
        assert proc.stdout is not None
        with _watchdog(proc, timeout) as timed_out, view:
            for line in proc.stdout:
                output_lines.append(line)
                view.feed(line)
            proc.wait()

    elapsed = time.monotonic() - start
    # On failure report the full output, not just the live tail: in a terminal the
    # live block only showed the last few lines, and piped/CI runs showed nothing,
    # so the complete output is what makes a failure diagnosable.
    detail = "".join(output_lines).strip()
    # The watchdog can lose a race: poll() saw the command still running, the
    # command then exited on its own, and the kill hit a dead process. A killed
    # process reports a non-zero returncode (-SIGKILL), so exit code 0 means the
    # command actually finished - treat that as success, not a timeout.
    if timed_out.is_set() and proc.returncode != 0:
        raise CommandError(
            f"command timed out after {job.timeout}" + (f":\n{detail}" if detail else ""),
            elapsed=elapsed,
        )
    if proc.returncode != 0:
        raise CommandError(
            f"command failed with exit code {proc.returncode}"
            + (f":\n{detail}" if detail else ""),
            elapsed=elapsed,
        )
    return CommandResult(output=detail, elapsed=elapsed)


def _discard_empty_job(
    *,
    job: Job,
    parents: list[str],
    repo: JJ,
    command_result: CommandResult,
    old_change_id: str | None,
) -> JobResult:
    """Clean up after a job whose command produced no diff.

    Abandons the empty working commit. The old bookmark (if any) is left intact
    here; the absorb phase deletes it and records the remote deletion. Returns a
    JobResult with produced_diff=False, carrying the original parents forward so
    dependents still have a base.
    """
    repo.abandon()
    elapsed = format_elapsed(command_result.elapsed)
    if old_change_id:
        print_status(job.name, ("no changes", "dim"), f", bookmark will be deleted ({elapsed})")
    else:
        print_status(job.name, ("no changes", "dim"), f" ({elapsed})")
    return JobResult(
        job=job,
        effective_revsets=parents,
        parents=parents,
        produced_diff=False,
        old_change_id=old_change_id,
        command_output=command_result.output,
    )


def _strip_boxquote_and_trailers(message: str) -> str:
    """Strip the boxquote section and trailer block from a commit message.

    Returns the author-controlled parts - the title line and description - so two
    commit messages can be compared while ignoring the command output (rendered
    in a boxquote) and the ``Repoactive-Job`` trailer(s).

    Trailers are stripped first, while they are still the final paragraph of the
    built message; ``strip_boxquotes`` reflows whitespace and could otherwise
    disturb that.

    Note: inaccurate when the commit description itself contains a boxquote
    """
    return strip_boxquotes(strip_trailers(message))


def _build_commit_message(job: Job, command_result: CommandResult) -> str:
    """Build the commit message recorded for a job's change.

    The title, an optional description, the command output rendered in a
    boxquote.el-style box (when ``output_in_commit`` is set), and finally the
    ``Repoactive-Job`` trailer(s).
    """
    message = f"{job.commit_title_prefix}{job.title}"
    if job.description:
        message += f"\n\n{job.description}"
    if job.output_in_commit and command_result.output:
        message += f"\n\n{boxquote(command_result.output, title=job.command)}"
    # Trailer must be the final paragraph so jj/git recognise it as a trailer;
    # it lets later runs detect when this job last landed (see cooldown handling).
    message += "\n\n" + "\n".join(job.commit_trailers())
    return message


def _commit_job(
    *,
    job: Job,
    parents: list[str],
    repo: JJ,
    command_result: CommandResult,
    old_change_id: str | None,
) -> JobResult:
    """Commit the diff a job's command produced in the fresh workspace.

    Writes the commit message and records the new commit's change-id. The
    bookmark is NOT set here — that happens in the absorb phase (phase 2).
    Returns a JobResult with produced_diff=True whose effective_revsets is the
    new change-id so dependent jobs build directly on this commit during phase 1.
    The push and MR are recorded in the absorb phase once the bookmark is set.
    """
    stat = repo.diff_stat()
    repo.describe(_build_commit_message(job, command_result))
    new_change_id = repo.change_id()

    print_status(
        job.name,
        ("committed", "green"),
        f" [{new_change_id}] ({format_elapsed(command_result.elapsed)})",
    )
    if stat:
        print("\n".join(f"    {line}" for line in stat.splitlines()))
        print()

    return JobResult(
        job=job,
        effective_revsets=[new_change_id],
        parents=parents,
        produced_diff=True,
        new_change_id=new_change_id,
        old_change_id=old_change_id,
        command_output=command_result.output,
    )


def run_job(
    ctx: RunContext,
    *,
    job: Job,
    parents: list[str],
) -> JobResult:
    logger.debug("starting job: %s", job.model_dump_json(indent=2))
    logger.debug("parents: %s", parents)

    with JJ(ctx.repo_path).temp_workspace(workspace_name(job.name)) as repo:
        bookmark = job.branch_name()
        # Record the pre-existing bookmark's change-id so the absorb phase can
        # mutate it in place (preserving jj change-id continuity). None for new jobs.
        old_change_id = repo.bookmark_change_id(bookmark)
        logger.debug("[%s] bookmark %s old_change_id=%s", job.name, bookmark, old_change_id)

        # Always start from a fresh commit: old bookmarks are never touched
        # during the run phase, so a failed command cannot destroy them.
        repo.new(*parents)
        repo.git_sync_head()
        logger.debug("[%s] running command: %s", job.name, job.command)
        try:
            command_result = _run_command(
                job,
                repo.cwd,
                secret_env_names=ctx.secret_env_names,
                extra_env=_job_extra_env(job),
            )
        except CommandError:
            # The command timed out or failed; discard only the fresh commit.
            # The old bookmark is completely unaffected.
            repo.abandon()
            raise
        logger.debug(
            "[%s] command finished in %.3fs, %d bytes output",
            job.name,
            command_result.elapsed,
            len(command_result.output),
        )
        if repo.is_empty():
            logger.debug("[%s] working copy is empty, no diff produced", job.name)
            return _discard_empty_job(
                job=job,
                parents=parents,
                repo=repo,
                command_result=command_result,
                old_change_id=old_change_id,
            )
        return _commit_job(
            job=job,
            parents=parents,
            repo=repo,
            command_result=command_result,
            old_change_id=old_change_id,
        )


def _load_job_specs(jobs_dir: Path) -> dict[str, dict]:
    """Parse the ``*.toml`` fragments a generator wrote into ``jobs_dir``.

    Files are read in sorted order and their ``[job.<name>]`` tables merged by
    name (later files win), the same machinery used for the ``.repoactive.d``
    directory. Fragments may only contain ``[job.<name>]`` tables (see
    ``FragmentShape``). Returns the raw job-spec table keyed by name, before
    inheritance/validation.
    """
    specs: dict[str, dict] = {}
    for path in expand_config_paths([jobs_dir]):
        data = tomllib.loads(path.read_text())
        specs = merge_jobs(base=specs, override=FragmentShape.model_validate(data).job)
    return specs


def _build_generated_job(
    *, generator: Job, name: str, spec: dict, run_names: set[str], all_config_names: set[str]
) -> Job:
    """Build one emitted ``Job`` from its raw spec, applying inheritance.

    ``name`` is the spec's table key. The job inherits the (resolved) generator's
    tags, ``depends_on`` and the ``_INHERITED_FIELDS`` unless the spec overrides
    them, and records the generator in ``generated_by``. Raises GeneratedJobError
    on a name colliding with an existing job, a nested generator, or a job that
    fails validation.
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
        return Job.model_validate(merged)
    except ValidationError as e:
        raise GeneratedJobError(generator.name, f"emitted job {name!r} is invalid: {e}") from e


def _build_generated_jobs(
    *, generator: Job, specs: dict[str, dict], run_names: set[str], all_config_names: set[str]
) -> list[Job]:
    """Turn a generator's raw specs into validated ``Job`` objects.

    Validates each spec (see ``_build_generated_job``), that every
    ``depends_on`` target is within this run (the existing jobs or a sibling
    emitted job), and that the emitted jobs are acyclic — a cycle would
    otherwise silently mis-order the topological sort and crash the run.
    ``generator`` must be resolved (its inherited fields filled in).
    """
    emitted = [
        _build_generated_job(
            generator=generator,
            name=name,
            spec=spec,
            run_names=run_names,
            all_config_names=all_config_names,
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


def _format_duration(seconds: float) -> str:
    """Format a duration in seconds as a human-readable string like '3d 2h' or '45m'."""
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        parts = [f"{days}d"]
        if hours:
            parts.append(f"{hours}h")
        return " ".join(parts)
    if hours:
        parts = [f"{hours}h"]
        if minutes:
            parts.append(f"{minutes}m")
        return " ".join(parts)
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def _on_cooldown(job: Job, repo_path: Path) -> datetime | None:
    """Return the last-run timestamp if the job is still on cooldown, else None."""
    delta = job.cooldown_timedelta()
    if delta is None:
        return None
    base = job.base_branch or "trunk()"
    since = datetime.now(UTC) - delta
    # A superseding job's landing also throttles this job (ADR 0015).
    last_run = JJ(repo_path).last_job_commit_date(
        job_names={job.name, *job.cooldown_on}, base=base, since=since
    )
    logger.debug(
        "[%s] cooldown check: base=%s since=%s -> last_run=%s",
        job.name,
        base,
        since.isoformat(),
        last_run,
    )
    return last_run


def _dispatch_job(ctx: RunContext, *, job: Job) -> list[Job]:
    """Run a single job, recording its outcome in ``ctx.summary``/``ctx.blocked``.

    A failed or skipped job adds its name to ``ctx.blocked`` so its dependents are
    skipped in turn. Returns the jobs a generator (``emits_jobs``) produced — an
    empty list for an ordinary job or a generator that emitted nothing/was
    skipped. ``ctx.selection.jobs`` is every job in this run (``_run_jobs`` keeps
    it in sync as generators emit), so its names reject an emitted job that
    collides with an existing one. ``ctx.selection.refreshed`` names the jobs that
    already have an unmerged branch; such a job is never cooldown-skipped or
    run_only_if_changed-skipped, so its branch is refreshed (ADR 0003).
    ``ctx.selection.successors`` names the jobs force-included because their
    commits sit above a selected job's bookmark; they bypass their own cooldown
    but are skipped when every dependency was itself skipped this run. The plan is
    built in the absorb phase (phase 2), not here.
    """
    summary = ctx.summary
    selection = ctx.selection
    blocking_deps = [d for d in job.depends_on if d in ctx.blocked]
    if blocking_deps:
        print_status(
            job.name, ("skipped", "yellow"), f" (dependency failed: {', '.join(blocking_deps)})"
        )
        summary.skipped.add(job.name)
        ctx.blocked.add(job.name)
        return []

    parents = _compute_parents(job, summary.results)

    # run_only_if_changed gates jobs whose effect is conditional on upstream
    # diffs. A refreshed job bypasses the gate for the same reason it bypasses
    # cooldown: it has an open branch that must be rebased (ADR 0003), and
    # skipping it here would leave the branch un-rebased and orphan its MR.
    if job.run_only_if_changed and job.name not in selection.refreshed:
        any_changed = any(
            r.produced_diff
            for d in job.run_only_if_changed
            if (r := summary.results.get(d)) is not None
        )
        if not any_changed:
            print_status(
                job.name,
                ("skipped", "yellow"),
                f" (run_only_if_changed: none of {job.run_only_if_changed} produced changes)",
            )
            summary.run_only_if_changed_skipped.add(job.name)
            summary.results[job.name] = JobResult(
                job=job, effective_revsets=parents, produced_diff=False
            )
            return []
    logger.debug("[%s] computed parents: %s", job.name, parents)
    # A successor exists to be rebuilt when the stack below it moves. If every
    # dependency was itself skipped this run (cooldown or an earlier successor
    # skip), nothing it builds on changed, so re-running it would reproduce the
    # same result; record a no-op so its own successors skip too and the absorb
    # phase leaves its bookmark alone. Judged on depends_on, not the commit
    # graph: a successor whose config no longer declares the dependency it is
    # stacked on falls through and runs — the safe direction.
    not_run = summary.on_cooldown | summary.successor_skipped
    if (
        job.name in selection.successors
        and job.depends_on
        and all(dep in not_run for dep in job.depends_on)
    ):
        print_status(job.name, ("skipped", "yellow"), " (successor: no dependency ran)")
        summary.successor_skipped.add(job.name)
        summary.results[job.name] = JobResult(
            job=job, effective_revsets=parents, produced_diff=False
        )
        return []

    # Cooldown only throttles *starting fresh work*. A job that already has an
    # open (unmerged) branch must always run so it is rebased on the latest trunk
    # and, when its change is now redundant, produces an empty diff that
    # self-closes the MR via the empty-diff path; skipping it here would leave the
    # branch un-rebased and orphan its MR, defeating the refresh guarantee of
    # ADR 0003. A successor bypasses cooldown for the same reason: its base just
    # moved, so it must rebuild regardless of when it last landed.
    #
    # Checked before the emits_jobs branch on purpose: a generator on cooldown
    # emits nothing, throttling its whole fan-out as a unit (the dual trailer
    # records a recent child landing as the generator's); a generator has no
    # branch, so it is never in selection.refreshed. See docs/adr/0004-job-generators.md.
    if (
        job.name not in selection.refreshed
        and job.name not in selection.successors
        and (last_run := _on_cooldown(job, ctx.repo_path))
    ):
        elapsed = datetime.now(UTC) - last_run
        elapsed_str = _format_duration(elapsed.total_seconds())
        print_status(
            job.name,
            ("on cooldown", "yellow"),
            f" ({job.cooldown_period}), last run {elapsed_str} ago, skipped",
        )
        summary.on_cooldown.add(job.name)
        # Treat like a no-op run so dependents proceed on the base branch.
        summary.results[job.name] = JobResult(
            job=job, effective_revsets=parents, produced_diff=False
        )
        return []

    start = time.monotonic()
    try:
        emitted: list[Job] = []
        if job.emits_jobs:
            result, emitted = _run_generator_job(ctx, job=job, parents=parents)
        else:
            result = run_job(ctx, job=job, parents=parents)
        summary.results[job.name] = result
        return emitted
    except Exception as e:
        # A command failure reports the command's own time (matching the
        # success prints); other failures have no command time, so fall back
        # to the wall time spent in run_job.
        elapsed = e.elapsed if isinstance(e, CommandError) else time.monotonic() - start
        print_status(job.name, ("failed", "red"), f": {e} ({format_elapsed(elapsed)})")
        summary.failed[job.name] = e
        ctx.blocked.add(job.name)
        return []


def _run_generator_job(
    ctx: RunContext, *, job: Job, parents: list[str]
) -> tuple[JobResult, list[Job]]:
    """Run a generator and return its emitted jobs (resolved ``job`` required).

    The command runs in a fresh workspace on top of ``parents`` with
    ``RA_JOBS_DIR`` pointing at an empty directory; it writes ``*.toml``
    fragments there which are parsed once it exits. The generator itself produces
    no diff: any working-copy change it leaves is discarded (ADR 0004), and a
    no-op ``JobResult`` is recorded so its emitted jobs (which depend on it)
    compute their parents through it. A failure to run or to build the emitted
    set blocks the generator's dependents, exactly like an ordinary job failure.
    """
    logger.debug("starting generator: %s", job.name)
    with (
        JJ(ctx.repo_path).temp_workspace(workspace_name(job.name)) as repo,
        # The output directory lives outside the workspace, so the files written there never
        # show up as a diff in the working copy.
        tempfile.TemporaryDirectory(prefix="repoactive-jobs-") as tmp,
    ):
        repo.new(*parents)
        try:
            repo.git_sync_head()
            jobs_dir = Path(tmp)
            logger.debug("[%s] running generator command (jobs dir %s)", job.name, jobs_dir)
            _run_command(
                job,
                repo.cwd,
                secret_env_names=ctx.secret_env_names,
                extra_env=_job_extra_env(job, {RA_JOBS_DIR_ENV: str(jobs_dir)}),
            )
            specs = _load_job_specs(jobs_dir)
        finally:
            repo.abandon()
    logger.debug("[%s] generator emitted %d job spec(s)", job.name, len(specs))
    # selection.jobs is every job already in the run (collision guard); config.jobs
    # are the statically configured names.
    emitted = _build_generated_jobs(
        generator=job,
        specs=specs,
        run_names={j.name for j in ctx.selection.jobs},
        all_config_names={j.name for j in ctx.config.jobs},
    )

    names = ", ".join(j.name for j in emitted) if emitted else "none"
    print_status(job.name, f"generated {len(emitted)} job(s): {names}")

    return JobResult(job=job, effective_revsets=parents, produced_diff=False), emitted


def _absorb_no_diff_bookmark(
    job: Job, result: JobResult, bookmark: str, *, repo: JJ, plan: UpdatePlan
) -> None:
    """Delete the local bookmark (if present) and schedule a remote delete push.

    Schedules the push when either the local bookmark was just deleted, or the
    remote still has it from a previous push (e.g. after a -mlocal run that
    deleted the local bookmark without applying the plan).
    """
    if result.old_change_id:
        repo.bookmark_delete(bookmark)
    if result.old_change_id or repo.remote_bookmark_exists(bookmark):
        plan.updates.append(
            JobUpdate(
                job_name=job.name,
                title=result.job.title,
                push=BookmarkPush(bookmark=bookmark, delete=True),
            )
        )


def _absorb_results(ctx: RunContext) -> None:
    """Phase 2: absorb fresh phase-1 commits into pre-existing commits.

    Iterates ``ctx.selection.jobs`` — every job in the run, including
    generator-emitted ones, in topological order (``_run_jobs`` keeps that list
    current). For each successful job:
    - No diff produced: delete the old bookmark if the command ran and found
      nothing (cooldown skips are left untouched), and record a remote deletion.
    - Diff produced, new job: set the bookmark directly on the new commit.
    - Diff produced, existing job: rebase the old commit onto the same parents,
      then abandon the new commit. Only when the diffs differ is the old
      commit's content restored from the new commit and its message updated.
      Change-id continuity is preserved so jj auto-rebases any dependents not
      in this run.

    Fills in ``ctx.plan`` (bookmark pushes and MR descriptors) as a side-effect.
    """
    summary = ctx.summary
    plan = ctx.plan
    repo = ctx.repo

    # Maps each phase-1 new_change_id to the canonical change-id post-absorb so
    # dependent jobs are rebased onto the right commit, not the abandoned fresh one.
    absorbed: dict[str, str] = {}

    for job in ctx.selection.jobs:
        result = summary.results.get(job.name)
        if result is None:
            logger.debug("absorb: [%s] no result, skipping", job.name)
            continue

        bookmark = result.job.branch_name()
        logger.debug(
            "absorb: [%s] produced_diff=%s new=%s old=%s parents=%s",
            job.name,
            result.produced_diff,
            result.new_change_id,
            result.old_change_id,
            result.parents,
        )

        if not result.produced_diff:
            # Only delete the bookmark when the command ran and produced no
            # diff. Cooldown, successor, and run_only_if_changed skips are
            # intentional — leave their bookmarks alone so the branch is not
            # destroyed.
            if job.name not in (
                summary.on_cooldown
                | summary.successor_skipped
                | summary.run_only_if_changed_skipped
            ):
                _absorb_no_diff_bookmark(job, result, bookmark, repo=repo, plan=plan)
            continue

        assert result.new_change_id is not None
        new_change_id = result.new_change_id
        message = _build_commit_message(
            result.job, CommandResult(output=result.command_output, elapsed=0.0)
        )

        # Translate phase-1 parent change-ids to their canonical post-absorb ids.
        canonical_parents = [absorbed.get(p, p) for p in result.parents]

        if result.old_change_id:
            old_change_id = result.old_change_id
            repo.rebase_revision(old_change_id, *canonical_parents)
            content_unchanged = repo.same_content(old_change_id, new_change_id)
            logger.debug(
                "absorb: [%s] rebased %s onto %s, content %s",
                job.name,
                old_change_id,
                canonical_parents,
                "unchanged" if content_unchanged else "differs, restoring",
            )
            if not content_unchanged:
                # restore names both revisions, so it rewrites old_change_id directly
                # without touching any working copy — no scratch workspace needed.
                repo.restore(source_rev=new_change_id, destination_rev=old_change_id)
                repo.describe_revision(old_change_id, message)
            elif _strip_boxquote_and_trailers(
                repo.get_description(old_change_id)
            ) != _strip_boxquote_and_trailers(message):
                repo.describe_revision(old_change_id, message)
            repo.abandon_revision(new_change_id)
            # No bookmark_set needed: the bookmark follows old_change_id through
            # the rewrites above (jj moves local bookmarks with the commit).
            absorbed[new_change_id] = old_change_id
        else:
            logger.debug(
                "absorb: [%s] new job, bookmark %s -> %s", job.name, bookmark, new_change_id
            )
            repo.bookmark_set(bookmark, new_change_id)
            absorbed[new_change_id] = new_change_id

        mr: MRUpdate | None = None
        if result.job.create_mr is not CreateMR.never:
            mr = MRUpdate(
                source_branch=bookmark,
                target_branch=result.job.base_branch,
                title=f"{result.job.mr_title_prefix}{result.job.title}",
                description=result.job.description or "",
                command=result.job.command,
                command_output=result.command_output,
                labels=result.job.labels,
                draft=result.job.draft,
                auto_merge=result.job.auto_merge or False,
                required_approvals=result.job.required_approvals,
                depends_on=list(result.job.depends_on),
            )
        plan.updates.append(
            JobUpdate(
                job_name=job.name,
                title=result.job.title,
                push=BookmarkPush(bookmark=bookmark),
                mr=mr,
            )
        )


@contextlib.contextmanager
def _prepare_repo(*, config: Config, repo_path: Path) -> Generator[JJ]:
    repo = JJ(repo_path)
    op_id = repo.op_id()

    try:
        # Drop any temporary workspaces a previous, killed run left behind before we
        # start adding fresh ones.
        repo.forget_stale_workspaces()
        # Track the bookmarks repoactive manages so a branch an earlier run pushed
        # is recognised (and rebased/updated) instead of recreated. Tracking an
        # absent bookmark is a harmless no-op.
        repo.bookmark_track(*sorted(config.bookmark_names() | config.base_branches()))
        yield repo
    finally:
        # Tell the user how to roll back the run. This only undoes changes made to the
        # local repository - a pushed branch or a created MR is not affected - so the
        # hint says so explicitly. Printed at the end (not the start) so it is the last
        # thing on screen after a run that can produce a lot of output.
        print_undo_hint(
            title="To undo this run",
            body=(
                "This undoes the changes made to the local repository by this run.\n"
                "It does not affect any pushed branches or created MRs."
            ),
            command=f"jj --repository {repo_path.resolve()} op restore {op_id}",
            style="cyan",
        )


def _run_jobs(ctx: RunContext) -> None:
    """Run ``ctx.selection`` in topological order, expanding generators in place.

    ``ctx.selection.jobs`` is topologically sorted, so the first job not yet in
    ``started`` always has its dependencies satisfied: every job ahead of it in
    the order has already run (were one not, *it* would be the first
    not-started job).
    A generator's emitted jobs are appended and the list re-sorted so each runs
    after its dependencies (the generator included); the next iteration picks
    them up once their turn comes. See docs/adr/0004-job-generators.md.

    Results are recorded in ``ctx.summary`` in place, and generator-emitted jobs
    are spliced into ``ctx.selection.jobs`` (still topologically sorted) — the
    absorb phase iterates that same list.

    ``ctx.selection.refreshed`` names the jobs being refreshed because they
    already have an unmerged branch; they bypass the cooldown skip so their
    branches are rebased (ADR 0003). Empty for explicit selection, which does not
    refresh (ADR 0003). ``ctx.selection.successors`` names the jobs force-included
    because their commits sit above a selected job's bookmark; they run only when
    something below them in the stack ran (see _dispatch_job).
    """
    selection = ctx.selection
    started: set[str] = set()
    while True:
        job = next((j for j in selection.jobs if j.name not in started), None)
        if job is None:
            break
        started.add(job.name)
        emitted = _dispatch_job(ctx, job=job)
        if emitted:
            # Resolve before splicing in so _dispatch_job receives resolved jobs,
            # matching the invariant for jobs from selection.
            resolved_emitted = [j.resolve(ctx.config.job_defaults) for j in emitted]
            # Track the new jobs' bookmarks so a branch an earlier run already
            # pushed is reused rather than recreated, then splice them into the
            # selection and re-sort so they run after their dependencies.
            ctx.repo.bookmark_track(*sorted(j.branch_name() for j in resolved_emitted))
            selection.jobs = topological_sort(selection.jobs + resolved_emitted)
            print_job_table(format_job_forest(selection.jobs), indent="  ")


def _suppress_superseded_mrs(*, plan: UpdatePlan, results: dict[str, JobResult]) -> None:
    """Drop the MR of every ``create_mr = "unless-superseded"`` job whose changes a dependent's MR already contains.

    A dependent's change is stacked on its dependencies' branches
    (``_compute_parents``), so a dependent's MR diff already includes this job's
    changes. ``results`` is in run order (topological), so walking it in reverse
    decides each job before its dependencies: a job whose MR survives covers its
    dependencies, and a covered job passes its cover down (even when it records
    no MR itself, e.g. an empty job the stack built through). Only MRs recorded
    in this run's plan count — a dependent that is empty, failed, on cooldown,
    or not selected does not supersede.
    See docs/adr/0009-unless-superseded-mr-creation.md.
    """
    updates = {u.job_name: u for u in plan.updates}
    # Job name -> the dependent whose surviving MR contains this job's changes.
    covered_by: dict[str, str] = {}
    for name in reversed(list(results)):
        job = results[name].job
        update = updates.get(name)
        has_mr = False
        if update is not None and update.mr is not None:
            if job.create_mr is CreateMR.unless_superseded and name in covered_by:
                update.mr = None
                print_status(name, "MR superseded by ", (f"[{covered_by[name]}]", "cyan"))
            else:
                has_mr = True
        cover = name if has_mr else covered_by.get(name)
        if cover is not None:
            for dep in job.depends_on:
                covered_by.setdefault(dep, cover)


def run_all(  # noqa: PLR0913
    *,
    config: Config,
    repo_path: Path,
    platform: Platform | None = None,
    requested_names: frozenset[str] = frozenset(),
    requested_tags: frozenset[str] = frozenset(),
    mode: RunMode = RunMode.local,
) -> RunSummary:
    # A publish run needs a platform to create MRs; local/push runs must not be
    # given one. The CLI keeps these in sync - this guards direct callers.
    assert (mode is RunMode.publish) == (platform is not None), (
        f"mode={mode} is inconsistent with platform={platform!r}"
    )
    run_start = time.monotonic()
    # Building the selector validates the request, failing on a mistyped job name
    # or tag before the lock is taken and before _prepare_repo mutates anything
    # (or promises an undo hint for a run that never started).
    selector = JobSelector(
        config=config, requested_names=requested_names, requested_tags=requested_tags
    )
    # Serialise runs against the same repository: a run mutates repo-global state
    # (workspaces, bookmarks, pushes), and forget_stale_workspaces would clobber a
    # concurrent run's live workspaces. Fail-fast if another run holds the lock.
    with run_lock(repo_path), _prepare_repo(config=config, repo_path=repo_path) as repo:
        logger.debug(
            "run_all: repo=%s mode=%s requested_names=%s requested_tags=%s",
            repo_path,
            mode,
            requested_names,
            requested_tags,
        )

        selection = selector.select_run_jobs(repo)
        summary = RunSummary()
        # Run-wide state, built once and threaded through _run_jobs → _dispatch_job.
        # ctx.selection is this same selection object, so the in-place splice in
        # _run_jobs keeps ctx.selection.jobs current as generators emit.
        ctx = RunContext(
            config=config,
            repo_path=repo_path,
            repo=repo,
            summary=summary,
            blocked=set(),
            selection=selection,
        )

        print(f"Running {len(selection.jobs)} job(s):")
        # The same dependency tree 'info jobs' shows, restricted to this run's
        # selection (generator-emitted jobs appear later, as they run).
        print_job_table(format_job_forest(selection.jobs), indent="  ")
        print()
        # Phase 1: run every job on a fresh commit; old bookmarks are untouched.
        # Generator-emitted jobs are spliced into ctx.selection.jobs so the absorb
        # phase processes them too. Jobs pulled in for refresh bypass the cooldown
        # skip so their branches are rebased (ADR 0003).
        _run_jobs(ctx)

        # Phase 2: absorb fresh commits into old commits (preserving change-ids),
        # set bookmarks for new jobs, delete bookmarks for empty jobs, fill ctx.plan.
        _absorb_results(ctx)

        # Resolve "unless-superseded" now that every job has run: a job's MR is
        # dropped from the plan when a dependent's MR in this run contains it.
        _suppress_superseded_mrs(plan=ctx.plan, results=summary.results)

        # A local run stops here: the plan is built but deliberately not applied, so
        # nothing is pushed and no MR is created.
        if mode is not RunMode.local:
            applied = apply_plan(ctx.plan, repo_path=repo_path, platform=platform, mode=mode)
            for name, url in applied.mr_urls.items():
                summary.results[name].mr_url = url
            # A job whose MR failed keeps its results entry (the command ran and
            # its branch was pushed) but the run still counts as failed.
            summary.failed.update(applied.failed)

        summary.elapsed = time.monotonic() - run_start
        summary.print_report()
        return summary


def _apply_plan_push(plan: UpdatePlan, *, repo_path: Path) -> None:
    """Push every bookmark recorded in the plan in a single jj call.

    A no-op when the plan records no pushes (git_push_bookmarks ignores an empty
    bookmark list).
    """
    bookmarks = [update.push.bookmark for update in plan.updates if update.push is not None]
    JJ(repo_path).git_push_bookmarks(*bookmarks)


@dataclass
class ApplyResult:
    """Outcome of applying an UpdatePlan: the MR URL or the failure, per job."""

    mr_urls: dict[str, str] = field(default_factory=dict)
    failed: dict[str, Exception] = field(default_factory=dict)


def _apply_plan_publish(
    plan: UpdatePlan,
    *,
    platform: Platform,
) -> ApplyResult:
    titles = {u.job_name: u.title for u in plan.updates}
    result = ApplyResult()

    pending = [
        u for u in plan.updates if not (u.push is not None and u.push.delete) and u.mr is not None
    ]
    for i, update in enumerate(pending):
        assert update.mr is not None  # filtered above
        dependency_links = [
            MRLink(title=titles[dep], url=result.mr_urls[dep])
            for dep in update.mr.depends_on
            if dep in result.mr_urls
        ]
        # Fail fast: a failing platform call usually means something is wrong
        # (expired token, rate limit), so the remaining MRs are not attempted
        # rather than hammered against the same failure. Nothing is lost - the
        # bookmarks are already pushed, ensure_mr is idempotent, and the next
        # run re-attempts every MR. The failure is recorded per job and
        # surfaces in the run summary.
        try:
            params = MRParams(
                source_branch=update.mr.source_branch,
                target_branch=update.mr.target_branch or platform.default_branch(),
                title=update.mr.title,
                description=build_mr_description(update.mr, dependency_links),
                labels=update.mr.labels,
                draft=update.mr.draft,
                auto_merge=update.mr.auto_merge,
                required_approvals=update.mr.required_approvals,
            )
            url = platform.ensure_mr(params)
        except Exception as e:
            print_status(update.job_name, ("failed", "red"), f" to create/update MR: {e}")
            result.failed[update.job_name] = e
            remaining = [u.job_name for u in pending[i + 1 :]]
            if remaining:
                print(f"==> aborting MR updates, not attempted: {', '.join(remaining)}")
            break
        result.mr_urls[update.job_name] = url
        print_status(update.job_name, url)
    return result


def apply_plan(
    plan: UpdatePlan, *, repo_path: Path, platform: Platform | None, mode: RunMode
) -> ApplyResult:
    """Carry out the remote operations collected during a run.

    Pushes each bookmark and, in ``publish`` mode, creates/updates each MR. MRs
    are processed in plan order (topological), so a dependency's MR URL is known
    by the time a dependent that links to it is reached. The MR loop is
    fail-fast: the first failing MR is recorded per job and the remaining
    updates are not attempted (the next run re-attempts them; ensure_mr is
    idempotent and the bookmarks are pushed regardless). Returns the MR URLs
    and failures per job.
    """
    assert mode is not RunMode.local
    if not plan.updates:
        return ApplyResult()
    print(f"Applying {len(plan.updates)} update(s)...")
    _apply_plan_push(plan, repo_path=repo_path)

    if mode is RunMode.publish:
        assert platform is not None
        return _apply_plan_publish(plan, platform=platform)
    return ApplyResult()
