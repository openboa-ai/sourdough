"""Persist a completed duty and claim its eligible successor."""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .company import RESULT_READ_SCHEMA, SCHEMA_REVISION, _company_version, _connect
from .duty_run import (
    ASSESSMENT_REVIEW,
    PAPER_EXECUTION,
    POSITION_REVIEW,
    DutyClaim,
    DutyRefused,
    resolve_run,
    start_run,
)
from .first_duty import FIRST_DUTY_SCOPE, resolve_first_run


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
    purpose: str = POSITION_REVIEW


@dataclass(frozen=True)
class FollowUpClaim:
    claim: DutyClaim
    result: ResultReference
    purpose: str = POSITION_REVIEW


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


def schedule_follow_up(
    connection: sqlite3.Connection,
    previous: DutyClaim,
    scope: str,
    result: ResultReference,
    due_at: int,
    *,
    purpose: str = POSITION_REVIEW,
) -> FollowUp:
    if purpose not in (POSITION_REVIEW, PAPER_EXECUTION, ASSESSMENT_REVIEW):
        raise DutyRefused("Unsupported duty purpose")
    successor = FollowUp(uuid4().hex, due_at, result, purpose)
    connection.execute(
        "INSERT INTO duty VALUES (?, ?, 'scheduled')",
        (successor.duty_id, previous.founder_id),
    )
    connection.execute(
        """INSERT INTO work_follow_up (
               previous_duty_id, duty_id, grant_id, grant_scope, due_at,
               result_kind, result_id, result_revision, available_at, purpose
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            purpose,
        ),
    )
    connection.execute(
        "UPDATE duty SET state = 'settled' WHERE id = ?", (previous.duty_id,)
    )
    return successor


def finish_first_duty(
    store: Path,
    claim: DutyClaim,
    result: ResultReference,
    *,
    due_at: int,
    purpose: str = POSITION_REVIEW,
) -> FollowUp:
    """Consume an internal result receipt; callers cannot mint one through Work."""
    _time(due_at)
    with closing(_connect(store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        _check_store(connection, claim.company_id)
        authority = resolve_first_run(connection, claim)
        scope, state = authority.current_scope, authority.duty_state
        existing = connection.execute(
            """SELECT duty_id, due_at, purpose, result_kind, result_id, result_revision, available_at
               FROM work_follow_up WHERE previous_duty_id = ?""",
            (claim.duty_id,),
        ).fetchone()
        if existing is not None:
            duty_id, saved_due_at, saved_purpose, *saved_result = existing
            if (state, saved_due_at, ResultReference(*saved_result), saved_purpose) != (
                "settled",
                due_at,
                result,
                purpose,
            ):
                raise DutyRefused("Work settlement conflicts with the original receipt")
            return FollowUp(duty_id, due_at, result, purpose)
        authority.require_current(PAPER_EXECUTION)
        if (scope, state) != (FIRST_DUTY_SCOPE, "running"):
            raise DutyRefused("Current duty cannot be settled")
        return schedule_follow_up(
            connection, claim, scope, result, due_at, purpose=purpose
        )


def claim_follow_up(
    store: Path,
    company_id: str,
    execution_profile: str,
    *,
    now: int,
    allow_paper_execution: bool = False,
) -> FollowUpClaim | None:
    purposes = (
        (POSITION_REVIEW, PAPER_EXECUTION)
        if allow_paper_execution
        else (POSITION_REVIEW,)
    )
    return _claim_follow_up(store, company_id, execution_profile, now, purposes)


def claim_assessment_review(
    store: Path,
    company_id: str,
    agent_id: str,
    execution_profile: str,
    *,
    now: int,
) -> FollowUpClaim | None:
    return _claim_follow_up(
        store,
        company_id,
        execution_profile,
        now,
        (ASSESSMENT_REVIEW,),
        agent_id=agent_id,
    )


def _claim_follow_up(
    store: Path,
    company_id: str,
    execution_profile: str,
    now: int,
    purposes: tuple[str, ...],
    *,
    agent_id: str | None = None,
) -> FollowUpClaim | None:
    _time(now)
    placeholders = ", ".join("?" for _ in purposes)
    with closing(_connect(store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        _check_store(connection, company_id)
        row = connection.execute(
            f"""SELECT d.id, f.id, g.id, w.grant_scope, w.purpose,
                      w.result_kind, w.result_id, w.result_revision, w.available_at
               FROM work_follow_up w
               JOIN duty d ON d.id = w.duty_id AND d.state = 'scheduled'
               JOIN agent f ON f.id = d.agent_id AND f.company_id = ?
               JOIN standing_grant g ON g.id = w.grant_id AND g.agent_id = f.id
               WHERE w.due_at <= ? AND w.available_at <= ?
                 AND g.scope = w.grant_scope AND f.execution_profile = ?
                 AND (? IS NULL OR f.id = ?)
                 AND w.purpose IN ({placeholders})
               ORDER BY w.due_at, d.id LIMIT 1""",
            (
                company_id,
                now,
                now,
                execution_profile,
                agent_id,
                agent_id,
                *purposes,
            ),
        ).fetchone()
        if row is None:
            return None
        duty_id, founder_id, grant_id, scope, purpose, *stored_result = row
        result = ResultReference(*stored_result)
        claim = start_run(
            connection,
            company_id,
            founder_id,
            grant_id,
            duty_id,
            execution_profile,
            purpose=purpose,
            grant_scope=scope,
        )
    return FollowUpClaim(claim, result, purpose)


def follow_up_state(
    connection: sqlite3.Connection, follow_up: FollowUpClaim, purpose: str
) -> tuple[str, str]:
    claim = follow_up.claim
    _check_store(connection, claim.company_id)
    authority = resolve_run(connection, claim)
    authority.require_current(purpose)
    row = connection.execute(
        """SELECT result_kind, result_id, result_revision, available_at
           FROM work_follow_up WHERE duty_id = ? AND grant_id = ? AND purpose = ?""",
        (claim.duty_id, claim.grant_id, purpose),
    ).fetchone()
    if (
        row is None
        or ResultReference(*row) != follow_up.result
        or purpose != follow_up.purpose
    ):
        raise DutyRefused("Review result does not match its scheduled duty")
    return authority.duty_state, authority.current_scope


def reopen_follow_up(store: Path, claim: DutyClaim) -> FollowUpClaim:
    with closing(_connect(store, "ro")) as connection:
        connection.execute("BEGIN")
        _check_store(connection, claim.company_id)
        authority = resolve_run(connection, claim)
        row = connection.execute(
            """SELECT result_kind, result_id, result_revision, available_at, purpose
               FROM work_follow_up WHERE duty_id = ? AND grant_id = ?""",
            (claim.duty_id, claim.grant_id),
        ).fetchone()
        if row is None or row[-1] != authority.purpose:
            raise DutyRefused("No matching scheduled duty for the original run")
        return FollowUpClaim(claim, ResultReference(*row[:4]), row[4])


def read_follow_up_result(
    store: Path,
    follow_up: FollowUpClaim,
    *,
    now: int,
    read: Callable[[sqlite3.Connection, ResultReference], dict],
    purpose: str = POSITION_REVIEW,
) -> dict:
    _time(now)
    claim = follow_up.claim
    with closing(_connect(store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        state, _ = follow_up_state(connection, follow_up, purpose)
        if state != "running" or now < follow_up.result.available_at:
            raise DutyRefused("Review cannot read this result")
        result = read(connection, follow_up.result)
        if result.get("revision") != follow_up.result.revision:
            raise DutyRefused("Review result revision changed")
        connection.execute(RESULT_READ_SCHEMA)
        connection.execute(
            """INSERT OR IGNORE INTO work_result_read VALUES (?, ?, ?, ?)""",
            (claim.duty_id, claim.run_id, follow_up.result.revision, now),
        )
    return result


def require_result_read(
    connection: sqlite3.Connection, follow_up: FollowUpClaim, revision: str, now: int
) -> None:
    claim = follow_up.claim
    has_reads = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE name = 'work_result_read' AND type = 'table'"
    ).fetchone()
    observed = (
        has_reads
        and connection.execute(
            "SELECT run_id, result_revision, read_at FROM work_result_read WHERE duty_id = ? AND run_id = ?",
            (claim.duty_id, claim.run_id),
        ).fetchone()
    )
    if (
        not observed
        or observed[:2] != (claim.run_id, revision)
        or revision != follow_up.result.revision
        or observed[2] > now
    ):
        raise DutyRefused("Decision requires a preceding authorized result read")


def settle_review(
    store: Path,
    follow_up: FollowUpClaim,
    decision: ReviewDecision,
    *,
    now: int,
    successor_purpose: str = POSITION_REVIEW,
) -> FollowUp:
    _time(now)
    next_due = _time(now + decision.next_check_seconds)
    claim = follow_up.claim
    if follow_up.purpose not in (POSITION_REVIEW, ASSESSMENT_REVIEW) or (
        (follow_up.purpose == ASSESSMENT_REVIEW)
        != (successor_purpose == ASSESSMENT_REVIEW)
    ):
        raise DutyRefused("Review cannot cross into a different work domain")
    with closing(_connect(store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        state, scope = follow_up_state(connection, follow_up, follow_up.purpose)
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
                          j.next_check_seconds, w.duty_id, w.due_at, w.purpose
                   FROM work_review_decision j
                   JOIN work_follow_up w ON w.previous_duty_id = j.duty_id
                   WHERE j.duty_id = ?""",
                (claim.duty_id,),
            ).fetchone()
            if (
                replay is not None
                and replay[:6] == terms
                and replay[8] == successor_purpose
            ):
                return FollowUp(replay[6], replay[7], follow_up.result, replay[8])
            raise DutyRefused("Review conflicts with the original decision")
        if state != "running":
            raise DutyRefused("Duty cannot settle a review")
        require_result_read(connection, follow_up, decision.observation_revision, now)
        connection.execute(
            "INSERT INTO work_review_decision VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (claim.duty_id, *terms, now),
        )
        return schedule_follow_up(
            connection,
            claim,
            scope,
            follow_up.result,
            next_due,
            purpose=successor_purpose,
        )


def mark_review_incomplete(store: Path, follow_up: FollowUpClaim) -> None:
    with closing(_connect(store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        state, _ = follow_up_state(connection, follow_up, POSITION_REVIEW)
        if state == "running":
            connection.execute(
                "UPDATE duty SET state = 'incomplete' WHERE id = ?",
                (follow_up.claim.duty_id,),
            )
