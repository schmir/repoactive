import os
from pathlib import Path

from repoactive.config import Config, PlatformConfig
from repoactive.jj import JJ
from repoactive.platforms.base import Platform, extract_host, parse_repo_from_url
from repoactive.platforms.github import GitHubPlatform
from repoactive.platforms.gitlab import GitLabPlatform


def _match_platform(remote_url: str, platforms: list[PlatformConfig]) -> PlatformConfig:
    remote_host = extract_host(remote_url)
    for p in platforms:
        if extract_host(p.url) == remote_host:
            return p
    raise RuntimeError(
        f"No platform configured for remote '{remote_url}' (host: {remote_host!r}); "
        f"add a [[platform]] entry with a matching url"
    )


def get_platform(config: Config, repo_path: Path) -> Platform:
    remote_url = JJ(repo_path).get_remote_url()
    platform_config = _match_platform(remote_url, config.platforms)

    token = os.environ.get(platform_config.token_env)
    if not token:
        raise RuntimeError(
            f"Platform token not set: environment variable '{platform_config.token_env}' is empty"
        )

    repo = parse_repo_from_url(remote_url)
    if platform_config.type == "gitlab":
        return GitLabPlatform(url=platform_config.url, token=token, repo=repo)
    if platform_config.type == "github":
        return GitHubPlatform(url=platform_config.url, token=token, repo=repo)
    raise RuntimeError(f"Unsupported platform type: {platform_config.type!r}")
