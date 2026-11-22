"""Expose only the delegated manager's current staffing tools."""

from dataclasses import asdict, dataclass

from .duty_run import DutyClaim, DutyRefused
from .staffing import (
    HireRequest,
    StaffingChoice,
    admit_hire,
    read_need,
    record_choice,
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
            return read_need(self.writer, self.claim)
        if name == "record_staffing_choice":
            alternatives = arguments.get("alternatives")
            if not isinstance(alternatives, list):
                raise DutyRefused("Expected a list of alternatives")
            try:
                choice = StaffingChoice(
                    **(arguments | {"alternatives": tuple(alternatives)})
                )
            except TypeError as error:
                raise DutyRefused("Unexpected staffing choice fields") from error
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
