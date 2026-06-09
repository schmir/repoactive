import re
from abc import ABC, abstractmethod
from dataclasses import dataclass


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
    ssh = re.match(r"git@[^:]+:(.+?)(?:\.git)?$", url)
    if ssh:
        return ssh.group(1)
    # HTTPS
    return re.sub(r"https?://[^/]+/", "", url).removesuffix(".git")
