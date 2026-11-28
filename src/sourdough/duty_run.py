"""Resolve execution run identity separately from current standing authority."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .company import _identity
from .legacy_duty import DutyClaim, resolve_legacy_duty_run, start_run

ASSESSMENT_REVIEW = "assessment_review"
STAFFING_REVIEW = "staffing_review"


class DutyRefused(Exception):
    """No eligible duty or required authority exists."""


@runtime_checkable
class ExecutionClaim(Protocol):
    """Common structural claim contract shared by WorkRunClaim and legacy DutyClaim."""

    @property
    def company_id(self) -> str: ...

    @property
    def founder_id(self) -> str: ...

    @property
    def duty_id(self) -> str: ...

    @property
    def run_id(self) -> str: ...

    @property
    def grant_id(self) -> str: ...

    @property
    def agent_id(self) -> str: ...

    @property
    def work_item_id(self) -> str: ...


@dataclass(frozen=True)
class WorkRunClaim:
    """Fenced execution claim for a Work Kernel run.

    Retains `founder_id` (semantic `agent_id`) and `duty_id` (semantic
    `work_item_id`) field names for structural compatibility with `ExecutionClaim`.
    """

    company_id: str
    founder_id: str
    duty_id: str
    run_id: str
    fencing_token: int
    grant_id: str
    grant_scope: str
    purpose: str
    _clock: int | Callable[[], int] | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def agent_id(self) -> str:
        return self.founder_id

    @property
    def work_item_id(self) -> str:
        return self.duty_id

    def current_now_ns(self) -> int | None:
        if self._clock is None:
            return None
        return self._clock() if callable(self._clock) else self._clock


@dataclass(frozen=True)
class RunAuthority:
    purpose: str
    issued_scope: str
    current_scope: str
    duty_state: str
    is_current: bool

    def require_current(self, purpose: str) -> None:
        if (
            not self.is_current
            or self.purpose != purpose
            or self.issued_scope != self.current_scope
        ):
            raise DutyRefused("Duty claim or grant is no longer current")


def employment_cutoff(connection: sqlite3.Connection, agent_id: str) -> int | None:
    row = connection.execute(
        "SELECT cutoff FROM agent_archive WHERE agent_id=?", (agent_id,)
    ).fetchone()
    return row[0] if row else None


def has_turn_capacity(connection: sqlite3.Connection, agent_id: str) -> bool:
    if employment_cutoff(connection, agent_id) is not None:
        return False
    row = connection.execute(
        """SELECT h.allocated_turns > (
               SELECT count(*) FROM (
                   SELECT spent.run_id
                   FROM duty owned
                   JOIN duty_run spent ON spent.duty_id = owned.id
                   WHERE owned.agent_id = h.agent_id
                   UNION
                   SELECT wr.run_id
                   FROM work_runs wr
                   WHERE wr.agent_id = h.agent_id
               )
           ) FROM hire_receipt h WHERE h.agent_id=?""",
        (agent_id,),
    ).fetchone()
    return row != (0,)


def has_active_work_run(connection: sqlite3.Connection, agent_id: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM work_runs WHERE agent_id = ? AND status = 'running' LIMIT 1",
            (agent_id,),
        ).fetchone()
        is not None
    )


def work_run_claim_from_context(
    connection: sqlite3.Connection,
    ctx: Any,
    *,
    purpose: str | None = None,
) -> WorkRunClaim:
    """Build a fenced WorkRunClaim from a DutyContext and authoritative standing_grant."""
    company_id = str(ctx.company_id)
    agent_id = str(ctx.work_item.owner_agent_id)
    if employment_cutoff(connection, agent_id) is not None:
        raise DutyRefused("Agent employment is fenced")
    row = connection.execute(
        """SELECT g.id, g.scope
           FROM standing_grant g
           JOIN agent a ON a.id = g.agent_id
           WHERE a.company_id = ? AND a.id = ?
             AND trim(g.scope) != '' AND lower(trim(g.scope)) != 'revoked'""",
        (company_id, agent_id),
    ).fetchone()
    if row is None:
        raise DutyRefused("Agent has no active standing grant")
    return WorkRunClaim(
        company_id=company_id,
        founder_id=agent_id,
        duty_id=str(ctx.work_item.work_item_id),
        run_id=str(ctx.run.run_id),
        fencing_token=int(ctx.run.fencing_token),
        grant_id=str(row[0]),
        grant_scope=str(row[1]),
        purpose=purpose or str(ctx.work_item.duty_kind),
        _clock=getattr(ctx, "_clock", None),
    )


def resolve_run(
    connection: sqlite3.Connection,
    claim: ExecutionClaim,
    *,
    now_ns: int | None = None,
) -> RunAuthority:
    if isinstance(claim, WorkRunClaim):
        from .runtime.work_kernel import assert_run_fencing

        effective_now = now_ns if now_ns is not None else claim.current_now_ns()
        run = assert_run_fencing(
            connection, claim.run_id, claim.fencing_token, now_ns=effective_now
        )
        if (
            run.company_id != claim.company_id
            or run.work_item_id != claim.duty_id
            or run.agent_id != claim.founder_id
        ):
            raise DutyRefused("Work run claim identity mismatch")
        item_row = connection.execute(
            """SELECT state FROM work_items
               WHERE work_item_id = ? AND company_id = ? AND owner_agent_id = ?""",
            (claim.duty_id, claim.company_id, claim.founder_id),
        ).fetchone()
        if item_row is None or str(item_row[0]) != "running":
            raise DutyRefused("Work run claim requires a running work item")
        grant_row = connection.execute(
            """SELECT g.scope
               FROM standing_grant g
               JOIN agent a ON a.id = g.agent_id
               WHERE g.id = ? AND g.agent_id = ? AND a.company_id = ?""",
            (claim.grant_id, claim.founder_id, claim.company_id),
        ).fetchone()
        if grant_row is None:
            raise DutyRefused("Work run claim grant is unavailable")
        current_scope = str(grant_row[0])
        if not current_scope.strip() or current_scope.strip().lower() == "revoked":
            raise DutyRefused("Work run claim grant is revoked")
        issued_scope = claim.grant_scope if claim.grant_scope else current_scope
        return RunAuthority(
            purpose=claim.purpose,
            issued_scope=issued_scope,
            current_scope=current_scope,
            duty_state="running",
            is_current=True,
        )

    if isinstance(claim, DutyClaim):
        return resolve_legacy_duty_run(connection, claim)
    raise DutyRefused("Unsupported execution claim type")


def count_company_runs_in_tx(connection: sqlite3.Connection, company_id: str) -> int:
    if _identity(connection) != company_id:
        raise DutyRefused("Company identity mismatch")
    return int(
        connection.execute(
            """SELECT count(*) FROM (
                   SELECT r.run_id
                   FROM duty_run r
                   JOIN duty d ON d.id = r.duty_id
                   JOIN agent a ON a.id = d.agent_id
                   WHERE a.company_id = ?
                   UNION
                   SELECT wr.run_id
                   FROM work_runs wr
                   WHERE wr.company_id = ?
               )""",
            (company_id, company_id),
        ).fetchone()[0]
    )


def count_company_runs(store: Path, company_id: str) -> int:
    from .storage.db import scoped_reader

    with scoped_reader(store) as connection:
        return count_company_runs_in_tx(connection, company_id)


__all__ = [
    "ASSESSMENT_REVIEW",
    "STAFFING_REVIEW",
    "DutyClaim",
    "DutyRefused",
    "ExecutionClaim",
    "RunAuthority",
    "WorkRunClaim",
    "count_company_runs",
    "count_company_runs_in_tx",
    "employment_cutoff",
    "has_active_work_run",
    "has_turn_capacity",
    "resolve_run",
    "start_run",
    "work_run_claim_from_context",
]
