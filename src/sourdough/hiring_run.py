"""Connect a delegated staffing turn to the hire's first accepted duty."""

from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic, sleep, time

from .duty_run import DutyClaim
from .employee_process import EmployeeProcess
from .first_trade import TradeRefused
from .onboarding import (
    OnboardingStatus,
    ResumeWork,
    claim_hired_first,
    first_duty_accepted,
    invoke_onboarding,
    pending_onboarding,
    provision_hire,
)
from .runtime.engine import CompanyRuntime, build_default_registry
from .runtime.registry import DutyOutcome
from .staffing import StaffingOutcome, claim_staffing, read_staffing_outcome
from .staffing_tools import StaffingTools
from .writer_lease import WriterLease


@dataclass(frozen=True)
class HiringRun:
    manager_claim: DutyClaim | None
    choice: StaffingOutcome | None
    provision: OnboardingStatus | None = None
    hire_claim: DutyClaim | None = None
    accepted: bool = False


def run_hiring(
    store: Path, company_id: str, runtime: EmployeeProcess, *, first_purpose: str
) -> HiringRun:
    def _execute(writer: WriterLease) -> HiringRun:
        prepared = prepare_hiring(writer, runtime, first_purpose=first_purpose)
        hire_claim = prepared.hire_claim
        if hire_claim is None:
            return prepared
        try:
            runtime.onboarding(
                lambda name, args: invoke_onboarding(writer, hire_claim, name, args)
            )
        except TradeRefused:
            if not first_duty_accepted(writer, hire_claim):
                raise
        return replace(prepared, accepted=first_duty_accepted(writer, hire_claim))

    registry = build_default_registry(
        {"org.hiring": lambda _ctx: DutyOutcome("completed")}
    )
    return CompanyRuntime(store, company_id, registry).run_bound_handler(
        "org.hiring", _execute
    )


def prepare_hiring(
    writer: WriterLease,
    runtime: EmployeeProcess,
    *,
    first_purpose: str,
    allow_staffing: bool = True,
    resume_work: ResumeWork | None = None,
) -> HiringRun:
    manager = pending_onboarding(
        writer, runtime.profile, first_purpose, resume_work=resume_work
    )
    resumed = manager is not None
    if manager is None:
        if not allow_staffing:
            return HiringRun(None, None)
        runtime.check_ready()
        manager = claim_staffing(
            writer, runtime.profile, now=int(time()), resume_interrupted=True
        )
        if manager is None:
            return HiringRun(None, None)
        manager_tools = StaffingTools(writer, manager)
        try:
            runtime.staffing(manager_tools.invoke)
        except TradeRefused:
            interrupted = read_staffing_outcome(writer, manager)
            if interrupted is None or (
                interrupted.action == "hire" and interrupted.hire is None
            ):
                raise
    outcome = read_staffing_outcome(writer, manager)
    if outcome is None or outcome.hire is None:
        return HiringRun(manager, outcome)

    def ready(profile: str) -> bool:
        if runtime.profile != profile:
            return False
        try:
            runtime.check_ready()
        except TradeRefused:
            return False
        return True

    hire_id = outcome.hire.agent_id
    status = provision_hire(writer, hire_id, first_purpose, ready, now=int(time()))
    deadline = monotonic() + 125
    while status.first_duty_id is None and status.retry_at is not None:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        wait = max(0, status.retry_at - time())
        if wait:
            sleep(min(wait, remaining))
        status = provision_hire(writer, hire_id, first_purpose, ready, now=int(time()))
    return HiringRun(
        manager,
        outcome,
        status,
        claim_hired_first(
            writer, hire_id, runtime.profile, resume=resumed, resume_work=resume_work
        ),
    )
