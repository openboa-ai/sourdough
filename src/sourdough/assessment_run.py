"""Run a hired assessment and its saved review without an execution venue."""

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from time import monotonic, sleep, time

from .abandoned_work import has_uncommitted_work, requeue_uncommitted_work
from .company import upgrade_work_schema
from .company_assessment import (
    ASSESSMENT,
    ASSESSMENT_REVIEW_WORKFLOW,
    ASSESSMENT_WORKFLOW,
    AssessmentReceipt,
    invoke_assessment,
    pending_publications,
    persist_assessment_draft,
    read_assessment_result,
    settle_assessment,
)
from .duty_run import ASSESSMENT_REVIEW, DutyClaim, DutyRefused, count_company_runs
from .employee_process import EmployeeProcess
from .first_trade import TradeRefused
from .harness import ScopedRun
from .hiring_run import HiringRun, prepare_hiring
from .work import (
    FollowUp,
    FollowUpClaim,
    ReviewDecision,
    claim_assessment_review,
    owed_hired_follow_up_owner,
    settle_review,
)
from .writer_lease import WriterLease, hold_company_writer


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
    later_reviews: tuple[AssessmentReview, ...] = ()


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
    with hold_company_writer(store, company_id, expected_store_key=store_key) as writer:
        upgrade_work_schema(store, company_id)
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
                employee.run_scoped(
                    "assessment_duty",
                    ScopedRun(
                        writer,
                        claim,
                        ASSESSMENT_WORKFLOW,
                        lambda name, args: invoke_assessment(writer, claim, name, args),
                    ),
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
            remaining = turn_budget - (
                count_company_runs(store, company_id) - budget_from
            )

        reviews = []
        deadline = monotonic() + idle_seconds
        while agent_id and remaining > 0:
            review = _review(writer, employee, agent_id, deadline)
            if review is None:
                break
            reviews.append(review)
            remaining -= 1
            agent_id = owed_hired_follow_up_owner(
                store, company_id, employee.profile, ASSESSMENT_REVIEW
            )
        return AssessmentRun(
            hiring,
            publication,
            tuple(restored),
            reviews[0] if reviews else None,
            tuple(reviews[1:]),
        )


def _review(
    writer: WriterLease, employee: EmployeeProcess, agent_id: str, deadline: float
) -> AssessmentReview | None:
    while True:
        writer.verify()
        work = claim_assessment_review(
            writer.store, writer.company_id, agent_id, employee.profile, now=int(time())
        )
        if work is not None:
            return _run_review(writer, employee, work)
        remaining = deadline - monotonic()
        if remaining <= 0:
            return None
        sleep(min(remaining, 0.5))


def _run_review(
    writer: WriterLease, employee: EmployeeProcess, work: FollowUpClaim
) -> AssessmentReview:
    committed = None

    def invoke(name: str, arguments: dict) -> dict:
        nonlocal committed
        if name == "save_private_draft":
            return persist_assessment_draft(
                writer, work.claim, ASSESSMENT_REVIEW, arguments
            )
        if name == "read_work_result":
            return read_assessment_result(writer, work, now=int(time()))
        if name != "record_review_decision":
            raise DutyRefused("Tool is unavailable for this company review")
        decision = ReviewDecision(call_id=f"review:{work.claim.run_id}", **arguments)
        writer.verify()
        next_duty = settle_review(
            writer.store,
            work,
            decision,
            now=int(time()),
            successor_purpose=ASSESSMENT_REVIEW,
        )
        committed = AssessmentReview(work.claim, decision, next_duty)
        return asdict(committed)

    run = ScopedRun(writer, work.claim, ASSESSMENT_REVIEW_WORKFLOW, invoke)
    try:
        employee.run_scoped("assessment_review", run)
    except TradeRefused:
        if committed is None:
            raise
    if committed is None:
        raise DutyRefused("Company review returned without a committed decision")
    return committed
