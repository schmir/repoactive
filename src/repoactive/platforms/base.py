from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass


class PlatformError(Exception):
    """Raised when the platform API rejects a request (bad token, unknown
    repository, insufficient permissions), wrapping the client library's own
    exception so callers need not know about PyGithub/python-gitlab types."""

    def __init__(self, platform: str, repo: str, error: Exception) -> None:
        super().__init__(f"{platform}: cannot access repository {repo!r}: {error}")


@dataclass
class MRParams:
    source_branch: str
    target_branch: str
    title: str
    description: str
    labels: list[str]
    draft: bool


class Platform(ABC):
    @abstractmethod
    def default_branch(self) -> str:
        """Return the repository's default branch name."""

    @abstractmethod
    def ensure_mr(self, params: MRParams) -> str:
        """Create or update an MR/PR. Returns the MR/PR URL."""


def parse_repo_from_url(url: str) -> str:
    """Extract 'namespace/repo' from an HTTPS or SSH git remote URL."""
    scp_match = re.match(r"git@[^:]+:(.+?)(?:\.git)?$", url)
    if scp_match:
        return scp_match.group(1)
    # ssh:// and https:// share a 'scheme://host/path' shape.
    return re.sub(r"[a-z]+://[^/]+/", "", url).removesuffix(".git")


def extract_host(url: str) -> str:
    """Extract hostname from an SSH or HTTPS URL."""
    scp_match = re.match(r"git@([^:]+):", url)
    if scp_match:
        return scp_match.group(1)
    # ssh://git@github.com/owner/repo or https://github.com/owner/repo
    host_match = re.match(r"[a-z]+://(?:[^@/]+@)?([^/]+)", url)
    return host_match.group(1) if host_match else ""
