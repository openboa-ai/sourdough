"""Resolve historical run identity separately from current duty authority."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .company import _connect, _identity

PAPER_EXECUTION = "paper_execution"
POSITION_REVIEW = "position_review"
ASSESSMENT_REVIEW = "assessment_review"


class DutyRefused(Exception):
    """No eligible duty or required authority exists."""


@dataclass(frozen=True)
class DutyClaim:
    company_id: str
    founder_id: str
    grant_id: str
    duty_id: str
    run_id: str
    execution_profile: str


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


def has_turn_capacity(connection: sqlite3.Connection, agent_id: str) -> bool:
    if not connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE name = 'hire_receipt' AND type = 'table'"
    ).fetchone():
        return True
    has_work_runs = (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name = 'work_runs' AND type = 'table'"
        ).fetchone()
        is not None
    )
    if has_work_runs:
        row = connection.execute(
            """SELECT h.allocated_turns > (
                   (SELECT count(*) FROM duty owned JOIN duty_run spent ON spent.duty_id=owned.id
                    WHERE owned.agent_id=h.agent_id)
                   + (SELECT count(*) FROM work_runs wr WHERE wr.agent_id=h.agent_id)
               ) FROM hire_receipt h WHERE h.agent_id=?""",
            (agent_id,),
        ).fetchone()
    else:
        row = connection.execute(
            """SELECT h.allocated_turns > (
                   SELECT count(*) FROM duty owned JOIN duty_run spent ON spent.duty_id=owned.id
                   WHERE owned.agent_id=h.agent_id
               ) FROM hire_receipt h WHERE h.agent_id=?""",
            (agent_id,),
        ).fetchone()
    return row != (0,)


def has_active_work_run(connection: sqlite3.Connection, agent_id: str) -> bool:
    if not connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE name = 'work_runs' AND type = 'table'"
    ).fetchone():
        return False
    return (
        connection.execute(
            "SELECT 1 FROM work_runs WHERE agent_id = ? AND status = 'running' LIMIT 1",
            (agent_id,),
        ).fetchone()
        is not None
    )


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
    if has_active_work_run(connection, founder_id):
        raise DutyRefused("Agent already has an active running execution")
    if not has_turn_capacity(connection, founder_id):
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
    return claim


def resolve_run(connection: sqlite3.Connection, claim: DutyClaim) -> RunAuthority:
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


def count_company_runs(store: Path, company_id: str) -> int:
    with closing(_connect(store, "ro")) as connection:
        connection.execute("BEGIN")
        if _identity(connection) != company_id:
            raise DutyRefused("Company identity mismatch")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        total = 0
        if "duty_run" in tables:
            total += connection.execute("SELECT count(*) FROM duty_run").fetchone()[0]
        else:
            if "first_duty_claim" in tables:
                total += connection.execute(
                    "SELECT count(*) FROM first_duty_claim"
                ).fetchone()[0]
            if "work_follow_up" in tables:
                total += connection.execute(
                    "SELECT count(*) FROM work_follow_up WHERE run_id IS NOT NULL"
                ).fetchone()[0]
        if "work_runs" in tables:
            total += connection.execute(
                "SELECT count(*) FROM work_runs WHERE company_id = ?", (company_id,)
            ).fetchone()[0]
        return total
