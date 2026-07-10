"""Tests for git trailer stripping."""

from repoactive.trailers import strip_trailers


class TestStripTrailers:
    def test_single_trailer_is_removed(self) -> None:
        assert strip_trailers("Subject\n\nRepoactive-Job: a") == "Subject"

    def test_multiple_trailers_are_removed(self) -> None:
        message = "Subject\n\nRepoactive-Job: a\nSigned-off-by: Someone <s@example.com>"
        assert strip_trailers(message) == "Subject"

    def test_body_is_kept(self) -> None:
        message = "Subject\n\nA description paragraph.\n\nRepoactive-Job: a"
        assert strip_trailers(message) == "Subject\n\nA description paragraph."

    def test_folded_continuation_is_removed(self) -> None:
        message = "Subject\n\nAcked-by: A\n  continued value"
        assert strip_trailers(message) == "Subject"

    def test_empty_value_trailer_is_removed(self) -> None:
        assert strip_trailers("Subject\n\nFixes:") == "Subject"

    def test_message_without_trailers_is_unchanged(self) -> None:
        message = "Subject\n\nJust a description with no trailers."
        assert strip_trailers(message) == message

    def test_subject_only_is_unchanged(self) -> None:
        assert strip_trailers("Subject") == "Subject"

    def test_subject_that_looks_like_a_trailer_is_kept(self) -> None:
        # No preceding blank line, so the lone line is the subject, not a trailer.
        assert strip_trailers("WIP: rework selection") == "WIP: rework selection"

    def test_mixed_final_paragraph_is_kept(self) -> None:
        # A prose line among the trailers means it is not a pure trailer block.
        message = "Subject\n\nRepoactive-Job: a\nthis line is prose"
        assert strip_trailers(message) == message

    def test_url_paragraph_is_not_a_trailer(self) -> None:
        message = "Subject\n\nSee https://example.com for details"
        assert strip_trailers(message) == message

    def test_trailing_blank_lines_are_ignored(self) -> None:
        assert strip_trailers("Subject\n\nRepoactive-Job: a\n\n") == "Subject"

    def test_trailer_only_message_becomes_empty(self) -> None:
        # A message with a body of trailers only (subject is the body paragraph).
        assert strip_trailers("Repoactive-Job: a\n\nRepoactive-Job: b") == "Repoactive-Job: a"
