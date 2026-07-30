#!/usr/bin/env bash
set -eux

# On any failure, print a loud banner naming the failing line and command, and
# dump the most recent run log so the actual repoactive error stands out instead
# of scrolling by. LAST_LOG is set before each `repoactive run` block below.
LAST_LOG=""
fail() {
    local exit_code=$?
    set +x
    # Bold red banner. Emitted unconditionally (raw ANSI) rather than gated on a
    # TTY: this script always runs under `docker run` with no TTY, so a `[ -t 2 ]`
    # check would suppress the colour exactly where we want to see it. GitHub
    # Actions and most CI log viewers render ANSI anyway.
    local red=$'\033[1;31m' reset=$'\033[0m'
    echo >&2
    echo "${red}################################################################${reset}" >&2
    echo "${red}# SMOKE TEST FAILED (exit $exit_code)${reset}" >&2
    echo "${red}#   line $1: $2${reset}" >&2
    echo "${red}################################################################${reset}" >&2
    if [ -n "$LAST_LOG" ] && [ -f "$LAST_LOG" ]; then
        echo "# ---- $LAST_LOG (full) ----" >&2
        cat "$LAST_LOG" >&2
        echo "# ---- end $LAST_LOG ----" >&2
    fi
    exit "$exit_code"
}
trap 'fail "$LINENO" "$BASH_COMMAND"' ERR

# Three runs against the real config; a non-zero exit is expected (in this
# minimal image update-flake needs nix and prek-* need prek, and every job
# may be on cooldown), so the grep gates are the actual pass/fail checks.
#
# Run 1 (plain): require repoactive to drive the pipeline far enough to
# select and run jobs -- fails if it crashed before printing "Running N
# job(s):" (bad clone, config error, PATH break).
#
# Run 2 forces uv-lock-upgrade past its cooldown so a real job executes (uv
# lock --upgrade), exercising the full run -> command -> commit path; the
# gate fails if the job was still skipped or its command errored.
#
# Run 3 exercises the demo tag: force the upgrade-deps generator past its
# cooldown so it emits its per-dependency jobs; the gate fails if the
# generator did not run and emit jobs.
#
# Each run also gets a negative gate: no Python traceback may appear in the
# log. The positive gates only prove a marker line was reached, so without
# this a crash later in the run would still pass.

repoactive --version 2>&1 | tee /tmp/version.log
grep -qE '^[0-9]+\.[0-9]+\.[0-9]+' /tmp/version.log
repoactive --help 2>&1 | tee /tmp/help.log
grep -q 'Usage:' /tmp/help.log
grep -q 'validate-config' /tmp/help.log
git clone https://github.com/schmir/repoactive .
repoactive validate-config
# A malformed --set must be rejected with a non-zero exit. Use `&& exit 1`
# rather than `! ...` so the gate actually fails the smoke test if the command
# wrongly succeeds (set -e ignores the exit status of a `!`-negated command).
repoactive validate-config --set 'job.foo.disabled=true' && exit 1
repoactive dump-schema -o /tmp/schema.json
test -s /tmp/schema.json
repoactive info jobs
repoactive info tags
jj config set --user user.name "repoactive smoke test"
jj config set --user user.email "smoke@example.com"

LAST_LOG=/tmp/run1.log
repoactive run 2>&1 | tee /tmp/run1.log || true
grep -q "Running [0-9]* job(s):" /tmp/run1.log
grep -q 'Traceback' /tmp/run1.log && exit 1

LAST_LOG=/tmp/run2.log
repoactive run -s 'job.uv-lock-upgrade.cooldown_period="0s"' 2>&1 | tee /tmp/run2.log || true
grep -qE '\[uv-lock-upgrade\] (committed|no changes)' /tmp/run2.log
grep -q 'Traceback' /tmp/run2.log && exit 1

LAST_LOG=/tmp/run3.log
repoactive run --tag demo -s 'job.upgrade-deps.cooldown_period="0s"' 2>&1 | tee /tmp/run3.log || true
grep -qE '\[upgrade-deps\] generated [0-9]+ job\(s\)' /tmp/run3.log
grep -q 'Traceback' /tmp/run3.log && exit 1

repoactive recent-commits
