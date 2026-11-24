"""Run a hired assessment and its saved review without an execution venue."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic, sleep, time

from .abandoned_work import has_uncommitted_work, requeue_uncommitted_work
from .company_assessment import (
    ASSESSMENT,
    AssessmentReceipt,
    invoke_assessment,
    pending_publications,
    read_assessment_result,
    settle_assessment,
)
from .duty_run import ASSESSMENT_REVIEW, DutyClaim, DutyRefused, count_company_runs
from .employee_process import EmployeeProcess
from .first_trade import TradeRefused
from .hiring_run import HiringRun, prepare_hiring
from .runtime.engine import CompanyRuntime, build_default_registry
from .runtime.registry import DutyOutcome
from .work import (
    FollowUp,
    ReviewDecision,
    claim_assessment_review,
    owed_hired_follow_up_owner,
    settle_review,
)
from .writer_lease import WriterLease


@dataclass(frozen=True)
class AssessmentReview:
    claim: DutyClaim
    decision: ReviewDecision
    next_duty: FollowUp


@dataclass(frozen=True)
class AssessmentRun:
    hiring: HiringRun | None
    publication: AssessmentReceipt | None
    restored: tuple[AssessmentReceipt, ...]
    review: AssessmentReview | None


def run_assessment_company(
    store: Path,
    company_id: str,
    runtime: Callable[[], EmployeeProcess],
    *,
    turn_budget: int,
    idle_seconds: int,
    budget_from: int,
    store_key: tuple[int, int],
    on_ready: Callable[[WriterLease], None],
) -> AssessmentRun:
    if not 3 <= turn_budget <= 8 or not 0 <= idle_seconds <= 300:
        raise DutyRefused("Invalid assessment activation limits")
    registry = build_default_registry(
        {"work.assessment": lambda _ctx: DutyOutcome("completed")}
    )
    company_runtime = CompanyRuntime(
        store, company_id, registry, expected_store_key=store_key
    )
    return company_runtime.run_bound_handler(
        "work.assessment",
        lambda writer: _run_assessment(
            writer,
            store,
            company_id,
            runtime,
            turn_budget=turn_budget,
            idle_seconds=idle_seconds,
            budget_from=budget_from,
            on_ready=on_ready,
        ),
    )


def _run_assessment(
    writer: WriterLease,
    store: Path,
    company_id: str,
    runtime: Callable[[], EmployeeProcess],
    *,
    turn_budget: int,
    idle_seconds: int,
    budget_from: int,
    on_ready: Callable[[WriterLease], None],
) -> AssessmentRun:
    restored = []
    for claim, receipt in pending_publications(writer):
        settle_assessment(writer, claim, receipt)
        restored.append(receipt)
    requeue_uncommitted_work(writer, purpose=ASSESSMENT_REVIEW)
    on_ready(writer)
    used = count_company_runs(store, company_id) - budget_from
    if used < 0:
        raise DutyRefused("Company run history is behind the assessment activation")
    remaining = max(0, turn_budget - used)
    if not remaining:
        return AssessmentRun(None, None, tuple(restored), None)

    employee = runtime()
    agent_id = owed_hired_follow_up_owner(
        store, company_id, employee.profile, ASSESSMENT_REVIEW
    )
    hiring = None
    publication = None
    if agent_id is None:
        hiring = prepare_hiring(
            writer,
            employee,
            first_purpose=ASSESSMENT,
            allow_staffing=remaining >= 2,
            resume_work=has_uncommitted_work,
        )
        claim = hiring.hire_claim
        if claim is None:
            return AssessmentRun(hiring, None, tuple(restored), None)
        try:
            employee.assess(
                lambda name, args: invoke_assessment(writer, claim, name, args)
            )
        except TradeRefused:
            if not any(
                original == claim for original, _ in pending_publications(writer)
            ):
                raise
        for original, receipt in pending_publications(writer):
            if original == claim:
                settle_assessment(writer, claim, receipt)
                publication = receipt
                break
        if publication is None:
            return AssessmentRun(hiring, None, tuple(restored), None)
        hiring = replace(hiring, accepted=True)
        agent_id = owed_hired_follow_up_owner(
            store, company_id, employee.profile, ASSESSMENT_REVIEW
        )
        remaining = turn_budget - (count_company_runs(store, company_id) - budget_from)

    review = (
        _review(writer, employee, agent_id, idle_seconds)
        if agent_id and remaining
        else None
    )
    return AssessmentRun(hiring, publication, tuple(restored), review)


def _review(
    writer: WriterLease, employee: EmployeeProcess, agent_id: str, idle_seconds: int
) -> AssessmentReview | None:
    deadline = monotonic() + idle_seconds
    while True:
        writer.verify()
        work = claim_assessment_review(
            writer.store, writer.company_id, agent_id, employee.profile, now=int(time())
        )
        if work is not None:
            proposal = employee.review_assessment(
                lambda: read_assessment_result(writer, work, now=int(time()))
            )
            decision = ReviewDecision.from_proposal(proposal)
            writer.verify()
            next_duty = settle_review(
                writer.store,
                work,
                decision,
                now=int(time()),
                successor_purpose=ASSESSMENT_REVIEW,
            )
            return AssessmentReview(work.claim, decision, next_duty)
        remaining = deadline - monotonic()
        if remaining <= 0:
            return None
        sleep(min(remaining, 0.5))
