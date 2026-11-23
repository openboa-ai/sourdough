"""Run a hired assessment and its saved review without an execution venue."""

from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from functools import partial
from pathlib import Path
from time import monotonic, sleep, time

from .abandoned_work import has_uncommitted_work, requeue_uncommitted_work
from .collaboration import company_work_run
from .company import _connect, upgrade_work_schema
from .company_assessment import (
    ASSESSMENT,
    ASSESSMENT_REVIEW_WORKFLOW,
    ASSESSMENT_WORKFLOW,
    MANAGER_ASSESSMENT,
    AssessmentReceipt,
    invoke_assessment,
    manager_review_project,
    pending_publications,
    persist_assessment_draft,
    read_assessment_result,
    review_result_allowed,
    settle_assessment,
)
from .duty_run import ASSESSMENT_REVIEW, DutyClaim, DutyRefused, count_company_runs
from .employee_process import EmployeeProcess
from .first_trade import TradeRefused
from .harness import ScopedRun, ScopedWorkflow, tool
from .hiring_run import HiringRun, prepare_hiring
from .work import (
    FollowUp,
    FollowUpClaim,
    ReviewDecision,
    ResultEligibility,
    claim_assessment_review,
    owed_hired_follow_up_owner,
    settle_review,
)
from .work_peer_response import (
    PEER_RESPONSE,
    PeerQuestion,
    answer_peer_question,
    assigned_peer,
    claim_peer_response,
    claim_resumed_peer_work,
    has_waiting_peer_work,
    peer_turn_has_disposition,
    peer_wait_for_run,
    read_peer_outcome,
    read_peer_question,
    recover_unanswered_peer_runs,
    request_peer_response,
)
from .writer_lease import WriterLease, hold_company_writer


_REQUEST_PEER = tool(
    "request_peer_response",
    "Ask the authorized colleague a question and yield this duty until an answer or deadline.",
    {
        "request_key": {"type": "string"},
        "recipient_id": {"type": "string"},
        "question": {"type": "string"},
        "expected_response": {"type": "string"},
        "expires_at": {
            "type": "integer",
            "description": "Absolute Unix seconds, after company_time.",
        },
        "asset_id": {"type": "string"},
        "asset_revision": {"type": "string"},
    },
)
_READ_PEER_ROUTE = tool(
    "read_assigned_peer", "Read your approved decision recipient.", {}
)
_READ_PEER_OUTCOME = tool(
    "read_peer_outcome", "Read why your original duty resumed.", {}
)
_PEER_WORKFLOW = ScopedWorkflow(
    PEER_RESPONSE,
    "Read the scoped question and deliver your answer or decline. "
    "Do not take over the requester's work or alter company records.",
    (
        tool("read_peer_question", "Read the approved Work question and evidence.", {}),
        tool(
            "answer_peer_question",
            "Record the decision or why you decline.",
            {
                "outcome": {"enum": ["answered", "declined"]},
                "answer": {"type": "string"},
            },
        ),
    ),
    "answer_peer_question",
)


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
        recover_unanswered_peer_runs(writer)
        on_ready(writer)
        used = count_company_runs(store, company_id) - budget_from
        if used < 0:
            raise DutyRefused("Company run history is behind the assessment activation")
        remaining = max(0, turn_budget - used)
        if not remaining:
            return AssessmentRun(None, None, tuple(restored), None)

        employee = runtime()

        eligible_result = partial(review_result_allowed, company_id=company_id)

        def next_owner():
            return owed_hired_follow_up_owner(
                store,
                company_id,
                employee.profile,
                ASSESSMENT_REVIEW,
                eligible_additional_owner=lambda connection, agent_id, result: (
                    result.kind == MANAGER_ASSESSMENT
                    and eligible_result(connection, agent_id, result)
                ),
            )

        agent_id = next_owner()
        hiring = None
        publication = None
        if agent_id is None and not has_waiting_peer_work(writer):
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
            remaining = turn_budget - (
                count_company_runs(store, company_id) - budget_from
            )
            publication = _run_assessment(
                writer, employee, claim, allow_peer=remaining >= 2
            )
            if publication is None and peer_wait_for_run(writer, claim) is None:
                return AssessmentRun(hiring, None, tuple(restored), None)
            hiring = replace(hiring, accepted=True)

        reviews = []
        deadline = monotonic() + idle_seconds
        while remaining > 0:
            now = int(time())
            resumed = claim_resumed_peer_work(writer, employee.profile, now=now)
            if resumed is not None:
                result = _run_assessment(
                    writer, employee, resumed, allow_peer=remaining >= 3
                )
                if result is not None:
                    publication = result
                remaining -= 1
                continue
            response = claim_peer_response(writer, employee.profile, now=now)
            if response is not None:
                _run_peer_answer(writer, employee, response)
                remaining -= 1
                continue
            agent_id = next_owner()
            if agent_id is not None:
                work = claim_assessment_review(
                    store,
                    company_id,
                    agent_id,
                    employee.profile,
                    now=now,
                    eligible_result=eligible_result,
                )
                if work is not None:
                    reviews.append(_run_review(writer, employee, work, eligible_result))
                    remaining -= 1
                    continue
            remaining_idle = deadline - monotonic()
            if (
                remaining_idle <= 0
                or agent_id is None
                and not has_waiting_peer_work(writer)
            ):
                break
            sleep(min(remaining_idle, 0.5))
        return AssessmentRun(
            hiring,
            publication,
            tuple(restored),
            reviews[0] if reviews else None,
            tuple(reviews[1:]),
        )


def _run_assessment(
    writer: WriterLease,
    employee: EmployeeProcess,
    claim: DutyClaim,
    *,
    allow_peer: bool,
) -> AssessmentReceipt | None:
    workflow = ASSESSMENT_WORKFLOW
    peer = assigned_peer(writer, claim) if allow_peer else None
    if peer is not None:
        workflow = replace(
            workflow,
            tools=workflow.tools + (_READ_PEER_ROUTE, _REQUEST_PEER),
            terminal_tool=("publish_assessment", "request_peer_response"),
            instruction=workflow.instruction
            + " If a colleague's decision is required before "
            "publication, read your assigned peer and request a response. This yields the duty. "
            "A chat message does not yield or start the colleague. Use empty asset fields if no "
            "shared asset is required.",
        )
    resumed = read_peer_outcome(writer, claim) is not None
    peer_outcome_read = False
    if resumed:
        workflow = replace(
            workflow,
            tools=workflow.tools + (_READ_PEER_OUTCOME,),
            instruction="Your original work resumed. Read the recorded peer outcome before "
            "deciding the next action. " + workflow.instruction,
        )

    def invoke(name: str, args: dict) -> dict:
        nonlocal peer_outcome_read
        if name == "read_assigned_peer":
            current = assigned_peer(writer, claim)
            if current is None:
                raise DutyRefused("The authorized decision route is unavailable")
            return current | {"company_time": int(time())}
        if name == "request_peer_response":
            return asdict(
                request_peer_response(
                    writer, claim, PeerQuestion(**args), now=int(time())
                )
            )
        if name == "read_peer_outcome":
            result = read_peer_outcome(writer, claim)
            if result is None:
                raise DutyRefused("No peer response belongs to this work")
            peer_outcome_read = True
            return result
        if resumed and not peer_outcome_read and name == "publish_assessment":
            raise DutyRefused("The resumed assessment must read its peer outcome")
        return invoke_assessment(writer, claim, name, args)

    try:
        employee.run_scoped(
            "assessment_duty",
            company_work_run(
                writer, claim, workflow, invoke, clock=lambda: int(time())
            ),
        )
    except TradeRefused:
        if peer_wait_for_run(writer, claim) is None and not any(
            original == claim for original, _ in pending_publications(writer)
        ):
            raise
    for original, receipt in pending_publications(writer):
        if original == claim:
            settle_assessment(writer, claim, receipt)
            return receipt
    return None


def _run_peer_answer(
    writer: WriterLease, employee: EmployeeProcess, claim: DutyClaim
) -> None:
    def invoke(name: str, args: dict) -> dict:
        if name == "read_peer_question":
            return read_peer_question(writer, claim, now=int(time()))
        if name == "answer_peer_question":
            return answer_peer_question(writer, claim, **args, now=int(time()))
        raise DutyRefused("Unexpected peer response tool")

    try:
        employee.run_scoped(
            "assessment_review", ScopedRun(writer, claim, _PEER_WORKFLOW, invoke)
        )
    except TradeRefused:
        if not peer_turn_has_disposition(writer, claim, now=int(time())):
            raise
    if not peer_turn_has_disposition(writer, claim, now=int(time())):
        raise DutyRefused("Peer turn did not commit an answer or refusal")


def _run_review(
    writer: WriterLease,
    employee: EmployeeProcess,
    work: FollowUpClaim,
    eligible_result: ResultEligibility,
) -> AssessmentReview:
    committed = None
    project_id = None
    workflow = ASSESSMENT_REVIEW_WORKFLOW
    if work.result.kind == MANAGER_ASSESSMENT:
        with closing(_connect(writer.store, "ro")) as connection:
            project_id = manager_review_project(
                connection, work.claim.company_id, work.claim.founder_id, work.result
            )
        workflow = replace(
            workflow,
            instruction="Review the published assessment addressed to you. "
            "Read the project discussion and reply if the result needs a response. "
            + workflow.instruction,
        )

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
            eligible_result=eligible_result,
        )
        committed = AssessmentReview(work.claim, decision, next_duty)
        return asdict(committed)

    run = company_work_run(
        writer,
        work.claim,
        workflow,
        invoke,
        clock=lambda: int(time()),
        project_id=project_id,
    )
    try:
        employee.run_scoped("assessment_review", run)
    except TradeRefused:
        if committed is None:
            raise
    if committed is None:
        raise DutyRefused("Company review returned without a committed decision")
    return committed
