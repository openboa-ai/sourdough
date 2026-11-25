"""Dispatch sealed trial material through an already qualified offline adapter."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from .company import FoundingTerms, found
from .runtime.engine import CompanyRuntime, build_default_registry
from .trial_registry import (
    TrialCeiling,
    TrialInputs,
    TrialRefused,
    TrialReservation,
    TrialTerminalEvidence,
    reserve_trial_launch,
)

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_MAX_I64 = 2**63 - 1


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
    if (
        not isinstance(run_id, str)
        or ".." in run_id
        or _SAFE_RUN_ID.fullmatch(run_id) is None
    ):
        raise TrialRefused("Trial run_id must be a safe non-traversal identifier")
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
    profile = getattr(runtime, "profile", None)
    if not callable(admit) or not callable(launch) or not isinstance(profile, str):
        raise TrialRefused("Runtime has no qualified offline trial interface")
    digests = tuple(sha256(content).hexdigest() for content in contents)
    inputs = TrialInputs(*digests, profile)
    if (
        admit(company_fixture_digest=inputs.company_fixture, worker_input=worker_input)
        is not True
    ):
        raise TrialRefused(
            "Runtime cannot establish trial isolation and all sealed limits"
        )
    if getattr(runtime, "profile", None) != inputs.runtime_profile:
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


def _canonical_digest(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        raise TrialRefused("Trial evidence section must be a JSON object")
    try:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise TrialRefused(
            "Trial evidence payload must be finite canonical JSON"
        ) from error
    return sha256(encoded).hexdigest()


def _is_private_dir(info: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and (info.st_mode & 0o077) == 0
    )


class CompanyRuntimeOfflineAdapter:
    """Isolated OfflineTrialRuntime driving a fresh CompanyRuntime + private Desk per run_id."""

    def __init__(
        self,
        *,
        trial_root: Path,
        evaluator_store: Path,
        profile: str,
        execute_case: Callable[
            [CompanyRuntime, Path, TrialWorkerInput], dict[str, Any]
        ],
        max_turns: int = 16,
        max_wall_seconds: int = 300,
        max_tokens: int = 100_000,
    ) -> None:
        if (
            not isinstance(trial_root, Path)
            or not isinstance(evaluator_store, Path)
            or not isinstance(profile, str)
            or not profile.strip()
            or not callable(execute_case)
            or not all(
                type(v) is int and 0 < v <= _MAX_I64
                for v in (max_turns, max_wall_seconds, max_tokens)
            )
        ):
            raise TrialRefused("Invalid CompanyRuntimeOfflineAdapter configuration")
        self._trial_root = trial_root.absolute()
        self._evaluator_store = evaluator_store.absolute()
        self._profile = profile
        self._execute_case = execute_case
        self._max_turns = max_turns
        self._max_wall_seconds = max_wall_seconds
        self._max_tokens = max_tokens
        self.evidences: dict[str, TrialTerminalEvidence] = {}
        self.run_stores: dict[str, Path] = {}

    @property
    def profile(self) -> str:
        return self._profile

    def _is_isolated_from_evaluator(self) -> bool:
        # Reject dangling or live symlink at trial_root and check existing dir privacy.
        if os.path.lexists(self._trial_root):
            try:
                info = self._trial_root.stat(follow_symlinks=False)
            except OSError:
                return False
            if stat.S_ISLNK(info.st_mode) or not _is_private_dir(info):
                return False
            # Refuse pointing trial_root directly at an existing operating company directory.
            if os.path.lexists(self._trial_root / "company.sqlite"):
                return False
        elif os.path.lexists(self._trial_root.parent):
            try:
                pinfo = self._trial_root.parent.stat(follow_symlinks=False)
            except OSError:
                return False
            if stat.S_ISLNK(pinfo.st_mode) or not stat.S_ISDIR(pinfo.st_mode):
                return False

        try:
            eval_resolved = self._evaluator_store.resolve()
            root_resolved = self._trial_root.resolve()
        except OSError:
            return False
        if eval_resolved == root_resolved:
            return False
        # Strictly forbid evaluator store inside trial_root OR trial_root inside evaluator dir.
        if eval_resolved.is_relative_to(root_resolved) or root_resolved.is_relative_to(
            eval_resolved.parent
        ):
            return False
        return True

    def can_isolate_and_enforce(
        self, *, company_fixture_digest: str, worker_input: TrialWorkerInput
    ) -> bool:
        if not self._is_isolated_from_evaluator():
            return False
        if not isinstance(worker_input, TrialWorkerInput) or not isinstance(
            worker_input.limits, TrialCeiling
        ):
            return False
        limits = worker_input.limits
        return (
            isinstance(company_fixture_digest, str)
            and _HEX64.fullmatch(company_fixture_digest) is not None
            and limits.turns <= self._max_turns
            and limits.wall_seconds <= self._max_wall_seconds
            and limits.tokens <= self._max_tokens
        )

    def run_trial(self, run_id: str, worker_input: TrialWorkerInput) -> None:
        if (
            not isinstance(run_id, str)
            or ".." in run_id
            or _SAFE_RUN_ID.fullmatch(run_id) is None
        ):
            raise TrialRefused("Trial run_id must be a safe workspace identifier")
        if not isinstance(worker_input, TrialWorkerInput) or not isinstance(
            worker_input.limits, TrialCeiling
        ):
            raise TrialRefused("Trial execution requires bounded worker input")
        limits = worker_input.limits
        if (
            limits.turns > self._max_turns
            or limits.wall_seconds > self._max_wall_seconds
            or limits.tokens > self._max_tokens
        ):
            raise TrialRefused("Trial limits exceed adapter enforcement caps")
        if not self._is_isolated_from_evaluator():
            raise TrialRefused("Trial workspace is not isolated from evaluator store")

        self._trial_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            root_fd = os.open(
                self._trial_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
        except OSError as error:
            raise TrialRefused("Cannot open private trial root directory") from error
        try:
            root_info = os.fstat(root_fd)
            if not _is_private_dir(root_info):
                raise TrialRefused("Trial root directory must be 0700 user-owned")
            run_name = f"trial-{run_id}"
            try:
                os.mkdir(run_name, mode=0o700, dir_fd=root_fd)
            except OSError as error:
                raise TrialRefused(
                    f"Cannot allocate fresh trial directory for {run_id}"
                ) from error
        finally:
            os.close(root_fd)

        run_dir = self._trial_root / f"trial-{run_id}"
        desk_dir = run_dir / "desk"
        desk_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        lease_dir = run_dir / "leases"
        lease_dir.mkdir(mode=0o700, parents=False, exist_ok=False)

        company_store = run_dir / "company.sqlite"
        self.run_stores[run_id] = company_store
        receipt = found(
            company_store,
            FoundingTerms(
                f"offline-{run_id}",
                f"charter-{run_id}",
                "offline-trial",
                self._profile,
            ),
        )
        runtime = CompanyRuntime(
            company_store,
            receipt.company_id,
            build_default_registry(),
            lease_owner=f"offline-{run_id}",
            lease_directory=lease_dir,
        )

        started = monotonic()
        try:
            result = self._execute_case(runtime, desk_dir, worker_input)
        except TimeoutError:
            obs_digest = _canonical_digest(
                {
                    "run_id": run_id,
                    "case_sha256": sha256(worker_input.case_inputs).hexdigest(),
                    "reason": "wall-clock-timeout",
                }
            )
            self.evidences[run_id] = TrialTerminalEvidence(
                outcome="timed-out",
                observation_digest=obs_digest,
            )
            return
        elapsed = monotonic() - started

        if not isinstance(result, dict):
            raise TrialRefused("Trial case adapter must return a JSON-compatible dict")
        usage = result.get("usage")
        if usage is not None and not isinstance(usage, dict):
            raise TrialRefused("Trial usage field must be a JSON object")

        reported_turns = usage.get("turns", 0) if isinstance(usage, dict) else 0
        reported_tokens = usage.get("tokens", 0) if isinstance(usage, dict) else 0
        reported_wall = usage.get("wall_seconds", 0) if isinstance(usage, dict) else 0
        if (
            isinstance(reported_turns, bool)
            or not isinstance(reported_turns, int)
            or reported_turns < 0
            or isinstance(reported_tokens, bool)
            or not isinstance(reported_tokens, int)
            or reported_tokens < 0
            or isinstance(reported_wall, bool)
            or not isinstance(reported_wall, (int, float))
            or not math.isfinite(reported_wall)
            or reported_wall < 0
        ):
            raise TrialRefused(
                "Trial usage counters must be finite non-negative numbers"
            )

        # Enforce sealed wall-clock, turn, and token ceilings directly on dispatch!
        if elapsed > limits.wall_seconds or reported_wall > limits.wall_seconds:
            obs_digest = _canonical_digest(
                {
                    "run_id": run_id,
                    "case_sha256": sha256(worker_input.case_inputs).hexdigest(),
                    "reason": "wall-seconds-ceiling-exceeded",
                }
            )
            self.evidences[run_id] = TrialTerminalEvidence(
                outcome="timed-out",
                observation_digest=obs_digest,
                usage_digest=_canonical_digest(usage) if usage is not None else None,
            )
            return
        if reported_turns > limits.turns or reported_tokens > limits.tokens:
            obs_digest = _canonical_digest(
                {
                    "run_id": run_id,
                    "case_sha256": sha256(worker_input.case_inputs).hexdigest(),
                    "reason": "resource-ceiling-exceeded",
                }
            )
            self.evidences[run_id] = TrialTerminalEvidence(
                outcome="failed",
                observation_digest=obs_digest,
                usage_digest=_canonical_digest(usage) if usage is not None else None,
            )
            return

        outcome = result.get("outcome", "completed")
        observation = result.get("observation", {})
        if not isinstance(observation, dict):
            raise TrialRefused("Trial observation field must be a JSON object")
        observation_digest = _canonical_digest(
            {
                "run_id": run_id,
                "case_sha256": sha256(worker_input.case_inputs).hexdigest(),
                "observation": observation,
            }
        )
        decision = result.get("decision")
        tool_trace = result.get("tool_trace")
        service_effects = result.get("service_effects")
        self.evidences[run_id] = TrialTerminalEvidence(
            outcome=outcome,
            observation_digest=observation_digest,
            decision_digest=_canonical_digest(decision)
            if decision is not None
            else None,
            tool_trace_digest=(
                _canonical_digest(tool_trace) if tool_trace is not None else None
            ),
            service_effects_digest=(
                _canonical_digest(service_effects)
                if service_effects is not None
                else None
            ),
            usage_digest=_canonical_digest(usage) if usage is not None else None,
        )
