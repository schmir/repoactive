# 17. Declare command secrets with `secret_env`: scope them to granting jobs

Status: Accepted (Phase 1 implemented; Phase 2 redaction deferred)

## Context

A job's `command` runs with repoactive's inherited environment, minus the
platform API token(s) stripped as defence-in-depth
([ADR 0006](0006-job-commands-are-trusted.md)). So a secret the operator
exports before invoking repoactive - an LLM key, a package-registry token -
_already reaches_ the command, and reaches **every** job's command equally,
because the whole environment is inherited. There is no "how do I get the
secret in" gap; there is a "the secret is silently everywhere" gap.

Two problems follow.

**No scoping.** Every job sees every exported secret whether it needs it or
not. If a variable is a secret, a job should have to **declare** it to read
it - or not be able to read it at all. This is the problem this ADR solves.

**Leakage into git.** Command output is captured and, when
`output_in_commit` is set, embedded verbatim into the commit message as a
boxquote (`runner._build_commit_message`), then pushed on a branch and
surfaced in the MR. There is no redaction anywhere in the codebase. A
command that echoes a secret it holds - a stray `set -x`, a `curl` error
dumping headers, a debug `print` - writes it into git history and the MR,
permanently. Scoping shrinks this to "a job leaks a secret it was granted";
closing it entirely needs redaction, which this ADR **defers to Phase 2**.

The values themselves must not live in config: a `.repoactive.toml` is
checked into the repo, so a secret _value_ there is the exact anti-pattern
this avoids. Config may name a secret; it may never hold one.

## Decision

Add a `secret_env` field on a job and in `[job-defaults]`: a list of
**environment-variable names, never values**.

```toml
[job.rewrite-docs]
command = "./llm-rewrite.sh"
secret_env = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]
```

`secret_env` expresses two distinct things, and keeping them apart is what
makes the model coherent:

- **Marking** - _which variable names are secrets._ A variable named in
  **any** `secret_env` in the merged config, `[job-defaults]` included,
  becomes a **managed secret** for the run. Marking is the union of every
  `secret_env` list; it drives stripping (and, in Phase 2, redaction).
- **Granting** - _which jobs may read a secret._ A job is granted only the
  names in **its own** `[job.<name>].secret_env`. `[job-defaults]` marks but
  never grants (see _Interaction with `[job-defaults]`_).

**Phasing.** Phase 1 - this decision - is scoping and fail-fast, and stands
on its own. Output redaction is **Phase 2**, deferred: it is additive,
changes no config surface or scoping semantics, and can land later. The two
are separable because scoping already removes the incoherence that motivated
redaction (a job cannot leak a value it was never given); what redaction
adds on top is guarding a _granting_ job against echoing its _own_ secret.

### 1. Scoped to granting jobs (stripped from the base environment)

The command environment is built by removing every managed secret from the
inherited environment (alongside the platform tokens already stripped), and
then **injecting each managed secret back only into the jobs that grant
it** - that name it in their own `secret_env` - read from repoactive's
environment at run time.

The consequence is the rule we want, enforced structurally: a job that does
not itself list a variable sees it **unset**, even if another job (or
`[job-defaults]`) marked it secret. To read a secret variable, a job must
grant it to itself - and by doing so, opts into the fail-fast below (and,
once Phase 2 lands, redaction). There is no third state where a job reads a
secret it did not grant.

This generalizes the platform-token strip rather than contradicting it: a
platform token is a managed secret granted by _no_ job (stripped from all,
injected into none). `secret_env` adds managed secrets granted by _some_
jobs (stripped from all, injected into those). It is a flat, declarative
scoping of named variables - an extension of ADR 0006's one hardening
exception - not the general host isolation (network, filesystem, syscalls)
that ADR 0006 places on the operator.

### 2. Fail-fast on a missing secret

If a job **grants** a `secret_env` name (lists it in its own `secret_env`)
that is unset in repoactive's own environment, that job fails at its start
with a clear, source-attributed error
(`==> [rewrite-docs] requires secret OPENAI_API_KEY, not set`) instead of
the command failing obscurely later. The precondition is per-granting-job: a
marked-but-ungranted secret imposes no requirement on jobs that do not use
it, so an unrelated job never fails over a secret it was never given.

### Interaction with `[job-defaults]`

`secret_env` deliberately does **not** inherit the way `labels` and the
other defaulted fields do. A name in `[job-defaults].secret_env` is
**marked** (added to the managed-secret set, so it is stripped from the base
environment - and redacted, in Phase 2) but is **granted to no job**. It is
the central place to declare "these variable names are secrets everywhere -
keep them out of every job's environment" without handing them to anything.

This is the opposite of the `labels` union, and on purpose: inheriting a
grant to every job is exactly the ambient-everywhere behaviour this ADR
removes. A job that needs a secret must name it in its own `secret_env`,
whether or not `[job-defaults]` also marked it. So a secret is never present
in a job that did not ask for it, even via defaults.

### Field mechanics

- **Marked set:** the union of every `secret_env` list in the merged config
  (`[job-defaults]` and every job), de-duplicated. Drives stripping (and the
  Phase 2 redaction set).
- **Grant per job:** the names in that job's own `secret_env` only; not
  inherited from `[job-defaults]`.
- **Validation:** each entry must match the environment-variable-name
  grammar, and the reserved `RA_` and `REPOACTIVE_` prefixes
  ([ADR 0016](0016-injected-env-var-prefix.md)) are rejected.
- **Companion, separate concern:** a non-secret `env` map (static literals
  written in config) is deliberately _not_ part of this decision. When both
  exist the rule reads cleanly: literals go in `env`, secret names go in
  `secret_env`, never the reverse. `env` can land in its own ADR.

## Phase 2 (deferred): redact secret values from captured output

Not part of the accepted decision above; recorded here so the follow-up is
unambiguous and Phase 1 does not accidentally over-promise.

**What it adds.** Before any captured output is displayed (the live progress
view) or persisted (the commit boxquote), every occurrence of a managed
secret's value is replaced with `[redacted]`. Redaction is **global**: the
set is the union of all managed-secret values present in repoactive's
environment (plus the platform token values repoactive already holds),
applied to every job's output - not only the granting job's, since a job
could echo a value it holds legitimately. This closes the residual
leak-into-git that scoping alone leaves: a granting job echoing its own
secret.

**Why it is cheap here.** Output already funnels through one line-oriented
choke point in `runner._run_command`:

```python
for line in proc.stdout:
    output_lines.append(line)   # -> commit message + error detail
    view.feed(line)             # -> live view
```

Both sinks - the commit boxquote and the live view - draw from that loop, so
redacting `line` once, before those two calls, covers everything downstream
(commit message, `CommandError` detail). Because output is line-by-line and
secret values are single-line tokens, a secret split across read chunks does
not arise, so no buffering is needed. The work is: a small redactor (sort
values longest-first, `str.replace` each with `[redacted]`, skip values
under ~5 characters so a trivial value does not blank the output), thread
the granting job's secret _values_ into `_run_command` (it currently
receives only names), and the one call at the choke point. Using a bare
`[redacted]` placeholder avoids needing a value→name map.

**What redaction will not cover** (state it so it is not mistaken for
containment): it operates only on strings repoactive itself displays and
writes, not on what a command writes into the working tree - a command that
writes a secret into a tracked file still commits it (the ADR 0006 trust
boundary, unchanged). It is a literal substring match, so base64- or
URL-encoded forms of a secret are not caught. Scoping (Phase 1) already
stops the common case: a job cannot echo a secret it was never given.

## Alternatives considered

**Redact only; leave the environment untouched (passthrough).** Redact
secret values from output but let every job keep reading every secret from
the inherited environment. Rejected as incoherent: treating a value as
secret in one job while another reads it freely is not a secret. It also
leaves scoping to the operator for something repoactive can express
declaratively at near-zero cost, and it does not satisfy the rule that
reading a secret should require granting it. (Scoping is precisely the part
this ADR keeps; redaction is the part it defers.)

**Ship redaction in Phase 1 too.** Rejected for now only to keep the first
change minimal, not on cost grounds - the choke point above makes it small.
Deferring is safe because it is additive and changes no config or scoping
semantics.

**Do nothing; tell users not to echo secrets.** The status quo. It leaves
both the silent everywhere-secret (Phase 1 fixes this) and the leak only
repoactive can close, since repoactive - not the command - writes captured
output into the commit (Phase 2).

**Source secrets from files/vaults/keychains.** Out of scope. Getting a
secret into repoactive's environment is the invoker's job (CI secret store,
`direnv`, a vault wrapper). repoactive forwards named variables and scopes
them; it does not grow a secret backend.

## Consequences

- A secret variable is readable by a job only if that job **grants** it in
  its own `secret_env`; every other job sees it unset, including when
  `[job-defaults]` marked it. Secrets stop being silently ambient, and a
  secret is never present in a job that did not ask for it.
- `[job-defaults].secret_env` centrally marks names as secret (stripped
  everywhere) without granting them, so the sensitive-name list lives in one
  place while access stays per-job.
- A missing granted secret fails only the granting job, immediately and
  legibly; an unrelated job never fails over a secret it does not use.
- Scoping shrinks the leak-into-git surface to "a job leaks a secret it was
  granted." Phase 1 does **not** close that residual case - documentation
  must describe the guarantee as _scoping_, not leak-proofing, until Phase 2
  redaction lands. It is defence for the careless, not containment of the
  malicious - the ADR 0006 stance, extended, not overturned.
- The platform-token strip and `secret_env` become the same mechanism: a
  managed-secret set, stripped from the base environment, injected only into
  granting jobs (nothing grants a platform token). The two can share one
  code path.
- New config surface (Phase 1): a list field on `Job` and `JobDefaults`, its
  validation, the marked-set union, and per-job grant/scoping (which does
  _not_ follow the normal defaults inheritance). `config-schema.json` must
  be regenerated and a README section added. Phase 2 adds only a redaction
  pass at the output choke point, no config change.
