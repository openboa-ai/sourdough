"""Isolated legacy DutyClaim / duty_run adapter retained for historical Flow-01..10 callers.

New runtime code should use `WorkRunClaim` and `ExecutionClaim` from `sourdough.duty_run`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from .duty_run import RunAuthority


@dataclass(frozen=True)
class DutyClaim:
    company_id: str
    founder_id: str
    grant_id: str
    duty_id: str
    run_id: str
    execution_profile: str

    @property
    def agent_id(self) -> str:
        return self.founder_id

    @property
    def work_item_id(self) -> str:
        return self.duty_id


def start_run(
    connection: sqlite3.Connection,
    company_id: str,
    founder_id: str,
    grant_id: str,
    duty_id: str,
    execution_profile: str,
    *,
    purpose: str,
    grant_scope: str,
) -> DutyClaim:
    from .duty_run import (
        STAFFING_REVIEW,
        DutyRefused,
        employment_cutoff,
        has_active_work_run,
        has_turn_capacity,
    )
    from .runtime.work_kernel import (
        reconcile_legacy_fenced_runs,
        sync_work_item,
        sync_work_run,
    )

    reconcile_legacy_fenced_runs(connection, founder_id)
    active_kernel_run = (
        connection.execute(
            """SELECT 1 FROM work_runs wr
               WHERE wr.agent_id = ? AND wr.status = 'running'
                 AND NOT EXISTS (
                     SELECT 1 FROM duty_run dr
                     WHERE dr.run_id = wr.run_id AND dr.purpose = ?
                 )
               LIMIT 1""",
            (founder_id, STAFFING_REVIEW),
        ).fetchone()
        is not None
    )
    if active_kernel_run:
        raise DutyRefused("Agent already has an active running execution")
    if not has_turn_capacity(connection, founder_id):
        if employment_cutoff(connection, founder_id) is not None:
            raise DutyRefused("Agent employment is fenced")
        raise DutyRefused("Agent has no remaining turn allocation")
    claim = DutyClaim(
        company_id, founder_id, grant_id, duty_id, uuid4().hex, execution_profile
    )
    changed = connection.execute(
        """UPDATE duty SET state = 'running'
           WHERE id = ? AND agent_id = ? AND state IN ('due', 'scheduled')""",
        (duty_id, founder_id),
    )
    if changed.rowcount != 1:
        raise DutyRefused("Duty is not eligible for a new run")
    connection.execute(
        "INSERT INTO duty_run VALUES (?, ?, ?, ?, ?, 1)",
        (claim.run_id, duty_id, grant_id, grant_scope, purpose),
    )
    sync_work_item(
        connection,
        company_id=company_id,
        work_item_id=duty_id,
        owner_agent_id=founder_id,
        duty_kind=f"work.{purpose}",
        state="running",
        source_kind="duty",
        source_id=duty_id,
    )
    run_status = "queued" if has_active_work_run(connection, founder_id) else "running"
    sync_work_run(
        connection,
        company_id=company_id,
        work_item_id=duty_id,
        run_id=claim.run_id,
        agent_id=founder_id,
        duty_kind=f"work.{purpose}",
        status=run_status,
    )
    return claim


def resolve_legacy_duty_run(
    connection: sqlite3.Connection, claim: DutyClaim
) -> RunAuthority:
    from .duty_run import DutyRefused, RunAuthority

    row = connection.execute(
        """SELECT r.purpose, r.grant_scope, g.scope, d.state, r.is_current
           FROM duty_run r
           JOIN duty d ON r.duty_id = d.id
           JOIN agent f ON d.agent_id = f.id
           JOIN standing_grant g ON g.id = r.grant_id AND g.agent_id = f.id
           WHERE d.id = ? AND r.run_id = ?
             AND f.company_id = ? AND f.id = ? AND g.id = ?
             AND f.execution_profile = ?""",
        (
            claim.duty_id,
            claim.run_id,
            claim.company_id,
            claim.founder_id,
            claim.grant_id,
            claim.execution_profile,
        ),
    ).fetchone()
    if row is None:
        raise DutyRefused("Duty claim identity mismatch")
    purpose, issued_scope, current_scope, state, is_current = row
    return RunAuthority(purpose, issued_scope, current_scope, state, bool(is_current))
