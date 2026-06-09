import pytest

from repoactive.platforms.base import parse_repo_from_url


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
        ],
    )
    def test_parses_url(self, url: str, expected: str) -> None:
        assert parse_repo_from_url(url) == expected
