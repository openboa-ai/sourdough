"""Offer project tools only when the current Agent has an assigned shared project."""

from collections.abc import Callable
from dataclasses import asdict, replace

from .company_assets import (
    Selection,
    discover_shared_assets,
    project_access,
    publish_selection,
    read_shared_asset,
)
from .duty_run import DutyClaim, DutyRefused
from .harness import ScopedRun, ScopedWorkflow, tool
from .writer_lease import WriterLease


PROJECTS = tool(
    "read_project_assignments",
    "Read current project permissions and one page of shared references. "
    "Use an empty cursor for the first page or the returned cursor for the next.",
    {"after": {"type": "string"}},
)
READ_ASSET = tool(
    "read_shared_asset",
    "Read one shared plain-text asset if its audience includes you.",
    {"asset_id": {"type": "string"}},
)
PUBLISH_DRAFT = tool(
    "publish_draft_selection",
    "Publish only a reviewed character range from your current private draft. This does not assign work.",
    {
        "request_id": {"type": "string"},
        "draft_name": {"type": "string"},
        "draft_revision": {"type": "string"},
        "project_id": {"type": "string"},
        "project_revision": {"type": "string"},
        "audience": {"type": "array", "items": {"type": "string"}},
        "start": {"type": "integer"},
        "end": {"type": "integer"},
    },
)


def company_work_run(
    writer: WriterLease,
    claim: DutyClaim,
    workflow: ScopedWorkflow,
    invoke: Callable[[str, dict], dict],
) -> ScopedRun:
    projects = project_access(writer, claim, workflow.purpose)
    additional = (PROJECTS, READ_ASSET) if projects else ()
    if any(project.can_publish for project in projects):
        additional += (PUBLISH_DRAFT,)

    def dispatch(name: str, arguments: dict) -> dict:
        if name == "read_project_assignments":
            assets, next_cursor = discover_shared_assets(
                writer, claim, workflow.purpose, arguments["after"]
            )
            return {
                "projects": [
                    asdict(project)
                    for project in project_access(writer, claim, workflow.purpose)
                ],
                "shared_assets": [asdict(asset) for asset in assets],
                "next_cursor": next_cursor,
            }
        if name == "read_shared_asset":
            return asdict(
                read_shared_asset(
                    writer, claim, workflow.purpose, arguments["asset_id"]
                )
            )
        if name == "publish_draft_selection":
            audience = arguments["audience"]
            if not isinstance(audience, list):
                raise DutyRefused("Publication requires an explicit audience")
            selection = Selection(**(arguments | {"audience": tuple(audience)}))
            return asdict(publish_selection(writer, claim, workflow.purpose, selection))
        return invoke(name, arguments)

    return ScopedRun(
        writer, claim, replace(workflow, tools=workflow.tools + additional), dispatch
    )
