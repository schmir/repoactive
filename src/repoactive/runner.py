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
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from repoactive import human_commits
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
from repoactive.jj import JJ, revset_heads, workspace_name
from repoactive.jobtree import format_job_forest, print_job_table
from repoactive.lock import run_lock
from repoactive.platforms.base import MRParams, Platform
from repoactive.progress import ProgressView, format_elapsed
from repoactive.selection import JobSelection, JobSelector
from repoactive.settings import load_settings
from repoactive.trailers import strip_trailers
from repoactive.ui import print_status, print_undo_hint
from repoactive.updates import (
    NEEDS_REBASE_LABEL,
    BookmarkPush,
    JobUpdate,
    MRLabelUpdate,
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

# Environment variable exposing to a job command the name of the job it belongs
# to (Job.name), so a command shared by several jobs can tell which one is
# running (e.g. to label its output).
RA_JOB_NAME_ENV = "RA_JOB_NAME"

# Environment variable exposing to a job command the branch the job's MR targets
# (Job.base_branch, or "trunk()" by default), so a command can diff against its
# target (e.g. `git diff $RA_JOB_BASE_BRANCH`). This is the *configured* base; for a
# stacked job (depends_on) the immediate parent commit is another job's output,
# not this value.
RA_JOB_BASE_BRANCH_ENV = "RA_JOB_BASE_BRANCH"

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


class MissingSecretError(RuntimeError):
    """A job granted a secret_env variable that is unset in repoactive's environment.

    Raised before the command runs so the failure is legible (ADR 0017) instead
    of surfacing as an obscure command error later.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"requires secret {name}, not set")
        self.name = name


class GeneratedJobError(ValueError):
    """Raised when a generator emits an invalid job set.

    Invalid means collision, recursion, unknown dependency, or a job that fails validation.
    """

    def __init__(self, generator: str, message: str) -> None:
        super().__init__(f"generator {generator!r}: {message}")


@dataclass
class JobResult:
    job: Job
    # Revsets a dependent should use as parents. Set directly to the branch
    # tip when the job runs: the fresh commit's change-id (produced_diff=True)
    # or the job's parent revsets (produced_diff=False); run_job already
    # rewrote the command commit in place (fixups included), so no later step
    # touches this (ADR 0020).
    effective_revsets: list[str]
    produced_diff: bool
    # human_commits.classify_branch() result (ADR 0019); None when recorded without running.
    prerun_branch_shape: human_commits.BranchShape | None = None
    # Filled in by the apply phase once the MR has been created.
    mr_url: str | None = None
    command_output: str = ""
    # Jobs a generator (``emits_jobs``) produced, consumed once by _dispatch_run
    # right after the job runs. Empty for an ordinary job or a generator that
    # emitted nothing.
    emitted: list[Job] = field(default_factory=list)
    # whether the branch is frozen because it has commits with conflicts (ADR 0019)
    frozen: bool = False


@dataclass
class RunSummary:
    results: dict[str, JobResult] = field(default_factory=dict)
    failed: dict[str, Exception] = field(default_factory=dict)
    dependency_failed: set[str] = field(default_factory=set)
    on_cooldown: set[str] = field(default_factory=set)
    # Successor jobs skipped because nothing below them in the stack ran this
    # run (see _dispatch_job). Like on_cooldown, an intentional skip: the job's
    # bookmark is left alone when its plan is recorded (_record_job_plan).
    successor_skipped: set[str] = field(default_factory=set)
    # Jobs whose run_only_if_changed gate fired (none of the watched deps
    # produced a diff). Like on_cooldown, an intentional skip: the job's
    # bookmark is left alone when its plan is recorded (_record_job_plan).
    run_only_if_changed_skipped: set[str] = field(default_factory=set)
    # Jobs frozen because rebuilding the branch would push a conflict below the
    # command commit (ADR 0019). The command did not run and nothing is pushed;
    # the remote stays at its last clean state and the conflict is left for the
    # human to resolve locally. Not a failure - like cooldown, an intentional
    # hold - so it does not affect ok.
    frozen: set[str] = field(default_factory=set)
    # Wall time of the whole run, filled in by run_all just before print_report.
    elapsed: float | None = None

    @property
    def ok(self) -> bool:
        # cooldown and successor skips are intentional, not failures, so they
        # do not affect ok.
        return not self.failed and not self.dependency_failed

    def print_report(self) -> None:
        # A name may sit in more than one bucket - cooldown jobs are also stored
        # in results (so dependents can read their effective_revsets), and a job
        # whose MR failed at apply time is in results and failed - so count the
        # union of names, not the sum of the buckets.
        total = len(self.results.keys() | self.failed.keys() | self.dependency_failed)
        produced = sum(1 for r in self.results.values() if r.produced_diff)
        print(
            f"\nDone: {produced}/{total} produced changes"
            + (f", {len(self.failed)} failed" if self.failed else "")
            + (f", {len(self.dependency_failed)} skipped" if self.dependency_failed else "")
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
            + (f", {len(self.frozen)} frozen" if self.frozen else "")
            + "."
            + (f" ({format_elapsed(self.elapsed)})" if self.elapsed is not None else "")
        )


@dataclass
class RunContext:
    """Run-wide state shared by every job in a single run_all pass.

    Built once in ``run_all`` and threaded through ``_run_jobs`` /
    ``_dispatch_job`` to ``run_job`` / ``_run_generator_job`` so every stage has a
    single handle to the run's config, target repo, accumulating results,
    selection, and the ``plan`` ``_record_job_plan`` fills in.
    ``selection`` is the live selection object (``_run_jobs`` splices
    generator-emitted jobs into ``selection.jobs`` in place), so ``selection.jobs``
    is always every job in the run.
    ``repo`` is the prepared, colocated ``JJ`` bound to ``repo_path``.
    """

    config: Config
    repo_path: Path
    repo: JJ
    summary: RunSummary
    selection: JobSelection
    # Bookmark pushes and MR descriptors, filled in by _record_job_plan (once
    # per job, right after it's dispatched) and then applied by apply_plan.
    plan: UpdatePlan = field(default_factory=UpdatePlan)

    @property
    def stripped_env_names(self) -> frozenset[str]:
        # Names removed from every job command's base environment: the platform
        # tokens (ADR 0006) plus every marked secret (ADR 0017). A job reads a
        # marked secret back only by granting it in its own secret_env; see
        # _resolve_granted_secrets.
        return frozenset(self.config.token_env_names() | self.config.marked_secret_names())


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
    *,
    extra_env: dict[str, str] | None,
    stripped_env_names: frozenset[str],
    granted_secret_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the environment a job command runs in.

    Starts from the inherited environment (so the command still sees PATH etc.),
    drops ``stripped_env_names`` (the platform tokens of ADR 0006 plus every
    marked secret of ADR 0017), injects back only the secrets this job granted
    (``granted_secret_env``), then layers on ``extra_env`` (the RA_* variables,
    e.g. RA_JOBS_DIR for a generator) last so repoactive's own variables win.
    """
    env = {k: v for k, v in os.environ.items() if k not in stripped_env_names}
    if granted_secret_env:
        env.update(granted_secret_env)
    if extra_env:
        env.update(extra_env)
    return env


def _resolve_granted_secrets(job: Job) -> dict[str, str]:
    """Values for the secrets ``job`` grants, read from repoactive's own environment.

    Only the names in the job's own ``secret_env`` are granted; ``job-defaults``
    marks names but grants to no job (ADR 0017). Raises MissingSecretError on the
    first granted name that is unset, so a misconfigured job fails legibly before
    its command runs rather than deep inside it.
    """
    granted: dict[str, str] = {}
    for name in job.secret_env:
        try:
            granted[name] = os.environ[name]
        except KeyError:
            raise MissingSecretError(name) from None
    return granted


def _job_extra_env(job: Job, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Extra environment for ``job``'s command: its name, branches, config dir, ``extra``.

    Always adds RA_JOB_NAME (the job's name), RA_JOB_BRANCH (the bookmark
    repoactive uses for the job's output), and RA_JOB_BASE_BRANCH (the branch the
    job's MR targets). Adds RA_CONFIG_SOURCE_DIR when the job has a
    ``config_source_dir`` (the directory of the config source that defined its
    command), on top of any caller-supplied entries (e.g. RA_JOBS_DIR for a
    generator).
    """
    env = dict(extra or {})
    env[RA_JOB_NAME_ENV] = job.name
    env[RA_JOB_BRANCH_ENV] = job.branch_name()
    env[RA_JOB_BASE_BRANCH_ENV] = job.base_branch or "trunk()"
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
        # Detach stdin so a command that reads it fails fast at EOF instead of hanging.
        stdin=subprocess.DEVNULL,
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
    stripped_env_names: frozenset[str] = frozenset(),
    extra_env: dict[str, str] | None = None,
) -> CommandResult:
    start = time.monotonic()
    # Fail before the command runs if a granted secret is unset (ADR 0017).
    granted_secret_env = _resolve_granted_secrets(job)
    # The workspace is always cwd, but expose it explicitly so a command that
    # cd's elsewhere can still find the workspace repoactive prepared for it.
    env = _command_env(
        extra_env={**(extra_env or {}), RA_WORKSPACE_DIR_ENV: str(cwd)},
        stripped_env_names=stripped_env_names,
        granted_secret_env=granted_secret_env,
    )

    logger.debug("[%s] running command: %s", job.name, job.command)

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
    command_result = CommandResult(output=detail, elapsed=elapsed)
    logger.debug(
        "[%s] command finished in %.3fs, %d bytes output",
        job.name,
        command_result.elapsed,
        len(command_result.output),
    )
    return command_result


def _bookmark_change_id(shape: human_commits.BranchShape | None) -> str | None:
    """Return the bookmark's change-id from a classify_branch() result."""
    match shape:
        case None | human_commits.NoBranch():
            return None
        case _:
            return shape.bookmark_change_id


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


def _run_job_prepare_command_commit(
    *, repo: JJ, job: Job, parents: list[str]
) -> human_commits.BranchShape:
    """Get the repo into a state where we can run the job's command."""
    bookmark = job.branch_name()
    shape = human_commits.classify_branch(
        repo=repo, parents=parents, bookmark=bookmark, job_name=job.name
    )
    logger.debug("[%s] bookmark %s shape=%s", job.name, bookmark, shape)

    match shape:
        case human_commits.NoBranch() | human_commits.AlreadyMerged():
            repo.new(revset_heads(parents))
        case human_commits.NormalLayers():
            # rewrite the command commit in place
            anchor = shape.command_commit.change_id
            repo.rebase_source(anchor, revset_heads(shape.prereq_heads + parents))
            repo.edit(anchor)
            repo.restore_working_copy()
        case human_commits.AllPrerequisites():
            repo.new(revset_heads(shape.prereq_heads + parents))
        case human_commits.UnexpectedLayers():
            raise NotImplementedError()

    repo.git_sync_head()
    return shape


def _pushable_branch_revset(shape: human_commits.BranchShape) -> str:
    """Revset for this job's own commits that a push would export.

    The command commit and, for a rewritten branch (NormalLayers), the fixups
    reapplied above it - bounded by the branch tip so a stacked dependent's
    commits (which sit *above* the tip) are excluded. Used for the post-command
    conflict check: a conflict here is one the command failed to resolve (its own
    merge with the run's parents, or a fixup that no longer applies).

    Prerequisites below the command commit are intentionally excluded; an
    already-conflicted parent is caught before the command runs by checking
    ``@-``. A prerequisite/trunk *merge* conflict, by contrast, materializes in
    the command commit itself (``@``) and so is covered here.
    """
    match shape:
        case human_commits.NormalLayers():
            return f"@::{shape.bookmark_change_id}"
        case _:
            return "@"


def _run_job_frozen(
    *,
    job: Job,
    parents: list[str],
    prerun_branch_shape: human_commits.BranchShape,
    detail: str,
) -> JobResult:
    """Freeze the branch: push nothing and leave the conflict for a human (ADR 0019).

    jj refuses to push a commit that contains a conflict, so a rebuild that would
    require one pushes nothing. Two checks in run_job lead here:

    - before the command, ``@-`` (a parent) already holds a conflict - a
      conflicted dependency tip or a prerequisite left conflicted below the
      command commit. The command runs on top of it and cannot resolve it, so it
      is skipped entirely.
    - after the command, this job's own pushable commits
      (:func:`_pushable_branch_revset`) still hold a conflict the command did not
      resolve - the command commit's merge with the run's parents, or a fixup
      reapplied onto its regenerated output.

    Either way the prepared/rebuilt state is left in place rather than rolled
    back: nothing is pushed (_record_job_plan leaves the bookmark and any open MR
    untouched), so the remote stays at its last clean state, while the human
    running the branch sees the conflict materialized locally where they can
    resolve it. ``effective_revsets`` stays at the branch's change-id (or the
    run's parents for a branch that does not exist yet).
    """
    frozen_tip = _bookmark_change_id(prerun_branch_shape)
    print_status(job.name, ("frozen", "yellow"), f" ({detail})")
    return JobResult(
        job=job,
        effective_revsets=[frozen_tip] if frozen_tip else parents,
        produced_diff=False,
        frozen=True,
        prerun_branch_shape=prerun_branch_shape,
    )


def _delete_local_bookmark(
    repo: JJ, job: Job, prerun_branch_shape: human_commits.BranchShape
) -> None:
    """Delete the branch's local bookmark after a no-diff run, if it existed.

    A fresh no-diff job never had a bookmark, so there is nothing to delete.
    run_job calls this in its temp workspace right after the command runs; the
    deletion persists to the shared repo, and _record_deleted_bookmark then
    schedules the matching remote-delete push (ADR 0020).
    """
    if _bookmark_change_id(prerun_branch_shape) is not None:
        repo.bookmark_delete(job.branch_name())


def _run_job_build_result(  # noqa: PLR0913
    *,
    repo: JJ,
    job: Job,
    parents: list[str],
    command_result: CommandResult,
    prerun_branch_shape: human_commits.BranchShape,
    restore: Callable[[], None],
) -> JobResult:
    commit_empty = repo.is_empty()
    if commit_empty:
        logger.debug("[%s] working copy is empty, no diff produced", job.name)
    elapsed = format_elapsed(command_result.elapsed)
    message = _build_commit_message(job, command_result)
    repo.describe(message)

    match prerun_branch_shape:
        case human_commits.NoBranch() | human_commits.AlreadyMerged() if commit_empty:
            print_status(job.name, ("no changes", "dim"), f" ({elapsed})")
            _delete_local_bookmark(repo, job, prerun_branch_shape)
            repo.abandon()
            return JobResult(
                job=job,
                effective_revsets=parents,
                produced_diff=False,
                prerun_branch_shape=prerun_branch_shape,
                command_output=command_result.output,
            )
        case (
            human_commits.NoBranch()
            | human_commits.AlreadyMerged()
            | human_commits.AllPrerequisites()
        ):
            repo.bookmark_set(job.branch_name(), "@")
            new_change_id = repo.change_id(revision="@")
            print_status(
                job.name,
                ("committed", "green"),
                f" [{new_change_id}] ({elapsed})",
            )
            if stat := repo.diff_stat():
                print("\n".join(f"    {line}" for line in stat.splitlines()))
                print()
            return JobResult(
                job=job,
                effective_revsets=[new_change_id],
                produced_diff=True,
                prerun_branch_shape=prerun_branch_shape,
                command_output=command_result.output,
            )

        case human_commits.NormalLayers() if commit_empty and not prerun_branch_shape.has_human:
            # No diff and no human commits to anchor: abandon the empty command
            # commit and delete its bookmark, then report no-diff. The plan step
            # records the matching remote deletion. A branch that *does* carry
            # human commits (prerequisites or fixups) keeps its empty command
            # commit instead: abandoning it would restructure the branch,
            # turning fixups into prerequisites on the next run, so it falls
            # through to the NormalLayers arm below.
            _delete_local_bookmark(repo, job, prerun_branch_shape)
            repo.abandon()
            print_status(job.name, ("no changes", "dim"), f", bookmark deleted ({elapsed})")
            return JobResult(
                job=job,
                effective_revsets=parents,
                produced_diff=False,
                prerun_branch_shape=prerun_branch_shape,
                command_output=command_result.output,
            )
        case human_commits.NormalLayers():
            # Non-empty, or empty but anchoring human commits (see the guarded
            # case above): record the message on the rewritten command commit and
            # keep it. The in-place rewrite already carried the bookmark tip along
            # with the preserved change-id, so no bookmark set is needed here.
            tip = _bookmark_change_id(prerun_branch_shape)
            assert tip is not None

            # Idempotency skip (ADR 0020): when classify_branch found the rewrite
            # would neither move the command commit nor diverge it from what was
            # pushed (run_idempotency_check), and the regenerated tree and stripped
            # message match the pre-rewrite command commit, roll back the rewrite.
            # The command commit keeps its original id (so the bookmark does not
            # move and jj pushes nothing) instead of being rewritten with a fresh
            # committer timestamp that would retrigger CI. The old commit is hidden
            # but its git object survives the rewrite, so it is still diffable.
            if (
                prerun_branch_shape.run_idempotency_check
                and repo.same_content(prerun_branch_shape.command_commit.commit_id, "@")
                and _strip_boxquote_and_trailers(
                    repo.get_description(prerun_branch_shape.command_commit.commit_id)
                )
                == _strip_boxquote_and_trailers(message)
            ):
                short_id = repo.change_id()
                restore()
                print_status(job.name, ("unchanged", "dim"), f" [{short_id}] ({elapsed})")
                return JobResult(
                    job=job,
                    effective_revsets=[tip],
                    produced_diff=True,
                    prerun_branch_shape=prerun_branch_shape,
                    command_output=command_result.output,
                )

            print_status(
                job.name,
                ("committed", "green"),
                f" [{repo.change_id()}] ({elapsed})",
            )
            if stat := repo.diff_stat():
                print("\n".join(f"    {line}" for line in stat.splitlines()))
                print()

            return JobResult(
                job=job,
                effective_revsets=[tip],
                produced_diff=True,
                prerun_branch_shape=prerun_branch_shape,
                command_output=command_result.output,
            )
        case _:
            raise NotImplementedError()


def run_job(
    ctx: RunContext,
    *,
    job: Job,
    parents: list[str],
) -> JobResult:
    """Run a job's command, rewriting its command commit in place (ADR 0020).

    An existing branch's command commit (and its fixup descendants) is rebased
    onto the run's parents (prerequisites merged in), then emptied so the
    command regenerates its content directly into it, keeping the change-id so
    the bookmark and any not-selected dependents follow for free. A new job or a
    merged-and-undeleted branch gets a fresh commit on the parents instead.

    A failed command is an exact, cheap rollback: ``op_checkpoint`` restores the
    op captured before any mutation, returning the branch to precisely its prior
    state (no in-place rewrite to unwind, no fresh-then-fold dance). An empty result abandons
    the now-empty command commit and lets the plan step retire the bookmark.
    """
    logger.debug("starting job: %s", job.model_dump_json(indent=2))
    logger.debug("parents: %s", parents)

    with (
        JJ(ctx.repo_path).temp_workspace(workspace_name(job.name)) as repo,
        repo.op_checkpoint() as restore,
    ):
        shape = _run_job_prepare_command_commit(repo=repo, job=job, parents=parents)
        # A conflict already present in a parent (@-) - a conflicted dependency
        # tip, or a prerequisite left conflicted below the command commit -
        # cannot be resolved by the command, which only regenerates the command
        # commit's own content. Stop before running it.
        if repo.has_conflict("@-"):
            return _run_job_frozen(
                job=job,
                parents=parents,
                prerun_branch_shape=shape,
                detail="conflict below command commit, needs rebase",
            )
        command_result = _run_command(
            job,
            repo.cwd,
            stripped_env_names=ctx.stripped_env_names,
            extra_env=_job_extra_env(job),
        )
        # A conflict materialized by the rebuild itself - the command commit's
        # merge with the run's parents (a prerequisite/trunk merge lands here, in
        # @, not in @-), or a fixup reapplied onto regenerated output - is left to
        # the command, which may clear it by rewriting @. Re-check afterwards:
        # jj refuses to push a still-conflicted commit, so freeze if one remains.
        if repo.has_conflict(_pushable_branch_revset(shape)):
            return _run_job_frozen(
                job=job,
                parents=parents,
                prerun_branch_shape=shape,
                detail="conflict in rebuilt branch, needs rebase",
            )
        return _run_job_build_result(
            repo=repo,
            job=job,
            parents=parents,
            prerun_branch_shape=shape,
            command_result=command_result,
            restore=restore,
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


def _build_generated_job(  # noqa: PLR0913
    *,
    generator: Job,
    name: str,
    spec: dict,
    run_names: set[str],
    all_config_names: set[str],
    marked_secret_names: frozenset[str],
) -> Job:
    """Build one emitted ``Job`` from its raw spec, applying inheritance.

    ``name`` is the spec's table key. The job inherits the (resolved) generator's
    tags, ``depends_on`` and the ``_INHERITED_FIELDS`` unless the spec overrides
    them, and records the generator in ``generated_by``. Raises GeneratedJobError
    on a name colliding with an existing job, a nested generator, a job that
    fails validation, or a ``secret_env`` naming a secret the static config did
    not already mark (``marked_secret_names``).
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


def _build_generated_jobs(
    *,
    generator: Job,
    specs: dict[str, dict],
    run_names: set[str],
    all_config_names: set[str],
    marked_secret_names: frozenset[str] = frozenset(),
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


def _last_run_if_on_cooldown(job: Job, repo_path: Path) -> datetime | None:
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


class SkipReason(StrEnum):
    """Which ``RunSummary`` set a skipped job's name is recorded in.

    ``_apply_outcome`` matches each member to the set it names.
    """

    dependency_failed = "dependency_failed"
    run_only_if_changed_skipped = "run_only_if_changed_skipped"
    successor_skipped = "successor_skipped"
    on_cooldown = "on_cooldown"


@dataclass
class DispatchOutcome:
    """What a dispatch step decided for a job; applied to ``ctx`` in one place.

    ``skip_reason`` names the ``RunSummary`` set the job's name is recorded in
    — ``None`` when the job actually ran. Each gate/run step builds one of
    these instead of touching ``ctx.summary`` directly, so ``_dispatch_job``
    has a single place that applies them. A plain ``DispatchOutcome`` instance
    is always truthy (unlike the ``list[Job]`` it ultimately yields), so gates
    can chain with ``or`` and still let an empty ``emitted`` list through.
    """

    result: JobResult | None = None
    skip_reason: SkipReason | None = None
    failed: Exception | None = None

    @property
    def emitted(self) -> list[Job]:
        return self.result.emitted if self.result is not None else []


def _apply_outcome(ctx: RunContext, job: Job, outcome: DispatchOutcome) -> None:
    """Record a job's dispatch outcome in ``ctx``; the only function that does."""
    summary = ctx.summary
    if outcome.result is not None:
        summary.results[job.name] = outcome.result
        # A frozen job ran far enough to classify and prepare but not to push;
        # it is not a skip_reason (the gates did not fire) and not a failure.
        if outcome.result.frozen:
            summary.frozen.add(job.name)
    match outcome.skip_reason:
        case SkipReason.dependency_failed:
            summary.dependency_failed.add(job.name)
        case SkipReason.run_only_if_changed_skipped:
            summary.run_only_if_changed_skipped.add(job.name)
        case SkipReason.successor_skipped:
            summary.successor_skipped.add(job.name)
        case SkipReason.on_cooldown:
            summary.on_cooldown.add(job.name)
        case None:
            pass
    if outcome.failed is not None:
        summary.failed[job.name] = outcome.failed


def _dispatch_job(ctx: RunContext, *, job: Job) -> list[Job]:
    """Run a single job, recording its outcome in ``ctx.summary``.

    A failed or dependency-skipped job lands in ``summary.failed``/
    ``summary.dependency_failed``, so ``_dispatch_blocked_deps`` blocks its
    dependents in turn. Returns the jobs a generator (``emits_jobs``) produced
    — an empty list for an ordinary job or a generator that emitted
    nothing/was skipped. ``ctx.selection.jobs`` is every job in this run
    (``_run_jobs`` keeps it in sync as generators emit), so its names reject an emitted job that
    collides with an existing one. ``ctx.selection.refreshed`` names the jobs that
    already have an unmerged branch; such a job is never cooldown-skipped or
    run_only_if_changed-skipped, so its branch is refreshed (ADR 0003).
    ``ctx.selection.successors`` names the jobs force-included because their
    commits sit above a selected job's bookmark; they bypass their own cooldown
    but are skipped when every dependency was itself skipped this run.
    ``ctx.selection.explicit`` names the jobs requested by name on the command
    line; naming a job runs it now, so it too bypasses the cooldown skip. The
    plan is built by _record_job_plan, not here.
    """
    outcome = _dispatch_blocked_deps(ctx, job)
    if outcome is None:
        parents = _compute_parents(job, ctx.summary.results)
        logger.debug("[%s] computed parents: %s", job.name, parents)
        outcome = (
            _dispatch_run_only_if_changed_gate(ctx, job, parents)
            or _dispatch_successor_gate(ctx, job, parents)
            or _dispatch_cooldown_gate(ctx, job, parents)
            or _dispatch_run(ctx, job, parents)
        )
    _apply_outcome(ctx, job, outcome)
    return outcome.emitted


def _dispatch_blocked_deps(ctx: RunContext, job: Job) -> DispatchOutcome | None:
    """Skip ``job`` if any of its dependencies already failed or were skipped."""
    summary = ctx.summary
    blocking_deps = [
        d for d in job.depends_on if d in summary.dependency_failed or d in summary.failed
    ]
    if not blocking_deps:
        return None
    print_status(
        job.name, ("skipped", "yellow"), f" (dependency failed: {', '.join(blocking_deps)})"
    )
    return DispatchOutcome(skip_reason=SkipReason.dependency_failed)


def _dispatch_run_only_if_changed_gate(
    ctx: RunContext, job: Job, parents: list[str]
) -> DispatchOutcome | None:
    """Skip ``job`` when none of its ``run_only_if_changed`` deps produced a diff.

    run_only_if_changed gates jobs whose effect is conditional on upstream
    diffs. A refreshed job bypasses the gate for the same reason it bypasses
    cooldown: it has an open branch that must be rebased (ADR 0003), and
    skipping it here would leave the branch un-rebased and orphan its MR.
    """
    summary = ctx.summary
    if not job.run_only_if_changed or job.name in ctx.selection.refreshed:
        return None
    any_changed = any(
        r.produced_diff
        for d in job.run_only_if_changed
        if (r := summary.results.get(d)) is not None
    )
    if any_changed:
        return None
    print_status(
        job.name,
        ("skipped", "yellow"),
        f" (run_only_if_changed: none of {job.run_only_if_changed} produced changes)",
    )
    result = JobResult(job=job, effective_revsets=parents, produced_diff=False)
    return DispatchOutcome(result=result, skip_reason=SkipReason.run_only_if_changed_skipped)


def _dispatch_successor_gate(
    ctx: RunContext, job: Job, parents: list[str]
) -> DispatchOutcome | None:
    """Skip a successor ``job`` when nothing below it in the stack ran.

    A successor exists to be rebuilt when the stack below it moves. If every
    dependency was itself skipped this run (cooldown or an earlier successor
    skip), nothing it builds on changed, so re-running it would reproduce the
    same result; record a no-op so its own successors skip too and
    _record_job_plan leaves its bookmark alone. Judged on depends_on, not the commit
    graph: a successor whose config no longer declares the dependency it is
    stacked on falls through and runs — the safe direction.
    """
    summary = ctx.summary
    not_run = summary.on_cooldown | summary.successor_skipped
    if not (
        job.name in ctx.selection.successors
        and job.depends_on
        and all(dep in not_run for dep in job.depends_on)
    ):
        return None
    print_status(job.name, ("skipped", "yellow"), " (successor: no dependency ran)")
    result = JobResult(job=job, effective_revsets=parents, produced_diff=False)
    return DispatchOutcome(result=result, skip_reason=SkipReason.successor_skipped)


def _dispatch_cooldown_gate(
    ctx: RunContext, job: Job, parents: list[str]
) -> DispatchOutcome | None:
    """Skip ``job`` if it is still within its cooldown period.

    Cooldown only throttles *starting fresh work*. A job that already has an
    open (unmerged) branch must always run so it is rebased on the latest trunk
    and, when its change is now redundant, produces an empty diff that
    self-closes the MR via the empty-diff path; skipping it here would leave the
    branch un-rebased and orphan its MR, defeating the refresh guarantee of
    ADR 0003. A successor bypasses cooldown for the same reason: its base just
    moved, so it must rebuild regardless of when it last landed. A job named
    explicitly on the command line also bypasses cooldown: naming it is a
    request to run it now, so its cooldown_period is ignored for this run.

    Checked before the emits_jobs branch on purpose: a generator on cooldown
    emits nothing, throttling its whole fan-out as a unit (the dual trailer
    records a recent child landing as the generator's); a generator has no
    branch, so it is never in selection.refreshed. See docs/adr/0004-job-generators.md.
    """
    selection = ctx.selection
    if (
        job.name in selection.refreshed
        or job.name in selection.successors
        or job.name in selection.explicit
        or not (last_run := _last_run_if_on_cooldown(job, ctx.repo_path))
    ):
        return None
    elapsed = datetime.now(UTC) - last_run
    elapsed_str = _format_duration(elapsed.total_seconds())
    print_status(
        job.name,
        ("on cooldown", "yellow"),
        f" ({job.cooldown_period}), last run {elapsed_str} ago, skipped",
    )
    # Treat like a no-op run so dependents proceed on the base branch.
    result = JobResult(job=job, effective_revsets=parents, produced_diff=False)
    return DispatchOutcome(result=result, skip_reason=SkipReason.on_cooldown)


def _dispatch_run(ctx: RunContext, job: Job, parents: list[str]) -> DispatchOutcome:
    """Run ``job`` for real (ordinary command or generator), recording the outcome."""
    start = time.monotonic()
    try:
        result = (
            _run_generator_job(ctx, job=job, parents=parents)
            if job.emits_jobs
            else run_job(ctx, job=job, parents=parents)
        )
        return DispatchOutcome(result=result)
    except Exception as e:
        # A command failure reports the command's own time (matching the
        # success prints); other failures have no command time, so fall back
        # to the wall time spent in run_job.
        elapsed = e.elapsed if isinstance(e, CommandError) else time.monotonic() - start
        print_status(job.name, ("failed", "red"), f": {e} ({format_elapsed(elapsed)})")
        return DispatchOutcome(failed=e)


def _run_generator_job(ctx: RunContext, *, job: Job, parents: list[str]) -> JobResult:
    """Run a generator and return a result carrying its emitted jobs (resolved ``job`` required).

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
                stripped_env_names=ctx.stripped_env_names,
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
        marked_secret_names=frozenset(ctx.config.marked_secret_names()),
    )

    names = ", ".join(j.name for j in emitted) if emitted else "none"
    print_status(job.name, f"generated {len(emitted)} job(s): {names}")

    return JobResult(job=job, effective_revsets=parents, produced_diff=False, emitted=emitted)


def _record_deleted_bookmark(result: JobResult, *, repo: JJ, plan: UpdatePlan) -> None:
    """Schedule a remote delete push for a bookmark run_job retired.

    run_job already deleted the local bookmark after the command produced no
    diff (see _delete_local_bookmark). Schedule the push when either the
    bookmark existed before this run, or the remote still has it from a previous
    push (e.g. after a -mlocal run that deleted the local bookmark without
    applying the plan).
    """
    bookmark = result.job.branch_name()
    bookmark_existed_prerun = _bookmark_change_id(result.prerun_branch_shape) is not None
    if bookmark_existed_prerun or repo.remote_bookmark_exists(bookmark):
        plan.updates.append(
            JobUpdate(
                job_name=result.job.name,
                title=result.job.title,
                push=BookmarkPush(bookmark=bookmark, delete=True),
            )
        )


def _record_job_plan(ctx: RunContext, job: Job) -> None:
    """Finalize a job's bookmark and record its push/MR in ``ctx.plan`` (ADR 0020).

    ``run_job`` already rewrote the command commit in place (or wrote a fresh
    commit and pointed the bookmark at it) and left ``result.effective_revsets``
    at the branch tip, so the bookmark is already positioned. This only:
    - No diff produced: deletes the old bookmark if the command ran and found
      nothing (cooldown/successor/gated skips are left untouched) and records a
      remote deletion.
    - Diff produced: appends the bookmark push and MR descriptor.
    """
    summary = ctx.summary
    plan = ctx.plan
    repo = ctx.repo

    result = summary.results.get(job.name)
    if result is None:
        logger.debug("plan: [%s] no result, skipping", job.name)
        return

    bookmark = result.job.branch_name()
    logger.debug(
        "plan: [%s] produced_diff=%s prerun_branch_shape=%s",
        job.name,
        result.produced_diff,
        result.prerun_branch_shape,
    )

    # A frozen branch pushes nothing: the bookmark stays at its last clean state
    # and the MR's content is left exactly as it was (ADR 0019). This is distinct
    # from the no-diff path below, which retires the bookmark. The only remote
    # change is the repoactive:needs-rebase label added to an already-open MR so
    # the human sees the branch needs attention; a job that never opens MRs has
    # nothing to label.
    if result.frozen:
        if result.job.create_mr is CreateMR.never:
            logger.debug("plan: [%s] frozen, no MR to label", job.name)
            return
        logger.debug("plan: [%s] frozen, labelling MR needs-rebase", job.name)
        plan.updates.append(
            JobUpdate(
                job_name=job.name,
                title=result.job.title,
                label_only=MRLabelUpdate(
                    source_branch=bookmark,
                    add_labels=[NEEDS_REBASE_LABEL],
                ),
            )
        )
        return

    if not result.produced_diff:
        if job.name not in (
            summary.on_cooldown | summary.successor_skipped | summary.run_only_if_changed_skipped
        ):
            _record_deleted_bookmark(result, repo=repo, plan=plan)
        return

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
    """Run each job in topological order and record its plan before the next dispatches.

    ctx.selection.jobs is topologically sorted, so each job's dependencies
    have already run by the time it dispatches. run_job rewrites each job's
    command commit in place before it returns (ADR 0020), so a stacked
    dependent's _compute_parents forks from its dependency's canonical,
    fixup-included tip, with no separate fold-in step. A generator's emitted jobs
    are spliced in and the list re-sorted so each runs after its dependencies
    (the generator included). See docs/adr/0004-job-generators.md.

    Results are recorded in ctx.summary in place and ctx.plan accumulates
    each job's push/MR as its plan is recorded.

    ctx.selection.refreshed names the jobs being refreshed because they
    already have an unmerged branch; they bypass the cooldown skip so their
    branches are rebased (ADR 0003). Empty for explicit selection, which does not
    refresh (ADR 0003). ctx.selection.successors names the jobs force-included
    because their commits sit above a selected job's bookmark; they run only when
    something below them in the stack ran (see _dispatch_job).
    """
    started: set[str] = set()
    while True:
        job = next((j for j in ctx.selection.jobs if j.name not in started), None)
        if job is None:
            break
        started.add(job.name)
        emitted = _dispatch_job(ctx, job=job)
        # run_job's rewrite (ADR 0020) can leave this workspace stale; reconcile before commands.
        ctx.repo.update_stale_working_copy()
        # run_job rewrote this job's commit in place; finalize its bookmark and push/MR now.
        _record_job_plan(ctx, job)
        if emitted:
            # Resolve before splicing in so _dispatch_job receives resolved jobs.
            resolved_emitted = [j.resolve(ctx.config.job_defaults) for j in emitted]
            # Track the new jobs' bookmarks so an already-pushed branch is reused, not recreated.
            ctx.repo.bookmark_track(*sorted(j.branch_name() for j in resolved_emitted))
            ctx.selection.jobs = topological_sort(ctx.selection.jobs + resolved_emitted)
            print_job_table(format_job_forest(ctx.selection.jobs), indent="  ")


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
    # Only publish needs a platform (for MRs); guards direct callers, the CLI keeps these aligned.
    assert (mode is RunMode.publish) == (platform is not None), (
        f"mode={mode} is inconsistent with platform={platform!r}"
    )
    run_start = time.monotonic()
    # Building the selector may fail on a bad job name or tag, so do it before touching the repo.
    selector = JobSelector(
        config=config, requested_names=requested_names, requested_tags=requested_tags
    )
    # Serialise runs: _prepare_repo's forget_stale_workspaces would clobber a concurrent run.
    with run_lock(repo_path), _prepare_repo(config=config, repo_path=repo_path) as repo:
        logger.debug(
            "run_all: repo=%s mode=%s requested_names=%s requested_tags=%s",
            repo_path,
            mode,
            requested_names,
            requested_tags,
        )

        ctx = RunContext(
            config=config,
            repo_path=repo_path,
            repo=repo,
            summary=RunSummary(),
            selection=selector.select_run_jobs(repo),
        )

        # Print 'info jobs'-like overview of selected jobs.
        print(f"Running {len(ctx.selection.jobs)} job(s):")
        print_job_table(format_job_forest(ctx.selection.jobs), indent="  ")
        print()

        _run_jobs(ctx)

        # Resolve "unless-superseded"
        _suppress_superseded_mrs(plan=ctx.plan, results=ctx.summary.results)

        if mode is not RunMode.local:
            applied = apply_plan(ctx.plan, repo_path=repo_path, platform=platform, mode=mode)
            for name, url in applied.mr_urls.items():
                ctx.summary.results[name].mr_url = url
            # A job whose MR failed keeps its results entry (the command ran and
            # its branch was pushed) but the run still counts as failed.
            ctx.summary.failed.update(applied.failed)

        ctx.summary.elapsed = time.monotonic() - run_start
        ctx.summary.print_report()
        return ctx.summary


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
        u
        for u in plan.updates
        if not (u.push is not None and u.push.delete)
        and (u.mr is not None or u.label_only is not None)
    ]
    for i, update in enumerate(pending):
        # Fail fast: a failing platform call usually means something is wrong
        # (expired token, rate limit), so the remaining MRs are not attempted
        # rather than hammered against the same failure. Nothing is lost - the
        # bookmarks are already pushed, ensure_mr is idempotent, and the next
        # run re-attempts every MR. The failure is recorded per job and
        # surfaces in the run summary.
        try:
            if update.label_only is not None:
                # Frozen branch (ADR 0019): only label the already-open MR, if
                # any. A no-op returning None when no MR is open - nothing was
                # ever pushed, so there is nothing to signal on.
                url = platform.add_mr_labels(
                    update.label_only.source_branch, update.label_only.add_labels
                )
                if url is None:
                    continue
                result.mr_urls[update.job_name] = url
                print_status(update.job_name, ("needs-rebase", "yellow"), f" {url}")
                continue

            assert update.mr is not None  # filtered above
            dependency_links = [
                MRLink(title=titles[dep], url=result.mr_urls[dep])
                for dep in update.mr.depends_on
                if dep in result.mr_urls
            ]
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
