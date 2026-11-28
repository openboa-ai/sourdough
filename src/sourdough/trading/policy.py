"""Pure Trading domain work policy constants, transition rules, and committed-result predicates."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..duty_run import ASSESSMENT_REVIEW
from ..runtime.domains import WorkDomainPolicy

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


def allow_trading_review_transition(
    from_purpose: str,
    to_purpose: str,
    successor_result: Any | None,
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
