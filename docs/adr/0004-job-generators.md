# 4. Job generators (dynamically created jobs)

Status: Accepted

## Context

The set of jobs repoactive runs is fixed by the config files
(`.repoactive.toml`, `.repoactive.d/*.toml`, plus any passed on the command
line). For some repositories the useful set of jobs is a function of the
repository's _contents_, not something you can enumerate ahead of time: one
"upgrade dependencies" job per package in a monorepo, one job per
subdirectory matching a pattern, one job per entry in a manifest, and so on.
Today you would have to hand-write and maintain those jobs, and edit the
config every time a package is added or removed.

We want a job that, instead of producing a diff to commit, **emits more
jobs** computed from the current state of the repository, and have those
emitted jobs run in the same invocation.

This must fit two existing, load-bearing properties of repoactive:

- **Selection is by tag** ([ADR 0002](0002-tag-based-job-selection.md)).
  Whatever a generator is, `repoactive run`, `repoactive run <name>`, and
  `repoactive run --tag <tag>` must keep working without a new selection
  axis.
- **Gating is stateless and derived from the landed trailer**
  ([ADR 0001](0001-no-schedule-field.md)). Cooldown, `recent-commits`, and
  unmerged-branch refresh
  ([ADR 0003](0003-refresh-unmerged-branches-in-default-run.md)) all key on
  the `Repoactive-Job: <name>` trailer on real commits. A generated job is a
  normal job to all of this machinery — which works only if its **name is a
  stable function of repository state**.

## Decision

Add a boolean `emits_jobs` field to the existing `Job` model. A generator is
an ordinary `[job.<name>]` — so tag/name selection is unchanged — with three
differences in behavior:

1. **It contributes no diff.** A generator never commits, never pushes a
   bookmark, and never creates an MR. Any change it happens to leave in the
   working copy is discarded (the same `abandon` path as an empty job). Its
   job is to emit jobs, not to change the tree.

2. **It hands back a job list by writing TOML files into a directory.**
   repoactive creates a fresh empty directory per run and passes its path to
   the command in the environment variable `REPOACTIVE_JOBS_DIR`. The
   command writes one or more `*.toml` files there. After the command exits,
   repoactive loads that directory through the **existing** config machinery
   — `expand_config_paths` (sorted `*.toml`) and `_merge_jobs`/`Config`
   validation — so a generator's output is validated exactly like a
   checked-in config fragment, `extra="forbid"` and all. Validation errors
   are reported against the generator as their source.

3. **Its emitted jobs are force-included into the same run, inheriting from
   the generator.** "Run the generator → run everything it produced" cannot
   travel along `depends_on` (that edge is walked child→parent for
   force-include, and the children don't exist until the generator runs), so
   inclusion is a runtime "produced-by" relationship: when a _selected_
   generator runs, each job it emits joins the selected set. The emitted job
   then **inherits the generator's field values as per-field defaults, each
   overridable** by the emitted entry — the generator acts as a scoped
   `[job-defaults]` for everything it produces, layered between the global
   `[job-defaults]` and the emitted job's own values (explicit emitted
   value > generator value > global default). Notably:
   - **tags** — an emitted job carries the generator's `effective_tags()`
     unless its own entry sets `tags`/`disabled`. This is what keeps
     `--tag weekly` working: tag a generator `weekly` and its children are
     weekly too.
   - **depends_on** — an emitted job defaults to `depends_on = [generator]`
     unless its own entry sets `depends_on`. Because the generator produces
     no diff and is abandoned, its `effective_revsets` falls back to its own
     parents (`trunk()` or its `base_branch`), so the implicit edge means
     "build flat on trunk" — the common fan-out case. Overriding
     `depends_on` to a _sibling_ generated job (or a static job) is how you
     opt into a stacked-MR chain (`deps-pkg-b depends_on deps-pkg-a`).
   - **cooldown_period** — and the other `job-defaults`-style fields
     (`base_branch`, `timeout`, `labels`, `branch_prefix`, the title
     prefixes, `draft`, `create_mr`): inherited from the generator unless
     the emitted entry overrides, so a fan-out's rate limit, base branch,
     and labels are set once on the generator instead of repeated on every
     emitted job.

4. **Emitted jobs carry a dual trailer.** A generated job's commit records
   both its own name and its generator's, as repeated `Repoactive-Job`
   trailers:

   ```
   Repoactive-Job: deps-pkg-a
   Repoactive-Job: per-package
   ```

   This makes the generator's _own_ `cooldown_period` meaningful even though
   a generator never commits: `_on_cooldown(generator)` finds any recently
   _landed_ commit carrying the generator's trailer — i.e. any child that
   landed — and throttles the whole fan-out as a unit. The semantics are the
   existing per-job cooldown's exactly (time since the last _landing_, not
   the last run), so this stays on the right side of
   [ADR 0001](0001-no-schedule-field.md) rather than reintroducing the
   rejected `schedule` field. Each child's own `cooldown_period` still
   throttles it individually via its own-name trailer. Implementation
   requirement: `Repoactive-Job` must be read as a **multi-valued** trailer
   (`has_recent_job_commit`, `pending_job_names`), and `_publish_job` must
   know an emitted job's generator (a repoactive-set `generated_by` field,
   not user-written).

Run flow (a refinement of `run_all` / `_select_run_jobs`):

1. Prepare the repo and select jobs as today (`_select_run_jobs`), with
   generators selected by the same tag/name rules as any job.
2. Run the selected generators. Each emits its `*.toml` files; repoactive
   parses and merges them, applies tag/`depends_on` inheritance, and inserts
   the new jobs into the run.
3. Re-topologically-sort and run the expanded set. The generator's own
   `JobResult` (empty, `produced_diff=False`) is retained so a dependent's
   `_compute_parents` resolves through it to `trunk()`.
4. Track the emitted jobs' bookmarks (`bookmark_track`) before running them,
   so a branch an earlier run already pushed for a generated job is
   recognised and rebased rather than recreated — the same guarantee
   `_prepare_repo` gives static jobs via `config.bookmark_names()`.

### Constraints (v1)

- **Stable names are the generator's contract.** A generated job's `name`
  must be a deterministic function of repository state (`deps-<package>`,
  not `deps-<random>`). Cooldown, the landed trailer, and the per-job
  bookmark all key on the name; an unstable name orphans a branch on every
  run. This is documented as the generator author's responsibility, not
  enforced.
- **No recursion.** An emitted job may not itself set `emits_jobs` — one
  level of generation only.
- **Dependency direction across the boundary.** Emitted jobs may
  `depends_on` static jobs and sibling emitted jobs; a **static** job may
  not `depends_on` an emitted job (its existence is unknown at config-load
  time, and selection/ordering is computed before any generator runs).
- **No name collisions.** An emitted job that reuses a static job's name is
  a validation error, not an override.
- **Generator cooldown throttles the whole batch.** Thanks to the dual
  trailer (behavior 4) a generator may set `cooldown_period`; it is then
  skipped until enough time has passed since the most recent _child_ landed
  — rate-limiting the entire fan-out at once. For finer control set
  `cooldown_period` per emitted job instead of (or in addition to) on the
  generator. A tag plus OS cron remains available for time-of-day scheduling
  ([ADR 0002](0002-tag-based-job-selection.md)).

## Consequences

- **Generated jobs are normal jobs to every other subsystem.** Because they
  are merged through the existing config path and carry their own trailer
  once they land, cooldown, `recent-commits`, MR creation, and stacking all
  work with no special cases. The only genuinely new machinery is the
  generation phase and the inheritance rules.
- **Unmerged children pull their generator back into the default run.**
  Because a child's commit also carries the generator's trailer (behavior
  4), an unmerged child makes `pending_job_names()` surface the _generator_,
  which is a static config job — so `_select_run_jobs`'s unmerged refresh
  (`pending_job_names() & {config jobs}`,
  [ADR 0003](0003-refresh-unmerged-branches-in-default-run.md))
  force-includes the generator, re-runs it, and re-emits the batch, keeping
  still-applicable children rebased on `trunk()`. The residual gap is a
  child that is _no longer emitted_ (its package was deleted): the generator
  runs but does not re-emit it, so that one branch lingers until landed or
  deleted manually. Acceptable, and much narrower than it would be without
  the dual trailer. (Self-correcting in the normal case: once a branch lands
  it is an ancestor of `trunk()` and no longer unmerged.)
- **New trust consideration: a generator launders repo data into executed
  commands.** A static config's `command` strings are written by whoever
  controls the config; a generator turns _repository contents_ into the
  `command` of jobs that repoactive then executes. Pointing a generator at
  an untrusted repository is therefore materially different from running
  static jobs against it — the generator command must not let repository
  data determine what gets run. This belongs in the user-facing docs.
- **Multiple files, deliberately.** Emitting a _directory_ of `*.toml`
  rather than one file or stdout reuses `.repoactive.d` semantics, lets a
  generator shell out to several sub-generators that each drop a file, and
  keeps job data off stdout (which stays diagnostic output, captured as
  today).
- **Cost.** One extra parse/merge per generator and one extra
  topological-sort/`bookmark_track` for the expanded set. No generators
  configured ⇒ no change to the run.

## Alternatives considered

- **Separate `[[generator]]` section.** Rejected: it would need its own
  selection rules, breaking the "just another job, tags keep working"
  property the design hinges on.
- **Commit a generated config file and pick it up next run.** Fully
  persisted and stateless, but two-run latency and it pollutes the repo with
  a tracked config file. Rejected for the in-run mechanism; a generator is
  of course still free to commit such a file as a normal diff if that is the
  actual goal.
- **Matrix/template expansion** (generator emits parameter rows; a template
  job is instantiated per row). More constrained and arguably cleaner names,
  but a larger new concept. Deferred — it can be layered on top of
  `emits_jobs` later (a built-in generator that expands a template) without
  changing this decision.
- **Emit jobs on stdout.** Simpler, but mixes job data with diagnostic
  output and does not naturally support multiple fragments. Rejected in
  favor of the output directory.
