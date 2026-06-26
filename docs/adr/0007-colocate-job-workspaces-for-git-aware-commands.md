# 7. Colocate job workspaces so commands get a working git repository

Status: Accepted

## Context

Each job runs in a fresh temporary jj workspace (`JJ.temp_workspace` →
`workspace_add`). repoactive drives that workspace entirely through the `jj`
CLI — `new`, `edit`, `rebase`, `describe`, `abandon`, diff inspection — and
for its own operations it has no need of git inside the workspace at all.

The job's `command`, however, is arbitrary user code (see
[ADR 0006](0006-job-commands-are-trusted.md)), and a lot of ordinary tooling
expects to run inside a real git repository:

- `uv` / `hatch-vcs` / `setuptools-scm` dynamic versioning, which derive the
  version from `git describe`;
- anything calling `git ls-files`, `git rev-parse`, `git status`,
  `.git`-aware build steps, pre-commit hooks, linters that skip ignored
  files, etc.

If the workspace were jj-only, all of these would fail with "not a git
repository" even though the surrounding project is a colocated jj+git repo.
The command runs against the working tree; from its point of view it should
look exactly like a normal checkout.

The obstacle is that **`jj workspace add` does not colocate the new
workspace**, even when the main repository is colocated
([jj#5252](https://github.com/jj-vcs/jj/issues/5252)): it creates the `.jj`
machinery but no `.git`, so git commands inside the workspace have nothing
to work with. jj also only exports git `HEAD` for the _default_ workspace,
so even once a `.git` exists it does not move as the working copy moves.

## Decision

repoactive manually colocates each job workspace so that **job commands see
a functioning git repository**. This is done for the commands' benefit, not
repoactive's own — repoactive would work fine without it.

`JJ._colocate_workspace` (called from `workspace_add` when the main repo is
colocated) works around jj#5252 by hand:

1. Create a git worktree of the colocated repo next to the new workspace
   (both `git worktree add` and `jj workspace add` refuse a non-empty
   directory, so it is built in a temp dir) and move its `.git` file into
   the workspace.
2. `git worktree repair` the paths, write the `.jj/.gitignore` jj normally
   writes for a colocated repo (but omits for workspaces), and `git reset`
   the index to the head jj already checked out.

Because jj does not move git `HEAD` for non-default workspaces,
`JJ.git_sync_head` re-points the colocated `HEAD`/index after every
working-copy move (`new`, `edit`, `rebase`) so the git view stays consistent
with the jj working copy the command sees.

## Consequences

- **Git-aware commands just work.** uv dynamic versioning, `git ls-files`,
  and any other tooling that needs `.git` behave inside a job exactly as in
  a normal checkout. This is the whole point of the feature.
- **It reaches into jj/git internals that are not a stable contract.**
  Moving a worktree's `.git` file, repairing it, and synthesising
  `.jj/.gitignore` depend on current jj/git layout. A future jj or git
  change could break it; the workaround is well-commented and isolated in
  `_colocate_workspace` / `git_sync_head` to contain the blast radius.
- **It is temporary by intent.** If/when jj#5252 is fixed so
  `jj workspace add` colocates directly, `_colocate_workspace` should be
  deleted in favour of jj's native behaviour (and `git_sync_head`
  re-evaluated). Worth re-checking the upstream issue periodically.
- **Cleanup must track the extra worktree.** Each colocated workspace also
  registers a git worktree, so teardown prunes it (`git_worktree_prune`) in
  addition to forgetting the jj workspace; `forget_stale_workspaces` does
  the same for workspaces a killed run left behind.
- **Scope: colocated repos only.** repoactive already requires the target to
  be a colocated jj+git repository (`require_colocated_repo`), so there is
  no jj-only case to support here.
