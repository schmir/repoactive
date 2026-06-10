import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from repoactive import jj
from repoactive.config import Config, Defaults, Job
from repoactive.platforms.base import MRParams, Platform


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

    @property
    def ok(self) -> bool:
        return not self.failed and not self.skipped


def _resolve_jobs(all_jobs: list[Job], requested: list[str]) -> list[Job]:
    by_name = {j.name: j for j in all_jobs}
    unknown = set(requested) - by_name.keys()
    if unknown:
        raise ValueError(f"Unknown job(s): {', '.join(sorted(unknown))}")

    included: set[str] = set()

    def include(name: str) -> None:
        if name in included:
            return
        included.add(name)
        for dep in by_name[name].depends_on:
            include(dep)

    for name in requested:
        include(name)

    return [j for j in all_jobs if j.name in included]


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
    defaults: Defaults,
    bookmark: str,
    base_branch: str,
    command_output: str = "",
    dep_outputs: list[tuple[str, str]] | None = None,
    dep_mr_urls: list[tuple[str, str]] | None = None,
) -> MRParams:
    labels = list(dict.fromkeys(defaults.labels + job.labels))
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
        title=f"{defaults.mr_title_prefix}{job.title}",
        description=description,
        labels=labels,
        draft=job.draft,
    )


def _run_command(job: Job, repo_path: Path) -> str:
    try:
        proc = subprocess.run(
            job.command,
            shell=True,
            cwd=repo_path,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        jj.abandon(cwd=repo_path)
        output = e.stdout or ""
        raise RuntimeError(
            f"command failed with exit code {e.returncode}" + (f":\n{output}" if output else "")
        ) from e
    return proc.stdout.strip()


def _handle_empty(  # noqa: PLR0913
    job: Job,
    bookmark: str,
    parents: list[str],
    repo_path: Path,
    command_output: str,
    *,
    local: bool = False,
) -> JobResult:
    if len(parents) == 1:
        jj.abandon(cwd=repo_path)
        jj.bookmark_set(bookmark, revision=parents[0], cwd=repo_path)
        print(f"  [{job.name}] no changes, bookmark set to parent")
    else:
        # Keep the empty merge commit so the bookmark has a single target.
        jj.bookmark_set(bookmark, cwd=repo_path)
        print(f"  [{job.name}] no changes, bookmark set to empty merge commit")

    if not local:
        jj.git_push(bookmark, cwd=repo_path)
    return JobResult(
        job=job,
        effective_revsets=[bookmark],
        produced_output=False,
        command_output=command_output,
    )


def _publish_job(  # noqa: PLR0913
    *,
    job: Job,
    defaults: Defaults,
    bookmark: str,
    repo_path: Path,
    platform: Platform | None,
    command_output: str,
    dep_outputs: list[tuple[str, str]] | None,
    dep_mr_urls: list[tuple[str, str]] | None,
    local: bool = False,
) -> JobResult:
    jj.bookmark_set(bookmark, cwd=repo_path)
    commit_message = f"{defaults.commit_title_prefix}{job.title}"
    if job.description:
        commit_message += f"\n\n{job.description}"
    if job.output_in_commit and command_output:
        indented = "\n".join(
            f"  {line}" for line in f"$ {job.command}\n{command_output}".splitlines()
        )
        commit_message += f"\n\n{indented}"
    jj.describe(commit_message, cwd=repo_path)

    if local:
        print(f"  [{job.name}] local: bookmark '{bookmark}' set (not pushed)")
        return JobResult(
            job=job,
            effective_revsets=[bookmark],
            produced_output=True,
            command_output=command_output,
        )

    jj.git_push(bookmark, cwd=repo_path)
    mr_url: str | None = None
    if platform is not None and job.create_mr:
        base_branch = job.base_branch or platform.default_branch()
        params = _mr_params(
            job=job,
            defaults=defaults,
            bookmark=bookmark,
            base_branch=base_branch,
            command_output=command_output,
            dep_outputs=dep_outputs,
            dep_mr_urls=dep_mr_urls,
        )
        mr_url = platform.ensure_mr(params)
        print(f"  [{job.name}] {mr_url}")
    else:
        print(f"  [{job.name}] bookmark '{bookmark}' pushed")

    return JobResult(
        job=job,
        effective_revsets=[bookmark],
        produced_output=True,
        mr_url=mr_url,
        command_output=command_output,
    )


def run_job(  # noqa: PLR0913
    *,
    job: Job,
    defaults: Defaults,
    parents: list[str],
    repo_path: Path,
    platform: Platform | None,
    dep_outputs: list[tuple[str, str]] | None = None,
    dep_mr_urls: list[tuple[str, str]] | None = None,
    local: bool = False,
) -> JobResult:
    bookmark = job.branch_name(defaults.branch_prefix)
    jj.new(*parents, cwd=repo_path)
    command_output = _run_command(job, repo_path)
    if jj.is_empty(cwd=repo_path):
        return _handle_empty(job, bookmark, parents, repo_path, command_output, local=local)
    return _publish_job(
        job=job,
        defaults=defaults,
        bookmark=bookmark,
        repo_path=repo_path,
        platform=platform,
        command_output=command_output,
        dep_outputs=dep_outputs,
        dep_mr_urls=dep_mr_urls,
        local=local,
    )


def _propagate_disabled(jobs: list[Job]) -> set[str]:
    """Return names of all disabled jobs, including those disabled transitively via depends_on."""
    disabled = {j.name for j in jobs if j.disabled}
    changed = True
    while changed:
        changed = False
        for j in jobs:
            if j.name not in disabled and any(dep in disabled for dep in j.depends_on):
                disabled.add(j.name)
                changed = True
    return disabled


def run_all(
    *,
    config: Config,
    repo_path: Path,
    platform: Platform | None = None,
    requested_jobs: list[str] | None = None,
    local: bool = False,
) -> RunSummary:
    disabled = _propagate_disabled(config.jobs)
    for j in config.jobs:
        if j.name in disabled and not j.disabled:
            print(f"  [{j.name}] disabled (dependency disabled)")

    if requested_jobs:
        disabled_requested = [name for name in requested_jobs if name in disabled]
        if disabled_requested:
            raise ValueError(
                f"Cannot run disabled job(s): {', '.join(sorted(disabled_requested))}"
            )
    enabled = [j for j in config.jobs if j.name not in disabled]
    selected_jobs = _resolve_jobs(enabled, requested_jobs) if requested_jobs else enabled
    ordered_jobs = _topological_sort(selected_jobs)
    summary = RunSummary()
    # Names of jobs that failed or were skipped - their dependents are blocked.
    blocked: set[str] = set()

    print(f"Running {len(ordered_jobs)} job(s)...")
    for job in ordered_jobs:
        blocking_deps = [d for d in job.depends_on if d in blocked]
        if blocking_deps:
            print(f"  [{job.name}] skipped (dependency failed: {', '.join(blocking_deps)})")
            summary.skipped.add(job.name)
            blocked.add(job.name)
            continue

        parents = _compute_parents(job, summary.results)
        dep_outputs = [
            (summary.results[dep].job.command, summary.results[dep].command_output)
            for dep in job.depends_on
        ]
        dep_mr_urls = [
            (summary.results[dep].job.title, url)
            for dep in job.depends_on
            if (url := summary.results[dep].mr_url) is not None
        ]
        try:
            result = run_job(
                job=job,
                defaults=config.defaults,
                parents=parents,
                repo_path=repo_path,
                platform=platform,
                dep_outputs=dep_outputs,
                dep_mr_urls=dep_mr_urls,
                local=local,
            )
            summary.results[job.name] = result
        except Exception as e:
            print(f"  [{job.name}] failed: {e}")
            summary.failed[job.name] = e
            blocked.add(job.name)

    produced = sum(1 for r in summary.results.values() if r.produced_output)
    total = len(ordered_jobs)
    print(
        f"\nDone: {produced}/{total} produced output"
        + (f", {len(summary.failed)} failed" if summary.failed else "")
        + (f", {len(summary.skipped)} skipped" if summary.skipped else "")
        + "."
    )
    return summary
