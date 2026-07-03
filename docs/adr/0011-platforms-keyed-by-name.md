# 11. Configure platforms as a name-keyed table

Status: Accepted (supersedes the platform note in
[ADR 0008](0008-jobs-keyed-by-name.md))

## Context

Platforms were configured as an array of tables, merged across config
sources by their `url`:

```toml
[[platform]]
url = "https://github.com"
type = "github"
token_env = "GITHUB_TOKEN"
```

[ADR 0008](0008-jobs-keyed-by-name.md) moved jobs from a `[[job]]` array to
a name-keyed `[job.<name>]` table but deliberately left platforms as an
array, reasoning that a platform's natural key is its URL, which makes an
awkward bare TOML key.

Two things made the array form the weaker choice in practice:

- The `--set NAME=VALUE` command-line override uses dotted TOML keys. An
  array has no dotted path into a single entry, so overriding one field
  meant re-supplying a whole inline list keyed by the matching `url`
  (`--set 'platform = [{ url = "...", token_env = "..." }]'`) — verbose and
  easy to get wrong.
- Cross-source merging needed a bespoke by-`url` routine
  (`_merge_platforms`) separate from the by-name merge jobs already used.

The URL does not actually have to be the key: it is a field used to match a
platform against a repository's remote host, and that matching is unchanged
whatever the entry is keyed by.

## Decision

Store platforms as a TOML table keyed by an arbitrary name, mirroring jobs:

```toml
[platform.github]
url = "https://github.com"
type = "github"
token_env = "GITHUB_TOKEN"
```

The name is only a label and a merge key; `url` remains a required field and
stays the sole basis for matching a platform to a repository. Internally
`Config.platforms` is still a `list[PlatformConfig]` — a before-validator
drops the keys — because platforms, unlike jobs, are never referenced by
name. Merging is the same keyed-dict merge jobs use (`_merge_named_tables`,
shared by `merge_jobs` and `merge_platforms`).

## Consequences

- A single platform field is reachable from the command line:
  `--set 'platform.github.token_env = "MY_TOKEN"'`.
- The by-`url` merge routine is gone; jobs and platforms share one merge
  helper.
- This is a **breaking** config change with no migration shim, consistent
  with ADR 0008. The old `[[platform]]` array form is detected and rejected
  with a message pointing at the new `[platform.<name>]` syntax (the project
  is pre-1.0, so a hard break is acceptable).
- Keying by name lifts the array's implicit de-duplication (the old by-`url`
  merge collapsed same-`url` entries into one). To keep matching
  unambiguous, a `model_validator` now rejects two platforms that resolve to
  the same host, so overriding a built-in default (`github`, `gitlab`) means
  reusing its name rather than adding a second entry for the same host.
