"""Run the first paper duty and its scoped position review without the Client."""

from dataclasses import asdict, dataclass
from pathlib import Path
from time import time

from .company import reopen, upgrade_work_schema
from .employee_process import EmployeeProcess
from .first_duty import DutyClaim, DutyRefused
from .first_trade import TradeRefused, TradeResult, run_first_trade
from .paper_books import PostingRefused, capture_position, read_position
from .paper_venue import FixedQuoteVenue
from .work import (
    FollowUp,
    ResultReference,
    ReviewDecision,
    claim_follow_up,
    finish_first_duty,
    mark_review_incomplete,
    read_follow_up_result,
    settle_review,
)


@dataclass(frozen=True)
class PositionReview:
    claim: DutyClaim
    decision: ReviewDecision
    next_duty: FollowUp


@dataclass(frozen=True)
class CompanyRun:
    first_trade: TradeResult | None
    review: PositionReview | None


def run_paper_company(
    store: Path, company_id: str, venue: FixedQuoteVenue, runtime: EmployeeProcess
) -> CompanyRun:
    upgrade_work_schema(store, company_id)
    first = None
    if reopen(store, company_id).due_work_ids:
        first = run_first_trade(store, company_id, venue, runtime)
        if first.order is None or first.order.posting is None:
            return CompanyRun(first, None)
        observation = capture_position(
            store, company_id, first.order.posting.fill_id, now=int(time())
        )
        result = ResultReference(
            "paper_position",
            observation.fill_id,
            observation.revision,
            observation.available_at,
        )
        finish_first_duty(store, first.claim, result, due_at=observation.available_at)

    follow_up = claim_follow_up(store, company_id, runtime.profile, now=int(time()))
    if follow_up is None:
        return CompanyRun(first, None)

    def project(connection, reference):
        if reference.kind != "paper_position":
            raise DutyRefused("This duty does not reference a paper position")
        try:
            observation = read_position(connection, company_id, reference.resource_id)
        except PostingRefused as error:
            raise DutyRefused("Committed paper observation is unavailable") from error
        return asdict(observation) | {"revision": observation.revision}

    def read_result():
        return read_follow_up_result(store, follow_up, now=int(time()), read=project)

    try:
        decision = ReviewDecision.from_proposal(runtime.review(read_result))
        next_duty = settle_review(store, follow_up, decision, now=int(time()))
    except (DutyRefused, TradeRefused):
        try:
            mark_review_incomplete(store, follow_up)
        except DutyRefused:
            pass
        raise
    return CompanyRun(first, PositionReview(follow_up.claim, decision, next_duty))
