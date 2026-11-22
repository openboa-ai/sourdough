"""Run the paper company without the Client."""

from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from sqlite3 import Connection
from time import monotonic, sleep, time

from .abandoned_work import requeue_uncommitted_work
from .company import _connect, upgrade_work_schema
from .duty_run import (
    PAPER_EXECUTION,
    POSITION_REVIEW,
    DutyClaim,
    DutyRefused,
    count_company_runs,
)
from .employee_process import EmployeeProcess
from .first_duty import founding_duty_due
from .first_trade import (
    TradeRefused,
    TradeResult,
    resume_opening_research,
    run_first_trade,
)
from .onboarding import is_hired_first_duty
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
    reopen_paper_decision,
    reopen_unsubmitted_decisions,
    settle_paper_decision,
)
from .paper_order import PaperIntent, reserved_exposure, submit_paper_order
from .paper_reconcile import PaperReconciliation, reconcile_paper_orders
from .paper_research import read_research_result, reopen_research_decision
from .paper_review import bind_review_quote, observe_review_quote
from .paper_venue import FixedQuoteVenue
from .writer_lease import WriterLease, hold_company_writer
from .work import (
    FollowUp,
    FollowUpClaim,
    ResultReference,
    ReviewDecision,
    claim_follow_up,
    finish_first_duty,
    mark_review_incomplete,
    read_follow_up_result,
    reopen_follow_up,
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
    reconciliation: PaperReconciliation | None = None
    blocked_work: tuple[str, ...] = ()
    fenced_runs: tuple[str, ...] = ()


def run_paper_company(
    store: Path,
    company_id: str,
    venue: FixedQuoteVenue,
    runtime: EmployeeProcess,
    *,
    repeat_paper: bool = False,
    turn_budget: int = 2,
    idle_seconds: int = 0,
    expected_store_key: tuple[int, int] | None = None,
    budget_from: int | None = None,
    on_ready: Callable[[WriterLease], None] | None = None,
) -> CompanyRun:
    if not 1 <= turn_budget <= 8 or not 0 <= idle_seconds <= 900:
        raise TradeRefused("Invalid company turn or idle budget")
    with hold_company_writer(
        store, company_id, expected_store_key=expected_store_key
    ) as writer:
        return _operate(
            writer,
            venue,
            runtime,
            repeat_paper,
            turn_budget,
            idle_seconds,
            budget_from,
            on_ready,
        )


def _operate(
    writer: WriterLease,
    venue: FixedQuoteVenue,
    runtime: EmployeeProcess,
    repeat_paper: bool,
    turn_budget: int,
    idle_seconds: int,
    budget_from: int | None,
    on_ready: Callable[[WriterLease], None] | None,
) -> CompanyRun:
    store, company_id = writer.store, writer.company_id
    writer.verify()
    upgrade_work_schema(store, company_id)
    if budget_from is not None:
        consumed = count_company_runs(store, company_id) - budget_from
        if consumed < 0:
            raise TradeRefused(
                "Company run history is behind the activation budget baseline"
            )
        turn_budget = max(0, turn_budget - consumed)
    idle_deadline = monotonic() + idle_seconds
    next_purpose = PAPER_EXECUTION if repeat_paper else POSITION_REVIEW
    reconciliation, blocked = _recover(writer, venue, next_purpose)
    fenced_runs = requeue_uncommitted_work(writer)
    if on_ready is not None:
        on_ready(writer)
    first = (
        None if reconciliation.unresolved else resume_opening_research(writer, venue)
    )
    if (
        first is None
        and turn_budget
        and not reconciliation.unresolved
        and founding_duty_due(store, company_id)
    ):
        first = run_first_trade(store, company_id, venue, runtime, writer=writer)
        turn_budget -= 1
    if first is not None:
        research = first.research
        writer.verify()
        if first.order is not None and first.order.posting is not None:
            result = _book_reference(store, company_id, first.order.posting)
            finish_first_duty(
                store,
                first.claim,
                result,
                due_at=research.next_due_at if research else result.available_at,
                purpose=next_purpose,
            )
        elif research is not None and research.decision.action != "buy":
            result = ResultReference(
                "research_finding",
                research.finding_id,
                research.decision.observation_revision,
                research.decided_at,
            )
            finish_first_duty(store, first.claim, result, due_at=research.next_due_at)
        else:
            return CompanyRun(
                first,
                None,
                reconciliation=reconciliation,
                blocked_work=blocked,
                fenced_runs=fenced_runs,
            )

    review = None
    trades = _resume_later_decisions(
        writer, venue, allow_buy=repeat_paper and not reconciliation.unresolved
    )
    if trades and trades[-1].next_duty is None:
        return CompanyRun(
            first, review, tuple(trades), reconciliation, blocked, fenced_runs
        )
    while turn_budget or reconciliation.next_query_at is not None:
        if (
            reconciliation.next_query_at is not None
            and reconciliation.next_query_at <= int(time())
        ):
            reconciliation, blocked = _recover(writer, venue, next_purpose)
            trades.extend(
                _resume_later_decisions(
                    writer,
                    venue,
                    allow_buy=repeat_paper and not reconciliation.unresolved,
                )
            )
            if trades and trades[-1].next_duty is None:
                break
        writer.verify()
        follow_up = None
        if turn_budget:
            follow_up = claim_follow_up(
                store,
                company_id,
                runtime.profile,
                now=int(time()),
                allow_paper_execution=repeat_paper and not reconciliation.unresolved,
            )
        if follow_up is None:
            if not turn_budget and reconciliation.next_query_at is None:
                break
            remaining = idle_deadline - monotonic()
            if remaining <= 0:
                break
            sleep(min(remaining, 0.5))
            continue
        turn_budget -= 1
        if follow_up.purpose == POSITION_REVIEW:
            review = _review(writer, follow_up, venue, runtime, next_purpose)
        else:
            trade = _trade(writer, follow_up, venue, runtime)
            trades.append(trade)
            if trade.next_duty is None:
                break
    return CompanyRun(
        first, review, tuple(trades), reconciliation, blocked, fenced_runs
    )


def _recover(
    writer: WriterLease, venue: FixedQuoteVenue, purpose: str
) -> tuple[PaperReconciliation, tuple[str, ...]]:
    store, company_id = writer.store, writer.company_id
    recovery = reconcile_paper_orders(
        store, company_id, venue, now=int(time()), writer=writer
    )
    if not recovery.recovered:
        return recovery, ()
    with closing(_connect(store, "ro")) as connection:
        (first_duty_id,) = connection.execute(
            "SELECT due_work_id FROM founding_receipt WHERE company_id = ?",
            (company_id,),
        ).fetchone()
        hired_opening = {
            order.original_claim.duty_id
            for order in recovery.recovered
            if is_hired_first_duty(connection, order.original_claim)
        }
    blocked = []
    for original in recovery.recovered:
        writer.verify()
        result = _book_reference(store, company_id, original.posting)
        claim = original.original_claim
        try:
            hired = claim.duty_id in hired_opening
            if claim.duty_id == first_duty_id or hired:
                research = reopen_research_decision(store, claim)
                if hired and research is None:
                    raise DutyRefused(
                        "Hired paper fill must retain its original research"
                    )
                finish_first_duty(
                    store,
                    claim,
                    result,
                    due_at=research.next_due_at if research else result.available_at,
                    purpose=POSITION_REVIEW if hired else purpose,
                )
            else:
                work = reopen_follow_up(store, claim)
                decision = reopen_paper_decision(store, work)
                settle_paper_decision(store, work, decision, result)
        except DutyRefused:
            blocked.append(claim.duty_id)
    return recovery, tuple(blocked)


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
    if reference.kind == "research_finding":
        return read_research_result(connection, company_id, reference.resource_id)
    if reference.kind != "paper_position":
        raise DutyRefused("This duty does not reference a paper position")
    try:
        observation = read_position(connection, company_id, reference.resource_id)
    except PostingRefused as error:
        raise DutyRefused("Committed paper observation is unavailable") from error
    return asdict(observation) | {"revision": observation.revision}


def _review(
    writer: WriterLease,
    follow_up: FollowUpClaim,
    venue: FixedQuoteVenue,
    runtime: EmployeeProcess,
    next_purpose: str,
) -> PositionReview:
    store = writer.store

    def read_result():
        writer.verify()
        quote = (
            observe_review_quote(store, follow_up.claim.company_id, venue)
            if follow_up.result.kind == "paper_position"
            else None
        )

        def project(connection, reference):
            result = _read_paper_observation(
                connection, reference, follow_up.claim.company_id
            )
            if quote is None:
                return result
            return bind_review_quote(connection, follow_up.claim.run_id, result, quote)

        return read_follow_up_result(
            store,
            follow_up,
            now=int(time()),
            read=project,
        )

    try:
        decision = ReviewDecision.from_proposal(runtime.review(read_result))
        writer.verify()
        next_duty = settle_review(
            store,
            follow_up,
            decision,
            now=int(time()),
            successor_purpose=next_purpose
            if follow_up.result.kind == "paper_position"
            else POSITION_REVIEW,
        )
    except (DutyRefused, TradeRefused):
        try:
            writer.verify()
            mark_review_incomplete(store, follow_up)
        except DutyRefused:
            pass
        raise
    return PositionReview(follow_up.claim, decision, next_duty)


def _trade(
    writer: WriterLease,
    follow_up: FollowUpClaim,
    venue: FixedQuoteVenue,
    runtime: EmployeeProcess,
) -> LaterPaperTrade:
    store = writer.store
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
        writer.verify()
        return read_follow_up_result(
            store, follow_up, now=int(time()), read=project, purpose=PAPER_EXECUTION
        )

    decision = PaperDecision.from_proposal(runtime.trade(read_result))
    writer.verify()
    receipt = persist_paper_decision(store, follow_up, decision, now=int(time()))
    return _apply_later_decision(writer, follow_up, venue, receipt)


def _resume_later_decisions(
    writer: WriterLease, venue: FixedQuoteVenue, *, allow_buy: bool
) -> list[LaterPaperTrade]:
    writer.verify()
    resumed = []
    for work, receipt in reopen_unsubmitted_decisions(
        writer.store, writer.company_id, allow_buy=allow_buy
    ):
        result = _apply_later_decision(writer, work, venue, receipt)
        resumed.append(result)
        if result.next_duty is None:
            break
    return resumed


def _apply_later_decision(
    writer: WriterLease,
    follow_up: FollowUpClaim,
    venue: FixedQuoteVenue,
    receipt: PaperDecisionReceipt,
) -> LaterPaperTrade:
    writer.verify()
    store, claim, decision = writer.store, follow_up.claim, receipt.decision
    if decision.action != "buy":
        next_duty = settle_paper_decision(store, follow_up, receipt, follow_up.result)
        return LaterPaperTrade(claim, receipt, None, None, next_duty)
    account = reopen_binding(store, claim.company_id).account
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
        writer=writer,
    )
    posting = post_confirmed_fill(
        store, claim.company_id, order.order_id, venue, writer=writer
    )
    if posting is None:
        return LaterPaperTrade(claim, receipt, order.order_id, None, None)
    writer.verify()
    result = _book_reference(store, claim.company_id, posting)
    next_duty = settle_paper_decision(store, follow_up, receipt, result)
    return LaterPaperTrade(claim, receipt, order.order_id, posting, next_duty)
