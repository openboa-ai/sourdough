"""Claim the founder's first duty for the configured runtime."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .company import _connect, _identity


class DutyRefused(Exception):
    """No eligible duty or required authority exists."""


FIRST_DUTY_SCOPE = "paper"


@dataclass(frozen=True)
class DutyClaim:
    company_id: str
    founder_id: str
    grant_id: str
    duty_id: str
    run_id: str
    execution_profile: str


def claim_first_duty(store: Path, company_id: str, execution_profile: str) -> DutyClaim:
    with closing(_connect(store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if _identity(connection) != company_id:
            raise DutyRefused("Company identity mismatch")
        row = connection.execute(
            """SELECT f.id, g.id, d.id, f.execution_profile
               FROM founding_receipt r
               JOIN founder f ON r.founder_id = f.id AND r.company_id = f.company_id
               JOIN standing_grant g ON r.grant_id = g.id AND g.founder_id = f.id
               JOIN duty d ON r.due_work_id = d.id AND d.founder_id = f.id
               WHERE r.company_id = ? AND d.state = 'due' AND g.scope = ?""",
            (company_id, FIRST_DUTY_SCOPE),
        ).fetchone()
        if row is None or not execution_profile or row[3] != execution_profile:
            raise DutyRefused("No authorized paper duty for this execution profile")
        claim = DutyClaim(company_id, *row[:3], uuid4().hex, row[3])
        connection.execute(
            """CREATE TABLE IF NOT EXISTS first_duty_claim (
                duty_id TEXT PRIMARY KEY NOT NULL REFERENCES duty(id),
                run_id TEXT NOT NULL UNIQUE
            )"""
        )
        connection.execute(
            "INSERT INTO first_duty_claim VALUES (?, ?)", (claim.duty_id, claim.run_id)
        )
        connection.execute(
            "UPDATE duty SET state = 'running' WHERE id = ?", (claim.duty_id,)
        )
        return claim


def first_claim_state(
    connection: sqlite3.Connection, claim: DutyClaim
) -> tuple[str, str]:
    row = connection.execute(
        """SELECT g.scope, d.state
           FROM first_duty_claim q
           JOIN duty d ON q.duty_id = d.id
           JOIN founder f ON d.founder_id = f.id
           JOIN standing_grant g ON g.founder_id = f.id
           WHERE q.duty_id = ? AND q.run_id = ?
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
    return row


def require_historical_claim(connection: sqlite3.Connection, claim: DutyClaim) -> None:
    first_claim_state(connection, claim)


def require_paper_authority(connection: sqlite3.Connection, claim: DutyClaim) -> None:
    if first_claim_state(connection, claim) != (FIRST_DUTY_SCOPE, "running"):
        raise DutyRefused("Current duty lacks paper authority")
