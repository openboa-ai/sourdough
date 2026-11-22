"""Persist a completed duty and claim its eligible successor."""

import sqlite3
from collections.abc import Callable
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


@dataclass(frozen=True)
class ReviewDecision:
    call_id: str
    observation_revision: str
    action: str
    rationale: str
    next_check_seconds: int

    @classmethod
    def from_proposal(cls, proposal: dict) -> "ReviewDecision":
        keys = {
            "tool",
            "call_id",
            "observation_revision",
            "action",
            "rationale",
            "next_check_seconds",
        }
        if set(proposal) != keys or proposal["tool"] != "record_review_decision":
            raise DutyRefused("Expected a scoped review proposal")
        return cls(
            *(
                proposal[key]
                for key in (
                    "call_id",
                    "observation_revision",
                    "action",
                    "rationale",
                    "next_check_seconds",
                )
            )
        )

    def __post_init__(self) -> None:
        if (
            not all(
                isinstance(value, str) and 0 < len(value.strip()) <= limit
                for value, limit in (
                    (self.call_id, 128),
                    (self.observation_revision, 128),
                    (self.rationale, 2000),
                )
            )
            or self.action not in ("hold", "investigate", "recommend_change")
            or type(self.next_check_seconds) is not int
            or not 60 <= self.next_check_seconds <= 86400
        ):
            raise DutyRefused("Invalid review decision")


def _check_store(connection: sqlite3.Connection, company_id: str) -> None:
    if _company_version(connection) != (company_id, SCHEMA_REVISION):
        raise DutyRefused("Expected current company work store")


def _schedule(
    connection: sqlite3.Connection,
    previous: DutyClaim,
    scope: str,
    result: ResultReference,
    due_at: int,
) -> FollowUp:
    successor = FollowUp(uuid4().hex, due_at, result)
    connection.execute(
        "INSERT INTO duty VALUES (?, ?, 'scheduled')",
        (successor.duty_id, previous.founder_id),
    )
    connection.execute(
        """INSERT INTO work_follow_up (
               previous_duty_id, duty_id, grant_id, grant_scope, due_at,
               result_kind, result_id, result_revision, available_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            previous.duty_id,
            successor.duty_id,
            previous.grant_id,
            scope,
            due_at,
            result.kind,
            result.resource_id,
            result.revision,
            result.available_at,
        ),
    )
    connection.execute(
        "UPDATE duty SET state = 'settled' WHERE id = ?", (previous.duty_id,)
    )
    return successor


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
        return _schedule(connection, claim, scope, result, due_at)


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


def _review_state(
    connection: sqlite3.Connection, follow_up: FollowUpClaim
) -> tuple[str, str]:
    claim = follow_up.claim
    _check_store(connection, claim.company_id)
    row = connection.execute(
        """SELECT d.state, g.scope, w.result_kind, w.result_id,
                  w.result_revision, w.available_at
           FROM work_follow_up w
           JOIN duty d ON d.id = w.duty_id
           JOIN founder f ON f.id = d.founder_id
           JOIN standing_grant g ON g.id = w.grant_id AND g.founder_id = f.id
           WHERE d.id = ? AND w.run_id = ? AND g.id = ? AND g.scope = w.grant_scope
             AND f.id = ? AND f.company_id = ? AND f.execution_profile = ?""",
        (
            claim.duty_id,
            claim.run_id,
            claim.grant_id,
            claim.founder_id,
            claim.company_id,
            claim.execution_profile,
        ),
    ).fetchone()
    if row is None or ResultReference(*row[2:]) != follow_up.result:
        raise DutyRefused("Review claim or grant is no longer current")
    return row[0], row[1]


def read_follow_up_result(
    store: Path,
    follow_up: FollowUpClaim,
    *,
    now: int,
    read: Callable[[sqlite3.Connection, ResultReference], dict],
) -> dict:
    _time(now)
    claim = follow_up.claim
    with closing(_connect(store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        state, _ = _review_state(connection, follow_up)
        if state != "running" or now < follow_up.result.available_at:
            raise DutyRefused("Review cannot read this result")
        result = read(connection, follow_up.result)
        if result.get("revision") != follow_up.result.revision:
            raise DutyRefused("Review result revision changed")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS work_result_read (
                duty_id TEXT PRIMARY KEY NOT NULL REFERENCES work_follow_up(duty_id),
                run_id TEXT NOT NULL,
                result_revision TEXT NOT NULL,
                read_at INTEGER NOT NULL
            )"""
        )
        connection.execute(
            """INSERT OR IGNORE INTO work_result_read VALUES (?, ?, ?, ?)""",
            (claim.duty_id, claim.run_id, follow_up.result.revision, now),
        )
    return result


def settle_review(
    store: Path, follow_up: FollowUpClaim, decision: ReviewDecision, *, now: int
) -> FollowUp:
    _time(now)
    next_due = _time(now + decision.next_check_seconds)
    claim = follow_up.claim
    with closing(_connect(store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        state, scope = _review_state(connection, follow_up)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS work_review_decision (
                duty_id TEXT PRIMARY KEY NOT NULL REFERENCES work_follow_up(duty_id),
                run_id TEXT NOT NULL,
                call_id TEXT NOT NULL UNIQUE,
                result_revision TEXT NOT NULL,
                action TEXT NOT NULL,
                rationale TEXT NOT NULL,
                next_check_seconds INTEGER NOT NULL,
                decided_at INTEGER NOT NULL
            )"""
        )
        terms = (
            claim.run_id,
            decision.call_id,
            decision.observation_revision,
            decision.action,
            decision.rationale,
            decision.next_check_seconds,
        )
        if state == "settled":
            replay = connection.execute(
                """SELECT j.run_id, j.call_id, j.result_revision, j.action, j.rationale,
                          j.next_check_seconds, w.duty_id, w.due_at
                   FROM work_review_decision j
                   JOIN work_follow_up w ON w.previous_duty_id = j.duty_id
                   WHERE j.duty_id = ?""",
                (claim.duty_id,),
            ).fetchone()
            if replay is not None and replay[:6] == terms:
                return FollowUp(replay[6], replay[7], follow_up.result)
            raise DutyRefused("Review conflicts with the original decision")
        has_reads = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name = 'work_result_read' AND type = 'table'"
        ).fetchone()
        observed = (
            has_reads
            and connection.execute(
                "SELECT run_id, result_revision, read_at FROM work_result_read WHERE duty_id = ?",
                (claim.duty_id,),
            ).fetchone()
        )
        if (
            state != "running"
            or not observed
            or observed[:2] != (claim.run_id, decision.observation_revision)
            or decision.observation_revision != follow_up.result.revision
            or observed[2] > now
        ):
            raise DutyRefused("Decision requires a preceding authorized result read")
        connection.execute(
            "INSERT INTO work_review_decision VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (claim.duty_id, *terms, now),
        )
        return _schedule(connection, claim, scope, follow_up.result, next_due)


def mark_review_incomplete(store: Path, follow_up: FollowUpClaim) -> None:
    with closing(_connect(store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        state, _ = _review_state(connection, follow_up)
        if state == "running":
            connection.execute(
                "UPDATE duty SET state = 'incomplete' WHERE id = ?",
                (follow_up.claim.duty_id,),
            )
