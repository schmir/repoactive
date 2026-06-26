# 6. Job commands are trusted

Status: Accepted

## Context

A job's `command` is an arbitrary shell command that `repoactive` runs
against the repository's working tree (`runner._run_command`, `shell=True`).
Its whole purpose is to mutate the tree; repoactive captures the resulting
diff, commits it with a `Repoactive-Job` trailer, and turns it into a
branch/MR.

This means a job command is not a sandboxed input that repoactive defends
itself against. By construction it can:

- write arbitrary content into the working tree, which becomes a commit and
  a pushed branch;
- run any executable on the host with the privileges of the process running
  `repoactive`.

Whoever controls the set of jobs — the merged TOML config
(`.repoactive.toml`, `.repoactive.d/`, `--config`) and any generator that
emits jobs (see [ADR 0004](0004-job-generators.md)) — therefore already
controls what code repoactive produces and runs. There is no later point at
which repoactive could "contain" a command it has decided to execute:
defending the commit-message format or the trailer against a command you
chose to run is guarding a line that is already behind the command.

## Decision

**Job commands are trusted.** The trust boundary is the configuration: if
you let a TOML source or a generator contribute a job, you trust that job's
command as much as code you wrote yourself. repoactive does not attempt to
sandbox, contain, or validate the behaviour of a command it runs, and design
proposals that would only matter against an untrusted command are out of
scope.

Concretely, two things repoactive deliberately does **not** treat as a
security boundary:

- **The commit message / trailer.** A command can already put arbitrary code
  in the repository, so it gains nothing by also influencing its own commit
  message (e.g. `output_in_commit`). repoactive keeps the trailer robust for
  correctness — it is appended as the final, unindented paragraph after the
  command output, which is wrapped in a boxquote (each line prefixed with
  `| `), so benign output cannot accidentally form a trailer — but this is
  not a defence against a malicious command.

- **General host isolation.** A command runs with repoactive's own
  privileges and inherited environment (minus the exception below).
  Isolating it (network, filesystem, syscalls) is the operator's job, not
  repoactive's.

### One hardening exception: platform tokens

Despite the trust assumption, repoactive **removes the platform API token(s)
from the environment a job command runs in** (`Config.token_env_names` →
`runner._command_env`). The token named by `platform.token_env` and every
other configured platform's token variable are stripped; the rest of the
environment (PATH, etc.) is passed through unchanged.

This is cheap defence-in-depth, not a contradiction of the trust model. The
distinction it draws:

- Code a command writes into the repository passes through the **MR review
  gate** before it lands — a human still sees it.
- The platform token is a **live credential** granting immediate,
  un-reviewed capability (push to any repo the token can reach, close/merge
  MRs, read private data). A job has no need for it, and a command does not
  have to be malicious to misuse it — a careless script that shells out to
  `gh`/`glab` or echoes its environment into a log could leak or act on it
  by accident.

Stripping the token costs one dictionary comprehension and removes a way for
a _trusted-but-careless_ command to do something it was never meant to. It
does not pretend to constrain a _malicious_ command, which has other
avenues.

## Consequences

- Treat the config and any job generator as trusted code paths. Reviewing a
  new job's `command` is reviewing code that will run on the host.
- `output_in_commit` and similar conveniences can stay simple; they are not
  attack surface under this model.
- Job commands cannot read `platform.token_env` (or any configured
  platform's token variable) from their environment. A job that legitimately
  needs a credential must be given its own, separate one — not the
  repoactive platform token.
- If you ever need to run genuinely untrusted commands, this assumption no
  longer holds and isolation must be added around repoactive (containers,
  restricted users, etc.); the token-stripping above is not a substitute for
  that.
