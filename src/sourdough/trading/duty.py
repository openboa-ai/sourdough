"""Trading domain work purposes, scopes, authority helpers, and committed-result predicates."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from ..duty_run import ASSESSMENT_REVIEW, DutyClaim, RunAuthority
from ..first_duty import (
    claim_first_duty,
    require_historical_claim,
    require_scope_authority,
    resolve_first_run,
)
from ..runtime.domains import WorkDomainPolicy
from ..work import (
    FollowUp,
    FollowUpClaim,
    ResultEligibility,
    ResultReference,
    ReviewDecision,
    claim_agent_follow_up,
    claim_follow_up,
    finish_first_duty,
    settle_review,
)

PAPER_EXECUTION = "paper_execution"
POSITION_REVIEW = "position_review"
FIRST_DUTY_SCOPE = "paper"

TRADING_WORK_POLICY = WorkDomainPolicy(
    domain_name="trading",
    purpose_scopes={
        PAPER_EXECUTION: FIRST_DUTY_SCOPE,
        POSITION_REVIEW: FIRST_DUTY_SCOPE,
    },
    follow_up_purposes=frozenset({PAPER_EXECUTION, POSITION_REVIEW}),
    review_purposes=frozenset({POSITION_REVIEW}),
    allowed_transitions=frozenset(
        {
            (PAPER_EXECUTION, PAPER_EXECUTION),
            (PAPER_EXECUTION, POSITION_REVIEW),
            (POSITION_REVIEW, POSITION_REVIEW),
        }
    ),
    result_replacement_purposes=frozenset({PAPER_EXECUTION}),
)


def claim_paper_first_duty(
    store: Path, company_id: str, execution_profile: str
) -> DutyClaim:
    return claim_first_duty(
        store,
        company_id,
        execution_profile,
        scope=FIRST_DUTY_SCOPE,
        purpose=PAPER_EXECUTION,
    )


def resolve_paper_first_run(
    connection: sqlite3.Connection, claim: DutyClaim
) -> RunAuthority:
    return resolve_first_run(connection, claim, purpose=PAPER_EXECUTION)


def require_historical_paper_claim(
    connection: sqlite3.Connection, claim: DutyClaim
) -> None:
    require_historical_claim(connection, claim, purpose=PAPER_EXECUTION)


def require_paper_authority(connection: sqlite3.Connection, claim: DutyClaim) -> None:
    require_scope_authority(
        connection, claim, purpose=PAPER_EXECUTION, scope=FIRST_DUTY_SCOPE
    )


def finish_paper_first_duty(
    store: Path,
    claim: DutyClaim,
    result: ResultReference,
    *,
    due_at: int,
    purpose: str = POSITION_REVIEW,
) -> FollowUp:
    return finish_first_duty(
        store,
        claim,
        result,
        due_at=due_at,
        purpose=purpose,
        first_purpose=PAPER_EXECUTION,
        expected_scope=FIRST_DUTY_SCOPE,
        allowed_purposes=(PAPER_EXECUTION, POSITION_REVIEW),
    )


def claim_paper_follow_up(
    store: Path,
    company_id: str,
    execution_profile: str,
    *,
    now: int,
    allow_paper_execution: bool = False,
    eligible_paper_result: Callable[[sqlite3.Connection, ResultReference], bool]
    | None = None,
) -> FollowUpClaim | None:
    purposes = (
        (POSITION_REVIEW, PAPER_EXECUTION)
        if allow_paper_execution
        else (POSITION_REVIEW,)
    )
    eligible_by_purpose = (
        {PAPER_EXECUTION: eligible_paper_result}
        if eligible_paper_result is not None
        else None
    )
    return claim_follow_up(
        store,
        company_id,
        execution_profile,
        now=now,
        purposes=purposes,
        eligible_by_purpose=eligible_by_purpose,
    )


def claim_bound_position_review(
    store: Path,
    company_id: str,
    agent_id: str,
    execution_profile: str,
    *,
    now: int,
) -> FollowUpClaim | None:
    return claim_agent_follow_up(
        store,
        company_id,
        agent_id,
        execution_profile,
        now=now,
        purpose=POSITION_REVIEW,
    )


def allow_trading_review_transition(
    from_purpose: str,
    to_purpose: str,
    successor_result: ResultReference | None,
    *,
    historical_paper: bool = False,
    paper_review_only: bool = False,
) -> bool:
    transition = (
        (paper_review_only or historical_paper)
        and from_purpose == PAPER_EXECUTION
        and (
            (to_purpose == PAPER_EXECUTION and successor_result is not None)
            or (to_purpose == POSITION_REVIEW and successor_result is None)
        )
    )
    if transition:
        return True
    if from_purpose == POSITION_REVIEW and to_purpose in (
        POSITION_REVIEW,
        PAPER_EXECUTION,
    ):
        return True
    if from_purpose == ASSESSMENT_REVIEW and to_purpose == ASSESSMENT_REVIEW:
        return True
    return False


def settle_paper_review(
    store: Path,
    follow_up: FollowUpClaim,
    decision: ReviewDecision,
    *,
    now: int,
    successor_purpose: str = POSITION_REVIEW,
    successor_result: ResultReference | None = None,
    historical_paper: bool = False,
    paper_review_only: bool = False,
    eligible_result: ResultEligibility | None = None,
) -> FollowUp:
    return settle_review(
        store,
        follow_up,
        decision,
        now=now,
        successor_purpose=successor_purpose,
        successor_result=successor_result,
        eligible_result=eligible_result,
        allow_transition=lambda frm, to, res: allow_trading_review_transition(
            frm,
            to,
            res,
            historical_paper=historical_paper,
            paper_review_only=paper_review_only,
        ),
    )


def has_trading_committed_result(
    connection: sqlite3.Connection, tables: set[str], duty_id: str
) -> bool:
    if "trading_book_order" in tables:
        if "work_runs" in tables:
            row = connection.execute(
                """SELECT 1 FROM trading_book_order o
                   JOIN work_runs wr ON wr.run_id = o.run_id
                   WHERE wr.work_item_id = ? LIMIT 1""",
                (duty_id,),
            ).fetchone()
            if row is not None:
                return True
        if "duty_run" in tables:
            row = connection.execute(
                """SELECT 1 FROM trading_book_order o
                   JOIN duty_run dr ON dr.run_id = o.run_id
                   WHERE dr.duty_id = ? LIMIT 1""",
                (duty_id,),
            ).fetchone()
            if row is not None:
                return True
    return False
