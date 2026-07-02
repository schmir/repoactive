"""Serializable description of the remote operations a run wants to apply.

A run is split into two phases: a *collect* phase that does only local jj work
(running the command, setting the bookmark, writing the commit) and records the
intended remote operations into an ``UpdatePlan``, and an *apply* phase that
performs those operations (``jj git push`` and ``Platform.ensure_mr``). The
plan models are pydantic so a plan can be serialized to disk and applied later;
``MRLink`` is not part of the plan — it exists only at apply time, once the
dependency MR URLs it carries are known.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field


class BookmarkPush(BaseModel):
    """A bookmark to push to the remote.

    ``delete=True`` pushes a locally-deleted bookmark, propagating the deletion;
    it is a no-op if the bookmark was never pushed.
    """

    bookmark: str
    delete: bool = False


class MRUpdate(BaseModel):
    """Everything needed to (re)create an MR/PR at apply time.

    ``target_branch`` is ``None`` when the job did not set ``base_branch``; the
    platform default branch is then resolved during apply, so building the plan
    needs no platform access. The description is assembled at apply time by
    ``build_mr_description`` because the ``depends_on`` MR URLs are not known
    until those MRs have been created.
    """

    source_branch: str
    target_branch: str | None = None
    title: str
    description: str
    command: str
    command_output: str
    labels: list[str]
    draft: bool
    depends_on: list[str] = Field(default_factory=list)


class JobUpdate(BaseModel):
    """One job's pending remote operations.

    ``title`` is the job's bare title (no prefix); it is the label used when this
    job appears as a dependency link in a dependent's MR description.
    """

    job_name: str
    title: str
    push: BookmarkPush | None = None
    mr: MRUpdate | None = None


class UpdatePlan(BaseModel):
    """All pending remote operations from a run, in topological order."""

    updates: list[JobUpdate] = Field(default_factory=list)


@dataclass(frozen=True)
class MRLink:
    """A dependency's MR, as linked from the "Depends on" section of a
    dependent's MR description."""

    title: str
    url: str


def build_mr_description(mr: MRUpdate, dependency_links: list[MRLink]) -> str:
    """Assemble an MR description from its parts.

    Order: the job's base description, then a "Depends on" section linking each
    dependency's MR, then the command output rendered as a fenced code block.
    """
    description = mr.description or ""
    if dependency_links:
        if description:
            description += "\n\n"
        links = "\n".join(f"- [{link.title}]({link.url})" for link in dependency_links)
        description += f"Depends on:\n{links}"
    if mr.command_output:
        if description:
            description += "\n\n"
        description += f"```\n$ {mr.command}\n{mr.command_output}\n```"
    return description
