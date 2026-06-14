from __future__ import annotations

from pathlib import Path
from typing import Literal
from unittest.mock import MagicMock, patch

import pytest

from repoactive.config import Config, PlatformConfig, load_config
from repoactive.platforms import _match_platform, get_platform
from repoactive.platforms.base import extract_host, parse_repo_from_url
from repoactive.platforms.github import GitHubPlatform


class TestParseRepoFromUrl:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://gitlab.com/namespace/project.git", "namespace/project"),
            ("https://gitlab.com/namespace/project", "namespace/project"),
            ("https://gitlab.example.com/group/subgroup/repo.git", "group/subgroup/repo"),
            ("git@gitlab.com:namespace/project.git", "namespace/project"),
            ("git@gitlab.com:namespace/project", "namespace/project"),
            ("git@github.com:owner/repo.git", "owner/repo"),
            ("ssh://git@github.com/owner/repo.git", "owner/repo"),
            ("ssh://git@gitlab.example.com/group/subgroup/repo.git", "group/subgroup/repo"),
        ],
    )
    def test_parses_url(self, url: str, expected: str) -> None:
        assert parse_repo_from_url(url) == expected


class TestExtractHost:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://gitlab.com/namespace/project.git", "gitlab.com"),
            ("git@github.com:owner/repo.git", "github.com"),
            ("ssh://git@github.com/owner/repo.git", "github.com"),
            ("ssh://git@gitlab.example.com/group/repo.git", "gitlab.example.com"),
        ],
    )
    def test_extracts_host(self, url: str, expected: str) -> None:
        assert extract_host(url) == expected


class TestMatchPlatform:
    def _p(
        self, url: str, platform_type: Literal["gitlab", "github"] = "github"
    ) -> PlatformConfig:
        return PlatformConfig(url=url, type=platform_type, token_env="TOKEN")

    def test_matches_by_url(self) -> None:
        p = self._p("https://github.com")
        assert _match_platform("git@github.com:owner/repo.git", [p]) is p

    def test_matches_custom_url(self) -> None:
        p = self._p("https://gitlab.example.com", "gitlab")
        assert _match_platform("git@gitlab.example.com:ns/repo.git", [p]) is p

    def test_selects_correct_platform_from_list(self) -> None:
        gh = self._p("https://github.com")
        gl = self._p("https://gitlab.com", "gitlab")
        assert _match_platform("git@github.com:owner/repo.git", [gh, gl]) is gh
        assert _match_platform("git@gitlab.com:ns/repo.git", [gh, gl]) is gl

    def test_no_match_raises(self) -> None:
        p = self._p("https://github.com")
        with pytest.raises(RuntimeError, match="No platform configured"):
            _match_platform("git@gitlab.example.com:ns/repo.git", [p])

    def test_default_platforms_match_github(self, tmp_path: Path) -> None:
        f = tmp_path / "c.toml"
        f.write_text("")
        cfg = load_config([f])
        p = _match_platform("git@github.com:owner/repo.git", cfg.platforms)
        assert p.url == "https://github.com"
        assert p.token_env == "GITHUB_TOKEN"

    def test_default_platforms_match_gitlab(self, tmp_path: Path) -> None:
        f = tmp_path / "c.toml"
        f.write_text("")
        cfg = load_config([f])
        p = _match_platform("https://gitlab.com/ns/repo.git", cfg.platforms)
        assert p.url == "https://gitlab.com"
        assert p.token_env == "GITLAB_TOKEN"


class TestGitHubPlatformInit:
    @patch("repoactive.platforms.github.Github")
    def test_public_github_uses_default_api_url(self, mock_github: MagicMock) -> None:
        GitHubPlatform(url="https://github.com", token="tok", repo="owner/repo")
        mock_github.assert_called_once_with("tok", base_url="https://api.github.com")

    @patch("repoactive.platforms.github.Github")
    def test_public_github_with_trailing_slash(self, mock_github: MagicMock) -> None:
        GitHubPlatform(url="https://github.com/", token="tok", repo="owner/repo")
        mock_github.assert_called_once_with("tok", base_url="https://api.github.com")

    @patch("repoactive.platforms.github.Github")
    def test_ghe_url_uses_api_v3(self, mock_github: MagicMock) -> None:
        GitHubPlatform(url="https://github.example.com", token="tok", repo="owner/repo")
        mock_github.assert_called_once_with("tok", base_url="https://github.example.com/api/v3")

    @patch("repoactive.platforms.github.Github")
    def test_none_url_uses_default_api_url(self, mock_github: MagicMock) -> None:
        GitHubPlatform(url=None, token="tok", repo="owner/repo")
        mock_github.assert_called_once_with("tok", base_url="https://api.github.com")


REPO = Path("/repo")


def _config(*platform_dicts: dict) -> Config:
    return Config.model_validate({"platform": list(platform_dicts)})


class TestGetPlatform:
    def _github_config(self) -> Config:
        return _config({"url": "https://github.com", "type": "github", "token_env": "GH_TOKEN"})

    def _gitlab_config(self) -> Config:
        return _config({"url": "https://gitlab.com", "type": "gitlab", "token_env": "GL_TOKEN"})

    @patch("repoactive.platforms.GitHubPlatform")
    @patch("repoactive.platforms.JJ")
    def test_returns_github_platform(
        self, mock_jj: MagicMock, mock_gh: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_jj.return_value.get_remote_url.return_value = "https://github.com/owner/repo.git"
        monkeypatch.setenv("GH_TOKEN", "ghtoken")

        result = get_platform(self._github_config(), REPO)

        mock_gh.assert_called_once_with(
            url="https://github.com", token="ghtoken", repo="owner/repo"
        )
        assert result is mock_gh.return_value

    @patch("repoactive.platforms.GitLabPlatform")
    @patch("repoactive.platforms.JJ")
    def test_returns_gitlab_platform(
        self, mock_jj: MagicMock, mock_gl: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_jj.return_value.get_remote_url.return_value = "https://gitlab.com/ns/repo.git"
        monkeypatch.setenv("GL_TOKEN", "gltoken")

        result = get_platform(self._gitlab_config(), REPO)

        mock_gl.assert_called_once_with(url="https://gitlab.com", token="gltoken", repo="ns/repo")
        assert result is mock_gl.return_value

    @patch("repoactive.platforms.JJ")
    def test_missing_token_raises(
        self, mock_jj: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_jj.return_value.get_remote_url.return_value = "https://github.com/owner/repo.git"
        monkeypatch.delenv("GH_TOKEN", raising=False)

        with pytest.raises(RuntimeError, match="GH_TOKEN"):
            get_platform(self._github_config(), REPO)

    @patch("repoactive.platforms.JJ")
    def test_no_matching_platform_raises(
        self, mock_jj: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_jj.return_value.get_remote_url.return_value = "https://bitbucket.org/owner/repo.git"
        monkeypatch.setenv("GH_TOKEN", "tok")

        with pytest.raises(RuntimeError, match="No platform configured"):
            get_platform(self._github_config(), REPO)

    @patch("repoactive.platforms.GitHubPlatform")
    @patch("repoactive.platforms.JJ")
    def test_repo_path_forwarded_to_jj(
        self, mock_jj: MagicMock, mock_gh: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_jj.return_value.get_remote_url.return_value = "https://github.com/owner/repo.git"
        monkeypatch.setenv("GH_TOKEN", "tok")

        get_platform(self._github_config(), REPO)

        mock_jj.assert_called_once_with(REPO)

    @patch("repoactive.platforms.GitHubPlatform")
    @patch("repoactive.platforms.JJ")
    def test_ssh_remote_url_parsed(
        self, mock_jj: MagicMock, mock_gh: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_jj.return_value.get_remote_url.return_value = "git@github.com:owner/repo.git"
        monkeypatch.setenv("GH_TOKEN", "tok")

        get_platform(self._github_config(), REPO)

        mock_gh.assert_called_once_with(url="https://github.com", token="tok", repo="owner/repo")
