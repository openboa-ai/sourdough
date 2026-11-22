"""Persist a completed duty and claim its eligible successor."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .company import SCHEMA_REVISION, _company_version, _connect
from .first_duty import FIRST_DUTY_SCOPE, DutyClaim, DutyRefused, first_claim_state


def _time(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise DutyRefused("Work time must be a nonnegative integer Unix timestamp")
    return value


@dataclass(frozen=True)
class ResultReference:
    kind: str
    resource_id: str
    revision: str
    available_at: int

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.kind, self.resource_id, self.revision)
        ):
            raise DutyRefused("Work result reference is incomplete")
        _time(self.available_at)


@dataclass(frozen=True)
class FollowUp:
    duty_id: str
    due_at: int
    result: ResultReference


@dataclass(frozen=True)
class FollowUpClaim:
    claim: DutyClaim
    result: ResultReference


def _check_store(connection: sqlite3.Connection, company_id: str) -> None:
    if _company_version(connection) != (company_id, SCHEMA_REVISION):
        raise DutyRefused("Expected current company work store")


def finish_first_duty(
    store: Path, claim: DutyClaim, result: ResultReference, *, due_at: int
) -> FollowUp:
    """Consume an internal result receipt; callers cannot mint one through Work."""
    _time(due_at)
    with closing(_connect(store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        _check_store(connection, claim.company_id)
        scope, state = first_claim_state(connection, claim)
        existing = connection.execute(
            """SELECT duty_id, due_at, result_kind, result_id, result_revision, available_at
               FROM work_follow_up WHERE previous_duty_id = ?""",
            (claim.duty_id,),
        ).fetchone()
        if existing is not None:
            duty_id, saved_due_at, *saved_result = existing
            if (state, saved_due_at, ResultReference(*saved_result)) != (
                "settled",
                due_at,
                result,
            ):
                raise DutyRefused("Work settlement conflicts with the original receipt")
            return FollowUp(duty_id, due_at, result)
        if (scope, state) != (FIRST_DUTY_SCOPE, "running"):
            raise DutyRefused("Current duty cannot be settled")
        successor = FollowUp(uuid4().hex, due_at, result)
        connection.execute(
            "INSERT INTO duty VALUES (?, ?, 'scheduled')",
            (successor.duty_id, claim.founder_id),
        )
        connection.execute(
            """INSERT INTO work_follow_up (
                   previous_duty_id, duty_id, grant_id, grant_scope, due_at,
                   result_kind, result_id, result_revision, available_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                claim.duty_id,
                successor.duty_id,
                claim.grant_id,
                scope,
                due_at,
                result.kind,
                result.resource_id,
                result.revision,
                result.available_at,
            ),
        )
        connection.execute(
            "UPDATE duty SET state = 'settled' WHERE id = ?", (claim.duty_id,)
        )
    return successor


def claim_follow_up(
    store: Path, company_id: str, execution_profile: str, *, now: int
) -> FollowUpClaim | None:
    _time(now)
    with closing(_connect(store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        _check_store(connection, company_id)
        row = connection.execute(
            """SELECT d.id, f.id, g.id,
                      w.result_kind, w.result_id, w.result_revision, w.available_at
               FROM work_follow_up w
               JOIN duty d ON d.id = w.duty_id AND d.state = 'scheduled'
               JOIN founder f ON f.id = d.founder_id AND f.company_id = ?
               JOIN standing_grant g ON g.id = w.grant_id AND g.founder_id = f.id
               WHERE w.run_id IS NULL AND w.due_at <= ? AND w.available_at <= ?
                 AND g.scope = w.grant_scope AND f.execution_profile = ?
               ORDER BY w.due_at, d.id LIMIT 1""",
            (company_id, now, now, execution_profile),
        ).fetchone()
        if row is None:
            return None
        duty_id, founder_id, grant_id, *stored_result = row
        result = ResultReference(*stored_result)
        claim = DutyClaim(
            company_id, founder_id, grant_id, duty_id, uuid4().hex, execution_profile
        )
        connection.execute(
            "UPDATE work_follow_up SET run_id = ? WHERE duty_id = ?",
            (claim.run_id, duty_id),
        )
        connection.execute("UPDATE duty SET state = 'running' WHERE id = ?", (duty_id,))
    return FollowUpClaim(claim, result)
