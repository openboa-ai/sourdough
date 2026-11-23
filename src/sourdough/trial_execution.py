"""Dispatch sealed trial material through an already qualified offline adapter."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from .trial_registry import (
    TrialCeiling,
    TrialInputs,
    TrialRefused,
    TrialReservation,
    reserve_trial_launch,
)


@dataclass(frozen=True)
class TrialWorkerInput:
    case_inputs: bytes
    worker_context: bytes
    tool_manifest: bytes
    limits: TrialCeiling


class OfflineTrialRuntime(Protocol):
    """Preflight is read-only; dispatch must enforce the same bound conditions."""

    @property
    def profile(self) -> str: ...

    def can_isolate_and_enforce(
        self, *, company_fixture_digest: str, worker_input: TrialWorkerInput
    ) -> bool: ...

    def run_trial(self, run_id: str, worker_input: TrialWorkerInput) -> None: ...


def dispatch_registered_trial(
    store: Path,
    *,
    revision: str,
    slot_id: str,
    run_id: str,
    company_fixture: bytes,
    worker_input: TrialWorkerInput,
    runtime: OfflineTrialRuntime,
) -> TrialReservation:
    if not isinstance(worker_input, TrialWorkerInput) or not isinstance(
        worker_input.limits, TrialCeiling
    ):
        raise TrialRefused("Trial dispatch requires bounded worker input")
    contents = (
        company_fixture,
        worker_input.case_inputs,
        worker_input.worker_context,
        worker_input.tool_manifest,
    )
    if not all(type(content) is bytes and content for content in contents):
        raise TrialRefused("Trial material must provide immutable exact bytes")
    admit = getattr(runtime, "can_isolate_and_enforce", None)
    launch = getattr(runtime, "run_trial", None)
    if not callable(admit) or not callable(launch):
        raise TrialRefused("Runtime has no qualified offline trial interface")
    digests = tuple(sha256(content).hexdigest() for content in contents)
    inputs = TrialInputs(*digests, runtime.profile)
    if (
        admit(company_fixture_digest=inputs.company_fixture, worker_input=worker_input)
        is not True
    ):
        raise TrialRefused(
            "Runtime cannot establish trial isolation and all sealed limits"
        )
    if runtime.profile != inputs.runtime_profile:
        raise TrialRefused("Runtime binding changed during trial preparation")
    reservation = reserve_trial_launch(
        store,
        revision=revision,
        slot_id=slot_id,
        run_id=run_id,
        inputs=inputs,
        limits=worker_input.limits,
    )
    if reservation.launch_permitted:
        launch(run_id, worker_input)
    return reservation
