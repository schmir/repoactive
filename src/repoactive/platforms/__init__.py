import os
from pathlib import Path
from typing import assert_never

from repoactive.config import Config, PlatformConfig
from repoactive.jj import JJ
from repoactive.platforms.base import Platform, extract_host, parse_repo_from_url
from repoactive.platforms.github import GitHubPlatform
from repoactive.platforms.gitlab import GitLabPlatform


class NoPlatformConfiguredError(RuntimeError):
    """Raised when no configured platform matches the git remote's host."""

    def __init__(self, remote_url: str, remote_host: str) -> None:
        super().__init__(
            f"no platform configured for remote '{remote_url}' (host: {remote_host!r}); "
            f"add a [platform.<name>] entry with a matching url"
        )


class PlatformTokenNotSetError(RuntimeError):
    """Raised when the platform token environment variable is unset or empty."""

    def __init__(self, token_env: str) -> None:
        super().__init__(f"platform token not set: environment variable '{token_env}' is empty")


def _match_platform(remote_url: str, platforms: list[PlatformConfig]) -> PlatformConfig:
    remote_host = extract_host(remote_url)
    for p in platforms:
        if extract_host(p.url) == remote_host:
            return p
    raise NoPlatformConfiguredError(remote_url, remote_host)


def get_platform(config: Config, repo_path: Path) -> Platform:
    remote_url = JJ(repo_path).get_remote_url()
    platform_config = _match_platform(remote_url, config.platforms)

    token = os.environ.get(platform_config.token_env)
    if not token:
        raise PlatformTokenNotSetError(platform_config.token_env)

    repo = parse_repo_from_url(remote_url)
    match platform_config.type:
        case "gitlab":
            return GitLabPlatform(url=platform_config.url, token=token, repo=repo)
        case "github":
            return GitHubPlatform(url=platform_config.url, token=token, repo=repo)
        case _:
            assert_never(platform_config.type)
