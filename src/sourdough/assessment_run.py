"""Run a hired assessment and its saved review without an execution venue."""

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from sqlite3 import Connection
from time import monotonic, sleep, time

from .abandoned_work import requeue_uncommitted_work
from .company import _connect, upgrade_work_schema
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
from .work import FollowUp, ReviewDecision, claim_assessment_review, settle_review
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


def _owed_agent(writer: WriterLease, execution_profile: str) -> str | None:
    with closing(_connect(writer.store, "ro")) as connection:
        candidates = connection.execute(
            """SELECT a.id FROM work_follow_up w
               JOIN duty d ON d.id=w.duty_id AND d.state='scheduled'
               JOIN agent a ON a.id=d.agent_id AND a.company_id=?
               JOIN standing_grant g ON g.id=w.grant_id AND g.agent_id=a.id
               WHERE w.purpose=? AND a.execution_profile=? AND g.scope=w.grant_scope
               ORDER BY max(w.due_at,w.available_at),d.id""",
            (writer.company_id, ASSESSMENT_REVIEW, execution_profile),
        )
        return next(
            (
                agent_id
                for (agent_id,) in candidates
                if _has_hired_turn(connection, agent_id)
            ),
            None,
        )


def _has_hired_turn(connection: Connection, agent_id: str) -> bool:
    row = connection.execute(
        """SELECT h.allocated_turns > count(r.run_id)
           FROM hire_receipt h
           LEFT JOIN duty d ON d.agent_id=h.agent_id LEFT JOIN duty_run r ON r.duty_id=d.id
           WHERE h.agent_id=? GROUP BY h.agent_id""",
        (agent_id,),
    ).fetchone()
    return row == (1,)


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
        agent_id = _owed_agent(writer, employee.profile)
        hiring = None
        publication = None
        if agent_id is None and remaining >= 2:
            hiring = prepare_hiring(writer, employee, first_purpose=ASSESSMENT)
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
            agent_id = _owed_agent(writer, employee.profile)
            remaining -= 2

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
