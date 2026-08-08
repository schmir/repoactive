"""Run a job's shell command: spawn it, enforce the timeout, stream its output."""

import contextlib
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

from repoactive.config import Job
from repoactive.progress import ProgressView
from repoactive.settings import load_settings

logger = logging.getLogger(__name__)

# Environment variable exposing to a job command the throwaway jj workspace
# repoactive created for it. This is always the command's working directory, but
# naming it explicitly lets a command that changes directory find its way back.
RA_WORKSPACE_DIR_ENV = "RA_WORKSPACE_DIR"


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


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the whole process group led by proc.

    The command is started with start_new_session=True so it leads its own
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
    drops stripped_env_names (the platform tokens of ADR 0006 plus every
    marked secret of ADR 0017), injects back only the secrets this job granted
    (granted_secret_env), then layers on extra_env (the RA_* variables,
    e.g. RA_JOBS_DIR for a generator) last so repoactive's own variables win.
    """
    env = {k: v for k, v in os.environ.items() if k not in stripped_env_names}
    if granted_secret_env:
        env.update(granted_secret_env)
    if extra_env:
        env.update(extra_env)
    return env


def _resolve_granted_secrets(job: Job) -> dict[str, str]:
    """Values for the secrets job grants, read from repoactive's own environment.

    Only the names in the job's own secret_env are granted; job-defaults
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


@contextlib.contextmanager
def _watchdog(proc: subprocess.Popen[str], timeout: float | None) -> Generator[threading.Event]:
    """Kill proc's process group if it outlives timeout seconds.

    The blocking stdout read in run_command cannot be interrupted by a
    timeout, so a background timer SIGKILLs the process group once the deadline
    passes; that closes stdout and ends the read loop. The poll() guard avoids
    flagging a false timeout when the command finishes just as the timer fires;
    the remaining race (the command exits between poll() and the kill) is closed
    by the caller, which treats only a non-zero exit as a timeout.

    Yields an event that is set iff the watchdog fired. timeout is None means
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
    """Run job.command in its own session, cleaning up on exit.

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


def run_command(
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
