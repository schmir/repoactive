import gitlab

from repoactive.platforms.base import MRParams, Platform

_DRAFT_PREFIX = "Draft: "


def _mr_title(title: str, *, draft: bool) -> str:
    return f"{_DRAFT_PREFIX}{title}" if draft else title


class GitLabPlatform(Platform):
    def __init__(self, *, url: str | None, token: str, repo: str) -> None:
        self._gl = gitlab.Gitlab(url or "https://gitlab.com", private_token=token)
        self._project = self._gl.projects.get(repo)

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
        return mr.web_url
