"""Trading domain work purposes, scopes, legacy DutyClaim helpers, and committed-result predicates."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from ..duty_run import DutyClaim, RunAuthority
from ..first_duty import (
    claim_first_duty,
    require_historical_claim,
    require_scope_authority,
    resolve_first_run,
)
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
from .policy import (
    FIRST_DUTY_SCOPE,
    PAPER_EXECUTION,
    POSITION_REVIEW,
    TRADING_WORK_POLICY,
    allow_trading_review_transition,
    has_trading_committed_result,
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


__all__ = [
    "FIRST_DUTY_SCOPE",
    "PAPER_EXECUTION",
    "POSITION_REVIEW",
    "TRADING_WORK_POLICY",
    "allow_trading_review_transition",
    "claim_bound_position_review",
    "claim_paper_first_duty",
    "claim_paper_follow_up",
    "finish_paper_first_duty",
    "has_trading_committed_result",
    "require_historical_paper_claim",
    "require_paper_authority",
    "resolve_paper_first_run",
    "settle_paper_review",
]
