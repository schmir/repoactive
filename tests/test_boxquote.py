"""Tests for boxquote rendering and stripping."""

from repoactive.boxquote import boxquote, strip_boxquotes


class TestBoxquote:
    def test_with_title(self) -> None:
        assert boxquote("line1\nline2", title="date") == (",----[ date ]\n| line1\n| line2\n`----")

    def test_without_title(self) -> None:
        assert boxquote("line1\nline2") == ",----\n| line1\n| line2\n`----"

    def test_single_line(self) -> None:
        assert boxquote("only", title="cmd") == ",----[ cmd ]\n| only\n`----"

    def test_empty_message(self) -> None:
        assert boxquote("", title="cmd") == ",----[ cmd ]\n\n`----"

    def test_preserves_blank_lines(self) -> None:
        assert boxquote("a\n\nb") == ",----\n| a\n| \n| b\n`----"


class TestStripBoxquotes:
    def test_lone_boxquote_is_removed(self) -> None:
        assert strip_boxquotes(boxquote("a\nb")) == ""

    def test_titled_boxquote_is_removed(self) -> None:
        assert strip_boxquotes(boxquote("out", title="cmd")) == ""

    def test_text_before_boxquote_is_kept(self) -> None:
        message = "Title\n\n" + boxquote("out", title="cmd")
        assert strip_boxquotes(message) == "Title"

    def test_text_after_boxquote_is_kept(self) -> None:
        message = boxquote("out") + "\n\nTail"
        assert strip_boxquotes(message) == "Tail"

    def test_text_around_boxquote_is_kept(self) -> None:
        message = "Before\n\n" + boxquote("out", title="cmd") + "\n\nAfter"
        assert strip_boxquotes(message) == "Before\n\nAfter"

    def test_multiple_boxquotes_are_all_removed(self) -> None:
        message = "A\n\n" + boxquote("x") + "\n\nB\n\n" + boxquote("y", title="cmd")
        assert strip_boxquotes(message) == "A\n\nB"

    def test_text_without_boxquote_is_unchanged(self) -> None:
        assert strip_boxquotes("just a message\n\nwith paragraphs") == (
            "just a message\n\nwith paragraphs"
        )

    def test_empty_boxquote_is_removed(self) -> None:
        message = "Title\n\n" + boxquote("", title="cmd")
        assert strip_boxquotes(message) == "Title"

    def test_body_line_that_looks_like_a_delimiter_is_kept_inside(self) -> None:
        # A body line is prefixed with "| ", so a literal ",----" in the output
        # does not start a nested block and the whole box is still removed.
        assert strip_boxquotes(boxquote("text ,---- more")) == ""
