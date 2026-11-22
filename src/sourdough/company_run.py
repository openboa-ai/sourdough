"""Run the paper company without the Client."""

from dataclasses import asdict, dataclass
from pathlib import Path
from sqlite3 import Connection
from time import monotonic, sleep, time

from .company import reopen, upgrade_work_schema
from .duty_run import PAPER_EXECUTION, POSITION_REVIEW, DutyClaim, DutyRefused
from .employee_process import EmployeeProcess
from .first_trade import TradeRefused, TradeResult, run_first_trade
from .paper_account import reopen_binding
from .paper_books import (
    Posting,
    PostingRefused,
    capture_position,
    post_confirmed_fill,
    read_position,
)
from .paper_decision import (
    PaperDecision,
    PaperDecisionReceipt,
    persist_paper_decision,
    settle_paper_decision,
)
from .paper_order import PaperIntent, reserved_exposure, submit_paper_order
from .paper_venue import FixedQuoteVenue
from .work import (
    FollowUp,
    FollowUpClaim,
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
class LaterPaperTrade:
    claim: DutyClaim
    decision: PaperDecisionReceipt
    order_id: str | None
    posting: Posting | None
    next_duty: FollowUp | None


@dataclass(frozen=True)
class CompanyRun:
    first_trade: TradeResult | None
    review: PositionReview | None
    later_trades: tuple[LaterPaperTrade, ...] = ()


def run_paper_company(
    store: Path,
    company_id: str,
    venue: FixedQuoteVenue,
    runtime: EmployeeProcess,
    *,
    repeat_paper: bool = False,
    turn_budget: int = 2,
    idle_seconds: int = 0,
) -> CompanyRun:
    if not 1 <= turn_budget <= 8 or not 0 <= idle_seconds <= 900:
        raise TradeRefused("Invalid company turn or idle budget")
    upgrade_work_schema(store, company_id)
    idle_deadline = monotonic() + idle_seconds
    next_purpose = PAPER_EXECUTION if repeat_paper else POSITION_REVIEW
    first = None
    if reopen(store, company_id).due_work_ids:
        first = run_first_trade(store, company_id, venue, runtime)
        turn_budget -= 1
        if first.order is None or first.order.posting is None:
            return CompanyRun(first, None)
        result = _book_reference(store, company_id, first.order.posting)
        finish_first_duty(
            store, first.claim, result, due_at=result.available_at, purpose=next_purpose
        )

    review = None
    trades = []
    while turn_budget:
        follow_up = claim_follow_up(
            store,
            company_id,
            runtime.profile,
            now=int(time()),
            allow_paper_execution=repeat_paper,
        )
        if follow_up is None:
            remaining = idle_deadline - monotonic()
            if remaining <= 0:
                break
            sleep(min(remaining, 0.5))
            continue
        turn_budget -= 1
        if follow_up.purpose == POSITION_REVIEW:
            review = _review(store, follow_up, runtime, next_purpose)
        else:
            trade = _trade(store, follow_up, venue, runtime)
            trades.append(trade)
            if trade.next_duty is None:
                break
    return CompanyRun(first, review, tuple(trades))


def _book_reference(store: Path, company_id: str, posting: Posting) -> ResultReference:
    observation = capture_position(store, company_id, posting.fill_id, now=int(time()))
    return ResultReference(
        "paper_position",
        observation.fill_id,
        observation.revision,
        observation.available_at,
    )


def _read_paper_observation(
    connection: Connection, reference: ResultReference, company_id: str
) -> dict:
    if reference.kind != "paper_position":
        raise DutyRefused("This duty does not reference a paper position")
    try:
        observation = read_position(connection, company_id, reference.resource_id)
    except PostingRefused as error:
        raise DutyRefused("Committed paper observation is unavailable") from error
    return asdict(observation) | {"revision": observation.revision}


def _review(
    store: Path,
    follow_up: FollowUpClaim,
    runtime: EmployeeProcess,
    next_purpose: str,
) -> PositionReview:
    def read_result():
        return read_follow_up_result(
            store,
            follow_up,
            now=int(time()),
            read=lambda db, ref: _read_paper_observation(
                db, ref, follow_up.claim.company_id
            ),
        )

    try:
        decision = ReviewDecision.from_proposal(runtime.review(read_result))
        next_duty = settle_review(
            store,
            follow_up,
            decision,
            now=int(time()),
            successor_purpose=next_purpose,
        )
    except (DutyRefused, TradeRefused):
        try:
            mark_review_incomplete(store, follow_up)
        except DutyRefused:
            pass
        raise
    return PositionReview(follow_up.claim, decision, next_duty)


def _trade(
    store: Path,
    follow_up: FollowUpClaim,
    venue: FixedQuoteVenue,
    runtime: EmployeeProcess,
) -> LaterPaperTrade:
    claim = follow_up.claim
    binding = reopen_binding(store, claim.company_id)
    account = binding.account
    if venue.observe_account(account.account_id) != account:
        raise TradeRefused("Paper destination does not match the bound account")
    quote = venue.observe_quote(account.account_id)

    def project(connection, reference):
        observation = _read_paper_observation(connection, reference, claim.company_id)
        if observation["account_id"] != account.account_id:
            raise DutyRefused("The position belongs to a different account")
        available = binding.exposure_limit_minor_units - reserved_exposure(
            connection, account.account_id
        )
        return observation | {
            "quote_minor_units": quote,
            "available_exposure_minor_units": available,
        }

    def read_result():
        return read_follow_up_result(
            store, follow_up, now=int(time()), read=project, purpose=PAPER_EXECUTION
        )

    decision = PaperDecision.from_proposal(runtime.trade(read_result))
    receipt = persist_paper_decision(store, follow_up, decision, now=int(time()))
    if decision.action != "buy":
        next_duty = settle_paper_decision(store, follow_up, receipt, follow_up.result)
        return LaterPaperTrade(claim, receipt, None, None, next_duty)
    order = submit_paper_order(
        store,
        claim,
        PaperIntent(
            receipt.decision_id,
            account.instrument_rules_digest,
            decision.quantity,
            decision.limit_minor_units,
        ),
        venue,
    )
    posting = post_confirmed_fill(store, claim.company_id, order.order_id, venue)
    if posting is None:
        return LaterPaperTrade(claim, receipt, order.order_id, None, None)
    result = _book_reference(store, claim.company_id, posting)
    next_duty = settle_paper_decision(store, follow_up, receipt, result)
    return LaterPaperTrade(claim, receipt, order.order_id, posting, next_duty)
