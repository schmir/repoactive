"""Detection of manual (human) commits on a repoactive branch (ADR 0019)."""

from dataclasses import dataclass

from repoactive.jj import JJ, JobCommit, revset_heads


@dataclass
class NoBranch:
    """The bookmark does not exist yet."""


@dataclass
class AlreadyMerged:
    """The bookmark tip is already an ancestor of the run's parents.

    ADR 0019's "empty P..R": a manually merged branch left un-deleted. There
    is nothing to preserve.
    """

    bookmark_change_id: str


@dataclass
class AllPrerequisites:
    """No commit in P..R carries this job's trailer.

    ADR 0019's "no trailer match": the whole range is human commits, treated
    entirely as prerequisites.
    """

    bookmark_change_id: str
    prereq_heads: list[str]

    @property
    def has_human(self) -> bool:
        return bool(self.prereq_heads)


@dataclass
class NormalLayers:
    """The healthy case: exactly one command commit in P..R.

    The branch has its full prerequisites -> command -> fixups layering, with
    this job's command commit anchoring the middle. This is the common shape,
    rewritten in place every run.
    """

    bookmark_change_id: str
    command_commit: JobCommit
    prereq_heads: list[str]
    fixup_roots: list[str]
    run_idempotency_check: bool = False

    @property
    def has_human(self) -> bool:
        return bool(self.prereq_heads or self.fixup_roots)


@dataclass
class UnexpectedLayers:
    """More than one commit in P..R carries this job's trailer.

    ADR 0019's "more than one trailer match": a corrupted branch from a
    botched earlier run. The branch should be frozen and signalled.
    """

    bookmark_change_id: str
    command_commits: list[JobCommit]


BranchShape = NoBranch | AlreadyMerged | AllPrerequisites | NormalLayers | UnexpectedLayers


def classify_branch(*, repo: JJ, bookmark: str, parents: list[str], job_name: str) -> BranchShape:
    """Classify bookmark's commits relative to parents per ADR 0019's branch-layer detection."""
    bookmark_change_id = repo.bookmark_change_id(bookmark)
    if bookmark_change_id is None:
        return NoBranch()

    parents_revset = f"({' | '.join(parents)})"
    branch_revset = f"{parents_revset}..{bookmark_change_id}"
    if repo.revset_is_empty(branch_revset):
        return AlreadyMerged(bookmark_change_id=bookmark_change_id)

    match repo.job_commits_in_revset(branch_revset, job_name):
        case []:
            prereq_heads = repo.heads(f"({parents_revset}..{bookmark_change_id})")
            return AllPrerequisites(
                bookmark_change_id=bookmark_change_id, prereq_heads=prereq_heads
            )
        case [command_commit]:
            cc_cid = command_commit.change_id
            prereq_heads = repo.heads(f"({parents_revset}..{cc_cid}) & ~{cc_cid}")
            fixup_roots = repo.roots(f"{cc_cid}..{bookmark_change_id}")
            rebase_is_noop = set(repo.commit_ids(revset_heads(prereq_heads + parents))) == set(
                repo.commit_ids(f"{cc_cid}-")
            )
            remote_tip = repo.remote_bookmark_commit_id(bookmark)
            matches_remote = remote_tip is not None and repo.commit_ids(bookmark_change_id) == [
                remote_tip
            ]
            return NormalLayers(
                bookmark_change_id=bookmark_change_id,
                command_commit=command_commit,
                prereq_heads=prereq_heads,
                fixup_roots=fixup_roots,
                run_idempotency_check=rebase_is_noop and matches_remote,
            )
        case command_commits:
            return UnexpectedLayers(
                bookmark_change_id=bookmark_change_id,
                command_commits=command_commits,
            )
