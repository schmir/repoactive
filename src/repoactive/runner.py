"""Job orchestration: select, run, commit, push, and publish MRs for each job."""

import contextlib
import logging
import os
import tempfile
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from repoactive import human_commits
from repoactive.boxquote import boxquote, strip_boxquotes
from repoactive.command import CommandError, CommandResult, run_command
from repoactive.config import (
    Config,
    CreateMR,
    Job,
)
from repoactive.constants import RA_JOBS_DIR_ENV, RA_WORKSPACE_DIR_ENV
from repoactive.generator import build_generated_jobs, load_job_specs
from repoactive.graph import topological_sort
from repoactive.jj import JJ, revset_heads, workspace_name
from repoactive.jobtree import format_job_forest, print_job_table
from repoactive.lock import run_lock
from repoactive.platforms.base import MRParams, Platform
from repoactive.progress import format_elapsed
from repoactive.selection import JobSelection, JobSelector
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
class JobRun:
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
    # Jobs a generator (emits_jobs) produced, consumed once by _dispatch_run
    # right after the job runs. Empty for an ordinary job or a generator that
    # emitted nothing.
    emitted: list[Job] = field(default_factory=list)
    # whether the branch is frozen because it has commits with conflicts (ADR 0019)
    frozen: bool = False


@dataclass
class RunSummary:
    results: dict[str, JobRun] = field(default_factory=dict)
    failed: dict[str, Exception] = field(default_factory=dict)
    dependency_failed: set[str] = field(default_factory=set)
    on_cooldown: set[str] = field(default_factory=set)
    # Successor jobs skipped because nothing below them in the stack ran this
    # run (see _dispatch_job); an intentional skip, like on_cooldown.
    successor_skipped: set[str] = field(default_factory=set)
    # Jobs whose run_only_if_changed gate fired (no watched dep produced a
    # diff); an intentional skip, like on_cooldown.
    run_only_if_changed_skipped: set[str] = field(default_factory=set)
    # Jobs frozen because rebuilding would push a conflict (ADR 0019, see
    # _run_job_frozen). Not a failure but an intentional hold, so it does not
    # affect ok.
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

    Built once in run_all and threaded through _run_jobs /
    _dispatch_job to run_job / _run_generator_job so every stage has a
    single handle to the run's config, target repo, accumulating results,
    selection, and the plan _record_job_plan fills in.
    selection is the live selection object (_run_jobs splices
    generator-emitted jobs into selection.jobs in place), so selection.jobs
    is always every job in the run.
    repo is the prepared, colocated JJ bound to repo_path.
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
        # Job.resolve_granted_secrets.
        return frozenset(self.config.token_env_names() | self.config.marked_secret_names())

    def base_env(self) -> dict[str, str]:
        """Return the process environment with every secret-bearing variable removed.

        Drops the names in stripped_env_names (the platform tokens of ADR 0006
        and every marked secret of ADR 0017), so what remains is safe to hand a
        job command as its base environment.
        """
        stripped = self.stripped_env_names
        return {k: v for k, v in os.environ.items() if k not in stripped}

    def command_env(self, job: Job, cwd: Path) -> dict[str, str]:
        """Compose the full environment for a job's command in workspace cwd.

        Layers the injected RA_* variables and the job's granted secrets over
        the secret-stripped process environment, then pins RA_WORKSPACE_DIR to
        cwd (ADR 0006/0017). A generator adds RA_JOBS_DIR on top of this.
        """
        return (
            self.base_env()
            | job.injected_env()
            | job.resolve_granted_secrets()
            | {RA_WORKSPACE_DIR_ENV: str(cwd)}
        )

    @contextlib.contextmanager
    def job_workspace(self, job: Job) -> Generator[JJ]:
        """Open a fresh temp jj workspace for a job, cleaned up on exit."""
        with JJ(self.repo_path).temp_workspace(workspace_name(job.name)) as repo:
            yield repo
        # A job's rewrite (ADR 0020) can leave the main working copy stale; reconcile it now.
        self.repo.update_stale_working_copy()

    @contextlib.contextmanager
    def generated_jobs_dir(self, generator: Job) -> "Generator[GeneratedJobsDir]":
        """Create the temp directory a generator writes its job fragments into.

        The directory lives outside the job workspace, so the fragments written
        there never show up as a working-copy diff (ADR 0004). Yields a handle
        exposing the directory (for RA_JOBS_DIR) that loads the emitted jobs
        once the generator command has run.
        """
        with tempfile.TemporaryDirectory(prefix="repoactive-jobs-") as tmp:
            yield GeneratedJobsDir(self, generator, Path(tmp))


@dataclass(frozen=True)
class GeneratedJobsDir:
    """Handle to the temp directory a generator writes its *.toml fragments into.

    Exposes the directory (for the RA_JOBS_DIR env var) and, once the generator
    command has run, loads and validates the emitted jobs from it (ADR 0004).
    """

    _ctx: RunContext
    _generator: Job
    path: Path

    def load(self) -> list[Job]:
        """Parse the fragments the generator wrote and build the emitted jobs."""
        specs = load_job_specs(self.path)
        # selection.jobs is every job already in the run (collision guard);
        # config.jobs are the statically configured names.
        return build_generated_jobs(
            generator=self._generator,
            specs=specs,
            run_names={j.name for j in self._ctx.selection.jobs},
            all_config_names={j.name for j in self._ctx.config.jobs},
            marked_secret_names=frozenset(self._ctx.config.marked_secret_names()),
        )


def _compute_parents(job: Job, results: dict[str, JobRun]) -> list[str]:
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
    in a boxquote) and the Repoactive-Job trailer(s).

    Trailers are stripped first, while they are still the final paragraph of the
    built message; strip_boxquotes reflows whitespace and could otherwise
    disturb that.

    Note: inaccurate when the commit description itself contains a boxquote
    """
    return strip_boxquotes(strip_trailers(message))


def _build_commit_message(job: Job, command_result: CommandResult) -> str:
    """Build the commit message recorded for a job's change.

    The title, an optional description, the command output rendered in a
    boxquote.el-style box (when output_in_commit is set), and finally the
    Repoactive-Job trailer(s).
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
    @-. A prerequisite/trunk *merge* conflict, by contrast, materializes in
    the command commit itself (@) and so is covered here.
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
) -> JobRun:
    """Freeze the branch: push nothing and leave the conflict for a human (ADR 0019).

    jj refuses to push a commit that contains a conflict, so a rebuild that would
    require one pushes nothing. Two checks in run_job lead here:

    - before the command, @- (a parent) already holds a conflict - a
      conflicted dependency tip or a prerequisite left conflicted below the
      command commit. The command runs on top of it and cannot resolve it, so it
      is skipped entirely.
    - after the command, this job's own pushable commits
      (_pushable_branch_revset) still hold a conflict the command did not
      resolve - the command commit's merge with the run's parents, or a fixup
      reapplied onto its regenerated output.

    Either way the prepared/rebuilt state is left in place rather than rolled
    back: nothing is pushed (_record_job_plan leaves the bookmark and any open MR
    untouched), so the remote stays at its last clean state, while the human
    running the branch sees the conflict materialized locally where they can
    resolve it. effective_revsets stays at the branch's change-id (or the
    run's parents for a branch that does not exist yet).
    """
    frozen_tip = _bookmark_change_id(prerun_branch_shape)
    print_status(job.name, ("frozen", "yellow"), f" ({detail})")
    return JobRun(
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


def _print_diff_stat(repo: JJ) -> None:
    """Print the indented diff --stat of the working-copy commit, if any."""
    if stat := repo.diff_stat():
        print("\n".join(f"    {line}" for line in stat.splitlines()))
        print()


class Disposition(StrEnum):
    """What to do with a job's rebuilt command commit (ADR 0019/0020).

    decide_disposition picks one from the branch shape and two facts about the
    regenerated commit; _run_job_build_result then carries it out (the jj
    mutation, the status line, the JobRun).
    """

    # No diff and nothing human to anchor: abandon the command commit and retire
    # the bookmark.
    retire = "retire"
    # A new branch, or one carrying only prerequisites: point the bookmark at the
    # fresh command commit.
    commit_fresh = "commit_fresh"
    # Keep the rewritten command commit under its preserved change-id; the
    # in-place rewrite already carried the bookmark along.
    commit_in_place = "commit_in_place"
    # The rewrite reproduced the pushed commit byte for byte: roll it back so the
    # bookmark does not move and jj pushes nothing (avoids a needless CI retrigger).
    idempotent_noop = "idempotent_noop"


def decide_disposition(
    shape: human_commits.BranchShape, *, commit_empty: bool, idempotent_match: bool
) -> Disposition:
    """Decide what to do with a job's rebuilt command commit (ADR 0019/0020).

    Pure: the fork that is easy to get wrong is decided from plain values, so it
    can be tested without a live jj repository. The caller gathers the two facts:
    commit_empty (the regenerated command commit adds nothing) and
    idempotent_match (a NormalLayers rewrite that reproduced the pushed commit,
    tree and stripped message alike), then applies the returned disposition.
    """
    match shape:
        case human_commits.NoBranch() | human_commits.AlreadyMerged():
            return Disposition.retire if commit_empty else Disposition.commit_fresh
        case human_commits.AllPrerequisites():
            # Only human prerequisites below: keep the branch and its fresh
            # command commit even when empty, so the prerequisites survive.
            return Disposition.commit_fresh
        case human_commits.NormalLayers():
            # An empty command commit with human commits to anchor
            # (prerequisites or fixups) is kept, not retired: abandoning it would
            # restructure the branch, turning fixups into prerequisites next run.
            if commit_empty and not shape.has_human:
                return Disposition.retire
            return Disposition.idempotent_noop if idempotent_match else Disposition.commit_in_place
        case _:
            raise NotImplementedError()


def _run_job_build_result(  # noqa: PLR0913
    *,
    repo: JJ,
    job: Job,
    parents: list[str],
    command_result: CommandResult,
    prerun_branch_shape: human_commits.BranchShape,
    restore: Callable[[], None],
) -> JobRun:
    commit_empty = repo.is_empty()
    if commit_empty:
        logger.debug("[%s] working copy is empty, no diff produced", job.name)
    elapsed = format_elapsed(command_result.elapsed)
    message = _build_commit_message(job, command_result)
    repo.describe(message)

    # Idempotency check (ADR 0020): consulted only for a NormalLayers rewrite that
    # classify_branch flagged as a no-op against what was pushed
    # (run_idempotency_check) and that is not the retire case. When the
    # regenerated tree and stripped message also match the pre-rewrite command
    # commit, the rewrite is rolled back so the command commit keeps its original
    # id (hidden, but its git object survives, so it stays diffable) instead of
    # gaining a fresh committer timestamp that would retrigger CI.
    idempotent_match = (
        isinstance(prerun_branch_shape, human_commits.NormalLayers)
        and prerun_branch_shape.run_idempotency_check
        and not (commit_empty and not prerun_branch_shape.has_human)
        and repo.same_content(prerun_branch_shape.command_commit.commit_id, "@")
        and _strip_boxquote_and_trailers(
            repo.get_description(prerun_branch_shape.command_commit.commit_id)
        )
        == _strip_boxquote_and_trailers(message)
    )

    disposition = decide_disposition(
        prerun_branch_shape, commit_empty=commit_empty, idempotent_match=idempotent_match
    )

    def build_result(*, effective_revsets: list[str], produced_diff: bool) -> JobRun:
        return JobRun(
            job=job,
            effective_revsets=effective_revsets,
            produced_diff=produced_diff,
            prerun_branch_shape=prerun_branch_shape,
            command_output=command_result.output,
        )

    match disposition:
        case Disposition.retire:
            _delete_local_bookmark(repo, job, prerun_branch_shape)
            repo.abandon()
            # A rewritten (NormalLayers) branch had a bookmark to retire; a fresh
            # one never did, so only the former mentions the deletion.
            deleted = isinstance(prerun_branch_shape, human_commits.NormalLayers)
            detail = f", bookmark deleted ({elapsed})" if deleted else f" ({elapsed})"
            print_status(job.name, ("no changes", "dim"), detail)
            return build_result(effective_revsets=parents, produced_diff=False)
        case Disposition.commit_fresh:
            repo.bookmark_set(job.branch_name(), "@")
            new_change_id = repo.change_id(revision="@")
            print_status(job.name, ("committed", "green"), f" [{new_change_id}] ({elapsed})")
            _print_diff_stat(repo)
            return build_result(effective_revsets=[new_change_id], produced_diff=True)
        case Disposition.idempotent_noop:
            tip = _bookmark_change_id(prerun_branch_shape)
            assert tip is not None
            # change_id before restore: the roll-back replaces the working copy.
            short_id = repo.change_id()
            restore()
            print_status(job.name, ("unchanged", "dim"), f" [{short_id}] ({elapsed})")
            return build_result(effective_revsets=[tip], produced_diff=True)
        case Disposition.commit_in_place:
            tip = _bookmark_change_id(prerun_branch_shape)
            assert tip is not None
            print_status(job.name, ("committed", "green"), f" [{repo.change_id()}] ({elapsed})")
            _print_diff_stat(repo)
            return build_result(effective_revsets=[tip], produced_diff=True)


def run_job(
    ctx: RunContext,
    *,
    job: Job,
    parents: list[str],
) -> JobRun:
    """Run a job's command, rewriting its command commit in place (ADR 0020).

    An existing branch's command commit (and its fixup descendants) is rebased
    onto the run's parents (prerequisites merged in), then emptied so the
    command regenerates its content directly into it, keeping the change-id so
    the bookmark and any not-selected dependents follow for free. A new job or a
    merged-and-undeleted branch gets a fresh commit on the parents instead.

    A failed command is an exact, cheap rollback: op_checkpoint restores the
    op captured before any mutation, returning the branch to precisely its prior
    state (no in-place rewrite to unwind, no fresh-then-fold dance). An empty result abandons
    the now-empty command commit and lets the plan step retire the bookmark.
    """
    logger.debug("starting job: %s", job.model_dump_json(indent=2))
    logger.debug("parents: %s", parents)

    with (
        ctx.job_workspace(job) as repo,
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
        command_result = run_command(
            job,
            repo.cwd,
            env=ctx.command_env(job, repo.cwd),
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
    """Which RunSummary set a gated (Skipped) job's name is recorded in.

    _apply_outcome matches each member to the set it names. Blocked and Failed
    outcomes name their own sets and are not SkipReasons.
    """

    run_only_if_changed_skipped = "run_only_if_changed_skipped"
    successor_skipped = "successor_skipped"
    on_cooldown = "on_cooldown"


@dataclass
class Ran:
    """A job that ran for real; result carries its diff/emitted/frozen state."""

    result: JobRun


@dataclass
class Skipped:
    """A gate declined to run the job but recorded a no-op result.

    The no-op result lets dependents proceed on the base branch; reason names
    the RunSummary set the job's name is recorded in.
    """

    result: JobRun
    reason: SkipReason


@dataclass
class Blocked:
    """A dependency failed or was itself blocked, so the job never ran.

    Records no result, so its dependents block in turn (summary.dependency_failed).
    """


@dataclass
class Failed:
    """The job raised; the exception lands in summary.failed."""

    error: Exception


# What a dispatch step decided for a job, applied to ctx in one place (ADR 0020).
# The four arms make the "carries a result vs doesn't" invariant explicit: Ran
# and Skipped hold a JobRun that flows to dependents and the plan; Blocked and
# Failed hold none. Each gate/run step builds one instead of touching ctx.summary
# directly, so _dispatch_job has a single place that applies them.
type JobOutcome = Ran | Skipped | Blocked | Failed


def _apply_outcome(ctx: RunContext, job: Job, outcome: JobOutcome) -> None:
    """Record a job's outcome in ctx.summary; the only function that does."""
    summary = ctx.summary
    match outcome:
        case Ran(result):
            summary.results[job.name] = result
            # A frozen job ran far enough to classify and prepare but not to
            # push; it is not a skip (no gate fired) and not a failure.
            if result.frozen:
                summary.frozen.add(job.name)
        case Skipped(result, reason):
            summary.results[job.name] = result
            match reason:
                case SkipReason.run_only_if_changed_skipped:
                    summary.run_only_if_changed_skipped.add(job.name)
                case SkipReason.successor_skipped:
                    summary.successor_skipped.add(job.name)
                case SkipReason.on_cooldown:
                    summary.on_cooldown.add(job.name)
        case Blocked():
            summary.dependency_failed.add(job.name)
        case Failed(error):
            summary.failed[job.name] = error


def _dispatch_job(ctx: RunContext, *, job: Job) -> JobOutcome:
    """Run a single job, recording its outcome in ctx.summary and returning it.

    A failed or dependency-skipped job lands in summary.failed/
    summary.dependency_failed, so _dispatch_blocked_deps blocks its dependents
    in turn. The returned outcome's arm (Ran/Skipped/Blocked/Failed) is what
    _record_job_plan reads directly rather than reaching back into ctx.summary.
    Which jobs bypass a skip gate is driven by ctx.selection (see JobSelection).
    """
    outcome = _dispatch_blocked_deps(ctx, job=job)
    if outcome is None:
        parents = _compute_parents(job, ctx.summary.results)
        logger.debug("[%s] computed parents: %s", job.name, parents)
        outcome = (
            _dispatch_run_only_if_changed_gate(ctx, job=job, parents=parents)
            or _dispatch_successor_gate(ctx, job=job, parents=parents)
            or _dispatch_cooldown_gate(ctx, job=job, parents=parents)
            or _dispatch_run(ctx, job=job, parents=parents)
        )
    _apply_outcome(ctx, job, outcome)
    return outcome


def _dispatch_blocked_deps(ctx: RunContext, *, job: Job) -> JobOutcome | None:
    """Skip job if any of its dependencies already failed or were skipped."""
    summary = ctx.summary
    blocking_deps = [
        d for d in job.depends_on if d in summary.dependency_failed or d in summary.failed
    ]
    if not blocking_deps:
        return None
    print_status(
        job.name, ("skipped", "yellow"), f" (dependency failed: {', '.join(blocking_deps)})"
    )
    return Blocked()


def _dispatch_run_only_if_changed_gate(
    ctx: RunContext, *, job: Job, parents: list[str]
) -> JobOutcome | None:
    """Skip job when none of its run_only_if_changed deps produced a diff.

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
    result = JobRun(job=job, effective_revsets=parents, produced_diff=False)
    return Skipped(result, SkipReason.run_only_if_changed_skipped)


def _dispatch_successor_gate(
    ctx: RunContext, *, job: Job, parents: list[str]
) -> JobOutcome | None:
    """Skip a successor job when nothing below it in the stack ran.

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
    result = JobRun(job=job, effective_revsets=parents, produced_diff=False)
    return Skipped(result, SkipReason.successor_skipped)


def _dispatch_cooldown_gate(ctx: RunContext, *, job: Job, parents: list[str]) -> JobOutcome | None:
    """Skip job if it is still within its cooldown period.

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
    result = JobRun(job=job, effective_revsets=parents, produced_diff=False)
    return Skipped(result, SkipReason.on_cooldown)


def _dispatch_run(ctx: RunContext, *, job: Job, parents: list[str]) -> JobOutcome:
    """Run job for real (ordinary command or generator), recording the outcome."""
    start = time.monotonic()

    def fail(e: Exception, elapsed: float) -> Failed:
        print_status(job.name, ("failed", "red"), f": {e} ({format_elapsed(elapsed)})")
        return Failed(e)

    try:
        return Ran(
            _run_generator_job(ctx, job=job, parents=parents)
            if job.emits_jobs
            else run_job(ctx, job=job, parents=parents)
        )
    except CommandError as e:
        # Command's own time, so the failure line matches the success prints.
        return fail(e, e.elapsed)
    except Exception as e:
        return fail(e, time.monotonic() - start)


def _run_generator_job(ctx: RunContext, *, job: Job, parents: list[str]) -> JobRun:
    """Run a generator and return a result carrying its emitted jobs (resolved job required).

    The command runs in a fresh workspace on top of parents with
    RA_JOBS_DIR pointing at an empty directory; it writes *.toml
    fragments there which are parsed once it exits. The generator itself produces
    no diff: any working-copy change it leaves is discarded (ADR 0004), and a
    no-op JobRun is recorded so its emitted jobs (which depend on it)
    compute their parents through it. A failure to run or to build the emitted
    set blocks the generator's dependents, exactly like an ordinary job failure.
    """
    logger.debug("starting generator: %s", job.name)
    with (
        ctx.job_workspace(job) as repo,
        ctx.generated_jobs_dir(job) as jobs_dir,
    ):
        repo.new(*parents)
        try:
            repo.git_sync_head()
            logger.debug("[%s] running generator command (jobs dir %s)", job.name, jobs_dir.path)
            run_command(
                job,
                repo.cwd,
                env=ctx.command_env(job, repo.cwd) | {RA_JOBS_DIR_ENV: str(jobs_dir.path)},
            )
            emitted = jobs_dir.load()
        finally:
            repo.abandon()
    logger.debug("[%s] generator emitted %d job(s)", job.name, len(emitted))

    names = ", ".join(j.name for j in emitted) if emitted else "none"
    print_status(job.name, f"generated {len(emitted)} job(s): {names}")

    return JobRun(job=job, effective_revsets=parents, produced_diff=False, emitted=emitted)


def _record_deleted_bookmark(result: JobRun, *, repo: JJ, plan: UpdatePlan) -> None:
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


def _record_job_plan(ctx: RunContext, outcome: JobOutcome) -> None:
    """Finalize a job's bookmark and record its push/MR in ctx.plan (ADR 0020).

    Decided entirely from the dispatch outcome, with no read-back into
    ctx.summary. run_job already rewrote the command commit in place (or wrote a
    fresh commit and pointed the bookmark at it) and left effective_revsets at
    the branch tip, so the bookmark is already positioned. This only:
    - Blocked/Failed: a dependency-blocked or failed job records no plan.
    - No diff produced: retires the bookmark when the command actually ran (Ran)
      and found nothing; a gate's no-op (Skipped: cooldown, successor, or gated)
      is left untouched.
    - Diff produced: appends the bookmark push and MR descriptor.
    """
    plan = ctx.plan
    repo = ctx.repo

    if isinstance(outcome, Blocked | Failed):
        logger.debug("plan: no result, skipping")
        return
    # Only Ran and Skipped carry a result (the union is closed).
    result = outcome.result

    job = result.job
    bookmark = job.branch_name()
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
        if job.create_mr is CreateMR.never:
            logger.debug("plan: [%s] frozen, no MR to label", job.name)
            return
        logger.debug("plan: [%s] frozen, labelling MR needs-rebase", job.name)
        plan.updates.append(
            JobUpdate(
                job_name=job.name,
                title=job.title,
                label_only=MRLabelUpdate(
                    source_branch=bookmark,
                    add_labels=[NEEDS_REBASE_LABEL],
                ),
            )
        )
        return

    if not result.produced_diff:
        # A gate recorded a no-op result (Skipped) so dependents proceed; its
        # bookmark must be left alone. Only a command that ran (Ran) and produced
        # nothing retires the bookmark.
        if not isinstance(outcome, Skipped):
            _record_deleted_bookmark(result, repo=repo, plan=plan)
        return

    mr: MRUpdate | None = None
    if job.create_mr is not CreateMR.never:
        mr = MRUpdate(
            source_branch=bookmark,
            target_branch=job.base_branch,
            title=f"{job.mr_title_prefix}{job.title}",
            description=job.description or "",
            command=job.command,
            command_output=result.command_output,
            labels=job.labels,
            draft=job.draft,
            auto_merge=job.auto_merge or False,
            required_approvals=job.required_approvals,
            depends_on=list(job.depends_on),
        )
    plan.updates.append(
        JobUpdate(
            job_name=job.name,
            title=job.title,
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
    """
    started: set[str] = set()
    while True:
        job = next((j for j in ctx.selection.jobs if j.name not in started), None)
        if job is None:
            break
        started.add(job.name)
        outcome = _dispatch_job(ctx, job=job)
        # run_job rewrote this job's commit in place; finalize its bookmark and push/MR now.
        _record_job_plan(ctx, outcome)
        # Only a generator that actually ran emits jobs.
        if isinstance(outcome, Ran) and (emitted := outcome.result.emitted):
            # Resolve before splicing in so _dispatch_job receives resolved jobs.
            resolved_emitted = [j.resolve(ctx.config.job_defaults) for j in emitted]
            # Track the new jobs' bookmarks so an already-pushed branch is reused, not recreated.
            ctx.repo.bookmark_track(*sorted(j.branch_name() for j in resolved_emitted))
            ctx.selection.jobs = topological_sort(ctx.selection.jobs + resolved_emitted)
            print_job_table(format_job_forest(ctx.selection.jobs), indent="  ")


def _suppress_superseded_mrs(*, plan: UpdatePlan, results: dict[str, JobRun]) -> None:
    """Drop the MR of every create_mr = "unless-superseded" job whose changes a dependent's MR already contains.

    A dependent's change is stacked on its dependencies' branches
    (_compute_parents), so a dependent's MR diff already includes this job's
    changes. results is in run order (topological), so walking it in reverse
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

    Pushes each bookmark and, in publish mode, creates/updates each MR. MRs
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
