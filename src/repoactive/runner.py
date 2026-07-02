import contextlib
import logging
import os
import shutil
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

from repoactive.config import (
    DEFAULT_TAG,
    CircularDependencyError,
    Config,
    CreateMR,
    Job,
    _merge_jobs,
    detect_dependency_cycle,
    expand_config_paths,
    jobs_table,
)
from repoactive.jj import JJ, workspace_name
from repoactive.lock import run_lock
from repoactive.platforms.base import MRParams, Platform
from repoactive.progress import ProgressView, progress_lines
from repoactive.ui import print_undo_hint
from repoactive.updates import (
    BookmarkPush,
    JobUpdate,
    MRUpdate,
    UpdatePlan,
    build_mr_description,
)

logger = logging.getLogger(__name__)

# Environment variable naming the directory a generator (``emits_jobs``) command
# writes its ``*.toml`` job fragments into. See docs/adr/0004-job-generators.md.
REPOACTIVE_JOBS_DIR_ENV = "REPOACTIVE_JOBS_DIR"

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
    """A job command exited non-zero. Carries the command's wall time so the
    failure can be reported with the same elapsed semantics as a success."""

    def __init__(self, message: str, elapsed: float) -> None:
        super().__init__(message)
        self.elapsed = elapsed


class UnknownJobsError(ValueError):
    """Raised when requested job names do not match any configured job."""

    def __init__(self, unknown: set[str]) -> None:
        super().__init__(f"Unknown job(s): {', '.join(sorted(unknown))}")


class GeneratedJobError(ValueError):
    """Raised when a generator emits an invalid job set (collision, recursion,
    unknown dependency, or a job that fails validation)."""

    def __init__(self, generator: str, message: str) -> None:
        super().__init__(f"generator {generator!r}: {message}")


@dataclass
class JobResult:
    job: Job
    # Revsets dependents should use as parents. Either the bookmark name (if
    # the command produced a diff) or the parent revsets the change was based
    # on (if the command produced nothing and the change was abandoned).
    effective_revsets: list[str]
    produced_diff: bool
    # Pending remote operations for this job, collected during the run and
    # carried out later by apply_plan. None for cooldown/local runs.
    update: JobUpdate | None = None
    # Filled in by the apply phase once the MR has been created.
    mr_url: str | None = None
    command_output: str = ""


@dataclass
class RunSummary:
    results: dict[str, JobResult] = field(default_factory=dict)
    failed: dict[str, Exception] = field(default_factory=dict)
    skipped: set[str] = field(default_factory=set)
    cooldown: set[str] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        # cooldown is an intentional skip, not a failure, so it does not affect ok.
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
            + (f", {len(self.cooldown)} on cooldown" if self.cooldown else "")
            + "."
        )


def _topological_sort(jobs: list[Job]) -> list[Job]:
    by_name = {j.name: j for j in jobs}
    visited: set[str] = set()
    result: list[Job] = []

    def visit(job: Job) -> None:
        if job.name in visited:
            return
        visited.add(job.name)
        for dep_name in job.depends_on:
            visit(by_name[dep_name])
        result.append(job)

    for job in jobs:
        visit(job)
    return result


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
    just the top-level shell."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def _command_env(
    *, extra_env: dict[str, str] | None, secret_env_names: frozenset[str]
) -> dict[str, str]:
    """The environment a job command runs in.

    Starts from the inherited environment (so the command still sees PATH etc.),
    drops the platform token variables (``secret_env_names``) so a command cannot read
    the credential repoactive uses to push/create MRs, then layers on ``extra_env``
    (e.g. REPOACTIVE_JOBS_DIR for a generator). See
    docs/adr/0006-job-commands-are-trusted.md.
    """
    env = {k: v for k, v in os.environ.items() if k not in secret_env_names}
    if extra_env:
        env.update(extra_env)
    return env


def _run_command(
    job: Job,
    cwd: Path,
    *,
    secret_env_names: frozenset[str] = frozenset(),
    extra_env: dict[str, str] | None = None,
) -> CommandResult:
    start = time.monotonic()
    # start_new_session puts the command in its own process group so a timeout
    # can kill the whole tree (see _kill_process_group).
    proc = subprocess.Popen(
        job.command,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # Decode as UTF-8 and never raise on undecodable bytes: a job command may
        # emit arbitrary output, and a decode error must not crash the run.
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
        env=_command_env(extra_env=extra_env, secret_env_names=secret_env_names),
    )

    # The blocking read below cannot be interrupted by a timeout, so a watchdog
    # thread kills the process group once the deadline passes; that closes stdout
    # and ends the read loop. The poll() guard avoids flagging a false timeout
    # when the command happens to finish just as the timer fires. A job with no
    # timeout (timeout_seconds() is None) runs without a watchdog.
    timed_out = threading.Event()

    def _on_timeout() -> None:
        if proc.poll() is None:
            timed_out.set()
            _kill_process_group(proc)

    timeout = job.timeout_seconds()
    timer = threading.Timer(timeout, _on_timeout) if timeout is not None else None
    if timer is not None:
        timer.start()

    # Stream the merged stdout/stderr line by line: keep the full output (needed
    # for the commit message and the success result) while feeding a live tail of
    # the last few lines (see repoactive.progress).
    collected: list[str] = []
    view = ProgressView(header=f"==> [{job.name}] running…", lines=progress_lines())
    try:
        assert proc.stdout is not None
        with view:
            for line in proc.stdout:
                collected.append(line)
                view.feed(line)
        proc.wait()
    finally:
        if timer is not None:
            timer.cancel()
        if proc.stdout is not None:
            proc.stdout.close()

    elapsed = time.monotonic() - start
    # On failure report the full output, not just the live tail: in a terminal the
    # live block only showed the last few lines, and piped/CI runs showed nothing,
    # so the complete output is what makes a failure diagnosable.
    detail = "".join(collected).strip()
    if timed_out.is_set():
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
    return CommandResult(output="".join(collected).strip(), elapsed=elapsed)


def _discard_empty_job(  # noqa: PLR0913
    *,
    job: Job,
    bookmark: str,
    parents: list[str],
    ws: JJ,
    command_result: CommandResult,
    bookmark_existed: bool,
) -> JobResult:
    """Clean up after a job whose command produced no diff.

    Abandons the empty change so it leaves no commit behind. If the job's
    bookmark already existed (from an earlier run that did produce a diff), it
    is now stale, so the bookmark is deleted locally and a deletion is recorded
    for the apply phase to push. Returns a JobResult with produced_diff=False,
    carrying the original parents forward so dependents still have a base.
    """
    ws.abandon()
    update: JobUpdate | None = None
    if bookmark_existed:
        ws.bookmark_delete(bookmark)
        # The remote deletion is recorded for the apply phase; whether it is
        # actually pushed is decided there (a local run skips applying).
        update = JobUpdate(
            job_name=job.name,
            title=job.title,
            push=BookmarkPush(bookmark=bookmark, delete=True),
        )
        print(f"==> [{job.name}] no changes, bookmark deleted ({command_result.elapsed:.1f}s)")
    else:
        print(f"==> [{job.name}] no changes ({command_result.elapsed:.1f}s)")
    return JobResult(
        job=job,
        effective_revsets=parents,
        produced_diff=False,
        update=update,
        command_output=command_result.output,
    )


def _boxquote(msg: str, title: str = "") -> str:
    """Render ``msg`` inside a boxquote.el-style box.

    The first line is ``,----[ title ]`` (or just ``,----`` when ``title`` is
    empty), each line of ``msg`` is prefixed with ``| ``, and the box closes
    with ``` `---- ```."""
    top = f",----[ {title} ]" if title else ",----"
    body = "\n".join(f"| {line}" for line in msg.splitlines())
    return f"{top}\n{body}\n`----"


def _build_commit_message(job: Job, command_result: CommandResult) -> str:
    """The commit message recorded for a job's change.

    The title, an optional description, the command output rendered in a
    boxquote.el-style box (when ``output_in_commit`` is set), and finally the
    ``Repoactive-Job`` trailer(s)."""
    message = f"{job.commit_title_prefix}{job.title}"
    if job.description:
        message += f"\n\n{job.description}"
    if job.output_in_commit and command_result.output:
        message += f"\n\n{_boxquote(command_result.output, title=job.command)}"
    # Trailer must be the final paragraph so jj/git recognise it as a trailer;
    # it lets later runs detect when this job last landed (see cooldown handling).
    message += "\n\n" + "\n".join(job.commit_trailers())
    return message


def _commit_job(
    *,
    job: Job,
    bookmark: str,
    ws: JJ,
    command_result: CommandResult,
) -> JobResult:
    """Commit the diff a job's command produced and stage its remote updates.

    Points the job's bookmark at the change and writes its commit message. The
    push and (when the job wants one) the MR are not carried out here; they are
    recorded on the returned JobResult for the apply phase. Returns a JobResult
    with produced_diff=True, whose effective_revsets is the bookmark so
    dependents build on this change.
    """
    stat = ws.diff_stat()
    ws.bookmark_set(bookmark)
    ws.describe(_build_commit_message(job, command_result))
    change_id = ws.change_id()

    print(f"==> [{job.name}] committed [{change_id}] ({command_result.elapsed:.1f}s)")
    if stat:
        print("\n".join(f"    {line}" for line in stat.splitlines()))
        print()

    # Record the push and (when the job wants one) the MR; both are carried out
    # later by apply_plan. The plan is built with no platform access: the target
    # branch is left unresolved when the job has no base_branch, and whether MRs
    # are actually created is decided at apply time (an MR is recorded here, but
    # apply_plan only acts on it when a platform is configured). apply_plan fills
    # in the platform default branch.
    mr: MRUpdate | None = None
    if job.create_mr is not CreateMR.never:
        mr = MRUpdate(
            source_branch=bookmark,
            target_branch=job.base_branch,
            title=f"{job.mr_title_prefix}{job.title}",
            description=job.description or "",
            command=job.command,
            command_output=command_result.output,
            labels=job.labels,
            draft=job.draft,
            depends_on=list(job.depends_on),
        )
    update = JobUpdate(
        job_name=job.name,
        title=job.title,
        push=BookmarkPush(bookmark=bookmark),
        mr=mr,
    )

    return JobResult(
        job=job,
        effective_revsets=[bookmark],
        produced_diff=True,
        update=update,
        command_output=command_result.output,
    )


def run_job(
    *,
    job: Job,
    parents: list[str],
    repo_path: Path,
    secret_env_names: frozenset[str] = frozenset(),
) -> JobResult:
    logger.debug("starting job: %s", job.model_dump_json(indent=2))
    logger.debug("parents: %s", parents)
    bookmark = job.branch_name()
    repo = JJ(repo_path)
    bookmark_existed = repo.bookmark_exists(bookmark)
    logger.debug("[%s] bookmark %s exists=%s", job.name, bookmark, bookmark_existed)

    with repo.temp_workspace(workspace_name(job.name)) as ws:
        if bookmark_existed:
            logger.debug("[%s] reusing existing bookmark, rebasing on parents", job.name)
            ws.edit(bookmark)
            ws.rebase(*parents)
            ws.restore(bookmark)
        else:
            ws.new(*parents)
        ws.git_sync_head()
        logger.debug("[%s] running command: %s", job.name, job.command)
        try:
            command_result = _run_command(job, ws.cwd, secret_env_names=secret_env_names)
        except CommandError:
            # The command timed out or failed; discard its partial change.
            ws.abandon()
            raise
        logger.debug(
            "[%s] command finished in %.3fs, %d bytes output",
            job.name,
            command_result.elapsed,
            len(command_result.output),
        )
        if ws.is_empty():
            logger.debug("[%s] working copy is empty, no diff produced", job.name)
            return _discard_empty_job(
                job=job,
                bookmark=bookmark,
                parents=parents,
                ws=ws,
                command_result=command_result,
                bookmark_existed=bookmark_existed,
            )
        return _commit_job(
            job=job,
            bookmark=bookmark,
            ws=ws,
            command_result=command_result,
        )


def _load_job_specs(jobs_dir: Path) -> dict[str, dict]:
    """Parse the ``*.toml`` fragments a generator wrote into ``jobs_dir``.

    Files are read in sorted order and their ``[job.<name>]`` tables merged by
    name (later files win), the same machinery used for the ``.repoactive.d``
    directory. Returns the raw job-spec table keyed by name, before
    inheritance/validation.
    """
    specs: dict[str, dict] = {}
    for path in expand_config_paths([jobs_dir]):
        data = tomllib.loads(path.read_text())
        specs = _merge_jobs(base=specs, override=jobs_table(data.get("job", {})))
    return specs


def run_generator(
    *, job: Job, parents: list[str], repo_path: Path, secret_env_names: frozenset[str] = frozenset()
) -> dict[str, dict]:
    """Run a generator job and return the raw job specs it emitted, keyed by name.

    The command runs in a fresh workspace on top of ``parents`` with
    ``REPOACTIVE_JOBS_DIR`` pointing at an empty directory; it writes ``*.toml``
    fragments there which are parsed once it exits. The generator produces no
    diff: any working-copy change it leaves is discarded (ADR 0004).
    """
    logger.debug("starting generator: %s", job.name)
    repo = JJ(repo_path)
    with repo.temp_workspace(workspace_name(job.name)) as ws:
        ws.new(*parents)
        ws.git_sync_head()
        # The output directory lives outside the workspace so the files written
        # there never show up as a diff in the working copy.
        jobs_dir = Path(tempfile.mkdtemp(prefix="repoactive-jobs-"))
        logger.debug("[%s] running generator command (jobs dir %s)", job.name, jobs_dir)
        try:
            try:
                _run_command(
                    job,
                    ws.cwd,
                    secret_env_names=secret_env_names,
                    extra_env={REPOACTIVE_JOBS_DIR_ENV: str(jobs_dir)},
                )
            except CommandError:
                ws.abandon()
                raise
            specs = _load_job_specs(jobs_dir)
        finally:
            shutil.rmtree(jobs_dir, ignore_errors=True)
        ws.abandon()
    logger.debug("[%s] generator emitted %d job spec(s)", job.name, len(specs))
    return specs


def _build_generated_job(*, generator: Job, name: str, spec: dict, run_names: set[str]) -> Job:
    """Build one emitted ``Job`` from its raw spec, applying inheritance.

    ``name`` is the spec's table key. The job inherits the (resolved) generator's
    tags, ``depends_on`` and the ``_INHERITED_FIELDS`` unless the spec overrides
    them, and records the generator in ``generated_by``. Raises GeneratedJobError
    on a name colliding with an existing job, a nested generator, or a job that
    fails validation."""
    if name in run_names:
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
    try:
        return Job.model_validate(merged)
    except ValidationError as e:
        raise GeneratedJobError(generator.name, f"emitted job {name!r} is invalid: {e}") from e


def _build_generated_jobs(
    *, generator: Job, specs: dict[str, dict], run_names: set[str]
) -> list[Job]:
    """Turn a generator's raw specs into validated ``Job`` objects.

    Validates each spec (see ``_build_generated_job``), that every
    ``depends_on`` target is within this run (the existing jobs or a sibling
    emitted job), and that the emitted jobs are acyclic — a cycle would
    otherwise silently mis-order the topological sort and crash the run.
    ``generator`` must be resolved (its inherited fields filled in).
    """
    emitted = [
        _build_generated_job(generator=generator, name=name, spec=spec, run_names=run_names)
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
        detect_dependency_cycle({j.name: j.depends_on for j in emitted})
    except CircularDependencyError as e:
        raise GeneratedJobError(generator.name, str(e)) from e
    return emitted


def _on_cooldown(job: Job, repo_path: Path) -> bool:
    """Whether the job landed on its base branch within its cooldown_period window."""
    delta = job.cooldown_timedelta()
    if delta is None:
        return False
    base = job.base_branch or "trunk()"
    since = datetime.now(UTC) - delta
    on_cooldown = JJ(repo_path).has_recent_job_commit(job_name=job.name, base=base, since=since)
    logger.debug(
        "[%s] cooldown check: base=%s since=%s -> on_cooldown=%s",
        job.name,
        base,
        since.isoformat(),
        on_cooldown,
    )
    return on_cooldown


def _include_dependencies(jobs: list[Job], selected: set[str]) -> None:
    """Add the transitive dependencies of every selected job to ``selected``.

    ``jobs`` must be topologically sorted; iterating in reverse propagates
    dependencies of dependencies in a single pass."""
    for j in reversed(jobs):
        if j.name in selected:
            selected.update(j.depends_on)


def _select_jobs(
    *,
    jobs: list[Job],
    requested_names: set[str],
    requested_tags: set[str] | None = None,
    refresh_names: set[str] | None = None,
) -> list[Job]:
    """Return the filtered, topologically sorted jobs to run.

    Selection is by tag. With no names and no tags this is the default run:
    every job carrying ``DEFAULT_TAG`` (see ``Job.effective_tags``), with a job
    dropped if any dependency is not itself selected. Naming jobs or passing
    tags is explicit selection: the union of the named jobs and the jobs
    matching any requested tag (``DEFAULT_TAG`` is not implied), with all
    dependencies force-included.

    ``refresh_names`` names the jobs that currently have an unmerged branch;
    they are force-included regardless of tag, along with their dependencies,
    so the default run keeps unmerged branches rebased on trunk rather than
    waiting for the job's next run."""
    requested_tags = requested_tags or set()
    refresh_names = refresh_names or set()
    jobs = _topological_sort(jobs)

    unknown = requested_names - {j.name for j in jobs}
    if unknown:
        raise UnknownJobsError(unknown)

    selected: set[str]
    if requested_names or requested_tags:
        selected = set(requested_names)
        selected.update(j.name for j in jobs if j.effective_tags() & requested_tags)
        _include_dependencies(jobs, selected)
    else:
        selected = {j.name for j in jobs if DEFAULT_TAG in j.effective_tags()}
        for j in jobs:
            if j.name in selected and any(dep not in selected for dep in j.depends_on):
                print(f"==> [{j.name}] skipped (dependency not in default run)")
                selected.remove(j.name)

    if refresh_names:
        selected.update(refresh_names & {j.name for j in jobs})
        _include_dependencies(jobs, selected)

    result = [j for j in jobs if j.name in selected]
    logger.debug(
        "selected jobs: %s (requested=%s, tags=%s, refresh=%s)",
        [j.name for j in result],
        sorted(requested_names),
        sorted(requested_tags),
        sorted(refresh_names),
    )
    return result


def _select_run_jobs(
    *,
    config: Config,
    repo: JJ,
    requested_names: list[str] | None,
    requested_tags: list[str] | None,
) -> list[Job]:
    """Pick and order the jobs to run, accounting for unmerged-branch refresh."""
    # On the bare default run, also refresh jobs with an unmerged branch so a
    # stale branch is rebased on trunk now rather than at the job's next run.
    refresh_names: set[str] = set()
    if not requested_names and not requested_tags:
        refresh_names = repo.unmerged_job_names() & {j.name for j in config.jobs}
        if refresh_names:
            print(f"==> refreshing unmerged branches: {', '.join(sorted(refresh_names))}")
        else:
            print("==> no unmerged branches to refresh")
    return _select_jobs(
        jobs=config.jobs,
        requested_names=set(requested_names or []),
        requested_tags=set(requested_tags or []),
        refresh_names=refresh_names,
    )


def _run_one_job(  # noqa: PLR0913
    *,
    job: Job,
    config: Config,
    repo_path: Path,
    summary: RunSummary,
    blocked: set[str],
    plan: UpdatePlan,
    run_names: set[str],
) -> list[Job]:
    """Run a single job, recording its outcome in ``summary``/``blocked``/``plan``.

    A failed or skipped job adds its name to ``blocked`` so its dependents are
    skipped in turn. Returns the jobs a generator (``emits_jobs``) produced — an
    empty list for an ordinary job or a generator that emitted nothing/was
    skipped. ``run_names`` is the set of job names already in this run, used to
    reject an emitted job that collides with an existing name."""
    blocking_deps = [d for d in job.depends_on if d in blocked]
    if blocking_deps:
        print(f"==> [{job.name}] skipped (dependency failed: {', '.join(blocking_deps)})")
        summary.skipped.add(job.name)
        blocked.add(job.name)
        return []

    resolved_job = job.resolve(config.job_defaults)
    parents = _compute_parents(resolved_job, summary.results)
    logger.debug("[%s] computed parents: %s", job.name, parents)
    # Checked before the emits_jobs branch on purpose: a generator on cooldown
    # emits nothing, throttling its whole fan-out as a unit (the dual trailer
    # records a recent child landing as the generator's). See
    # docs/adr/0004-job-generators.md.
    if _on_cooldown(resolved_job, repo_path):
        print(f"==> [{job.name}] on cooldown ({resolved_job.cooldown_period}), skipped")
        summary.cooldown.add(job.name)
        # Treat like a no-op run so dependents proceed on the base branch.
        summary.results[job.name] = JobResult(
            job=resolved_job, effective_revsets=parents, produced_diff=False
        )
        return []

    secret_env_names = frozenset(config.token_env_names())
    if resolved_job.emits_jobs:
        return _run_generator_job(
            job=resolved_job,
            parents=parents,
            repo_path=repo_path,
            summary=summary,
            blocked=blocked,
            run_names=run_names,
            secret_env_names=secret_env_names,
        )

    start = time.monotonic()
    try:
        result = run_job(
            job=resolved_job,
            parents=parents,
            repo_path=repo_path,
            secret_env_names=secret_env_names,
        )
        summary.results[job.name] = result
        if result.update is not None:
            plan.updates.append(result.update)
    except Exception as e:
        # A command failure reports the command's own time (matching the
        # success prints); other failures have no command time, so fall back
        # to the wall time spent in run_job.
        elapsed = e.elapsed if isinstance(e, CommandError) else time.monotonic() - start
        print(f"==> [{job.name}] failed: {e} ({elapsed:.1f}s)")
        summary.failed[job.name] = e
        blocked.add(job.name)
    return []


def _run_generator_job(  # noqa: PLR0913
    *,
    job: Job,
    parents: list[str],
    repo_path: Path,
    summary: RunSummary,
    blocked: set[str],
    run_names: set[str],
    secret_env_names: frozenset[str],
) -> list[Job]:
    """Run a generator and return its emitted jobs (resolved ``job`` required).

    The generator itself produces no diff; a no-op ``JobResult`` is recorded so
    its emitted jobs (which depend on it) compute their parents through it. A
    failure to run or to build the emitted set blocks the generator's
    dependents, exactly like an ordinary job failure."""
    start = time.monotonic()
    try:
        specs = run_generator(job=job, parents=parents, repo_path=repo_path, secret_env_names=secret_env_names)
        emitted = _build_generated_jobs(generator=job, specs=specs, run_names=run_names)
    except Exception as e:
        elapsed = e.elapsed if isinstance(e, CommandError) else time.monotonic() - start
        print(f"==> [{job.name}] failed: {e} ({elapsed:.1f}s)")
        summary.failed[job.name] = e
        blocked.add(job.name)
        return []

    summary.results[job.name] = JobResult(
        job=job, effective_revsets=parents, produced_diff=False
    )
    names = ", ".join(j.name for j in emitted) if emitted else "none"
    print(f"==> [{job.name}] generated {len(emitted)} job(s): {names}")
    return emitted


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


def _run_jobs(  # noqa: PLR0913
    *,
    ordered_jobs: list[Job],
    config: Config,
    repo: JJ,
    repo_path: Path,
    summary: RunSummary,
    plan: UpdatePlan,
) -> None:
    """Run ``ordered_jobs`` in topological order, expanding generators in place.

    ``ordered_jobs`` is topologically sorted, so the first job not yet in
    ``done`` always has its dependencies satisfied: every job ahead of it in the
    order is already done (were one not, *it* would be the first not-done job).
    A generator's emitted jobs are appended and the list re-sorted so each runs
    after its dependencies (the generator included); the next iteration picks
    them up once their turn comes. See docs/adr/0004-job-generators.md.

    Results and remote operations are recorded in ``summary``/``plan`` in place.
    """
    # Names of jobs that failed or were skipped - their dependents are blocked.
    blocked: set[str] = set()
    done: set[str] = set()
    while True:
        pending = [j for j in ordered_jobs if j.name not in done]
        if not pending:
            break
        job = pending[0]
        done.add(job.name)
        emitted = _run_one_job(
            job=job,
            config=config,
            repo_path=repo_path,
            summary=summary,
            blocked=blocked,
            plan=plan,
            run_names={j.name for j in ordered_jobs},
        )
        if emitted:
            # Track the new jobs' bookmarks so a branch an earlier run already
            # pushed is reused rather than recreated, then splice them in and
            # re-sort so they run after their dependencies.
            repo.bookmark_track(
                *sorted(j.resolve(config.job_defaults).branch_name() for j in emitted)
            )
            ordered_jobs = _topological_sort(ordered_jobs + emitted)


def _suppress_superseded_mrs(*, plan: UpdatePlan, results: dict[str, JobResult]) -> None:
    """Drop the MR of every ``create_mr = "unless-superseded"`` job whose changes
    a dependent's MR already contains.

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
                print(f"==> [{name}] MR superseded by [{covered_by[name]}]")
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
    requested_names: list[str] | None = None,
    requested_tags: list[str] | None = None,
    mode: RunMode = RunMode.local,
) -> RunSummary:
    # A publish run needs a platform to create MRs; local/push runs must not be
    # given one. The CLI keeps these in sync - this guards direct callers.
    assert (mode is RunMode.publish) == (platform is not None), (
        f"mode={mode} is inconsistent with platform={platform!r}"
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

        ordered_jobs = list(
            _select_run_jobs(
                config=config,
                repo=repo,
                requested_names=requested_names,
                requested_tags=requested_tags,
            )
        )
        summary = RunSummary()
        # Remote operations collected during the run. They are applied (in
        # topological order) once every job has run, unless this is a local-only run
        # - then the plan is built but never applied.
        plan = UpdatePlan()

        print(f"Running {len(ordered_jobs)} job(s)...")
        _run_jobs(
            ordered_jobs=ordered_jobs,
            config=config,
            repo=repo,
            repo_path=repo_path,
            summary=summary,
            plan=plan,
        )

        # Resolve "unless-superseded" now that every job has run: a job's MR is
        # dropped from the plan when a dependent's MR in this run contains it.
        _suppress_superseded_mrs(plan=plan, results=summary.results)

        # A local run stops here: the plan is built but deliberately not applied, so
        # nothing is pushed and no MR is created.
        if mode is not RunMode.local:
            applied = apply_plan(plan, repo_path=repo_path, platform=platform, mode=mode)
            for name, url in applied.mr_urls.items():
                summary.results[name].mr_url = url
            # A job whose MR failed keeps its results entry (the command ran and
            # its branch was pushed) but the run still counts as failed.
            summary.failed.update(applied.failed)

        summary.print_report()
        return summary


def _apply_plan_push(plan: UpdatePlan, *, repo_path: Path) -> None:
    """Push every bookmark recorded in the plan in a single jj call.

    A no-op when the plan records no pushes (git_push_bookmarks ignores an empty
    bookmark list)."""
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
        dep_urls = [
            (titles[dep], result.mr_urls[dep])
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
                description=build_mr_description(update.mr, dep_urls),
                labels=update.mr.labels,
                draft=update.mr.draft,
            )
            url = platform.ensure_mr(params)
        except Exception as e:
            print(f"==> [{update.job_name}] failed to create/update MR: {e}")
            result.failed[update.job_name] = e
            remaining = [u.job_name for u in pending[i + 1 :]]
            if remaining:
                print(f"==> aborting MR updates, not attempted: {', '.join(remaining)}")
            break
        result.mr_urls[update.job_name] = url
        print(f"==> [{update.job_name}] {url}")
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
