# 8. Configure jobs as a name-keyed table

Status: Accepted

## Context

Jobs were originally an array of tables, each carrying a `name`:

```toml
[[job]]
name = "regenerate-api-client"
command = "..."
```

But a job's name is not just a label — it is its identity. It keys the
branch name (`branch_prefix + name`), the `Repoactive-Job` commit trailer
(and thus cooldown throttling and unmerged-branch detection), `depends_on`
links, and the merge-by-name used when config is assembled from multiple
sources (`.repoactive.d/`, generator fragments). The array form left that
identity as an ordinary field, which meant:

- names had to be validated as present and unique at load time, rather than
  being structurally guaranteed;
- merging two sources meant scanning each list for matching `name` fields
  instead of a plain dict lookup;
- every job repeated `name = "..."` boilerplate.

## Decision

Store jobs as a TOML table keyed by name, with the name as the table key and
no `name` field in the body:

```toml
[job.regenerate-api-client]
command = "..."
```

The key _is_ the name. TOML forbids duplicate keys, so uniqueness is
enforced by the parser; merging by name is a dict merge; and a `name` field
inside the body is rejected as redundant. The `Job` model keeps its `name`
attribute — only the TOML→model layer changes, injecting the name from the
key — so internal code and generator-emitted jobs are unaffected. Generator
(`emits_jobs`) fragments use the same `[job.<name>]` form for consistency.

Platforms stay a `[[platform]]` array: their natural key is the URL, which
makes an awkward bare TOML key (dots and slashes), and there is no
boilerplate to remove.

## Consequences

- Uniqueness and presence of names are guaranteed by TOML itself; the
  "missing name" validation path is gone.
- Cross-source merging (`_merge_jobs`) is a straightforward keyed-dict
  merge, preserving insertion order (existing names first, new names
  appended).
- This is a **breaking** config change with no migration shim. The old
  `[[job]]` array form is detected and rejected with a clear message
  pointing at the new `[job.<name>]` syntax (the project is pre-1.0, so a
  hard break is acceptable).
- Job ordering still comes from document order, since TOML tables preserve
  it.
