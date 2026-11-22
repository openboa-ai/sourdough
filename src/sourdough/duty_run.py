"""Resolve historical run identity separately from current duty authority."""

import sqlite3
from dataclasses import dataclass
from uuid import uuid4


PAPER_EXECUTION = "paper_execution"
POSITION_REVIEW = "position_review"


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
    claim = DutyClaim(
        company_id, founder_id, grant_id, duty_id, uuid4().hex, execution_profile
    )
    changed = connection.execute(
        "UPDATE duty SET state = 'running' WHERE id = ? AND state IN ('due', 'scheduled')",
        (duty_id,),
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
           JOIN founder f ON d.founder_id = f.id
           JOIN standing_grant g ON g.id = r.grant_id AND g.founder_id = f.id
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
