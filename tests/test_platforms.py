"""Tests for platform selection and MR parameter handling."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from unittest.mock import MagicMock, patch

import pytest
from github import GithubException
from gitlab.exceptions import GitlabAuthenticationError, GitlabMRClosedError

from repoactive.config import Config, PlatformConfig, load_config
from repoactive.platforms import (
    NoPlatformConfiguredError,
    PlatformTokenNotSetError,
    _match_platform,
    get_platform,
)
from repoactive.platforms.base import MRParams, PlatformError, extract_host, parse_repo_from_url
from repoactive.platforms.github import GitHubPlatform
from repoactive.platforms.gitlab import GitLabPlatform


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
        with pytest.raises(NoPlatformConfiguredError, match="no platform configured"):
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

    @patch("repoactive.platforms.github.Github")
    def test_inaccessible_repo_raises_platform_error(self, mock_github: MagicMock) -> None:
        # A rejected token or unknown repo surfaces as a PlatformError, not a
        # raw PyGithub exception.
        mock_github.return_value.get_repo.side_effect = GithubException(
            401, {"message": "Bad credentials"}, {}
        )
        with pytest.raises(PlatformError, match="cannot access repository 'owner/repo'"):
            GitHubPlatform(url=None, token="bad", repo="owner/repo")


class TestGitLabPlatformInit:
    @patch("repoactive.platforms.gitlab.gitlab")
    def test_inaccessible_project_raises_platform_error(self, mock_gitlab: MagicMock) -> None:
        mock_gitlab.Gitlab.return_value.projects.get.side_effect = GitlabAuthenticationError(
            "401: invalid token"
        )
        with pytest.raises(PlatformError, match="cannot access repository 'ns/repo'"):
            GitLabPlatform(url=None, token="bad", repo="ns/repo")


def _mr_params(**overrides: object) -> MRParams:
    defaults: dict = {
        "source_branch": "repoactive/a",
        "target_branch": "develop",
        "title": "Change a",
        "description": "desc",
        "labels": ["auto"],
        "draft": False,
    }
    defaults.update(overrides)
    return MRParams(**defaults)


class TestGitHubEnsureMR:
    def _platform(self, mock_github: MagicMock) -> tuple[GitHubPlatform, MagicMock]:
        repo = mock_github.return_value.get_repo.return_value
        repo.owner.login = "owner"
        return GitHubPlatform(url=None, token="tok", repo="owner/repo"), repo

    @patch("repoactive.platforms.github.Github")
    def test_updates_and_retargets_existing_pr(self, mock_github: MagicMock) -> None:
        platform, repo = self._platform(mock_github)
        pr = MagicMock()
        pr.html_url = "https://example.com/pull/1"
        repo.get_pulls.return_value = [pr]

        url = platform.ensure_mr(_mr_params())

        # Looked up by head only - a base filter would miss the PR after the
        # job's base_branch changed.
        repo.get_pulls.assert_called_once_with(state="open", head="owner:repoactive/a")
        pr.edit.assert_called_once_with(title="Change a", body="desc", base="develop")
        pr.set_labels.assert_called_once_with("auto")
        repo.create_pull.assert_not_called()
        assert url == "https://example.com/pull/1"

    @patch("repoactive.platforms.github.Github")
    def test_creates_pr_when_none_open(self, mock_github: MagicMock) -> None:
        platform, repo = self._platform(mock_github)
        repo.get_pulls.return_value = []
        repo.create_pull.return_value.html_url = "https://example.com/pull/2"

        url = platform.ensure_mr(_mr_params(draft=True))

        repo.create_pull.assert_called_once_with(
            title="Change a",
            body="desc",
            head="repoactive/a",
            base="develop",
            draft=True,
        )
        repo.create_pull.return_value.set_labels.assert_called_once_with("auto")
        assert url == "https://example.com/pull/2"

    @patch("repoactive.platforms.github.Github")
    def test_create_without_labels_skips_set_labels(self, mock_github: MagicMock) -> None:
        platform, repo = self._platform(mock_github)
        repo.get_pulls.return_value = []

        platform.ensure_mr(_mr_params(labels=[]))

        repo.create_pull.return_value.set_labels.assert_not_called()

    @patch("repoactive.platforms.github.Github")
    def test_auto_merge_calls_enable_automerge_on_existing(self, mock_github: MagicMock) -> None:
        platform, repo = self._platform(mock_github)
        pr = MagicMock()
        pr.html_url = "https://example.com/pull/1"
        repo.get_pulls.return_value = [pr]

        platform.ensure_mr(_mr_params(auto_merge=True))

        pr.enable_automerge.assert_called_once_with()

    @patch("repoactive.platforms.github.Github")
    def test_auto_merge_calls_enable_automerge_on_new(self, mock_github: MagicMock) -> None:
        platform, repo = self._platform(mock_github)
        repo.get_pulls.return_value = []
        pr = repo.create_pull.return_value
        pr.html_url = "https://example.com/pull/2"

        platform.ensure_mr(_mr_params(auto_merge=True))

        pr.enable_automerge.assert_called_once_with()

    @patch("repoactive.platforms.github.Github")
    def test_no_auto_merge_skips_enable_automerge(self, mock_github: MagicMock) -> None:
        platform, repo = self._platform(mock_github)
        pr = MagicMock()
        pr.html_url = "https://example.com/pull/1"
        repo.get_pulls.return_value = [pr]

        platform.ensure_mr(_mr_params(auto_merge=False))

        pr.enable_automerge.assert_not_called()

    @patch("repoactive.platforms.github.Github")
    def test_auto_merge_failure_warns_and_returns_url(
        self, mock_github: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # enable_automerge() raises when the repo doesn't allow auto-merge or
        # the PR isn't mergeable. The URL is already known at that point, so
        # the error must not propagate — it should warn and return the URL so
        # subsequent MRs in the same run are not aborted.
        platform, repo = self._platform(mock_github)
        pr = MagicMock()
        pr.html_url = "https://example.com/pull/1"
        repo.get_pulls.return_value = [pr]
        pr.enable_automerge.side_effect = GithubException(405, "not allowed")

        url = platform.ensure_mr(_mr_params(auto_merge=True))

        assert url == "https://example.com/pull/1"
        assert "warning" in capsys.readouterr().out


class TestGitLabEnsureMR:
    def _platform(self, mock_gitlab: MagicMock) -> tuple[GitLabPlatform, MagicMock]:
        project = mock_gitlab.Gitlab.return_value.projects.get.return_value
        return GitLabPlatform(url=None, token="tok", repo="ns/repo"), project

    @patch("repoactive.platforms.gitlab.gitlab")
    def test_updates_and_retargets_existing_mr(self, mock_gitlab: MagicMock) -> None:
        platform, project = self._platform(mock_gitlab)
        mr = MagicMock()
        mr.web_url = "https://example.com/mr/1"
        project.mergerequests.list.return_value = [mr]

        url = platform.ensure_mr(_mr_params())

        project.mergerequests.list.assert_called_once_with(
            source_branch="repoactive/a", state="opened", iterator=False
        )
        assert mr.title == "Change a"
        assert mr.description == "desc"
        assert mr.labels == ["auto"]
        # Retargeted in case the job's base_branch changed since creation.
        assert mr.target_branch == "develop"
        mr.save.assert_called_once_with()
        project.mergerequests.create.assert_not_called()
        assert url == "https://example.com/mr/1"

    @patch("repoactive.platforms.gitlab.gitlab")
    def test_draft_prefixes_title_on_update(self, mock_gitlab: MagicMock) -> None:
        platform, project = self._platform(mock_gitlab)
        mr = MagicMock()
        project.mergerequests.list.return_value = [mr]

        platform.ensure_mr(_mr_params(draft=True))

        assert mr.title == "Draft: Change a"

    @patch("repoactive.platforms.gitlab.gitlab")
    def test_creates_mr_when_none_open(self, mock_gitlab: MagicMock) -> None:
        platform, project = self._platform(mock_gitlab)
        project.mergerequests.list.return_value = []
        project.mergerequests.create.return_value.web_url = "https://example.com/mr/2"

        url = platform.ensure_mr(_mr_params())

        project.mergerequests.create.assert_called_once_with(
            {
                "source_branch": "repoactive/a",
                "target_branch": "develop",
                "title": "Change a",
                "description": "desc",
                "labels": ["auto"],
            }
        )
        assert url == "https://example.com/mr/2"

    @staticmethod
    def _ready_mr() -> MagicMock:
        """Build an MR whose mergeability check is done and that has a pipeline."""
        mr = MagicMock()
        mr.detailed_merge_status = "ci_still_running"
        mr.head_pipeline = {"id": 1}
        return mr

    @patch("repoactive.platforms.gitlab.gitlab")
    def test_auto_merge_calls_merge_on_existing(self, mock_gitlab: MagicMock) -> None:
        platform, project = self._platform(mock_gitlab)
        mr = MagicMock()
        mr.web_url = "https://example.com/mr/1"
        project.mergerequests.list.return_value = [mr]
        # ensure_mr re-fetches the MR to read its merge status and pipeline.
        project.mergerequests.get.return_value = self._ready_mr()

        platform.ensure_mr(_mr_params(auto_merge=True))

        project.mergerequests.get.return_value.merge.assert_called_once_with(
            merge_when_pipeline_succeeds=True
        )

    @patch("repoactive.platforms.gitlab.gitlab")
    def test_auto_merge_calls_merge_on_new(self, mock_gitlab: MagicMock) -> None:
        platform, project = self._platform(mock_gitlab)
        project.mergerequests.list.return_value = []
        new_mr = project.mergerequests.create.return_value
        new_mr.web_url = "https://example.com/mr/2"
        project.mergerequests.get.return_value = self._ready_mr()

        platform.ensure_mr(_mr_params(auto_merge=True))

        project.mergerequests.get.return_value.merge.assert_called_once_with(
            merge_when_pipeline_succeeds=True
        )

    @patch("repoactive.platforms.gitlab.time.sleep")
    @patch("repoactive.platforms.gitlab.gitlab")
    def test_auto_merge_waits_for_check_and_pipeline(
        self, mock_gitlab: MagicMock, mock_sleep: MagicMock
    ) -> None:
        platform, project = self._platform(mock_gitlab)
        project.mergerequests.list.return_value = []
        new_mr = project.mergerequests.create.return_value
        new_mr.iid = 7
        # First re-fetch: mergeability check still running, no pipeline yet.
        pending = MagicMock()
        pending.detailed_merge_status = "checking"
        pending.head_pipeline = None
        # Second re-fetch: check done and a pipeline exists.
        ready = self._ready_mr()
        project.mergerequests.get.side_effect = [pending, ready]

        platform.ensure_mr(_mr_params(auto_merge=True))

        project.mergerequests.get.assert_called_with(7)
        pending.merge.assert_not_called()
        ready.merge.assert_called_once_with(merge_when_pipeline_succeeds=True)
        mock_sleep.assert_called_once_with(3.0)

    @patch("repoactive.platforms.gitlab._AUTO_MERGE_TIMEOUT", 0.0)
    @patch("repoactive.platforms.gitlab.time.sleep")
    @patch("repoactive.platforms.gitlab.gitlab")
    def test_auto_merge_no_pipeline_merges_after_timeout(
        self, mock_gitlab: MagicMock, mock_sleep: MagicMock
    ) -> None:
        platform, project = self._platform(mock_gitlab)
        project.mergerequests.list.return_value = []
        # Repo has no CI: a pipeline never appears. The poll times out and the
        # merge call falls back to an immediate merge.
        no_ci = MagicMock()
        no_ci.detailed_merge_status = "mergeable"
        no_ci.head_pipeline = None
        project.mergerequests.get.return_value = no_ci

        platform.ensure_mr(_mr_params(auto_merge=True))

        no_ci.merge.assert_called_once_with(merge_when_pipeline_succeeds=True)
        mock_sleep.assert_not_called()

    @patch("repoactive.platforms.gitlab.gitlab")
    def test_auto_merge_failure_warns(
        self, mock_gitlab: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        platform, project = self._platform(mock_gitlab)
        project.mergerequests.list.return_value = [MagicMock()]
        ready = self._ready_mr()
        ready.merge.side_effect = GitlabMRClosedError("Branch cannot be merged")
        project.mergerequests.get.return_value = ready

        platform.ensure_mr(_mr_params(auto_merge=True))

        assert "could not enable auto-merge" in capsys.readouterr().out

    @patch("repoactive.platforms.gitlab.gitlab")
    def test_no_auto_merge_skips_merge_call(self, mock_gitlab: MagicMock) -> None:
        platform, project = self._platform(mock_gitlab)
        mr = MagicMock()
        mr.web_url = "https://example.com/mr/1"
        project.mergerequests.list.return_value = [mr]

        platform.ensure_mr(_mr_params(auto_merge=False))

        mr.merge.assert_not_called()
        project.mergerequests.get.assert_not_called()


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

        with pytest.raises(PlatformTokenNotSetError, match="GH_TOKEN"):
            get_platform(self._github_config(), REPO)

    @patch("repoactive.platforms.JJ")
    def test_no_matching_platform_raises(
        self, mock_jj: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_jj.return_value.get_remote_url.return_value = "https://bitbucket.org/owner/repo.git"
        monkeypatch.setenv("GH_TOKEN", "tok")

        with pytest.raises(NoPlatformConfiguredError, match="no platform configured"):
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
