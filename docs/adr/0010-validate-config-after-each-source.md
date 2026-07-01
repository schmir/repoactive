# 10. Validate the merged config after each source

Status: Accepted

## Context

Configuration is assembled from multiple TOML sources: the built-in
default-platforms block, then directories (expanded to sorted `*.toml`
files) and files, merged in order — for the default layout, the
`.repoactive.d/*.toml` fragments in filename order, then `.repoactive.toml`
last.

`load_config` has two options for when to validate the result:

- validate only the final merged config, or
- validate the cumulative merge after each source.

Validating only at the end is maximally permissive: a fragment could
reference anything defined anywhere, including in a later-sorted file. But a
validation error would then point at the merged whole, not at the file that
introduced the problem, and fragments could grow entangled in both
directions — understanding one file would require reading every other.

## Decision

Validate the cumulative merged config after merging each source
(`Config.model_validate` per source in `load_config`), wrapping any failure
in a `ConfigError` naming that source.

This deliberately rejects forward references: a job may only `depends_on`
jobs already defined — by an earlier-sorted fragment, or by its own file.
Each fragment must leave the config valid given only what precedes it. That
constraint is a feature, not a limitation: it keeps config files cleaner and
simpler (a fragment is understandable from itself plus its predecessors),
and it attributes every error to the exact file that introduced it.

Overriding is unaffected: the merge is cumulative and field-by-field, so a
later fragment may partially override a job or platform defined earlier —
the result just has to validate at that point.

## Consequences

- A config error names the source file that introduced it, even when the
  offending value only becomes invalid in combination with earlier sources.
- A fragment cannot `depends_on` a job defined in a later-sorted file. To
  express such a dependency, put both jobs in one file or name the files so
  the dependency sorts first. Dependency cycles across fragments are
  structurally impossible.
- Fragment ordering (filenames) is load-bearing for references, not just for
  override precedence.
- Generator (`emits_jobs`) fragments are exempt: `_load_job_specs` merges
  all emitted `*.toml` files before any validation, so emitted jobs may
  reference siblings regardless of which fragment file defines them. A
  generator's fan-out is produced by one command in one run, so per-file
  attribution buys nothing there.
- Each source pays a full-config validation, so loading is O(sources ×
  config size) — irrelevant at the config sizes repoactive handles.
