"""Bind a company-owned workflow to the current Agent run and private Desk."""

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from hashlib import sha256

from .agent_desk import resolve_desk
from .duty_run import DutyClaim, DutyRefused
from .writer_lease import WriterLease


def tool(name: str, description: str, properties: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": sorted(properties),
            "additionalProperties": False,
        },
    }


@dataclass(frozen=True)
class ScopedWorkflow:
    purpose: str
    instruction: str
    tools: tuple[dict, ...]
    terminal_tool: str | tuple[str, ...]


class ScopedRun:
    """Expose only company-selected tools without delegating grants to the adapter."""

    def __init__(
        self,
        writer: WriterLease,
        claim: DutyClaim,
        workflow: ScopedWorkflow,
        invoke: Callable[[str, dict], dict],
        *,
        context_current: Callable[[], bool] | None = None,
    ):
        definitions = {item["name"]: item["inputSchema"] for item in workflow.tools}
        terminal = workflow.terminal_tool
        terminal_names = (terminal,) if isinstance(terminal, str) else terminal
        if (
            not 1 <= len(definitions) <= 24
            or len(definitions) != len(workflow.tools)
            or not terminal_names
            or any(name not in definitions for name in terminal_names)
            or len(set(terminal_names)) != len(terminal_names)
            or any(
                not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) for name in definitions
            )
            or not workflow.instruction.strip()
            or len(workflow.instruction.encode()) > 16_000
        ):
            raise DutyRefused("Invalid scoped workflow manifest")
        self._writer = writer
        self._claim = claim
        self._purpose = workflow.purpose
        self._invoke = invoke
        self._context_current = context_current
        self._desk_revision, drafts = resolve_desk(
            writer,
            claim,
            workflow.purpose,
            include_drafts=True,
        )
        manifest = {
            "run_id": claim.run_id,
            "desk_revision": self._desk_revision,
            "drafts": [asdict(draft) for draft in drafts],
            "instruction": workflow.instruction,
            "tools": workflow.tools,
            "terminal_tool": workflow.terminal_tool,
        }
        self._manifest = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        if len(self._manifest.encode()) > 32_000:
            raise DutyRefused("Scoped workflow manifest exceeds the run budget")
        self._definitions = {
            item["name"]: item["inputSchema"]
            for item in json.loads(self._manifest)["tools"]
        }

    def _current_desk(self) -> str:
        revision, _ = resolve_desk(self._writer, self._claim, self._purpose)
        return revision

    @property
    def claim(self) -> DutyClaim:
        return self._claim

    def manifest(self) -> dict:
        if self._current_desk() != self._desk_revision:
            raise DutyRefused("The Agent Desk changed after the context was prepared")
        if self._context_current is not None and not self._context_current():
            raise DutyRefused("Scoped context changed before disclosure")
        return json.loads(self._manifest) | {
            "revision": sha256(self._manifest.encode()).hexdigest()
        }

    def call(self, name: str, arguments: dict) -> dict:
        if not isinstance(name, str) or name not in self._definitions:
            raise DutyRefused("Tool is not available to this run")
        schema = self._definitions[name]
        if (
            not isinstance(arguments, dict)
            or not set(schema["required"])
            <= arguments.keys()
            <= schema["properties"].keys()
        ):
            raise DutyRefused("Tool request does not match the scoped manifest")
        if self._desk_revision != self._current_desk():
            raise DutyRefused("The Agent Desk changed after the context was prepared")
        return self._invoke(name, arguments)
