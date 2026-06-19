"""Serializable description of the remote operations a run wants to apply.

A run is split into two phases: a *collect* phase that does only local jj work
(running the command, setting the bookmark, writing the commit) and records the
intended remote operations into an ``UpdatePlan``, and an *apply* phase that
performs those operations (``jj git push`` and ``Platform.ensure_mr``). The
models here are pydantic so a plan can be serialized to disk and applied later.
"""

from __future__ import annotations

from pydantic import BaseModel


class BookmarkPush(BaseModel):
    """A bookmark to push to the remote.

    ``delete=True`` pushes a locally-deleted bookmark, propagating the deletion;
    it is a no-op if the bookmark was never pushed.
    """

    bookmark: str
    delete: bool = False


class MRUpdate(BaseModel):
    """Everything needed to (re)create an MR/PR at apply time.

    ``target_branch`` is already resolved (the platform default branch is looked
    up during collect), so applying needs no further resolution. The description
    is assembled at apply time by ``build_mr_description`` because the
    ``depends_on`` MR URLs are not known until those MRs have been created.
    """

    source_branch: str
    target_branch: str
    title: str
    description: str
    command: str
    command_output: str
    labels: list[str]
    draft: bool
    depends_on: list[str] = []


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

    updates: list[JobUpdate] = []


def build_mr_description(mr: MRUpdate, dep_urls: list[tuple[str, str]]) -> str:
    """Assemble an MR description from its parts.

    Order: the job's base description, then a "Depends on" section linking each
    dependency's MR (``dep_urls`` is ``(title, url)`` pairs), then the command
    output rendered as a fenced code block.
    """
    description = mr.description or ""
    if dep_urls:
        if description:
            description += "\n\n"
        links = "\n".join(f"- [{title}]({url})" for title, url in dep_urls)
        description += f"Depends on:\n{links}"
    if mr.command_output:
        if description:
            description += "\n\n"
        description += f"```\n$ {mr.command}\n{mr.command_output}\n```"
    return description
