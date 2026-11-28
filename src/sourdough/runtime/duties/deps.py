"""Shared Core duty constants, dependency bundle, profile lookup, and read helpers."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...agent_runtime.protocol import AgentProfileBinding, ToolHandler
from ...duty_run import WorkRunClaim
from ...storage.db import scoped_reader
from ...writer_lease import WriterLease
from ..domains import DecisionActionRegistry, DomainPlugin, ProfileBindingLookup
from ..proactive_tools import build_core_proactive_tool_contribution
from ..registry import RuntimeRefused
from ..shift_runner import ActiveMethodsProvider, ShiftRunner
from ..work_kernel import mark_peer_request_resumed, read_peer_request_outcome

DUTY_CEO_RESPOND_MESSAGE = "ceo.respond_message"
DUTY_BOARD_APPLY_DECISION = "board.apply_decision"
DUTY_WORK_PEER_RESPONSE = "work.peer_response"
DUTY_WORK_ASSESSMENT_REVIEW = "work.assessment_review"
DUTY_WORK_HANDOFF_FOLLOWUP = "work.handoff_followup"
DUTY_WORK_DISCOVERY_REVIEW = "work.discovery_review"
DUTY_WORK_DREAMING = "work.dreaming"


@dataclass(frozen=True)
class CoreDutyDeps:
    """Explicitly injected dependencies shared by Core duty handler classes."""

    shift_runner: ShiftRunner
    decision_actions: DecisionActionRegistry
    domain_plugins: tuple[DomainPlugin, ...]
    active_methods_provider: ActiveMethodsProvider


def resolve_profile(lookup: ProfileBindingLookup, agent_id: str) -> AgentProfileBinding:
    if callable(lookup):
        return lookup(agent_id)
    binding = lookup.get(agent_id)
    if binding is None:
        raise RuntimeRefused(
            f"No AgentProfileBinding configured for agent '{agent_id}'"
        )
    return binding


def build_store_profile_lookup(
    store: Path,
    company_id: str,
    *,
    overrides: Mapping[str, AgentProfileBinding] | None = None,
) -> Callable[[str], AgentProfileBinding]:
    """Resolve AgentProfileBinding dynamically from overrides or persisted agent_runtime_binding."""
    override_map = dict(overrides or {})

    def _lookup(agent_id: str) -> AgentProfileBinding:
        if agent_id in override_map:
            return override_map[agent_id]
        with scoped_reader(store) as db:
            row = db.execute(
                """SELECT provider, model, revision, sandbox_backend,
                          credential_handle_ref, profile_id
                   FROM agent_runtime_binding
                   WHERE company_id = ? AND agent_id = ?""",
                (company_id, agent_id),
            ).fetchone()
        if row is None:
            raise RuntimeRefused(
                f"No AgentProfileBinding configured for agent '{agent_id}'"
            )
        binding = AgentProfileBinding(
            provider=str(row[0]),
            model=str(row[1]),
            revision=str(row[2]),
            sandbox_backend=str(row[3]),
            credential_handle_ref=None if row[4] is None else str(row[4]),
        )
        binding.verify_matches_agent(str(row[5]))
        return binding

    return _lookup


def build_proactive_tool_handlers(
    writer: WriterLease,
    claim: WorkRunClaim,
    *,
    tx_lock: threading.RLock | None = None,
    domain_plugins: Sequence[DomainPlugin] = (),
) -> dict[str, ToolHandler]:
    """Return fenced proactive condition-scheduling and work_decision tool handlers."""
    return dict(
        build_core_proactive_tool_contribution(
            writer=writer,
            claim=claim,
            tx_lock=tx_lock,
            domain_plugins=domain_plugins,
        ).handlers
    )


def read_agent_state(store: Path, company_id: str, agent_id: str) -> tuple[str, str]:
    with scoped_reader(store) as db:
        row = db.execute(
            "SELECT execution_profile FROM agent WHERE company_id = ? AND id = ?",
            (company_id, agent_id),
        ).fetchone()
        if row is None:
            raise RuntimeRefused(f"Agent '{agent_id}' not found in company")
        grant_row = db.execute(
            """SELECT scope FROM standing_grant
               WHERE agent_id = ? AND lower(trim(scope)) != 'revoked'
               ORDER BY id LIMIT 1""",
            (agent_id,),
        ).fetchone()
        if grant_row is None or not str(grant_row[0]).strip():
            raise RuntimeRefused(f"Agent '{agent_id}' has no active standing grant")
        return str(row[0]), str(grant_row[0])


def read_company_status(store: Path, company_id: str) -> dict[str, Any]:
    with scoped_reader(store) as db:
        agent_count = int(
            db.execute(
                "SELECT COUNT(*) FROM agent WHERE company_id = ?", (company_id,)
            ).fetchone()[0]
        )
        restrictions = [
            {"restriction_id": str(r[0]), "scope_ref": str(r[1]), "reason": str(r[2])}
            for r in db.execute(
                """SELECT restriction_id, scope_ref, reason
                   FROM restriction_cause
                   WHERE company_id = ? AND active = 1
                   ORDER BY restriction_id""",
                (company_id,),
            ).fetchall()
        ]
        due_work = int(
            db.execute(
                """SELECT COUNT(*) FROM work_items
                   WHERE company_id = ? AND state IN ('due', 'running', 'waiting')""",
                (company_id,),
            ).fetchone()[0]
        )
    return {
        "company_id": company_id,
        "agent_count": agent_count,
        "active_restrictions": restrictions,
        "open_work_items": due_work,
    }


def hydrate_resolved_child_peer_outcome(
    db: Any,
    *,
    company_id: str,
    work_item_id: str,
    run_id: str,
    agent_id: str,
    receipts: list[dict[str, Any]],
    notes: list[str],
) -> None:
    unresolved_resume = db.execute(
        """SELECT request_id
           FROM work_peer_requests
           WHERE company_id = ? AND requester_work_item_id = ?
             AND state != 'open' AND resumed_run_id IS NULL
           ORDER BY rowid DESC LIMIT 1""",
        (company_id, work_item_id),
    ).fetchone()
    if unresolved_resume is not None:
        mark_peer_request_resumed(
            db,
            company_id=company_id,
            request_id=str(unresolved_resume[0]),
            resumed_run_id=run_id,
        )
    peer_outcome = read_peer_request_outcome(
        db,
        company_id=company_id,
        requester_work_item_id=work_item_id,
        actor_id=agent_id,
        include_provenance=True,
    )
    if peer_outcome is not None:
        receipts.append({"kind": "peer_outcome", **peer_outcome})
        notes.append(
            f"Peer outcome ({peer_outcome['request_key']}): "
            f"{peer_outcome['outcome']} -> {peer_outcome['answer']}"
        )
