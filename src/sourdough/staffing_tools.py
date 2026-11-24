"""Expose only the delegated manager's current staffing tools."""

from dataclasses import asdict, dataclass

from .duty_run import DutyClaim, DutyRefused
from .staffing import (
    HireCommitment,
    HireRequest,
    StaffingChoice,
    admit_hire,
    read_need,
    record_choice,
    submit_staffing_decision,
)
from .writer_lease import WriterLease


@dataclass(frozen=True)
class StaffingTools:
    writer: WriterLease
    claim: DutyClaim

    def invoke(self, name: str, arguments: dict) -> dict:
        if not isinstance(arguments, dict):
            raise DutyRefused("Staffing tools require a structured request")
        if name == "read_staffing_need" and not arguments:
            snapshot = read_need(self.writer, self.claim)
            existing_staff_capacity = {
                "unassigned": snapshot.pop("available_capacity"),
                "assigned": snapshot.pop("assigned_capacity"),
            }
            return snapshot | {"existing_staff_capacity": existing_staff_capacity}
        if name in ("record_staffing_choice", "submit_staffing_decision"):
            terms = arguments.copy()
            if name == "submit_staffing_decision":
                if "hire" not in terms:
                    raise DutyRefused(
                        "Staffing submission must include a hire request or null"
                    )
                requested_hire = terms.pop("hire")
                if requested_hire is not None and not isinstance(requested_hire, dict):
                    raise DutyRefused("Invalid explicit hire request")
            alternatives = terms.get("alternatives")
            if not isinstance(alternatives, list):
                raise DutyRefused("Expected a list of alternatives")
            try:
                choice = StaffingChoice(
                    **(terms | {"alternatives": tuple(alternatives)})
                )
            except TypeError as error:
                raise DutyRefused("Unexpected staffing choice fields") from error
            if name == "submit_staffing_decision":
                try:
                    commitment = (
                        HireCommitment(**requested_hire)
                        if requested_hire is not None
                        else None
                    )
                except TypeError as error:
                    raise DutyRefused(
                        "Unexpected explicit hire request fields"
                    ) from error
                choice_id, receipt = submit_staffing_decision(
                    self.writer, self.claim, choice, commitment
                )
                return {
                    "choice_id": choice_id,
                    "action": choice.action,
                    "hire": None if receipt is None else asdict(receipt),
                }
            choice_id = record_choice(self.writer, self.claim, choice)
            return {"choice_id": choice_id, "action": choice.action}
        if name == "request_hire":
            try:
                request = HireRequest(**arguments)
            except TypeError as error:
                raise DutyRefused("Unexpected hire request fields") from error
            receipt = admit_hire(self.writer, self.claim, request)
            return {"request_id": request.request_id} | asdict(receipt)
        raise DutyRefused("Tool is not available to this staffing duty")
