# 21. Read configuration from a revset in a temporary workspace

Status: Accepted

## Context

`repoactive` reads `.repoactive.d/` and `.repoactive.toml` from the `--repo`
working copy. That copy can contain an incomplete change or be on another
branch. There is no way to use the trunk configuration or the merged
configuration of several revisions.

## Decision

The commands `run`, `validate-config`, `info jobs`, and `info tags` accept
`--config-revset REVSET`. With this option, `repoactive`:

1. Creates a temporary, non-colocated jj workspace.
2. Runs `jj new <revset>` there to produce the merged tree.
3. Reads the configuration from that workspace instead of `--repo`.

The workspace remains until the command completes because
`RA_CONFIG_SOURCE_DIR` can refer a job to a script in it.

`--config-revset` and `--config` cannot be used together. A `--config` path
is relative to the current directory. If `--config-revset` made this path
relative to the workspace, `--config-revset` would change the meaning of the
path. If the path remained relative to the current directory,
`--config-revset` would not affect that path.

Conflicts cause a warning, not an immediate error, because they can occur in
unused files. The warning names the conflicted files. A conflicted
configuration file then fails to parse normally.

The workspace is not colocated because no code reads git data there. See
[ADR 0007](0007-colocate-job-workspaces-for-git-aware-commands.md).

Its name contains `CONFIG_WORKSPACE_PREFIX` and a random UUID. The UUID
avoids collisions between concurrent commands. The distinct prefix prevents
`JJ.forget_stale_workspaces` from removing a live config workspace: config
commands create their workspaces before the run lock, and most never take
that lock.

Each config workspace has an advisory lock at `.jj/<workspace name>.lock`. A
command takes this lock before it registers the workspace and holds it until
the workspace is no longer in use. This order prevents another command from
seeing a registered but unlocked workspace and reclaiming it. On normal
exit, the command releases the lock and removes the lock file.

Before a config-reading command creates its workspace, it examines all
registered config workspaces. It leaves a workspace alone if its lock is
held. A missing or unlocked lock file means that no live command owns the
workspace, so the command forgets that workspace. The kernel releases the
lock if its process dies, which makes this test independent of process IDs
and their reuse.

## Consequences

- A command can use configuration from a revset independently of the working
  copy, for example `repoactive run --config-revset 'trunk()'`.
- [ADR 0005](0005-local-repository-is-the-source-of-truth.md) still applies:
  the revset uses the local repository, so the clone must be fetched first.
- The option requires jj and a colocated repository, even for `info jobs`.
  For a git-only repository, `repoactive` runs `jj git init --colocate` and
  prints the same undo message as a bare `run`.
- Conflicts in unused files do not stop the command. Conflicts in
  configuration files produce the usual invalid-configuration error after
  the warning.
- An immediate signal can leave the workspace and an unlocked lock file
  behind. The next config-reading command forgets the workspace. The lock
  file and temporary directory remain until an external cleanup removes
  them.
- The lock file is under `.jj`, so git never tracks or pushes it.
- `RA_CONFIG_SOURCE_DIR` points to a temporary directory that is removed
  when the command ends. Commands can read from it but must not store
  persistent data there.
