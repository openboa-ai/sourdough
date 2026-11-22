"""Operate the paper binding for a hired Agent's first duty and review."""

from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from time import monotonic, sleep, time

from .abandoned_work import requeue_uncommitted_work
from .company import _connect, upgrade_work_schema
from .company_run import (
    PositionReview,
    _order_reference,
    _recover,
    _refresh_recovery,
    _retry_reconciliation,
    _review,
)
from .duty_run import (
    PAPER_EXECUTION,
    POSITION_REVIEW,
    DutyClaim,
    DutyRefused,
    count_company_runs,
)
from .employee_process import EmployeeProcess
from .first_trade import TradeRefused, _apply_paper_order, resume_opening_research
from .hiring_run import HiringRun, prepare_hiring
from .onboarding import first_duty_accepted, invoke_onboarding
from .paper_account import has_pending_binding_change, reopen_binding
from .paper_books import Posting
from .paper_execution import PaperExecution, PaperIntent
from .paper_reconcile import PaperReconciliation
from .paper_research import (
    ResearchDecisionReceipt,
    ResearchTools,
    allows_hired_research_order,
    authorize_hired_research_order,
    read_pending_hired_buy,
    recoverable_hired_research,
    reopen_research_decision,
)
from .work import (
    ResultReference,
    claim_bound_position_review,
    finish_first_duty,
    owed_hired_follow_up_owner,
)
from .writer_lease import WriterLease, hold_company_writer


@dataclass(frozen=True)
class HiredPaperRun:
    hiring: HiringRun | None
    research: ResearchDecisionReceipt | None
    reconciliation: PaperReconciliation
    review: PositionReview | None
    blocked_work: tuple[str, ...] = ()


def _settle_opening(
    writer: WriterLease,
    claim: DutyClaim,
    receipt: ResearchDecisionReceipt,
    venue: PaperExecution,
    posting: Posting | None = None,
    order_id: str | None = None,
) -> bool:
    writer.verify_for(writer.store, claim.company_id)
    if order_id is not None:
        result = _order_reference(
            writer.store, claim.company_id, order_id, posting, venue
        )
        if result is None:
            return False
    elif receipt.decision.action != "buy":
        result = ResultReference(
            "research_finding",
            receipt.finding_id,
            receipt.decision.observation_revision,
            receipt.decided_at,
        )
    else:
        return False
    finish_first_duty(writer.store, claim, result, due_at=receipt.next_due_at)
    return True


def _hired_research(
    writer: WriterLease,
    claim: DutyClaim,
    venue: PaperExecution,
    employee: EmployeeProcess,
) -> tuple[ResearchDecisionReceipt, bool]:
    binding = reopen_binding(writer.store, claim.company_id)
    account = binding.account
    research = ResearchTools(
        writer.store,
        claim,
        venue.observe_quote(account.account_id),
        venue.observe_fee_bps(account.account_id),
        binding.exposure_limit_minor_units,
        writer,
        sizing=venue.observe_order_sizing(account.account_id),
    )

    def invoke(name: str, arguments: dict) -> dict:
        if name == "read_first_duty":
            desk = invoke_onboarding(writer, claim, name, arguments)
            pending = read_pending_hired_buy(writer, claim)
            return desk if pending is None else desk | {"saved_buy": asdict(pending)}
        if name == "accept_first_duty":
            return invoke_onboarding(writer, claim, name, arguments)
        if not first_duty_accepted(writer, claim):
            raise DutyRefused(
                "Accept the assigned first duty before using research tools"
            )
        if name == "request_research_order" and set(arguments) == {"decision_id"}:
            return authorize_hired_research_order(
                writer, claim, arguments["decision_id"]
            )
        return research.invoke(name, arguments)

    interrupted = None
    try:
        employee.research_hired(invoke)
    except TradeRefused as error:
        interrupted = error
    receipt = reopen_research_decision(writer.store, claim)
    if receipt is None:
        raise DutyRefused(
            "Hired paper execution needs the Agent's own committed finding"
        )
    if receipt.decision.action != "buy":
        _settle_opening(writer, claim, receipt, venue)
        return receipt, False
    decision = receipt.decision
    with closing(_connect(writer.store, "ro")) as connection:
        requested = allows_hired_research_order(
            connection,
            claim,
            receipt.decision_id,
            decision.quantity,
            decision.limit_minor_units,
        )
    if not requested:
        if interrupted is not None:
            raise interrupted
        return receipt, False
    intent = PaperIntent(
        receipt.decision_id,
        account.instrument_rules_digest,
        decision.quantity,
        decision.limit_minor_units,
    )
    order = _apply_paper_order(writer.store, claim, intent, venue, writer)
    settled = _settle_opening(
        writer, claim, receipt, venue, order.posting, order.order_id
    )
    return receipt, not settled


def _recover_hired_submission(
    writer: WriterLease,
    venue: PaperExecution,
    previous: PaperReconciliation,
    deadline: float,
) -> tuple[PaperReconciliation, tuple[str, ...]]:
    reconciled, blocked = _refresh_recovery(writer, venue, POSITION_REVIEW, previous)
    return _retry_reconciliation(
        writer, venue, POSITION_REVIEW, reconciled, blocked, deadline
    )


def run_hired_paper_company(
    store: Path,
    company_id: str,
    venue: PaperExecution,
    runtime: Callable[[], EmployeeProcess],
    *,
    turn_budget: int,
    idle_seconds: int,
    budget_from: int,
    store_key: tuple[int, int],
    on_ready: Callable[[WriterLease], None],
) -> HiredPaperRun:
    if not 3 <= turn_budget <= 8 or not 0 <= idle_seconds <= 300:
        raise DutyRefused("Invalid hired-paper activation limits")
    venue.require_independent_from(store)
    with hold_company_writer(store, company_id, expected_store_key=store_key) as writer:
        upgrade_work_schema(store, company_id)
        reconciled, blocked = _recover(writer, venue, POSITION_REVIEW)
        requeue_uncommitted_work(writer, purpose=POSITION_REVIEW, hired_only=True)
        on_ready(writer)
        deadline = monotonic() + idle_seconds
        if reconciled.next_query_at is not None:
            reconciled, blocked = _retry_reconciliation(
                writer, venue, POSITION_REVIEW, reconciled, blocked, deadline
            )
        used = count_company_runs(store, company_id) - budget_from
        if used < 0:
            raise DutyRefused(
                "Company run history is behind the hired paper activation"
            )
        remaining = max(0, turn_budget - used)
        if reconciled.unresolved or blocked:
            return HiredPaperRun(None, None, reconciled, None, blocked)
        with closing(_connect(store, "ro")) as connection:
            switching = has_pending_binding_change(connection)
        opening = (
            None if switching else resume_opening_research(writer, venue, hired=True)
        )
        research = opening.research if opening is not None else None
        if opening is not None and research is not None:
            posting = opening.order.posting if opening.order is not None else None
            order_id = opening.order.order_id if opening.order is not None else None
            settled = _settle_opening(
                writer, opening.claim, research, venue, posting, order_id
            )
            if order_id is not None and not settled:
                reconciled, blocked = _recover_hired_submission(
                    writer, venue, reconciled, deadline
                )
                if reconciled.unresolved or blocked:
                    return HiredPaperRun(None, research, reconciled, None, blocked)
        if not remaining:
            return HiredPaperRun(None, research, reconciled, None)
        employee = runtime()
        agent_id = owed_hired_follow_up_owner(
            store, company_id, employee.profile, POSITION_REVIEW
        )
        hiring = None
        if opening is None and agent_id is None:
            if switching:
                return HiredPaperRun(None, research, reconciled, None)
            hiring = prepare_hiring(
                writer,
                employee,
                first_purpose=PAPER_EXECUTION,
                allow_staffing=remaining >= 2,
                resume_work=recoverable_hired_research,
            )
            claim = hiring.hire_claim
            if claim is None:
                return HiredPaperRun(hiring, None, reconciled, None)
            research, pending = _hired_research(writer, claim, venue, employee)
            hiring = replace(hiring, accepted=True)
            if pending:
                reconciled, blocked = _recover_hired_submission(
                    writer, venue, reconciled, deadline
                )
                if reconciled.unresolved or blocked:
                    return HiredPaperRun(hiring, research, reconciled, None, blocked)
            agent_id = owed_hired_follow_up_owner(
                store, company_id, employee.profile, POSITION_REVIEW
            )
            remaining = turn_budget - (
                count_company_runs(store, company_id) - budget_from
            )
        if agent_id is None or not remaining:
            return HiredPaperRun(hiring, research, reconciled, None)
        while True:
            writer.verify()
            work = claim_bound_position_review(
                store, company_id, agent_id, employee.profile, now=int(time())
            )
            if work is not None:
                return HiredPaperRun(
                    hiring,
                    research,
                    reconciled,
                    _review(writer, work, venue, employee, POSITION_REVIEW),
                )
            available = deadline - monotonic()
            if available <= 0:
                break
            sleep(min(available, 0.5))
        return HiredPaperRun(hiring, research, reconciled, None)
