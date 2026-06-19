import contextlib
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from repoactive.config import DEFAULT_TAG, Config, Job
from repoactive.jj import JJ, JOB_TRAILER_KEY, JJError, workspace_name
from repoactive.platforms.base import MRParams, Platform

logger = logging.getLogger(__name__)


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


@dataclass
class JobResult:
    job: Job
    # Revsets dependents should use as parents. Either the bookmark name (if
    # the command produced a diff) or the parent revsets the change was based
    # on (if the command produced nothing and the change was abandoned).
    effective_revsets: list[str]
    produced_output: bool
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
        # Cooldown jobs are also stored in results (so dependents can read their
        # effective_revsets), so omit len(self.cooldown) to avoid counting them twice.
        total = len(self.results) + len(self.failed) + len(self.skipped)
        produced = sum(1 for r in self.results.values() if r.produced_output)
        print(
            f"\nDone: {produced}/{total} produced output"
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
        base = job.base_branch or "trunk()"
        return [base]

    parents: list[str] = []
    seen: set[str] = set()
    for dep_name in job.depends_on:
        for revset in results[dep_name].effective_revsets:
            if revset not in seen:
                seen.add(revset)
                parents.append(revset)
    return parents


def _mr_params(
    *,
    job: Job,
    bookmark: str,
    base_branch: str,
    command_output: str = "",
    dep_mr_urls: list[tuple[str, str]] | None = None,
) -> MRParams:
    description = job.description or ""
    if dep_mr_urls:
        if description:
            description += "\n\n"
        links = "\n".join(f"- [{title}]({url})" for title, url in dep_mr_urls)
        description += f"Depends on:\n{links}"
    if command_output:
        if description:
            description += "\n\n"
        description += f"```\n$ {job.command}\n{command_output}\n```"
    return MRParams(
        source_branch=bookmark,
        target_branch=base_branch,
        title=f"{job.mr_title_prefix}{job.title}",
        description=description,
        labels=job.labels,
        draft=job.draft,
    )


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the whole process group led by ``proc``.

    The command is started with ``start_new_session=True`` so it leads its own
    process group; killing the group reaps any children the command spawned, not
    just the top-level shell."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def _run_command(job: Job, ws: JJ) -> CommandResult:
    start = time.monotonic()
    # start_new_session puts the command in its own process group so a timeout
    # can kill the whole tree (see _kill_process_group).
    proc = subprocess.Popen(
        job.command,
        shell=True,
        cwd=ws.cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # Decode as UTF-8 and never raise on undecodable bytes: a job command may
        # emit arbitrary output, and a decode error must not crash the run.
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    try:
        output, _ = proc.communicate(timeout=job.timeout_seconds())
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        # communicate again to reap the killed process and drain its output.
        output, _ = proc.communicate()
        ws.abandon()
        output = output or ""
        raise CommandError(
            f"command timed out after {job.timeout}" + (f":\n{output}" if output else ""),
            elapsed=time.monotonic() - start,
        ) from None
    if proc.returncode != 0:
        ws.abandon()
        output = output or ""
        raise CommandError(
            f"command failed with exit code {proc.returncode}"
            + (f":\n{output}" if output else ""),
            elapsed=time.monotonic() - start,
        )
    return CommandResult(output=output.strip(), elapsed=time.monotonic() - start)


def _handle_empty(  # noqa: PLR0913
    *,
    job: Job,
    bookmark: str,
    parents: list[str],
    ws: JJ,
    command_result: CommandResult,
    bookmark_existed: bool,
    local: bool = False,
) -> JobResult:
    ws.abandon()
    if bookmark_existed:
        ws.bookmark_delete(bookmark)
        if not local:
            ws.git_push_bookmarks(bookmark)
        print(f"==> [{job.name}] no changes, bookmark deleted ({command_result.elapsed:.1f}s)")
    else:
        print(f"==> [{job.name}] no changes ({command_result.elapsed:.1f}s)")
    return JobResult(
        job=job,
        effective_revsets=parents,
        produced_output=False,
        command_output=command_result.output,
    )


def _publish_job(  # noqa: PLR0913
    *,
    job: Job,
    bookmark: str,
    ws: JJ,
    platform: Platform | None,
    command_result: CommandResult,
    dep_mr_urls: list[tuple[str, str]] | None,
    local: bool = False,
) -> JobResult:
    stat = ws.diff_stat()
    ws.bookmark_set(bookmark)
    commit_message = f"{job.commit_title_prefix}{job.title}"
    if job.description:
        commit_message += f"\n\n{job.description}"
    if job.output_in_commit and command_result.output:
        indented = "\n".join(
            f"  {line}" for line in f"$ {job.command}\n{command_result.output}".splitlines()
        )
        commit_message += f"\n\n{indented}"
    # Trailer must be the final paragraph so jj/git recognise it as a trailer;
    # it lets later runs detect when this job last landed (see cooldown handling).
    commit_message += f"\n\n{JOB_TRAILER_KEY}: {job.name}"
    ws.describe(commit_message)
    change_id = ws.change_id()

    if local:
        print(
            f"==> [{job.name}] bookmark set (local) [{change_id}] ({command_result.elapsed:.1f}s)"
        )
        if stat:
            print("\n".join(f"    {line}" for line in stat.splitlines()))
            print()
        return JobResult(
            job=job,
            effective_revsets=[bookmark],
            produced_output=True,
            command_output=command_result.output,
        )

    ws.git_push_bookmarks(bookmark)
    mr_url: str | None = None
    if platform is not None and job.create_mr:
        base_branch = job.base_branch or platform.default_branch()
        params = _mr_params(
            job=job,
            bookmark=bookmark,
            base_branch=base_branch,
            command_output=command_result.output,
            dep_mr_urls=dep_mr_urls,
        )
        mr_url = platform.ensure_mr(params)
        print(f"==> [{job.name}] {mr_url} [{change_id}] ({command_result.elapsed:.1f}s)")
    else:
        print(f"==> [{job.name}] pushed [{change_id}] ({command_result.elapsed:.1f}s)")
    if stat:
        print("\n".join(f"    {line}" for line in stat.splitlines()))
        print()

    return JobResult(
        job=job,
        effective_revsets=[bookmark],
        produced_output=True,
        mr_url=mr_url,
        command_output=command_result.output,
    )


def run_job(  # noqa: PLR0913
    *,
    job: Job,
    parents: list[str],
    repo_path: Path,
    platform: Platform | None,
    dep_mr_urls: list[tuple[str, str]] | None = None,
    local: bool = False,
) -> JobResult:
    logger.debug("starting job: %s", job.model_dump_json(indent=2))
    logger.debug("parents: %s", parents)
    bookmark = job.branch_name()
    repo = JJ(repo_path)
    bookmark_existed = repo.bookmark_exists(bookmark)
    logger.debug("[%s] bookmark %s exists=%s", job.name, bookmark, bookmark_existed)

    tmp_root = Path(tempfile.mkdtemp(prefix="repoactive_"))
    workspace_path = tmp_root / "workspace"
    ws_name = workspace_name(job.name)
    logger.debug("[%s] adding workspace %s at %s", job.name, ws_name, workspace_path)
    repo.workspace_add(ws_name, workspace_path)
    ws = JJ(workspace_path)
    try:
        if bookmark_existed:
            logger.debug("[%s] reusing existing bookmark, rebasing on parents", job.name)
            ws.edit(bookmark)
            ws.rebase(*parents)
            ws.restore(bookmark)
        else:
            ws.new(*parents)
        ws.git_sync_head()
        logger.debug("[%s] running command: %s", job.name, job.command)
        command_result = _run_command(job, ws)
        logger.debug(
            "[%s] command finished in %.3fs, %d bytes output",
            job.name,
            command_result.elapsed,
            len(command_result.output),
        )
        if ws.is_empty():
            logger.debug("[%s] working copy is empty, no diff produced", job.name)
            return _handle_empty(
                job=job,
                bookmark=bookmark,
                parents=parents,
                ws=ws,
                command_result=command_result,
                bookmark_existed=bookmark_existed,
                local=local,
            )
        return _publish_job(
            job=job,
            bookmark=bookmark,
            ws=ws,
            platform=platform,
            command_result=command_result,
            dep_mr_urls=dep_mr_urls,
            local=local,
        )
    finally:
        logger.debug("[%s] cleaning up workspace %s", job.name, ws_name)
        with contextlib.suppress(JJError):
            repo.workspace_forget(ws_name)
        shutil.rmtree(tmp_root, ignore_errors=True)
        with contextlib.suppress(JJError):
            repo.git_worktree_prune()


def _on_cooldown(job: Job, repo_path: Path) -> bool:
    """Whether the job landed on its base branch within its cooldown_period window."""
    delta = job.cooldown_timedelta()
    if delta is None:
        return False
    base = job.base_branch or "trunk()"
    since = datetime.now(UTC) - delta
    on_cooldown = JJ(repo_path).has_recent_job_commit(job.name, base, since)
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
    jobs: list[Job],
    requested_jobs: set[str],
    requested_tags: set[str] | None = None,
    refresh_jobs: set[str] | None = None,
) -> list[Job]:
    """Return the filtered, topologically sorted jobs to run.

    Selection is by tag. With no names and no tags this is the default run:
    every job carrying ``DEFAULT_TAG`` (see ``Job.effective_tags``), with a job
    dropped if any dependency is not itself selected. Naming jobs or passing
    tags is explicit selection: the union of the named jobs and the jobs
    matching any requested tag (``DEFAULT_TAG`` is not implied), with all
    dependencies force-included.

    ``refresh_jobs`` (jobs that currently have an unmerged branch) are
    force-included regardless of tag, along with their dependencies, so the
    default run keeps unmerged branches rebased on trunk rather than waiting for
    the job's next run."""
    requested_tags = requested_tags or set()
    refresh_jobs = refresh_jobs or set()
    jobs = _topological_sort(jobs)

    unknown = requested_jobs - {j.name for j in jobs}
    if unknown:
        raise ValueError(f"Unknown job(s): {', '.join(sorted(unknown))}")

    selected: set[str]
    if requested_jobs or requested_tags:
        selected = set(requested_jobs)
        selected.update(j.name for j in jobs if j.effective_tags() & requested_tags)
        _include_dependencies(jobs, selected)
    else:
        selected = {j.name for j in jobs if DEFAULT_TAG in j.effective_tags()}
        for j in jobs:
            if j.name in selected and any(dep not in selected for dep in j.depends_on):
                print(f"==> [{j.name}] skipped (dependency not in default run)")
                selected.remove(j.name)

    if refresh_jobs:
        selected.update(refresh_jobs & {j.name for j in jobs})
        _include_dependencies(jobs, selected)

    result = [j for j in jobs if j.name in selected]
    logger.debug(
        "selected jobs: %s (requested=%s, tags=%s, refresh=%s)",
        [j.name for j in result],
        sorted(requested_jobs),
        sorted(requested_tags),
        sorted(refresh_jobs),
    )
    return result


def run_all(  # noqa: PLR0913
    *,
    config: Config,
    repo_path: Path,
    platform: Platform | None = None,
    requested_jobs: list[str] | None = None,
    requested_tags: list[str] | None = None,
    local: bool = False,
) -> RunSummary:
    repo = JJ(repo_path)
    op_id = repo.op_id()
    logger.debug(
        "run_all: repo=%s local=%s requested_jobs=%s requested_tags=%s op_id=%s",
        repo_path,
        local,
        requested_jobs,
        requested_tags,
        op_id,
    )
    # Drop any temporary workspaces a previous, killed run left behind before we
    # start adding fresh ones.
    repo.forget_stale_workspaces()

    # For a local run, tell the user how to roll it back. Only local state can be
    # undone this way - a pushed branch or a created MR is not - so the hint is
    # suppressed for --push/--create-prs runs. Printed again at the end since a
    # run can produce a lot of output.
    restore_hint: str | None = None
    if local:
        restore_hint = f"To undo this run, run:\n    jj op restore {op_id}"
        print(restore_hint + "\n")

    # On the bare default run, also refresh jobs with an unmerged branch so a
    # stale branch is rebased on trunk now rather than at the job's next run.
    refresh_jobs: set[str] = set()
    if not requested_jobs and not requested_tags:
        refresh_jobs = repo.unmerged_job_names() & {j.name for j in config.jobs}
        if refresh_jobs:
            print(f"==> refreshing unmerged branches: {', '.join(sorted(refresh_jobs))}")
        else:
            print("==> no unmerged branches to refresh")
    ordered_jobs = _select_jobs(
        config.jobs,
        set(requested_jobs or []),
        set(requested_tags or []),
        refresh_jobs,
    )
    summary = RunSummary()
    # Names of jobs that failed or were skipped - their dependents are blocked.
    blocked: set[str] = set()

    print(f"Running {len(ordered_jobs)} job(s)...")
    for job in ordered_jobs:
        blocking_deps = [d for d in job.depends_on if d in blocked]
        if blocking_deps:
            print(f"==> [{job.name}] skipped (dependency failed: {', '.join(blocking_deps)})")
            summary.skipped.add(job.name)
            blocked.add(job.name)
            continue

        resolved_job = job.resolve(config.job_defaults)
        parents = _compute_parents(resolved_job, summary.results)
        logger.debug("[%s] computed parents: %s", job.name, parents)
        if _on_cooldown(resolved_job, repo_path):
            print(f"==> [{job.name}] on cooldown ({resolved_job.cooldown_period}), skipped")
            summary.cooldown.add(job.name)
            # Treat like a no-op run so dependents proceed on the base branch.
            summary.results[job.name] = JobResult(
                job=resolved_job, effective_revsets=parents, produced_output=False
            )
            continue

        dep_mr_urls = [
            (summary.results[dep].job.title, url)
            for dep in job.depends_on
            if (url := summary.results[dep].mr_url) is not None
        ]
        start = time.monotonic()
        try:
            result = run_job(
                job=resolved_job,
                parents=parents,
                repo_path=repo_path,
                platform=platform,
                dep_mr_urls=dep_mr_urls,
                local=local,
            )
            summary.results[job.name] = result
        except Exception as e:
            # A command failure reports the command's own time (matching the
            # success prints); other failures have no command time, so fall back
            # to the wall time spent in run_job.
            elapsed = e.elapsed if isinstance(e, CommandError) else time.monotonic() - start
            print(f"==> [{job.name}] failed: {e} ({elapsed:.1f}s)")
            summary.failed[job.name] = e
            blocked.add(job.name)

    summary.print_report()
    if restore_hint is not None:
        print("\n" + restore_hint)
    return summary
