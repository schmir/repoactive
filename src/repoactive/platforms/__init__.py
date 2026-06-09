import os
from pathlib import Path

from repoactive import jj
from repoactive.config import PlatformConfig
from repoactive.platforms.base import Platform, parse_repo_from_url
from repoactive.platforms.github import GitHubPlatform
from repoactive.platforms.gitlab import GitLabPlatform


def get_platform(config: PlatformConfig, repo_path: Path) -> Platform:
    token = os.environ.get(config.token_env)
    if not token:
        raise RuntimeError(
            f"Platform token not set: environment variable '{config.token_env}' is empty"
        )

    repo = config.repo
    if not repo:
        remote_url = jj.get_remote_url(cwd=repo_path)
        repo = parse_repo_from_url(remote_url)

    if config.type == "gitlab":
        return GitLabPlatform(url=config.url, token=token, repo=repo)
    if config.type == "github":
        return GitHubPlatform(url=config.url, token=token, repo=repo)
    raise RuntimeError(f"Unsupported platform type: {config.type!r}")
