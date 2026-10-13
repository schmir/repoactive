# 24. Use jj's native workspace colocation

Status: Accepted

## Context

[ADR 0007](0007-colocate-job-workspaces-for-git-aware-commands.md) colocated
each job workspace by hand, because `jj workspace add` never colocated a new
workspace ([jj#5252](https://github.com/jj-vcs/jj/issues/5252)) and jj
exported git `HEAD` only for the default workspace. repoactive created a git
worktree, moved its `.git` file into the workspace, repaired the paths,
wrote `.jj/.gitignore`, and re-pointed `HEAD` and the index after every
working-copy move (`git_sync_head`).

jj 0.46.0 colocates non-default workspaces itself. `jj workspace add` takes
`--colocate` and `--no-colocate`, creates the git worktree, and keeps git
`HEAD` and the index in step with the workspace's working copy.
`jj workspace forget` also removes the matching git worktree.

## Decision

repoactive requires jj 0.46.0 or newer and passes `--colocate` to
`jj workspace add` for job workspaces of a colocated repository, and
`--no-colocate` for configuration workspaces (ADR 0021) and for repositories
that are not colocated. The manual workaround is deleted:
`_colocate_workspace`, `_target_git_head`, and `git_sync_head` with its call
sites in the runner.

## Consequences

- Job commands still see a working git repository, as ADR 0007 intended, but
  through jj's supported behaviour instead of jj and git internals.
- Workspaces of a colocated repository whose parent is the root commit are
  now colocated too; the manual code skipped them.
- repoactive no longer runs `git worktree prune`. `jj workspace forget`
  removes the git worktree while the workspace directory exists. If a killed
  run's directory is already gone, jj leaves a `prunable` worktree
  registration behind, which `git worktree prune` or `git gc` clears.
- Raising the minimum jj version is a breaking change, noted in the
  CHANGELOG.
