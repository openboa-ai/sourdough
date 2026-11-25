"""Run the paper company without the Client."""

from collections.abc import Callable
from contextlib import closing, nullcontext
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from sqlite3 import Connection
from time import monotonic, sleep, time, time_ns

from .abandoned_work import requeue_uncommitted_work
from .company import _connect
from .storage.db import open_reader, read_tx
from .duty_run import (
    PAPER_EXECUTION,
    POSITION_REVIEW,
    DutyClaim,
    DutyRefused,
    count_company_runs,
    employment_cutoff,
)
from .employee_process import EmployeeProcess
from .first_duty import founding_duty_due
from .first_trade import (
    TradeRefused,
    TradeResult,
    _apply_paper_order,
    resume_opening_research,
    run_first_trade,
)
from .onboarding import is_hired_first_duty
from .paper_account import (
    has_pending_binding_change,
    is_retired_account_scope,
    reopen_binding,
)
from .paper_books import (
    NoFillObservation,
    Posting,
    PostingRefused,
    capture_position,
    read_binding_position,
    read_book_observation,
    read_booked_no_fill,
    read_captured_position,
)
from .paper_decision import (
    PaperDecision,
    PaperDecisionReceipt,
    persist_paper_decision,
    reopen_paper_decision,
    reopen_unsubmitted_decisions,
    settle_paper_decision,
)
from .paper_execution import PaperExecution, PaperIntent, observe_bound_position
from .paper_order import reserved_exposure
from .paper_reconcile import PaperReconciliation, reconcile_paper_orders
from .paper_research import read_research_result, reopen_research_decision
from .paper_review import bind_review_quote, observe_review_quote
from .paper_risk import effective_paper_risk
from .paper_window import RegisteredWindow
from .runtime.engine import CompanyRuntime, build_default_registry
from .runtime.registry import DutyOutcome
from .window_sampler import WindowSampler
from .writer_lease import WriterLease
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


_PAPER_RESULTS = ("paper_position", "paper_no_fill", "paper_binding")


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
    venue: PaperExecution,
    runtime: EmployeeProcess,
    *,
    repeat_paper: bool = False,
    turn_budget: int = 2,
    idle_seconds: int = 0,
    expected_store_key: tuple[int, int] | None = None,
    budget_from: int | None = None,
    on_ready: Callable[[WriterLease], None] | None = None,
    sampler: WindowSampler | None = None,
    start_work_before_ns: int | None = None,
) -> CompanyRun:
    if not 1 <= turn_budget <= 8 or not 0 <= idle_seconds <= 900:
        raise TradeRefused("Invalid company turn or idle budget")
    if start_work_before_ns is not None and (
        sampler is None
        or sampler.observe_through_ns is None
        or not 0 < start_work_before_ns <= sampler.observe_through_ns
    ):
        raise TradeRefused("Work cutoff requires a bounded observation segment")
    registry = build_default_registry(
        {"paper.company_operation": lambda _ctx: DutyOutcome("completed")}
    )
    company_runtime = CompanyRuntime(
        store,
        company_id,
        registry,
        expected_store_key=expected_store_key,
    )

    def _run_with_sampler(writer: WriterLease) -> CompanyRun:
        sampling = (
            sampler.activate(writer, venue) if sampler is not None else nullcontext()
        )
        with sampling as window:
            return _operate(
                writer,
                venue,
                runtime,
                repeat_paper,
                turn_budget,
                idle_seconds,
                budget_from,
                on_ready,
                window,
                start_work_before_ns,
            )

    return company_runtime.run_bound_handler(
        "paper.company_operation",
        _run_with_sampler,
        venue=venue,
    )


def _operate(
    writer: WriterLease,
    venue: PaperExecution,
    runtime: EmployeeProcess,
    repeat_paper: bool,
    turn_budget: int,
    idle_seconds: int,
    budget_from: int | None,
    on_ready: Callable[[WriterLease], None] | None,
    window: RegisteredWindow | None,
    start_work_before_ns: int | None,
) -> CompanyRun:
    store, company_id = writer.store, writer.company_id
    writer.verify()
    window_limit = window.plan.agent_turn_limit if window is not None else None
    if budget_from is not None or window_limit is not None:
        current = count_company_runs(store, company_id)
        if budget_from is not None:
            if current < budget_from:
                raise TradeRefused(
                    "Company run history is behind the activation budget baseline"
                )
            turn_budget -= current - budget_from
        if window_limit is not None:
            sealed_count = window.run_count_at_seal
            if sealed_count is None or current < sealed_count:
                raise TradeRefused(
                    "Company run history is behind the sealed window baseline"
                )
            turn_budget = min(turn_budget, window_limit - (current - sealed_count))
        turn_budget = max(0, turn_budget)
    idle_deadline = monotonic() + idle_seconds
    binding = reopen_binding(store, company_id)
    active_scope = (binding.account.account_id, binding.account.instrument_rules_digest)
    with closing(open_reader(store)) as connection, read_tx(connection):
        switching = has_pending_binding_change(connection)
        suspended = effective_paper_risk(connection).suspended
    repeat_paper = repeat_paper and not (switching or suspended)
    next_purpose = PAPER_EXECUTION if repeat_paper else POSITION_REVIEW
    reconciliation, blocked = _recover(writer, venue, next_purpose)
    fenced_runs = requeue_uncommitted_work(writer)
    if on_ready is not None:
        on_ready(writer)
    first = (
        None
        if reconciliation.unresolved or switching or suspended
        else resume_opening_research(writer, venue)
    )

    def may_start_work():
        return start_work_before_ns is None or time_ns() < start_work_before_ns

    if (
        first is None
        and turn_budget
        and may_start_work()
        and not reconciliation.unresolved
        and not switching
        and not suspended
        and founding_duty_due(store, company_id)
    ):
        first = run_first_trade(store, company_id, venue, runtime, writer=writer)
        turn_budget -= 1
    if first is not None:
        research = first.research
        writer.verify()
        result = (
            _order_reference(
                store, company_id, first.order.order_id, first.order.posting, venue
            )
            if first.order is not None
            else None
        )
        if result is not None:
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
        elif first.order is None:
            return CompanyRun(
                first,
                None,
                reconciliation=reconciliation,
                blocked_work=(*blocked, first.claim.duty_id),
                fenced_runs=fenced_runs,
            )
        else:
            reconciliation, blocked = _refresh_recovery(
                writer, venue, next_purpose, reconciliation
            )

    review = None
    trades = _resume_later_decisions(
        writer,
        venue,
        allow_buy=repeat_paper and not (reconciliation.unresolved or blocked),
    )
    if trades and trades[-1].next_duty is None:
        reconciliation, blocked = _refresh_recovery(
            writer, venue, next_purpose, reconciliation
        )

    def scope_of(connection, reference):
        evidence = _read_paper_observation(connection, reference, company_id)
        return evidence["account_id"], evidence["instrument_rules_digest"]

    def eligible(connection, reference):
        if reference.kind not in _PAPER_RESULTS:
            return False
        scope = scope_of(connection, reference)
        return scope == active_scope or (
            is_retired_account_scope(connection, company_id, *scope)
            and read_binding_position(connection, company_id, binding.revision)
            is not None
        )

    while turn_budget or reconciliation.next_query_at is not None:
        if not may_start_work():
            turn_budget = 0
        if (
            reconciliation.next_query_at is not None
            and reconciliation.next_query_at <= int(time())
        ):
            reconciliation, blocked = _refresh_recovery(
                writer, venue, next_purpose, reconciliation
            )
            resumed = _resume_later_decisions(
                writer,
                venue,
                allow_buy=repeat_paper and not (reconciliation.unresolved or blocked),
            )
            trades.extend(resumed)
            if resumed and resumed[-1].next_duty is None:
                reconciliation, blocked = _refresh_recovery(
                    writer, venue, next_purpose, reconciliation
                )
        writer.verify()
        follow_up = None
        if turn_budget:
            follow_up = claim_follow_up(
                store,
                company_id,
                runtime.profile,
                now=int(time()),
                allow_paper_execution=(repeat_paper or suspended)
                and not (reconciliation.unresolved or blocked),
                eligible_paper_result=eligible,
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
        historical_execution = False
        if follow_up.purpose == PAPER_EXECUTION:
            with closing(_connect(store, "ro")) as connection:
                historical_execution = (
                    scope_of(connection, follow_up.result) != active_scope
                )
        if follow_up.purpose == POSITION_REVIEW or historical_execution or suspended:
            review = _review(writer, follow_up, venue, runtime, next_purpose)
        else:
            trade = _trade(writer, follow_up, venue, runtime)
            trades.append(trade)
            if trade.next_duty is None:
                reconciliation, blocked = _refresh_recovery(
                    writer, venue, next_purpose, reconciliation
                )
    return CompanyRun(
        first, review, tuple(trades), reconciliation, blocked, fenced_runs
    )


def _recover(
    writer: WriterLease, venue: PaperExecution, purpose: str
) -> tuple[PaperReconciliation, tuple[str, ...]]:
    store, company_id = writer.store, writer.company_id
    recovery = reconcile_paper_orders(
        store, company_id, venue, now=int(time()), writer=writer
    )
    if not recovery.recovered:
        return recovery, ()
    with closing(open_reader(store)) as connection, read_tx(connection):
        (first_duty_id,) = connection.execute(
            "SELECT due_work_id FROM founding_receipt WHERE company_id = ?",
            (company_id,),
        ).fetchone()
        hired_opening = {
            order.original_claim.duty_id
            for order in recovery.recovered
            if is_hired_first_duty(connection, order.original_claim)
        }
        archived = {
            order.original_claim.founder_id
            for order in recovery.recovered
            if employment_cutoff(connection, order.original_claim.founder_id)
            is not None
        }
    blocked = []
    for original in recovery.recovered:
        writer.verify()
        claim = original.original_claim
        if claim.founder_id in archived:
            continue
        try:
            if original.posting is not None:
                result = _book_reference(store, company_id, original.posting, venue)
                if result is None:
                    raise DutyRefused("Position observation is not available")
            elif original.no_fill is not None:
                result = _no_fill_reference(original.no_fill)
            else:
                raise DutyRefused("Reconciled order has no book result")
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


def _retry_reconciliation(
    writer: WriterLease,
    venue: PaperExecution,
    purpose: str,
    recovery: PaperReconciliation,
    blocked: tuple[str, ...],
    deadline: float,
) -> tuple[PaperReconciliation, tuple[str, ...]]:
    while recovery.next_query_at is not None and not blocked:
        remaining = deadline - monotonic()
        delay = max(0, recovery.next_query_at - time())
        if remaining <= 0 or delay >= remaining:
            break
        if delay:
            sleep(delay)
        recovery, blocked = _refresh_recovery(writer, venue, purpose, recovery)
    return recovery, blocked


def _refresh_recovery(
    writer: WriterLease,
    venue: PaperExecution,
    purpose: str,
    previous: PaperReconciliation,
) -> tuple[PaperReconciliation, tuple[str, ...]]:
    recovery, blocked = _recover(writer, venue, purpose)
    return replace(recovery, recovered=previous.recovered + recovery.recovered), blocked


def _book_reference(
    store: Path, company_id: str, posting: Posting, venue: PaperExecution
) -> ResultReference | None:
    observation = read_captured_position(store, company_id, posting.fill_id)
    if observation is None:
        account = reopen_binding(store, company_id).account
        source = observe_bound_position(venue, account, posting.order_id)
        if source is None:
            return None
        observation = capture_position(
            store, company_id, posting.fill_id, source, now=int(time())
        )
    return ResultReference(
        "paper_position",
        observation.fill_id,
        observation.revision,
        observation.available_at,
    )


def _no_fill_reference(observation: NoFillObservation) -> ResultReference:
    return ResultReference(
        "paper_no_fill",
        observation.order_id,
        observation.revision,
        observation.available_at,
    )


def _order_reference(
    store: Path,
    company_id: str,
    order_id: str,
    posting: Posting | None,
    venue: PaperExecution,
) -> ResultReference | None:
    if posting is not None:
        return _book_reference(store, company_id, posting, venue)
    no_fill = read_booked_no_fill(store, company_id, order_id)
    return _no_fill_reference(no_fill) if no_fill else None


def _read_paper_observation(
    connection: Connection, reference: ResultReference, company_id: str
) -> dict:
    if reference.kind == "research_finding":
        return read_research_result(connection, company_id, reference.resource_id)
    try:
        observation = read_book_observation(
            connection, company_id, reference.kind, reference.resource_id
        )
    except PostingRefused as error:
        raise DutyRefused("Committed paper observation is unavailable") from error
    return asdict(observation) | {"revision": observation.revision}


def _review(
    writer: WriterLease,
    follow_up: FollowUpClaim,
    venue: PaperExecution,
    runtime: EmployeeProcess,
    next_purpose: str,
) -> PositionReview:
    store = writer.store

    def read_result():
        writer.verify()
        quote = (
            observe_review_quote(store, follow_up.claim.company_id, venue)
            if follow_up.result.kind in _PAPER_RESULTS
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
            purpose=follow_up.purpose,
        )

    try:
        decision = ReviewDecision.from_proposal(runtime.review(read_result))
        writer.verify()
        successor = None
        purpose = (
            next_purpose if follow_up.result.kind in _PAPER_RESULTS else POSITION_REVIEW
        )
        if purpose == PAPER_EXECUTION:
            binding = reopen_binding(store, writer.company_id)
            with closing(open_reader(store)) as connection, read_tx(connection):
                prior = _read_paper_observation(
                    connection, follow_up.result, writer.company_id
                )
                if (prior["account_id"], prior["instrument_rules_digest"]) != (
                    binding.account.account_id,
                    binding.account.instrument_rules_digest,
                ):
                    baseline = read_binding_position(
                        connection, writer.company_id, binding.revision
                    )
                    if baseline is None:
                        purpose = POSITION_REVIEW
                    else:
                        successor = ResultReference(
                            "paper_binding",
                            baseline.binding_revision,
                            baseline.revision,
                            baseline.available_at,
                        )
        next_duty = settle_review(
            store,
            follow_up,
            decision,
            now=int(time()),
            successor_purpose=purpose,
            successor_result=successor,
            paper_review_only=follow_up.purpose == PAPER_EXECUTION,
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
    venue: PaperExecution,
    runtime: EmployeeProcess,
) -> LaterPaperTrade:
    store = writer.store
    claim = follow_up.claim
    binding = reopen_binding(store, claim.company_id)
    account = binding.account
    if venue.observe_account(account.account_id) != account:
        raise TradeRefused("Paper destination does not match the bound account")
    quote = venue.observe_quote(account.account_id)
    sizing = venue.observe_order_sizing(account.account_id)
    sizing_context = (
        {
            "order_sizing": asdict(sizing),
            "position_units": "lots",
            "quote_units": "quote_minor_units_per_lot",
        }
        if sizing is not None
        else {}
    )

    def project(connection, reference):
        observation = _read_paper_observation(connection, reference, claim.company_id)
        if (observation["account_id"], observation["instrument_rules_digest"]) != (
            account.account_id,
            account.instrument_rules_digest,
        ):
            raise DutyRefused("The position belongs to a different account")
        available = binding.exposure_limit_minor_units - reserved_exposure(
            connection, account.account_id
        )
        return (
            observation
            | sizing_context
            | {
                "quote_minor_units": quote,
                "available_exposure_minor_units": available,
            }
        )

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
    writer: WriterLease, venue: PaperExecution, *, allow_buy: bool
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
    venue: PaperExecution,
    receipt: PaperDecisionReceipt,
) -> LaterPaperTrade:
    writer.verify()
    store, claim, decision = writer.store, follow_up.claim, receipt.decision
    if decision.action != "buy":
        next_duty = settle_paper_decision(store, follow_up, receipt, follow_up.result)
        return LaterPaperTrade(claim, receipt, None, None, next_duty)
    account = reopen_binding(store, claim.company_id).account
    order = _apply_paper_order(
        store,
        claim,
        PaperIntent(
            receipt.decision_id,
            account.instrument_rules_digest,
            decision.quantity,
            decision.limit_minor_units,
        ),
        venue,
        writer,
    )
    result = _order_reference(
        store, claim.company_id, order.order_id, order.posting, venue
    )
    if result is None:
        return LaterPaperTrade(claim, receipt, order.order_id, order.posting, None)
    writer.verify()
    next_duty = settle_paper_decision(store, follow_up, receipt, result)
    return LaterPaperTrade(claim, receipt, order.order_id, order.posting, next_duty)
