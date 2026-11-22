"""Claim the founder's first duty for the configured runtime."""

import sqlite3
from contextlib import closing
from pathlib import Path

from .company import _connect, _identity, upgrade_work_schema
from .duty_run import (
    PAPER_EXECUTION,
    DutyClaim,
    DutyRefused,
    RunAuthority,
    resolve_run,
    start_run,
)
from .onboarding import has_hired_acceptance


FIRST_DUTY_SCOPE = "paper"


def founding_duty_due(store: Path, company_id: str) -> bool:
    with closing(_connect(store, "ro")) as connection:
        connection.execute("BEGIN")
        if _identity(connection) != company_id:
            raise DutyRefused("Company identity mismatch")
        return bool(
            connection.execute(
                """SELECT 1 FROM founding_receipt receipt
                   JOIN duty d ON d.id = receipt.due_work_id
                   WHERE receipt.company_id = ? AND d.state = 'due'""",
                (company_id,),
            ).fetchone()
        )


def claim_first_duty(store: Path, company_id: str, execution_profile: str) -> DutyClaim:
    upgrade_work_schema(store, company_id)
    with closing(_connect(store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if _identity(connection) != company_id:
            raise DutyRefused("Company identity mismatch")
        row = connection.execute(
            """SELECT f.id, g.id, d.id, f.execution_profile
               FROM founding_receipt r
               JOIN agent f ON r.founder_id = f.id AND r.company_id = f.company_id
               JOIN standing_grant g ON r.grant_id = g.id AND g.agent_id = f.id
               JOIN duty d ON r.due_work_id = d.id AND d.agent_id = f.id
               WHERE r.company_id = ? AND d.state = 'due' AND g.scope = ?""",
            (company_id, FIRST_DUTY_SCOPE),
        ).fetchone()
        if row is None or not execution_profile or row[3] != execution_profile:
            raise DutyRefused("No authorized paper duty for this execution profile")
        return start_run(
            connection,
            company_id,
            *row,
            purpose=PAPER_EXECUTION,
            grant_scope=FIRST_DUTY_SCOPE,
        )


def resolve_first_run(connection: sqlite3.Connection, claim: DutyClaim) -> RunAuthority:
    authority = resolve_run(connection, claim)
    founder = connection.execute(
        "SELECT 1 FROM founding_receipt WHERE company_id = ? AND due_work_id = ?",
        (claim.company_id, claim.duty_id),
    ).fetchone()
    if authority.purpose != PAPER_EXECUTION or (
        founder is None and not has_hired_acceptance(connection, claim)
    ):
        raise DutyRefused("Claim is not an admitted original paper duty")
    return authority


def require_historical_claim(connection: sqlite3.Connection, claim: DutyClaim) -> None:
    if resolve_run(connection, claim).purpose != PAPER_EXECUTION:
        raise DutyRefused("Historical order requires a paper execution claim")


def require_paper_authority(connection: sqlite3.Connection, claim: DutyClaim) -> None:
    authority = resolve_run(connection, claim)
    authority.require_current(PAPER_EXECUTION)
    if (authority.current_scope, authority.duty_state) != (FIRST_DUTY_SCOPE, "running"):
        raise DutyRefused("Current duty lacks paper authority")
