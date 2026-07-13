"""GitLab platform implementation using python-gitlab."""

import time

import gitlab
from gitlab.exceptions import GitlabError, GitlabMRClosedError
from gitlab.v4.objects import ProjectMergeRequest

from repoactive.platforms.base import MRParams, Platform, PlatformError

_DRAFT_PREFIX = "Draft: "

# detailed_merge_status / merge_status values that mean GitLab's async
# mergeability check hasn't finished yet. On GitLab without the auto_merge
# parameter (< 17.11), setting auto-merge while the check is pending returns
# 422 "Branch cannot be merged".
_MERGE_CHECK_PENDING = frozenset({"unchecked", "checking", "preparing"})

# Fallback for GitLab without the auto_merge parameter: a freshly created MR is
# not immediately ready for merge_when_pipeline_succeeds because GitLab runs
# the mergeability check and creates the MR pipeline in the background. Poll
# for that to settle, bounded by this timeout; the next run re-attempts if it
# never does.
_AUTO_MERGE_TIMEOUT = 60.0
_AUTO_MERGE_POLL_INTERVAL = 3.0


def _mr_title(title: str, *, draft: bool) -> str:
    return f"{_DRAFT_PREFIX}{title}" if draft else title


def _ready_for_auto_merge(mr: ProjectMergeRequest) -> bool:
    """Report whether GitLab can accept merge_when_pipeline_succeeds for ``mr``.

    Two conditions must hold. The mergeability check must be done: setting the
    flag while detailed_merge_status is still ``checking``/``unchecked``/
    ``preparing`` returns 422 "Branch cannot be merged". And a pipeline must
    exist: with none, merge_when_pipeline_succeeds has nothing to wait for and
    GitLab merges immediately, defeating the point of auto-merge.
    """
    status = getattr(mr, "detailed_merge_status", None) or getattr(mr, "merge_status", None)
    if status in _MERGE_CHECK_PENDING:
        return False
    return getattr(mr, "head_pipeline", None) is not None


class GitLabPlatform(Platform):
    def __init__(self, *, url: str | None, token: str, repo: str) -> None:
        self._gl = gitlab.Gitlab(url or "https://gitlab.com", private_token=token)
        try:
            self._project = self._gl.projects.get(repo)
        except GitlabError as e:
            raise PlatformError("GitLab", repo, e) from e

    def default_branch(self) -> str:
        return self._project.default_branch

    def ensure_mr(self, params: MRParams) -> str:
        title = _mr_title(params.title, draft=params.draft)
        existing = self._project.mergerequests.list(
            source_branch=params.source_branch,
            state="opened",
            iterator=False,
        )
        if existing:
            mr = existing[0]
            mr.title = title
            mr.description = params.description
            mr.labels = params.labels
            # Retarget in case the job's base_branch changed since the MR was
            # created.
            mr.target_branch = params.target_branch
            mr.save()
        else:
            mr = self._project.mergerequests.create(
                {
                    "source_branch": params.source_branch,
                    "target_branch": params.target_branch,
                    "title": title,
                    "description": params.description,
                    "labels": params.labels,
                }
            )
        web_url = mr.web_url
        if params.required_approvals is not None:
            try:
                approvals = mr.approvals.get()
                approvals.approvals_required = params.required_approvals
                approvals.save()
            except GitlabError as e:
                print(f"  warning: could not set required approvals ({e})")
        if params.auto_merge:
            self._enable_auto_merge(mr)
        return web_url

    def _enable_auto_merge(self, mr: ProjectMergeRequest) -> None:
        """Enable auto-merge on ``mr``, retrying until it is accepted.

        The merge call sends both auto-merge parameters: on GitLab >= 17.11
        ``auto_merge`` selects the "merge when checks pass" strategy and older
        GitLab ignores it and honors the deprecated
        merge_when_pipeline_succeeds. Right after a push GitLab rejects the
        call (405/422) while its state is still settling - the mergeability
        check runs, the MR pipeline gets created and linked - so a rejection
        is retried until the deadline, waiting in between for the MR to look
        ready (re-fetched, since list/create payloads omit head_pipeline).
        That readiness gate keeps the old-GitLab semantics: retrying without a
        pipeline there would merge immediately and skip CI. A repo with no CI
        never grows a pipeline; there the wait times out and one final call
        merges immediately, the best available behavior.
        """
        iid = mr.iid
        deadline = time.monotonic() + _AUTO_MERGE_TIMEOUT
        final_attempt = False
        while True:
            try:
                mr.merge(merge_when_pipeline_succeeds=True, auto_merge=True)
                return
            except GitlabMRClosedError as e:
                # python-gitlab wraps every merge failure in
                # GitlabMRClosedError; this is usually the 422/405 "not ready
                # yet" case, not a closed MR.
                if final_attempt:
                    print(f"  warning: could not enable auto-merge ({e})")
                    return
            time.sleep(_AUTO_MERGE_POLL_INTERVAL)
            while True:
                mr = self._project.mergerequests.get(iid)
                if _ready_for_auto_merge(mr) or time.monotonic() >= deadline:
                    break
                time.sleep(_AUTO_MERGE_POLL_INTERVAL)
            final_attempt = time.monotonic() >= deadline
