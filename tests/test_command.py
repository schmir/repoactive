"""Tests for running a job's shell command (spawn, timeout, env stripping)."""

import os
import shutil
import time
from pathlib import Path

import pytest

from repoactive.command import (
    CommandError,
    CommandResult,
    _spawn,
    run_command,
)
from repoactive.config import Job, MissingSecretError
from repoactive.constants import RA_WORKSPACE_DIR_ENV


def _run(
    job: Job,
    cwd: Path,
    *,
    stripped_env_names: frozenset[str] = frozenset(),
    extra_env: dict[str, str] | None = None,
) -> CommandResult:
    """Run job, composing its env the way the runner does."""
    base = {k: v for k, v in os.environ.items() if k not in stripped_env_names}
    env = (
        base | (extra_env or {}) | job.resolve_granted_secrets() | {RA_WORKSPACE_DIR_ENV: str(cwd)}
    )
    return run_command(job, cwd, env=env)


def _alive(pid: int) -> bool:
    """Whether pid still names a live (non-reaped) process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class TestRunCommand:
    @pytest.mark.slow
    def test_timeout_kills_whole_process_group(self, tmp_path: Path) -> None:
        # The command backgrounds a long sleep, records its PID, then waits. The
        # sleep shares the command's process group, so the timeout must kill it
        # too - not just the top-level shell.
        pidfile = tmp_path / "child.pid"
        job = Job(
            name="foo",
            command=f"sleep 30 & echo $! > {pidfile}; wait",
            title="t",
            timeout="1s",
            branch_prefix="repoactive/",
            commit_title_prefix="",
        )
        with pytest.raises(CommandError, match="timed out after 1s"):
            _run(job, tmp_path)

        child_pid = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(child_pid), "backgrounded child survived the timeout kill"

    def test_spawn_kills_group_when_body_raises(self, tmp_path: Path) -> None:
        # A body that raises for a reason other than a timeout must still leave no
        # orphan: _spawn kills the whole process group (including a backgrounded
        # child) on exit, not just the top-level shell.
        pidfile = tmp_path / "child.pid"
        job = Job(
            name="foo",
            command=f"sleep 30 & echo $! > {pidfile}; echo ready; wait",
            title="t",
            branch_prefix="repoactive/",
            commit_title_prefix="",
        )

        class BoomError(Exception):
            pass

        def _spawn_then_raise() -> None:
            with _spawn(job, tmp_path, dict(os.environ)) as proc:
                assert proc.stdout is not None
                proc.stdout.readline()  # block until the child pid is recorded
                raise BoomError

        with pytest.raises(BoomError):
            _spawn_then_raise()

        child_pid = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(child_pid), "backgrounded child survived the kill on exception"

    def test_non_utf8_output_does_not_crash(self, tmp_path: Path) -> None:
        # A command may emit arbitrary bytes; an undecodable byte must be
        # replaced rather than raising UnicodeDecodeError and crashing the run.
        job = Job(
            # \377 is octal for 0xff: POSIX printf supports octal escapes
            # everywhere, but \xHH hex escapes are not portable (dash omits them).
            name="foo",
            command=r"printf '\377'",
            title="t",
            branch_prefix="repoactive/",
            commit_title_prefix="",
        )
        result = _run(job, tmp_path)

        assert result.output == "�"  # U+FFFD REPLACEMENT CHARACTER

    def test_stdin_reading_command_fails_fast_at_eof(self, tmp_path: Path) -> None:
        # stdin is detached (subprocess.DEVNULL), so a command that reads it gets
        # EOF immediately and fails fast instead of blocking on the inherited
        # terminal and burning the whole timeout window.
        job = Job(
            name="foo",
            command="read line; echo got=[$line]",
            title="t",
            timeout="30s",
            branch_prefix="repoactive/",
            commit_title_prefix="",
        )
        # A timeout would raise CommandError; reaching here means it did not hang.
        result = _run(job, tmp_path)

        # `read` hits EOF immediately, so $line is empty in the echo that follows.
        assert result.output.strip() == "got=[]"

    def test_secret_env_stripped_from_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A platform token in the environment must not be visible to a job
        # command (see docs/adr/0006). PATH and other vars still pass through.
        monkeypatch.setenv("GITHUB_TOKEN", "supersecret")
        job = Job(
            name="foo",
            command="echo token=[${GITHUB_TOKEN:-unset}] path=[${PATH:+present}]",
            title="t",
            branch_prefix="repoactive/",
            commit_title_prefix="",
        )
        result = _run(job, tmp_path, stripped_env_names=frozenset({"GITHUB_TOKEN"}))

        assert "token=[unset]" in result.output
        assert "supersecret" not in result.output
        assert "path=[present]" in result.output

    def test_secret_env_default_passes_environment_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With no secrets to strip, the inherited environment is preserved.
        monkeypatch.setenv("REPOACTIVE_TEST_VAR", "visible")
        job = Job(
            name="foo",
            command="echo [${REPOACTIVE_TEST_VAR:-unset}]",
            title="t",
            branch_prefix="repoactive/",
            commit_title_prefix="",
        )
        result = _run(job, tmp_path)

        assert result.output == "[visible]"

    def test_granted_secret_injected_into_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A secret the job grants (lists in its own secret_env) is stripped from
        # the base environment as a marked name, then injected back for this
        # command, so the command sees its value (ADR 0017).
        monkeypatch.setenv("MY_SECRET", "s3cr3t")
        job = Job(
            name="foo",
            command="echo [${MY_SECRET:-unset}]",
            title="t",
            branch_prefix="repoactive/",
            commit_title_prefix="",
            secret_env=["MY_SECRET"],
        )
        result = _run(job, tmp_path, stripped_env_names=frozenset({"MY_SECRET"}))

        assert result.output == "[s3cr3t]"

    def test_marked_secret_stripped_from_non_granting_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A marked secret this job does not grant stays stripped: it is in
        # stripped_env_names but not in the job's own secret_env, so it is never
        # injected back and the command sees it unset (ADR 0017).
        monkeypatch.setenv("MY_SECRET", "s3cr3t")
        job = Job(
            name="foo",
            command="echo [${MY_SECRET:-unset}]",
            title="t",
            branch_prefix="repoactive/",
            commit_title_prefix="",
        )
        result = _run(job, tmp_path, stripped_env_names=frozenset({"MY_SECRET"}))

        assert result.output == "[unset]"
        assert "s3cr3t" not in result.output

    def test_missing_granted_secret_raises_before_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A granted secret that is unset fails fast, before the command runs.
        monkeypatch.delenv("MY_SECRET", raising=False)
        job = Job(
            name="foo",
            command="echo should-not-run",
            title="t",
            branch_prefix="repoactive/",
            commit_title_prefix="",
            secret_env=["MY_SECRET"],
        )
        with pytest.raises(MissingSecretError, match="requires secret MY_SECRET, not set"):
            _run(job, tmp_path, stripped_env_names=frozenset({"MY_SECRET"}))

    def test_config_source_dir_visible_to_command(self, tmp_path: Path) -> None:
        # A job with a config_source_dir sees it as RA_CONFIG_SOURCE_DIR.
        job = Job(
            name="foo",
            command="echo [$RA_CONFIG_SOURCE_DIR]",
            title="t",
            branch_prefix="repoactive/",
            commit_title_prefix="",
            config_source_dir="/cfg/dir",
        )
        result = _run(job, tmp_path, extra_env=job.injected_env())

        assert result.output == "[/cfg/dir]"

    def test_workspace_dir_visible_to_command(self, tmp_path: Path) -> None:
        # The command sees its workspace (the cwd) as RA_WORKSPACE_DIR.
        job = Job(
            name="foo",
            command="echo [$RA_WORKSPACE_DIR]",
            title="t",
            branch_prefix="repoactive/",
            commit_title_prefix="",
        )
        result = _run(job, tmp_path)

        assert result.output == f"[{tmp_path}]"

    def test_branch_visible_to_command(self, tmp_path: Path) -> None:
        # The command sees its target bookmark/branch as RA_JOB_BRANCH.
        job = Job(
            name="foo",
            command="echo [$RA_JOB_BRANCH]",
            title="t",
            branch_prefix="repoactive/",
            commit_title_prefix="",
        )
        result = _run(job, tmp_path, extra_env=job.injected_env())

        assert result.output == "[repoactive/foo]"

    def test_job_name_visible_to_command(self, tmp_path: Path) -> None:
        # The command sees its job's name as RA_JOB_NAME.
        job = Job(
            name="foo",
            command="echo [$RA_JOB_NAME]",
            title="t",
            branch_prefix="repoactive/",
            commit_title_prefix="",
        )
        result = _run(job, tmp_path, extra_env=job.injected_env())

        assert result.output == "[foo]"

    def test_base_branch_visible_to_command(self, tmp_path: Path) -> None:
        # The command sees the branch its MR targets as RA_JOB_BASE_BRANCH.
        job = Job(
            name="foo",
            command="echo [$RA_JOB_BASE_BRANCH]",
            title="t",
            branch_prefix="repoactive/",
            commit_title_prefix="",
            base_branch="release",
        )
        result = _run(job, tmp_path, extra_env=job.injected_env())

        assert result.output == "[release]"

    def test_default_shell_is_sh(self, tmp_path: Path) -> None:
        # With shell unset the command runs under /bin/sh. subprocess sets the
        # shell as argv[0], so $0 is the interpreter path (this holds regardless
        # of what /bin/sh actually is - on macOS it is bash in sh mode).
        job = Job(
            name="foo",
            command="echo [$0]",
            title="t",
            branch_prefix="repoactive/",
            commit_title_prefix="",
        )
        result = _run(job, tmp_path)

        assert result.output == "[/bin/sh]"

    def test_shell_selects_interpreter(self, tmp_path: Path) -> None:
        # A configured shell becomes argv[0], so $0 is that interpreter, proving
        # the command runs under the chosen shell rather than /bin/sh.
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("bash not available")
        job = Job(
            name="foo",
            command="echo [$0]",
            title="t",
            branch_prefix="repoactive/",
            commit_title_prefix="",
            shell=bash,
        )
        result = _run(job, tmp_path)

        assert result.output == f"[{bash}]"
