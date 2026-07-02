from repoactive.updates import (
    BookmarkPush,
    JobUpdate,
    MRLink,
    MRUpdate,
    UpdatePlan,
    build_mr_description,
)


def _mr(
    *,
    description: str = "",
    command: str = "cmd-x",
    command_output: str = "",
    depends_on: list[str] | None = None,
) -> MRUpdate:
    return MRUpdate(
        source_branch="repoactive/x",
        target_branch="main",
        title="Change x",
        description=description,
        command=command,
        command_output=command_output,
        labels=[],
        draft=False,
        depends_on=depends_on or [],
    )


class TestBuildMrDescription:
    def test_empty_when_nothing_set(self) -> None:
        assert build_mr_description(_mr(), []) == ""

    def test_description_used_when_set(self) -> None:
        assert build_mr_description(_mr(description="Details."), []) == "Details."

    def test_command_output_appended(self) -> None:
        result = build_mr_description(_mr(command_output="some output"), [])
        assert result == "```\n$ cmd-x\nsome output\n```"

    def test_command_output_appended_after_description(self) -> None:
        result = build_mr_description(
            _mr(description="Details.", command_output="some output"), []
        )
        assert result == "Details.\n\n```\n$ cmd-x\nsome output\n```"

    def test_empty_command_output_not_appended(self) -> None:
        assert (
            build_mr_description(_mr(description="Details.", command_output=""), []) == "Details."
        )

    def test_dep_urls_included(self) -> None:
        result = build_mr_description(_mr(), [MRLink("Dep A", "https://example.com/mr/1")])
        assert result == "Depends on:\n- [Dep A](https://example.com/mr/1)"

    def test_dep_urls_multiple(self) -> None:
        result = build_mr_description(
            _mr(),
            [
                MRLink("Dep A", "https://example.com/mr/1"),
                MRLink("Dep B", "https://example.com/mr/2"),
            ],
        )
        assert result == (
            "Depends on:\n- [Dep A](https://example.com/mr/1)\n- [Dep B](https://example.com/mr/2)"
        )

    def test_dep_urls_after_description(self) -> None:
        result = build_mr_description(
            _mr(description="Details."), [MRLink("Dep A", "https://example.com/mr/1")]
        )
        assert result == "Details.\n\nDepends on:\n- [Dep A](https://example.com/mr/1)"

    def test_dep_urls_before_command_output(self) -> None:
        result = build_mr_description(
            _mr(command_output="some output"), [MRLink("Dep A", "https://example.com/mr/1")]
        )
        assert result == (
            "Depends on:\n- [Dep A](https://example.com/mr/1)\n\n```\n$ cmd-x\nsome output\n```"
        )


class TestSerialization:
    def test_plan_round_trips_through_json(self) -> None:
        plan = UpdatePlan(
            updates=[
                JobUpdate(
                    job_name="a",
                    title="Change a",
                    push=BookmarkPush(bookmark="repoactive/a"),
                    mr=_mr(description="A", depends_on=["b"]),
                ),
                JobUpdate(
                    job_name="b",
                    title="Change b",
                    push=BookmarkPush(bookmark="repoactive/b", delete=True),
                ),
            ]
        )

        restored = UpdatePlan.model_validate_json(plan.model_dump_json())

        assert restored == plan

    def test_defaults(self) -> None:
        update = JobUpdate(job_name="a", title="Change a")
        assert update.push is None
        assert update.mr is None
        assert UpdatePlan().updates == []
