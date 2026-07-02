from github import Github

from repoactive.platforms.base import MRParams, Platform

_DEFAULT_API_URL = "https://api.github.com"
_PUBLIC_GITHUB_URL = "https://github.com"


class GitHubPlatform(Platform):
    def __init__(self, *, url: str | None, token: str, repo: str) -> None:
        normalized = (url or "").rstrip("/")
        base_url = (
            normalized + "/api/v3"
            if normalized and normalized != _PUBLIC_GITHUB_URL
            else _DEFAULT_API_URL
        )
        self._gh = Github(token, base_url=base_url)
        self._repo = self._gh.get_repo(repo)

    def default_branch(self) -> str:
        return self._repo.default_branch

    def ensure_mr(self, params: MRParams) -> str:
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
        return pr.html_url
