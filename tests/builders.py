"""Shared Job/Config builders for the runner and selection test modules."""

from repoactive.config import Config, CreateMR, Job


def _job(  # noqa: PLR0913
    name: str,
    *,
    depends_on: list[str] | None = None,
    run_only_if_changed: list[str] | None = None,
    base_branch: str | None = None,
    description: str | None = None,
    labels: list[str] | None = None,
    branch_prefix: str = "repoactive/",
    mr_title_prefix: str = "",
    commit_title_prefix: str = "",
    create_mr: CreateMR = CreateMR.always,
) -> Job:
    return Job(
        name=name,
        command=f"cmd-{name}",
        title=f"Change {name}",
        depends_on=depends_on or [],
        run_only_if_changed=run_only_if_changed or [],
        base_branch=base_branch,
        description=description,
        labels=labels or [],
        branch_prefix=branch_prefix,
        mr_title_prefix=mr_title_prefix,
        commit_title_prefix=commit_title_prefix,
        create_mr=create_mr,
    )


def _djob(
    name: str,
    *,
    disabled: bool = False,
    tags: list[str] | None = None,
    depends_on: list[str] | None = None,
) -> Job:
    return Job(
        name=name,
        command="cmd",
        title=name,
        disabled=disabled,
        tags=tags or [],
        depends_on=depends_on or [],
    )


def _config(*jobs: Job) -> Config:
    return Config.model_validate(
        {
            "platform": [{"url": "https://gitlab.com", "type": "gitlab", "token_env": "T"}],
            "jobs": [
                {
                    "name": j.name,
                    "command": j.command,
                    "title": j.title,
                    "depends_on": j.depends_on,
                    "run_only_if_changed": j.run_only_if_changed,
                    "disabled": j.disabled,
                    "tags": j.tags,
                    "create_mr": j.create_mr,
                }
                for j in jobs
            ],
        }
    )


def _names(jobs: list[Job]) -> list[str]:
    return [j.name for j in jobs]
