import contextlib
import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from repoactive.config import Config, Job
from repoactive.jj import JJ, JOB_TRAILER_KEY, JJError
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
    # the command produced a diff) or the parent revsets that were passed to
    # jj new (if the command produced nothing and the change was abandoned).
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


def _mr_params(  # noqa: PLR0913
    *,
    job: Job,
    bookmark: str,
    base_branch: str,
    command_output: str = "",
    dep_outputs: list[tuple[str, str]] | None = None,
    dep_mr_urls: list[tuple[str, str]] | None = None,
) -> MRParams:
    description = job.description or ""
    if dep_mr_urls:
        if description:
            description += "\n\n"
        links = "\n".join(f"- [{title}]({url})" for title, url in dep_mr_urls)
        description += f"Depends on:\n{links}"
    all_entries = [*(dep_outputs or []), (job.command, command_output)]
    output_entries = [(cmd, out) for cmd, out in all_entries if out]
    if output_entries:
        if description:
            description += "\n\n"
        blocks = "\n\n".join(f"$ {cmd}\n{out}" for cmd, out in output_entries)
        description += f"```\n{blocks}\n```"
    return MRParams(
        source_branch=bookmark,
        target_branch=base_branch,
        title=f"{job.mr_title_prefix}{job.title}",
        description=description,
        labels=job.labels,
        draft=job.draft,
    )


def _run_command(job: Job, ws: JJ) -> CommandResult:
    start = time.monotonic()
    try:
        proc = subprocess.run(
            job.command,
            shell=True,
            cwd=ws.cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        ws.abandon()
        output = e.stdout or ""
        raise CommandError(
            f"command failed with exit code {e.returncode}" + (f":\n{output}" if output else ""),
            elapsed=time.monotonic() - start,
        ) from e
    return CommandResult(output=proc.stdout.strip(), elapsed=time.monotonic() - start)


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
    dep_outputs: list[tuple[str, str]] | None,
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

    if local:
        print(f"==> [{job.name}] bookmark set (local) ({command_result.elapsed:.1f}s)")
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
            dep_outputs=dep_outputs,
            dep_mr_urls=dep_mr_urls,
        )
        mr_url = platform.ensure_mr(params)
        print(f"==> [{job.name}] {mr_url} ({command_result.elapsed:.1f}s)")
    else:
        print(f"==> [{job.name}] pushed ({command_result.elapsed:.1f}s)")
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
    dep_outputs: list[tuple[str, str]] | None = None,
    dep_mr_urls: list[tuple[str, str]] | None = None,
    local: bool = False,
) -> JobResult:
    logger.debug("starting job: %s", job.model_dump_json(indent=2))
    logger.debug("parents: %s", parents)
    bookmark = job.branch_name()
    repo = JJ(repo_path)
    bookmark_existed = repo.bookmark_exists(bookmark)

    tmp_parent = Path(tempfile.mkdtemp(prefix="repoactive_"))
    workspace_path = tmp_parent / "workspace"
    repo.workspace_add(job.name, workspace_path)
    ws = JJ(workspace_path)
    try:
        if bookmark_existed:
            ws.edit(bookmark)
            ws.rebase(*parents)
            ws.restore(bookmark)
        else:
            ws.new(*parents)
        ws.git_sync_head()
        command_result = _run_command(job, ws)
        if ws.is_empty():
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
            dep_outputs=dep_outputs,
            dep_mr_urls=dep_mr_urls,
            local=local,
        )
    finally:
        with contextlib.suppress(JJError):
            repo.workspace_forget(job.name)
        shutil.rmtree(tmp_parent, ignore_errors=True)
        with contextlib.suppress(JJError):
            repo.git_worktree_prune()


def _on_cooldown(job: Job, repo_path: Path) -> bool:
    """Whether the job landed on its base branch within its cooldown_period window."""
    delta = job.cooldown_timedelta()
    if delta is None:
        return False
    base = job.base_branch or "trunk()"
    since = datetime.now(UTC) - delta
    return JJ(repo_path).has_recent_job_commit(job.name, base, since)


def _select_jobs(config: Config, requested_jobs: list[str] | None) -> list[Job]:
    """Return the enabled, filtered, topologically sorted jobs to run."""
    jobs = _topological_sort(config.jobs)

    disabled: set[str] = set()
    for j in jobs:
        if j.disabled:
            disabled.add(j.name)
        elif any(dep in disabled for dep in j.depends_on):
            print(f"==> [{j.name}] disabled (dependency disabled)")
            disabled.add(j.name)

    if not requested_jobs:
        return [j for j in jobs if j.name not in disabled]

    unknown = set(requested_jobs) - {j.name for j in jobs}
    if unknown:
        raise ValueError(f"Unknown job(s): {', '.join(sorted(unknown))}")

    disabled_requested = [name for name in requested_jobs if name in disabled]
    if disabled_requested:
        raise ValueError(f"Cannot run disabled job(s): {', '.join(sorted(disabled_requested))}")

    selected: set[str] = set(requested_jobs)
    for j in reversed(jobs):
        if j.name in selected:
            for dep in j.depends_on:
                selected.add(dep)

    return [j for j in jobs if j.name in selected]


def run_all(
    *,
    config: Config,
    repo_path: Path,
    platform: Platform | None = None,
    requested_jobs: list[str] | None = None,
    local: bool = False,
) -> RunSummary:
    ordered_jobs = _select_jobs(config, requested_jobs)
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
        if _on_cooldown(resolved_job, repo_path):
            print(f"==> [{job.name}] on cooldown ({resolved_job.cooldown_period}), skipped")
            summary.cooldown.add(job.name)
            # Treat like a no-op run so dependents proceed on the base branch.
            summary.results[job.name] = JobResult(
                job=resolved_job, effective_revsets=parents, produced_output=False
            )
            continue

        dep_outputs = [
            (summary.results[dep].job.command, summary.results[dep].command_output)
            for dep in job.depends_on
        ]
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
                dep_outputs=dep_outputs,
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
    return summary
