"""Connect a delegated staffing turn to the hire's first accepted duty."""

from dataclasses import dataclass
from pathlib import Path
from time import time

from .duty_run import DutyClaim
from .employee_process import EmployeeProcess
from .first_trade import TradeRefused
from .onboarding import (
    OnboardingStatus,
    claim_hired_first,
    first_duty_accepted,
    invoke_onboarding,
    provision_hire,
)
from .staffing import StaffingOutcome, claim_staffing, read_staffing_outcome
from .staffing_tools import StaffingTools
from .writer_lease import hold_company_writer


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
    with hold_company_writer(store, company_id) as writer:
        runtime.check_ready()
        manager = claim_staffing(writer, runtime.profile, now=int(time()))
        if manager is None:
            return HiringRun(None, None)
        manager_tools = StaffingTools(writer, manager)
        try:
            runtime.staffing(manager_tools.invoke)
        except TradeRefused:
            if read_staffing_outcome(writer, manager) is None:
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
        hire_claim = claim_hired_first(writer, hire_id, runtime.profile)
        if hire_claim is None:
            return HiringRun(manager, outcome, status)
        try:
            runtime.onboarding(
                lambda name, args: invoke_onboarding(writer, hire_claim, name, args)
            )
        except TradeRefused:
            if not first_duty_accepted(writer, hire_claim):
                raise
        return HiringRun(
            manager,
            outcome,
            status,
            hire_claim,
            first_duty_accepted(writer, hire_claim),
        )
