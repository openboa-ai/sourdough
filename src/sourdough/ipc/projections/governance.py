"""Read-only SQLite projections for Board approvals, finance, organization, and restrictions."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from time import time_ns
from typing import Any

from ...company import FoundingRefused, _identity
from ...runtime.domains import DomainPlugin
from ...storage.kernel import max_event_seq
from ._shared import (
    PROJECTION_APPROVALS,
    PROJECTION_FINANCE,
    PROJECTION_ORG,
    PROJECTION_RESTRICTIONS,
    _active_restrictions_rows,
    _agent_directory,
    _apply_core_enrichers,
    _close_issues_summary,
    _count_private_drafts_for_projection,
    _json_loads_safe,
    _open_projection_snapshot,
    _sum_gl_accounts,
)


def read_approvals_projection(
    store: Path,
    company_id: str,
    *,
    domain_plugins: Sequence[DomainPlugin] | None = None,
) -> tuple[dict[str, Any], int]:
    """Read Area 4 Board Approvals queue & resolution ledger (zero popups)."""
    with _open_projection_snapshot(store) as db:
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        charter_revision = str(
            db.execute(
                "SELECT charter_revision FROM company WHERE id = ?", (company_id,)
            ).fetchone()[0]
        )
        directory = _agent_directory(db, company_id)

        rows = db.execute(
            """SELECT question_id, kind, title, summary, impact_json, options_json,
                      revision, payload_hash, state, respond_by_ns,
                      created_by_agent_id, created_at_ns, resolved_at_ns,
                      resolved_command_id, resolution_choice, resolution_reason,
                      application_status, delivery_status
               FROM decision_question
               WHERE company_id = ?
               ORDER BY CASE state WHEN 'open' THEN 0 ELSE 1 END, created_at_ns DESC""",
            (company_id,),
        ).fetchall()

        pending: list[dict[str, Any]] = []
        history: list[dict[str, Any]] = []
        for r in rows:
            creator_id = str(r[10])
            creator = directory.get(creator_id, {})
            item = {
                "question_id": str(r[0]),
                "kind": str(r[1]),
                "title": str(r[2]),
                "summary": str(r[3]),
                "impact": _json_loads_safe(str(r[4]), {}),
                "options": _json_loads_safe(str(r[5]), ["approve", "decline"]),
                "revision": int(r[6]),
                "payload_hash": str(r[7]),
                "state": str(r[8]),
                "respond_by_ns": None if r[9] is None else int(r[9]),
                "created_by_agent_id": creator_id,
                "created_by_name": creator.get("display_name", creator_id),
                "created_by_role": creator.get("role_label", "Agent"),
                "created_at_ns": int(r[11]),
                "resolved_at_ns": None if r[12] is None else int(r[12]),
                "resolved_command_id": None if r[13] is None else str(r[13]),
                "resolution_choice": None if r[14] is None else str(r[14]),
                "resolution_reason": None if r[15] is None else str(r[15]),
                "application_status": str(r[16]),
                "delivery_status": str(r[17]),
            }
            if item["state"] == "open":
                pending.append(item)
            else:
                history.append(item)

        payload = {
            "projection": PROJECTION_APPROVALS,
            "company_id": company_id,
            "charter_revision": charter_revision,
            "as_of_ns": as_of_ns,
            "event_seq": event_seq,
            "pending_count": len(pending),
            "pending": pending,
            "history": history,
            "promote_candidates": [],
        }
        _apply_core_enrichers(
            PROJECTION_APPROVALS, db, company_id, payload, domain_plugins
        )
        return payload, event_seq


def read_finance_projection(
    store: Path,
    company_id: str,
    *,
    domain_plugins: Sequence[DomainPlugin] | None = None,
) -> tuple[dict[str, Any], int]:
    """Read Two-Layer P&L, General Ledger entries, period closes, and domain supplements."""
    with _open_projection_snapshot(store) as db:
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        gl_balances = _sum_gl_accounts(db)
        closes, open_issues = _close_issues_summary(db)

        journals = [
            {
                "entry_id": str(r[0]),
                "source": str(r[1]),
                "source_event": str(r[2]),
                "effective_date": str(r[3]),
                "currency": str(r[4]),
                "lines": [
                    {
                        "ordinal": int(line[0]),
                        "account": str(line[1]),
                        "amount_minor": int(line[2]),
                    }
                    for line in db.execute(
                        """SELECT ordinal, account, amount_minor
                           FROM journal_line WHERE entry_id = ? ORDER BY ordinal ASC""",
                        (str(r[0]),),
                    ).fetchall()
                ],
            }
            for r in db.execute(
                """SELECT id, source, source_event, effective_date, currency
                   FROM journal_entry ORDER BY rowid DESC LIMIT 40"""
            ).fetchall()
        ]

        payload = {
            "projection": PROJECTION_FINANCE,
            "company_id": company_id,
            "as_of_ns": as_of_ns,
            "event_seq": event_seq,
            "trading_enabled": False,
            "account_balances": [
                {"account": k, "balance_minor": v}
                for k, v in sorted(gl_balances.items())
            ],
            "journals": journals,
            "accounting_closes": closes,
            "open_close_issues": open_issues,
            "funding_settlements": [],
            "retained_statements": [],
        }
        _apply_core_enrichers(
            PROJECTION_FINANCE, db, company_id, payload, domain_plugins
        )
        return payload, event_seq


def read_org_projection(store: Path, company_id: str) -> tuple[dict[str, Any], int]:
    """Read Roster, Published Reports, and read-only Delegated HR Custody ledger."""
    with _open_projection_snapshot(store) as db:
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        directory = _agent_directory(db, company_id)

        roster = [info | {"private_draft_count": 0} for info in directory.values()]
        publications = [
            {
                "asset_id": str(r[0]),
                "project_id": str(r[1]),
                "author_id": str(r[2]),
                "author_name": directory.get(str(r[2]), {}).get(
                    "display_name", str(r[2])
                ),
                "author_role": directory.get(str(r[2]), {}).get("role_label", "Agent"),
                "work_item_id": str(r[3]),
                "audience": _json_loads_safe(str(r[4]), []),
                "content": str(r[5]),
            }
            for r in db.execute(
                """SELECT asset_id, project_id, author_id, work_item_id, audience, content
                   FROM shared_publication
                   WHERE company_id = ?
                   ORDER BY rowid DESC LIMIT 40""",
                (company_id,),
            ).fetchall()
        ]
        archives = [
            {
                "cutoff": int(r[0]),
                "agent_id": str(r[1]),
                "agent_name": directory.get(str(r[1]), {}).get(
                    "display_name", str(r[1])
                ),
                "manager_id": str(r[2]),
                "manager_name": directory.get(str(r[2]), {}).get(
                    "display_name", str(r[2])
                ),
                "employment_revision": str(r[3]),
                "reason": str(r[4]),
                "previous_scope": str(r[5]),
            }
            for r in db.execute(
                """SELECT cutoff, agent_id, manager_id, employment_revision, reason, previous_scope
                   FROM agent_archive
                   WHERE company_id = ?
                   ORDER BY cutoff DESC""",
                (company_id,),
            ).fetchall()
        ]
        payload = {
            "projection": PROJECTION_ORG,
            "company_id": company_id,
            "as_of_ns": as_of_ns,
            "event_seq": event_seq,
            "roster": roster,
            "publications": publications,
            "delegated_hr_custody": {
                "board_can_retire": False,
                "policy_notice": "Managed by CEO · Board view is read-only.",
                "archived_employees": archives,
            },
        }
    for entry in payload["roster"]:
        entry["private_draft_count"] = _count_private_drafts_for_projection(
            store, str(entry["agent_id"])
        )
    return payload, event_seq


def read_restrictions_projection(
    store: Path,
    company_id: str,
    *,
    domain_plugins: Sequence[DomainPlugin] | None = None,
) -> tuple[dict[str, Any], int]:
    """Read active & historical competing restriction causes plus domain risk readiness."""
    with _open_projection_snapshot(store) as db:
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        charter_revision = str(
            db.execute(
                "SELECT charter_revision FROM company WHERE id = ?", (company_id,)
            ).fetchone()[0]
        )
        causes = _active_restrictions_rows(db, company_id)
        payload = {
            "projection": PROJECTION_RESTRICTIONS,
            "company_id": company_id,
            "charter_revision": charter_revision,
            "as_of_ns": as_of_ns,
            "event_seq": event_seq,
            "active_causes": [c for c in causes if c["active"]],
            "history": [c for c in causes if not c["active"]],
            "trading_risk": None,
        }
        _apply_core_enrichers(
            PROJECTION_RESTRICTIONS, db, company_id, payload, domain_plugins
        )
        return payload, event_seq
