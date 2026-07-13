"""GitHub platform implementation using PyGithub."""

from github import Github, GithubException

from repoactive.platforms.base import MRParams, Platform, PlatformError

_DEFAULT_API_URL = "https://api.github.com"
_PUBLIC_GITHUB_URL = "https://github.com"


class RequiredApprovalsNotSupportedError(Exception):
    """Raised when a job sets required_approvals but its platform is GitHub.

    GitHub has no per-PR approval requirement: the number of required
    approvals is a repository-wide branch protection setting, so repoactive
    cannot honor a per-job value there. required_approvals is GitLab-only.
    """

    def __init__(self) -> None:
        super().__init__(
            "required_approvals is not supported on GitHub: the number of required "
            "approvals is a repository-wide branch protection setting, not a per-PR "
            "value. Remove required_approvals from the job (it is supported on GitLab only)."
        )


class GitHubPlatform(Platform):
    def __init__(self, *, url: str | None, token: str, repo: str) -> None:
        normalized = (url or "").rstrip("/")
        base_url = (
            normalized + "/api/v3"
            if normalized and normalized != _PUBLIC_GITHUB_URL
            else _DEFAULT_API_URL
        )
        self._gh = Github(token, base_url=base_url)
        try:
            self._repo = self._gh.get_repo(repo)
        except GithubException as e:
            raise PlatformError("GitHub", repo, e) from e

    def default_branch(self) -> str:
        return self._repo.default_branch

    def ensure_mr(self, params: MRParams) -> str:
        if params.required_approvals is not None:
            raise RequiredApprovalsNotSupportedError
        owner = self._repo.owner.login
        # Look up by head only: filtering on base would miss the PR after the
        # job's base_branch changed, and a new PR would be opened next to the
        # stale one. The edit below retargets the base instead. (GitHub allows
        # several open PRs from one head to different bases; the first match is
        # updated.)
        existing = list(
            self._repo.get_pulls(
                state="open",
                head=f"{owner}:{params.source_branch}",
            )
        )
        if existing:
            pr = existing[0]
            pr.edit(
                title=params.title,
                body=params.description,
                base=params.target_branch,
                # PyGithub does not support converting to/from draft via edit;
                # draft state can only be set at creation time.
            )
            # Labels are set separately
            pr.set_labels(*params.labels)
        else:
            pr = self._repo.create_pull(
                title=params.title,
                body=params.description,
                head=params.source_branch,
                base=params.target_branch,
                draft=params.draft,
            )
            if params.labels:
                pr.set_labels(*params.labels)
        if params.auto_merge:
            try:
                pr.enable_automerge()
            except GithubException as e:
                print(f"  warning: could not enable auto-merge ({e})")
        return pr.html_url
