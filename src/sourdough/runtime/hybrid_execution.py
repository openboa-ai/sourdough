"""4-Plane Hybrid Execution: Code Plane, Decision Model Plane, LLM Meta Plane, and Unified Control Gate.

Separates execution computation from company governance so every decision—whether
produced by deterministic $0 code rules, version-pinned quantitative/ML models with
explicit fallback, or offline-eval-gated asynchronous LLM meta-reasoning—commits
through the exact same DeterministicControlPlaneGate (WorkRunClaim fencing, active
standing_grant, restriction_cause checks, notional caps, evidence-backed work_decision,
and service-owned ConnectorProxy admission).
"""

from __future__ import annotations

import hmac
import json
import math
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from time import time_ns
from typing import Any

from ..agent_runtime.protocol import (
    PROVIDER_CODE_PLANE,
    PROVIDER_DECISION_MODEL,
    VALID_EXECUTION_PLANES,
    AdapterEvent,
    AdapterManifest,
    AgentProfileBinding,
    AgentRuntimeRefused,
    ExecutionPlane,
    OuterToolBroker,
    ShiftRequest,
    ShiftResult,
    TokenUsage,
)
from ..connectors.protocol import (
    ConnectorRefused,
    ConnectorResponse,
    validate_payload_against_schema,
)
from ..connectors.proxy import ConnectorProxy
from ..connectors.registry import (
    issue_connector_admission_in_tx,
    read_connector_binding,
    read_connector_grants,
    revoke_connector_admission,
)
from ..duty_run import DutyRefused, ExecutionClaim, resolve_run
from ..storage.db import write_uow
from ..storage.kernel import append_audit_event
from ..writer_lease import WriterLease
from .kernel import (
    WORK_DECISION_ACTION_KINDS,
    DecisionEvidence,
    RunFenced,
    WorkDecision,
    WorkKernelRefused,
    record_work_decision,
)
from .shift.flow_guard import ShiftFlowGuard

DEFENSIVE_ACTION_KINDS: frozenset[str] = frozenset({"hold", "noop", "defer", "reject"})
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
MAX_MODEL_ARTIFACT_BYTES = 64 * 1024 * 1024


class HybridExecutionRefused(Exception):
    """Base error for refused hybrid execution configuration or inputs."""


class HybridControlGateDenied(HybridExecutionRefused):
    """DeterministicControlPlaneGate refused to commit a plane proposal."""


class HybridAntiLookaheadViolation(HybridExecutionRefused):
    """Feature or evaluation input violated causal availability (available_at_ns > decision_at_ns)."""


class HybridOfflineEvalRequired(HybridExecutionRefused):
    """LLM meta-reasoning policy patch lacked a passing, digest-bound offline evaluation verdict."""


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _clone_json_mapping(mapping: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    if not isinstance(mapping, Mapping):
        raise HybridExecutionRefused(f"{label} must be a mapping")
    try:
        serialized = json.dumps(dict(mapping), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise HybridExecutionRefused(
            f"{label} must contain strictly finite JSON-serializable values"
        ) from error
    loaded = json.loads(serialized)
    if not isinstance(loaded, dict):
        raise HybridExecutionRefused(f"{label} must serialize to a JSON object")
    return loaded


def _require_safe_id(value: str, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID_RE.fullmatch(value) is None:
        raise HybridExecutionRefused(f"Invalid {label}: {value!r}")
    return value


def _require_hex64(value: str, label: str) -> str:
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise HybridExecutionRefused(f"{label} must be a lowercase 64-hex SHA-256")
    return value


def _require_finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HybridExecutionRefused(f"{label} must be a finite number")
    fval = float(value)
    if not math.isfinite(fval):
        raise HybridExecutionRefused(f"{label} must be finite (no NaN/Inf)")
    return fval


@dataclass(frozen=True)
class FeaturePoint:
    """Single point-in-time feature observation with explicit causal availability."""

    name: str
    value: float | int | str | bool
    observed_at_ns: int
    available_at_ns: int

    def __post_init__(self) -> None:
        _require_safe_id(self.name, "feature.name")
        if type(self.observed_at_ns) is not int or self.observed_at_ns < 0:
            raise HybridExecutionRefused(
                "feature.observed_at_ns must be a nonnegative integer"
            )
        if type(self.available_at_ns) is not int or self.available_at_ns < 0:
            raise HybridExecutionRefused(
                "feature.available_at_ns must be a nonnegative integer"
            )
        if self.available_at_ns < self.observed_at_ns:
            raise HybridAntiLookaheadViolation(
                f"Feature '{self.name}' available_at_ns ({self.available_at_ns}) "
                f"precedes observed_at_ns ({self.observed_at_ns})"
            )
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise HybridExecutionRefused(
                f"Feature '{self.name}' contains non-finite float value"
            )
        if not isinstance(self.value, (int, float, str, bool)):
            raise HybridExecutionRefused(
                f"Feature '{self.name}' has unsupported value type"
            )


@dataclass(frozen=True)
class FeatureSnapshot:
    """Point-in-time structured feature input enforcing strict anti-lookahead."""

    snapshot_id: str
    schema_version: str
    decision_at_ns: int
    features: tuple[FeaturePoint, ...]
    source_evidence: tuple[DecisionEvidence, ...] = ()

    def __post_init__(self) -> None:
        _require_safe_id(self.snapshot_id, "snapshot_id")
        _require_safe_id(self.schema_version, "schema_version")
        if type(self.decision_at_ns) is not int or self.decision_at_ns <= 0:
            raise HybridExecutionRefused("decision_at_ns must be a positive integer")
        if not isinstance(self.features, tuple):
            raise HybridExecutionRefused("features must be a tuple of FeaturePoint")
        seen: set[str] = set()
        for point in self.features:
            if not isinstance(point, FeaturePoint):
                raise HybridExecutionRefused("features entries must be FeaturePoint")
            if point.name in seen:
                raise HybridExecutionRefused(
                    f"Duplicate feature name in snapshot: '{point.name}'"
                )
            seen.add(point.name)
        self.validate_anti_lookahead()

    def validate_anti_lookahead(self, *, at_ns: int | None = None) -> None:
        cutoff = self.decision_at_ns if at_ns is None else at_ns
        if type(cutoff) is not int or cutoff <= 0:
            raise HybridExecutionRefused("anti-lookahead cutoff must be positive int")
        for point in self.features:
            if point.available_at_ns > cutoff:
                raise HybridAntiLookaheadViolation(
                    f"Feature '{point.name}' available_at_ns ({point.available_at_ns}) "
                    f"exceeds decision_at_ns ({cutoff})"
                )
        for ev in self.source_evidence:
            if ev.available_at_ns > cutoff:
                raise HybridAntiLookaheadViolation(
                    f"Evidence '{ev.resource_id}' available_at_ns ({ev.available_at_ns}) "
                    f"exceeds decision_at_ns ({cutoff})"
                )

    def feature_map(self) -> dict[str, float | int | str | bool]:
        return {point.name: point.value for point in self.features}

    def content_digest(self) -> str:
        payload = {
            "snapshot_id": self.snapshot_id,
            "schema_version": self.schema_version,
            "decision_at_ns": self.decision_at_ns,
            "features": [
                {
                    "name": p.name,
                    "value": p.value,
                    "observed_at_ns": p.observed_at_ns,
                    "available_at_ns": p.available_at_ns,
                }
                for p in sorted(self.features, key=lambda item: item.name)
            ],
        }
        return sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConnectorEffectIntent:
    """Structured external connector intent proposed by an execution plane.

    Never carries credentials or admission IDs; DeterministicControlPlaneGate is the
    sole owner permitted to issue a service-owned connector_admission.
    """

    binding_id: str
    operation: str
    request_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    expected_environment: str | None = None

    def __post_init__(self) -> None:
        _require_safe_id(self.binding_id, "connector_effect.binding_id")
        _require_safe_id(self.operation, "connector_effect.operation")
        _require_safe_id(self.request_id, "connector_effect.request_id")
        clean_payload = _clone_json_mapping(
            self.payload, label="connector_effect.payload"
        )
        if "admission_id" in clean_payload:
            raise HybridExecutionRefused(
                "connector_effect.payload must not include admission_id"
            )
        object.__setattr__(self, "payload", clean_payload)
        if self.expected_environment is not None:
            _require_safe_id(
                self.expected_environment, "connector_effect.expected_environment"
            )


@dataclass(frozen=True)
class ParameterBound:
    """Numeric safety envelope for an LLM-proposed policy parameter delta."""

    min_value: float
    max_value: float
    max_abs_delta: float

    def __post_init__(self) -> None:
        lo = _require_finite_number(self.min_value, "min_value")
        hi = _require_finite_number(self.max_value, "max_value")
        delta = _require_finite_number(self.max_abs_delta, "max_abs_delta")
        if lo > hi:
            raise HybridExecutionRefused("ParameterBound min_value > max_value")
        if delta < 0:
            raise HybridExecutionRefused("ParameterBound max_abs_delta must be >= 0")
        object.__setattr__(self, "min_value", lo)
        object.__setattr__(self, "max_value", hi)
        object.__setattr__(self, "max_abs_delta", delta)


@dataclass(frozen=True)
class PolicyPatchProposal:
    """Structured parameter patch produced asynchronously by LLM meta-reasoning."""

    proposal_id: str
    policy_id: str
    base_revision: str
    target_revision: str
    parameter_deltas: Mapping[str, float]
    rationale: str
    produced_by_run_id: str
    evidence: tuple[DecisionEvidence, ...] = ()
    token_usage: TokenUsage = field(default_factory=lambda: TokenUsage(0, 0))
    target_scope_ref: str = "company"
    active_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        _require_safe_id(self.proposal_id, "proposal_id")
        _require_safe_id(self.policy_id, "policy_id")
        _require_safe_id(self.base_revision, "base_revision")
        _require_safe_id(self.target_revision, "target_revision")
        _require_safe_id(self.produced_by_run_id, "produced_by_run_id")
        _require_safe_id(self.target_scope_ref, "target_scope_ref")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise HybridExecutionRefused(
                "PolicyPatchProposal rationale must be nonempty"
            )
        if not isinstance(self.parameter_deltas, Mapping) or not self.parameter_deltas:
            raise HybridExecutionRefused(
                "PolicyPatchProposal parameter_deltas must be a nonempty mapping"
            )
        clean_deltas: dict[str, float] = {}
        for k, v in self.parameter_deltas.items():
            _require_safe_id(str(k), "parameter_deltas key")
            clean_deltas[str(k)] = _require_finite_number(v, f"parameter_deltas[{k}]")
        object.__setattr__(self, "parameter_deltas", clean_deltas)

    def canonical_digest(self) -> str:
        return sha256(
            _canonical_json(
                {
                    "proposal_id": self.proposal_id,
                    "policy_id": self.policy_id,
                    "base_revision": self.base_revision,
                    "target_revision": self.target_revision,
                    "parameter_deltas": dict(self.parameter_deltas),
                    "rationale": self.rationale,
                }
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class OfflineEvalVerdict:
    """Deterministic offline evaluation receipt gating LLM meta-plane patches."""

    verdict_id: str
    policy_id: str
    target_revision: str
    proposal_digest: str
    suite_revision: str
    passed: bool
    sample_count: int
    holdout_margin: float
    max_drawdown_bps: int
    evaluated_at_ns: int
    evaluator_id: str

    def __post_init__(self) -> None:
        _require_safe_id(self.verdict_id, "verdict_id")
        _require_safe_id(self.policy_id, "policy_id")
        _require_safe_id(self.target_revision, "target_revision")
        _require_hex64(self.proposal_digest, "proposal_digest")
        _require_safe_id(self.suite_revision, "suite_revision")
        _require_safe_id(self.evaluator_id, "evaluator_id")
        if type(self.passed) is not bool:
            raise HybridExecutionRefused("OfflineEvalVerdict.passed must be bool")
        if type(self.sample_count) is not int or self.sample_count < 0:
            raise HybridExecutionRefused(
                "OfflineEvalVerdict.sample_count must be nonnegative int"
            )
        margin = _require_finite_number(self.holdout_margin, "holdout_margin")
        object.__setattr__(self, "holdout_margin", margin)
        if type(self.max_drawdown_bps) is not int or self.max_drawdown_bps < 0:
            raise HybridExecutionRefused(
                "OfflineEvalVerdict.max_drawdown_bps must be nonnegative int"
            )
        if type(self.evaluated_at_ns) is not int or self.evaluated_at_ns <= 0:
            raise HybridExecutionRefused(
                "OfflineEvalVerdict.evaluated_at_ns must be positive int"
            )

    def canonical_digest(self) -> str:
        return sha256(
            _canonical_json(
                {
                    "verdict_id": self.verdict_id,
                    "policy_id": self.policy_id,
                    "target_revision": self.target_revision,
                    "proposal_digest": self.proposal_digest,
                    "suite_revision": self.suite_revision,
                    "passed": self.passed,
                    "sample_count": self.sample_count,
                    "holdout_margin": self.holdout_margin,
                    "max_drawdown_bps": self.max_drawdown_bps,
                    "evaluated_at_ns": self.evaluated_at_ns,
                    "evaluator_id": self.evaluator_id,
                }
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class PlaneExecutionProposal:
    """Normalized decision proposal produced by any of the 3 execution planes."""

    proposal_id: str
    plane: ExecutionPlane
    engine_id: str
    engine_revision: str
    action_kind: str
    rationale: str
    decision_at_ns: int
    evidence: tuple[DecisionEvidence, ...] = ()
    proposed_notional_minor_units: int = 0
    target_scope_ref: str = "company"
    active_snapshot_id: str | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    connector_effect: ConnectorEffectIntent | None = None
    token_usage: TokenUsage = field(default_factory=lambda: TokenUsage(0, 0))
    fallback_used: bool = False
    fallback_reason: str | None = None
    reproducibility_digest: str = ""
    eval_verdict: OfflineEvalVerdict | None = None
    eval_verdict_digest: str | None = None
    policy_patch_digest: str | None = None

    def __post_init__(self) -> None:
        _require_safe_id(self.proposal_id, "proposal_id")
        if self.plane not in VALID_EXECUTION_PLANES:
            raise HybridExecutionRefused(f"Unsupported execution plane: {self.plane!r}")
        _require_safe_id(self.engine_id, "engine_id")
        _require_safe_id(self.engine_revision, "engine_revision")
        if self.action_kind not in WORK_DECISION_ACTION_KINDS:
            raise HybridExecutionRefused(
                f"Unsupported work_decision action_kind: {self.action_kind!r}"
            )
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise HybridExecutionRefused("proposal rationale must be nonempty")
        if type(self.decision_at_ns) is not int or self.decision_at_ns <= 0:
            raise HybridExecutionRefused("decision_at_ns must be a positive integer")
        if (
            type(self.proposed_notional_minor_units) is not int
            or self.proposed_notional_minor_units < 0
        ):
            raise HybridExecutionRefused(
                "proposed_notional_minor_units must be a nonnegative integer"
            )
        _require_safe_id(self.target_scope_ref, "target_scope_ref")
        clean_params = _clone_json_mapping(self.parameters, label="proposal.parameters")
        object.__setattr__(self, "parameters", clean_params)
        if self.connector_effect is not None and not isinstance(
            self.connector_effect, ConnectorEffectIntent
        ):
            raise HybridExecutionRefused(
                "connector_effect must be a ConnectorEffectIntent or None"
            )
        if self.plane in ("code", "decision_model") and (
            self.token_usage.input_tokens != 0 or self.token_usage.output_tokens != 0
        ):
            raise HybridExecutionRefused(
                f"Non-LLM execution plane '{self.plane}' must report zero LLM tokens"
            )
        if self.eval_verdict_digest is not None:
            _require_hex64(self.eval_verdict_digest, "eval_verdict_digest")
        if self.policy_patch_digest is not None:
            _require_hex64(self.policy_patch_digest, "policy_patch_digest")
        if not self.reproducibility_digest:
            digest = sha256(
                _canonical_json(
                    {
                        "proposal_id": self.proposal_id,
                        "plane": self.plane,
                        "engine_id": self.engine_id,
                        "engine_revision": self.engine_revision,
                        "action_kind": self.action_kind,
                        "decision_at_ns": self.decision_at_ns,
                        "parameters": clean_params,
                        "fallback_used": self.fallback_used,
                        "fallback_reason": self.fallback_reason,
                    }
                ).encode("utf-8")
            ).hexdigest()
            object.__setattr__(self, "reproducibility_digest", digest)


@dataclass(frozen=True)
class CodeRuleDecision:
    """Output returned by a pure deterministic CodePlaneExecutor rule callable."""

    action_kind: str
    rationale: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    proposed_notional_minor_units: int = 0
    target_scope_ref: str = "company"
    connector_effect: ConnectorEffectIntent | None = None


@dataclass(frozen=True)
class ModelScore:
    """Structured scoring output returned by a pinned DecisionModelPlaneExecutor scorer."""

    score: float
    confidence: float
    regime_label: str
    action_kind: str
    rationale: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    proposed_notional_minor_units: int = 0
    target_scope_ref: str = "company"
    connector_effect: ConnectorEffectIntent | None = None


@dataclass(frozen=True)
class PlaneExecutionOutcome:
    """Committed outcome returned by DeterministicControlPlaneGate.commit."""

    decision: WorkDecision
    proposal: PlaneExecutionProposal
    connector_response: ConnectorResponse | None = None
    admission_id: str | None = None


# ---------------------------------------------------------------------------
# Plane 1: Deterministic Code Plane ($0 LLM cost)
# ---------------------------------------------------------------------------


class CodePlaneExecutor:
    """Plane 1 deterministic rule executor running at $0 LLM cost with reproducible digests."""

    def __init__(
        self,
        *,
        rule_id: str,
        code_revision: str,
        rule_fn: Callable[[FeatureSnapshot, Mapping[str, Any]], CodeRuleDecision],
        default_parameters: Mapping[str, Any] | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._rule_id = _require_safe_id(rule_id, "rule_id")
        self._code_revision = _require_safe_id(code_revision, "code_revision")
        if not callable(rule_fn):
            raise HybridExecutionRefused("rule_fn must be callable")
        self._rule_fn = rule_fn
        self._default_parameters = _clone_json_mapping(
            default_parameters or {}, label="default_parameters"
        )
        self._timeout_seconds = _require_finite_number(
            timeout_seconds, "timeout_seconds"
        )
        if self._timeout_seconds <= 0:
            raise HybridExecutionRefused("timeout_seconds must be positive")

    @property
    def rule_id(self) -> str:
        return self._rule_id

    @property
    def code_revision(self) -> str:
        return self._code_revision

    def evaluate(
        self,
        snapshot: FeatureSnapshot,
        *,
        proposal_id: str | None = None,
        parameters: Mapping[str, Any] | None = None,
        extra_evidence: Sequence[DecisionEvidence] = (),
        fallback_used: bool = False,
        fallback_reason: str | None = None,
    ) -> PlaneExecutionProposal:
        snapshot.validate_anti_lookahead()
        merged_params = dict(self._default_parameters)
        if parameters is not None:
            merged_params.update(_clone_json_mapping(parameters, label="parameters"))
        done = threading.Event()
        holder: dict[str, Any] = {}

        def _worker() -> None:
            try:
                holder["result"] = self._rule_fn(snapshot, merged_params)
            except BaseException as error:
                holder["error"] = error
            finally:
                done.set()

        thread = threading.Thread(
            target=_worker,
            name=f"sourdough-code-{self._rule_id}",
            daemon=True,
        )
        thread.start()
        if not done.wait(timeout=self._timeout_seconds):
            raise HybridExecutionRefused(
                f"CodePlaneExecutor '{self._rule_id}' exceeded timeout ({self._timeout_seconds}s)"
            )
        if "error" in holder:
            err = holder["error"]
            if isinstance(err, HybridExecutionRefused):
                raise err
            raise HybridExecutionRefused(
                f"CodePlaneExecutor '{self._rule_id}' failed ({type(err).__name__}): {err}"
            ) from err
        decision = holder.get("result")
        if not isinstance(decision, CodeRuleDecision):
            raise HybridExecutionRefused(
                "CodePlaneExecutor rule_fn must return a CodeRuleDecision"
            )
        clean_out_params = _clone_json_mapping(
            decision.parameters, label="decision.parameters"
        )
        repro_digest = sha256(
            _canonical_json(
                {
                    "rule_id": self._rule_id,
                    "code_revision": self._code_revision,
                    "snapshot_digest": snapshot.content_digest(),
                    "input_parameters": merged_params,
                    "action_kind": decision.action_kind,
                    "output_parameters": clean_out_params,
                    "proposed_notional_minor_units": decision.proposed_notional_minor_units,
                }
            ).encode("utf-8")
        ).hexdigest()
        pid = proposal_id or f"code-{repro_digest[:16]}"
        return PlaneExecutionProposal(
            proposal_id=pid,
            plane="code",
            engine_id=self._rule_id,
            engine_revision=self._code_revision,
            action_kind=decision.action_kind,
            rationale=decision.rationale,
            decision_at_ns=snapshot.decision_at_ns,
            evidence=tuple(snapshot.source_evidence) + tuple(extra_evidence),
            proposed_notional_minor_units=decision.proposed_notional_minor_units,
            target_scope_ref=decision.target_scope_ref,
            active_snapshot_id=snapshot.snapshot_id,
            parameters=clean_out_params,
            connector_effect=decision.connector_effect,
            token_usage=TokenUsage(0, 0),
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            reproducibility_digest=repro_digest,
        )


# ---------------------------------------------------------------------------
# Plane 2: Version-Pinned Decision Model Plane with Explicit Deterministic Fallback
# ---------------------------------------------------------------------------


class DecisionModelPlaneExecutor:
    """Plane 2 version- and SHA-256-pinned model scorer with explicit deterministic fallback."""

    def __init__(
        self,
        *,
        model_id: str,
        model_revision: str,
        expected_artifact_sha256: str,
        feature_schema_version: str,
        scorer_fn: Callable[[FeatureSnapshot, bytes], ModelScore],
        artifact_bytes: bytes | None = None,
        artifact_path: Path | None = None,
        min_confidence: float = 0.6,
        timeout_seconds: float = 2.0,
        fallback_executor: CodePlaneExecutor | None = None,
    ) -> None:
        self._model_id = _require_safe_id(model_id, "model_id")
        self._model_revision = _require_safe_id(model_revision, "model_revision")
        self._expected_artifact_sha256 = _require_hex64(
            expected_artifact_sha256, "expected_artifact_sha256"
        )
        self._feature_schema_version = _require_safe_id(
            feature_schema_version, "feature_schema_version"
        )
        if not callable(scorer_fn):
            raise HybridExecutionRefused("scorer_fn must be callable")
        if (artifact_bytes is None) == (artifact_path is None):
            raise HybridExecutionRefused(
                "DecisionModelPlaneExecutor requires exactly one of artifact_bytes or artifact_path"
            )
        if artifact_bytes is not None and not isinstance(artifact_bytes, bytes):
            raise HybridExecutionRefused("artifact_bytes must be bytes")
        min_conf = _require_finite_number(min_confidence, "min_confidence")
        if not (0.0 < min_conf <= 1.0):
            raise HybridExecutionRefused("min_confidence must be in (0.0, 1.0]")
        t_sec = _require_finite_number(timeout_seconds, "timeout_seconds")
        if t_sec <= 0:
            raise HybridExecutionRefused("timeout_seconds must be positive")
        self._scorer_fn = scorer_fn
        self._artifact_bytes = artifact_bytes
        self._artifact_path = artifact_path
        self._min_confidence = min_conf
        self._timeout_seconds = t_sec
        self._fallback_executor = fallback_executor

    def _load_verified_artifact(self) -> tuple[bytes | None, str | None]:
        if self._artifact_bytes is not None:
            raw = self._artifact_bytes
        else:
            assert self._artifact_path is not None
            try:
                if not self._artifact_path.is_file():
                    return None, "artifact_unavailable"
                size = self._artifact_path.stat().st_size
                if size > MAX_MODEL_ARTIFACT_BYTES:
                    return None, "artifact_oversized"
                raw = self._artifact_path.read_bytes()
            except OSError:
                return None, "artifact_unavailable"
        actual_sha = sha256(raw).hexdigest()
        if not hmac.compare_digest(actual_sha, self._expected_artifact_sha256):
            return None, "artifact_digest_mismatch"
        return raw, None

    def _run_fallback(
        self,
        snapshot: FeatureSnapshot,
        *,
        proposal_id: str | None,
        reason: str,
        extra_evidence: Sequence[DecisionEvidence],
    ) -> PlaneExecutionProposal:
        if self._fallback_executor is not None:
            try:
                return self._fallback_executor.evaluate(
                    snapshot,
                    proposal_id=proposal_id,
                    extra_evidence=extra_evidence,
                    fallback_used=True,
                    fallback_reason=reason,
                )
            except HybridExecutionRefused:
                reason = f"{reason}:fallback_failed"
        pid = proposal_id or f"dmfb-{snapshot.content_digest()[:16]}"
        return PlaneExecutionProposal(
            proposal_id=pid,
            plane="decision_model",
            engine_id=self._model_id,
            engine_revision=self._model_revision,
            action_kind="hold",
            rationale=f"Deterministic fallback to hold ({reason})",
            decision_at_ns=snapshot.decision_at_ns,
            evidence=tuple(snapshot.source_evidence) + tuple(extra_evidence),
            proposed_notional_minor_units=0,
            target_scope_ref="company",
            active_snapshot_id=snapshot.snapshot_id,
            parameters={"fallback_reason": reason},
            connector_effect=None,
            token_usage=TokenUsage(0, 0),
            fallback_used=True,
            fallback_reason=reason,
        )

    def _invoke_with_timeout(
        self, snapshot: FeatureSnapshot, artifact: bytes
    ) -> tuple[ModelScore | None, str | None]:
        done = threading.Event()
        holder: dict[str, Any] = {}

        def _worker() -> None:
            try:
                holder["result"] = self._scorer_fn(snapshot, artifact)
            except Exception as error:
                holder["error"] = error
            finally:
                done.set()

        thread = threading.Thread(
            target=_worker,
            name=f"sourdough-dm-{self._model_id}",
            daemon=True,
        )
        thread.start()
        if not done.wait(timeout=self._timeout_seconds):
            return None, "model_timeout"
        if "error" in holder:
            err = holder["error"]
            return None, f"model_error:{type(err).__name__}"
        return holder.get("result"), None

    def evaluate(
        self,
        snapshot: FeatureSnapshot,
        *,
        proposal_id: str | None = None,
        extra_evidence: Sequence[DecisionEvidence] = (),
    ) -> PlaneExecutionProposal:
        # Anti-lookahead violations are hard data-integrity errors (never silent fallback).
        snapshot.validate_anti_lookahead()

        if snapshot.schema_version != self._feature_schema_version:
            return self._run_fallback(
                snapshot,
                proposal_id=proposal_id,
                reason="feature_schema_mismatch",
                extra_evidence=extra_evidence,
            )

        artifact, art_err = self._load_verified_artifact()
        if art_err is not None or artifact is None:
            return self._run_fallback(
                snapshot,
                proposal_id=proposal_id,
                reason=art_err or "artifact_unavailable",
                extra_evidence=extra_evidence,
            )

        result, run_err = self._invoke_with_timeout(snapshot, artifact)
        if run_err is not None or not isinstance(result, ModelScore):
            return self._run_fallback(
                snapshot,
                proposal_id=proposal_id,
                reason=run_err or "invalid_model_output",
                extra_evidence=extra_evidence,
            )

        # Validate finite score, confidence in [0, 1], valid regime & action_kind.
        try:
            score_val = _require_finite_number(result.score, "model.score")
            conf_val = _require_finite_number(result.confidence, "model.confidence")
            if not (0.0 <= conf_val <= 1.0):
                raise HybridExecutionRefused("confidence out of [0, 1]")
            if (
                not isinstance(result.regime_label, str)
                or not result.regime_label.strip()
            ):
                raise HybridExecutionRefused("regime_label must be nonempty")
            if result.action_kind not in WORK_DECISION_ACTION_KINDS:
                raise HybridExecutionRefused("unsupported action_kind")
            if not isinstance(result.rationale, str) or not result.rationale.strip():
                raise HybridExecutionRefused("rationale must be nonempty")
            out_params = _clone_json_mapping(
                result.parameters, label="model.parameters"
            )
        except HybridExecutionRefused:
            return self._run_fallback(
                snapshot,
                proposal_id=proposal_id,
                reason="invalid_model_output",
                extra_evidence=extra_evidence,
            )

        if conf_val < self._min_confidence:
            return self._run_fallback(
                snapshot,
                proposal_id=proposal_id,
                reason="confidence_below_threshold",
                extra_evidence=extra_evidence,
            )

        enriched_params = {
            **out_params,
            "model_score": score_val,
            "model_confidence": conf_val,
            "regime_label": result.regime_label.strip(),
            "artifact_sha256": self._expected_artifact_sha256,
        }
        repro_digest = sha256(
            _canonical_json(
                {
                    "model_id": self._model_id,
                    "model_revision": self._model_revision,
                    "artifact_sha256": self._expected_artifact_sha256,
                    "snapshot_digest": snapshot.content_digest(),
                    "action_kind": result.action_kind,
                    "parameters": enriched_params,
                }
            ).encode("utf-8")
        ).hexdigest()
        pid = proposal_id or f"dm-{repro_digest[:16]}"
        return PlaneExecutionProposal(
            proposal_id=pid,
            plane="decision_model",
            engine_id=self._model_id,
            engine_revision=self._model_revision,
            action_kind=result.action_kind,
            rationale=result.rationale,
            decision_at_ns=snapshot.decision_at_ns,
            evidence=tuple(snapshot.source_evidence) + tuple(extra_evidence),
            proposed_notional_minor_units=result.proposed_notional_minor_units,
            target_scope_ref=result.target_scope_ref,
            active_snapshot_id=snapshot.snapshot_id,
            parameters=enriched_params,
            connector_effect=result.connector_effect,
            token_usage=TokenUsage(0, 0),
            fallback_used=False,
            fallback_reason=None,
            reproducibility_digest=repro_digest,
        )


# ---------------------------------------------------------------------------
# Plane 3: Asynchronous LLM Meta-Reasoning Plane (Offline-Eval-Gated)
# ---------------------------------------------------------------------------


class LlmMetaPlaneExecutor:
    """Plane 3 asynchronous meta-reasoning gate admitting bounded, offline-evaluated policy patches."""

    def __init__(
        self,
        *,
        parameter_bounds: Mapping[str, ParameterBound],
        min_eval_samples: int = 30,
        min_holdout_margin: float = 0.0,
        max_drawdown_bps: int = 2_000,
    ) -> None:
        if not isinstance(parameter_bounds, Mapping) or not parameter_bounds:
            raise HybridExecutionRefused(
                "LlmMetaPlaneExecutor requires nonempty parameter_bounds"
            )
        clean_bounds: dict[str, ParameterBound] = {}
        for name, bound in parameter_bounds.items():
            _require_safe_id(str(name), "parameter_bounds key")
            if not isinstance(bound, ParameterBound):
                raise HybridExecutionRefused(
                    f"parameter_bounds[{name}] must be a ParameterBound"
                )
            clean_bounds[str(name)] = bound
        if type(min_eval_samples) is not int or min_eval_samples <= 0:
            raise HybridExecutionRefused("min_eval_samples must be a positive integer")
        margin = _require_finite_number(min_holdout_margin, "min_holdout_margin")
        if type(max_drawdown_bps) is not int or max_drawdown_bps < 0:
            raise HybridExecutionRefused(
                "max_drawdown_bps must be a nonnegative integer"
            )
        self._parameter_bounds = clean_bounds
        self._min_eval_samples = min_eval_samples
        self._min_holdout_margin = margin
        self._max_drawdown_bps = max_drawdown_bps

    def admit_policy_patch(
        self,
        proposal: PolicyPatchProposal,
        *,
        current_parameters: Mapping[str, float],
        verdict: OfflineEvalVerdict | None,
        decision_at_ns: int,
    ) -> PlaneExecutionProposal:
        """Validate parameter bounds and OfflineEvalVerdict before emitting a control-plane proposal."""
        if not isinstance(proposal, PolicyPatchProposal):
            raise HybridExecutionRefused("proposal must be a PolicyPatchProposal")
        if type(decision_at_ns) is not int or decision_at_ns <= 0:
            raise HybridExecutionRefused("decision_at_ns must be a positive integer")
        if verdict is None or not isinstance(verdict, OfflineEvalVerdict):
            raise HybridOfflineEvalRequired(
                "LLM meta-reasoning policy patch requires an OfflineEvalVerdict"
            )
        if not verdict.passed:
            raise HybridOfflineEvalRequired(
                f"Offline evaluation verdict '{verdict.verdict_id}' did not pass"
            )
        if verdict.policy_id != proposal.policy_id:
            raise HybridOfflineEvalRequired(
                "OfflineEvalVerdict policy_id does not match PolicyPatchProposal"
            )
        if verdict.target_revision != proposal.target_revision:
            raise HybridOfflineEvalRequired(
                "OfflineEvalVerdict target_revision does not match PolicyPatchProposal"
            )
        expected_digest = proposal.canonical_digest()
        if not hmac.compare_digest(verdict.proposal_digest, expected_digest):
            raise HybridOfflineEvalRequired(
                "OfflineEvalVerdict proposal_digest does not match canonical PolicyPatchProposal"
            )
        if verdict.evaluated_at_ns > decision_at_ns:
            raise HybridAntiLookaheadViolation(
                f"OfflineEvalVerdict evaluated_at_ns ({verdict.evaluated_at_ns}) "
                f"exceeds decision_at_ns ({decision_at_ns})"
            )
        if verdict.sample_count < self._min_eval_samples:
            raise HybridOfflineEvalRequired(
                f"OfflineEvalVerdict sample_count ({verdict.sample_count}) "
                f"below minimum ({self._min_eval_samples})"
            )
        if verdict.holdout_margin < self._min_holdout_margin:
            raise HybridOfflineEvalRequired(
                f"OfflineEvalVerdict holdout_margin ({verdict.holdout_margin}) "
                f"below minimum ({self._min_holdout_margin})"
            )
        if verdict.max_drawdown_bps > self._max_drawdown_bps:
            raise HybridOfflineEvalRequired(
                f"OfflineEvalVerdict max_drawdown_bps ({verdict.max_drawdown_bps}) "
                f"exceeds maximum ({self._max_drawdown_bps})"
            )

        updated_parameters: dict[str, float] = {}
        for key, delta in proposal.parameter_deltas.items():
            if key not in self._parameter_bounds:
                raise HybridExecutionRefused(
                    f"Parameter '{key}' is outside allowed LLM meta-plane bounds"
                )
            if key not in current_parameters:
                raise HybridExecutionRefused(
                    f"Base parameter '{key}' missing from current_parameters"
                )
            bound = self._parameter_bounds[key]
            base_val = _require_finite_number(
                current_parameters[key], f"current_parameters[{key}]"
            )
            if abs(delta) > bound.max_abs_delta:
                raise HybridExecutionRefused(
                    f"Parameter '{key}' delta ({delta}) exceeds max_abs_delta ({bound.max_abs_delta})"
                )
            new_val = base_val + delta
            if not (bound.min_value <= new_val <= bound.max_value):
                raise HybridExecutionRefused(
                    f"Parameter '{key}' updated value ({new_val}) falls outside "
                    f"[{bound.min_value}, {bound.max_value}]"
                )
            updated_parameters[key] = new_val

        return PlaneExecutionProposal(
            proposal_id=proposal.proposal_id,
            plane="llm",
            engine_id=proposal.policy_id,
            engine_revision=proposal.target_revision,
            action_kind="approve",
            rationale=proposal.rationale,
            decision_at_ns=decision_at_ns,
            evidence=proposal.evidence,
            proposed_notional_minor_units=0,
            target_scope_ref=proposal.target_scope_ref,
            active_snapshot_id=proposal.active_snapshot_id,
            parameters={
                "policy_id": proposal.policy_id,
                "base_revision": proposal.base_revision,
                "target_revision": proposal.target_revision,
                "parameter_deltas": dict(proposal.parameter_deltas),
                "updated_parameters": updated_parameters,
                "produced_by_run_id": proposal.produced_by_run_id,
                "verdict_id": verdict.verdict_id,
                "suite_revision": verdict.suite_revision,
                "evaluator_id": verdict.evaluator_id,
            },
            connector_effect=None,
            token_usage=proposal.token_usage,
            fallback_used=False,
            fallback_reason=None,
            eval_verdict=verdict,
            eval_verdict_digest=verdict.canonical_digest(),
            policy_patch_digest=expected_digest,
        )


# ---------------------------------------------------------------------------
# Runtime Adapters integrating Code & Decision Model Planes into AgentAdapterRegistry
# ---------------------------------------------------------------------------


class CodePlaneRuntimeAdapter:
    """AgentRuntimeAdapter hosting a deterministic CodePlaneExecutor at $0 LLM cost."""

    def __init__(
        self,
        *,
        provider: str = PROVIDER_CODE_PLANE,
        handler: Callable[[AgentProfileBinding, ShiftRequest, OuterToolBroker], str],
        adapter_revision: str = "code-plane-adapter-v1",
        default_timeout_seconds: float = 5.0,
    ) -> None:
        if not callable(handler):
            raise AgentRuntimeRefused(
                "CodePlaneRuntimeAdapter handler must be callable"
            )
        self._handler = handler
        self._manifest = AdapterManifest(
            provider=provider,
            adapter_revision=adapter_revision,
            transport="in_process",
            supports_tool_mediation=True,
            supports_usage_accounting=True,
            supports_sandbox_desk=False,
            execution_plane="code",
            default_timeout_seconds=default_timeout_seconds,
        )

    @property
    def manifest(self) -> AdapterManifest:
        return self._manifest

    def run_shift(
        self,
        profile: AgentProfileBinding,
        request: ShiftRequest,
        broker: OuterToolBroker,
    ) -> ShiftResult:
        if profile.provider != self._manifest.provider:
            raise AgentRuntimeRefused(
                f"CodePlaneRuntimeAdapter cannot run provider '{profile.provider}'"
            )
        output_text = self._handler(profile, request, broker)
        if not isinstance(output_text, str):
            raise AgentRuntimeRefused("CodePlaneRuntimeAdapter handler must return str")
        events = list(broker.events)
        events.append(
            AdapterEvent(
                kind="terminal",
                recorded_at_ns=time_ns(),
                payload={"plane": "code", "output_chars": len(output_text)},
            )
        )
        return ShiftResult(
            provider=self._manifest.provider,
            model=profile.model,
            run_id=request.run_id,
            turn_id=request.turn_id,
            output_text=output_text,
            usage=TokenUsage(0, 0),
            tool_calls_executed=broker.call_count,
            events=tuple(events),
        )


class DecisionModelRuntimeAdapter:
    """AgentRuntimeAdapter hosting a version-pinned Decision Model scorer at $0 LLM cost."""

    def __init__(
        self,
        *,
        provider: str = PROVIDER_DECISION_MODEL,
        handler: Callable[[AgentProfileBinding, ShiftRequest, OuterToolBroker], str],
        adapter_revision: str = "decision-model-adapter-v1",
        default_timeout_seconds: float = 10.0,
    ) -> None:
        if not callable(handler):
            raise AgentRuntimeRefused(
                "DecisionModelRuntimeAdapter handler must be callable"
            )
        self._handler = handler
        self._manifest = AdapterManifest(
            provider=provider,
            adapter_revision=adapter_revision,
            transport="in_process",
            supports_tool_mediation=True,
            supports_usage_accounting=True,
            supports_sandbox_desk=False,
            execution_plane="decision_model",
            default_timeout_seconds=default_timeout_seconds,
        )

    @property
    def manifest(self) -> AdapterManifest:
        return self._manifest

    def run_shift(
        self,
        profile: AgentProfileBinding,
        request: ShiftRequest,
        broker: OuterToolBroker,
    ) -> ShiftResult:
        if profile.provider != self._manifest.provider:
            raise AgentRuntimeRefused(
                f"DecisionModelRuntimeAdapter cannot run provider '{profile.provider}'"
            )
        output_text = self._handler(profile, request, broker)
        if not isinstance(output_text, str):
            raise AgentRuntimeRefused(
                "DecisionModelRuntimeAdapter handler must return str"
            )
        events = list(broker.events)
        events.append(
            AdapterEvent(
                kind="terminal",
                recorded_at_ns=time_ns(),
                payload={"plane": "decision_model", "output_chars": len(output_text)},
            )
        )
        return ShiftResult(
            provider=self._manifest.provider,
            model=profile.model,
            run_id=request.run_id,
            turn_id=request.turn_id,
            output_text=output_text,
            usage=TokenUsage(0, 0),
            tool_calls_executed=broker.call_count,
            events=tuple(events),
        )


# ---------------------------------------------------------------------------
# Plane 4: Unified Deterministic Control Plane Gate
# ---------------------------------------------------------------------------


class DeterministicControlPlaneGate:
    """Single authoritative pre-commit gate governing proposals from all 3 execution planes.

    Enforces live WorkRunClaim fencing, active standing_grant, active restriction_cause
    blocks, notional ceilings, LLM offline-eval verdict verification, evidence-backed
    work_decision persistence, immutable audit trails, and service-owned ConnectorProxy
    admission/dispatch without holding SQLite write locks during outbound network I/O.
    """

    def __init__(
        self,
        *,
        connector_proxy: ConnectorProxy | None = None,
        max_notional_minor_units: int = 0,
        min_llm_eval_samples: int = 1,
        min_llm_holdout_margin: float = 0.0,
        domain_plugins: Sequence[Any] = (),
        tx_lock: threading.RLock | None = None,
    ) -> None:
        if type(max_notional_minor_units) is not int or max_notional_minor_units < 0:
            raise HybridExecutionRefused(
                "max_notional_minor_units must be a nonnegative integer"
            )
        if type(min_llm_eval_samples) is not int or min_llm_eval_samples <= 0:
            raise HybridExecutionRefused("min_llm_eval_samples must be positive int")
        min_margin = _require_finite_number(
            min_llm_holdout_margin, "min_llm_holdout_margin"
        )
        self._connector_proxy = connector_proxy
        self._max_notional_minor_units = max_notional_minor_units
        self._min_llm_eval_samples = min_llm_eval_samples
        self._min_llm_holdout_margin = min_margin
        self._domain_plugins = tuple(domain_plugins)
        self._tx_lock = tx_lock

    def _verify_llm_eval_gate(self, proposal: PlaneExecutionProposal) -> str | None:
        if proposal.plane != "llm":
            return None
        if proposal.connector_effect is not None:
            return "llm_meta_plane_direct_connector_effect_prohibited"
        if proposal.action_kind in DEFENSIVE_ACTION_KINDS:
            return None
        verdict = proposal.eval_verdict
        if verdict is None or not proposal.eval_verdict_digest:
            return "llm_offline_eval_verdict_missing"
        if not verdict.passed:
            return "llm_offline_eval_verdict_failed"
        if not hmac.compare_digest(
            proposal.eval_verdict_digest, verdict.canonical_digest()
        ):
            return "llm_offline_eval_digest_mismatch"
        if (
            verdict.policy_id != proposal.engine_id
            or verdict.target_revision != proposal.engine_revision
        ):
            return "llm_offline_eval_policy_target_mismatch"
        base_rev = proposal.parameters.get("base_revision")
        raw_deltas = proposal.parameters.get("parameter_deltas")
        if (
            not isinstance(base_rev, str)
            or not base_rev.strip()
            or not isinstance(raw_deltas, Mapping)
            or not raw_deltas
        ):
            return "llm_offline_eval_missing_patch_terms"
        validated_deltas: dict[str, float] = {}
        for k, v in raw_deltas.items():
            if not isinstance(k, str) or _SAFE_ID_RE.fullmatch(k) is None:
                return "llm_offline_eval_invalid_delta_key"
            if (
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(float(v))
            ):
                return "llm_offline_eval_invalid_delta_value"
            validated_deltas[k] = float(v)
        recomputed_patch_digest = sha256(
            _canonical_json(
                {
                    "proposal_id": proposal.proposal_id,
                    "policy_id": proposal.engine_id,
                    "base_revision": base_rev,
                    "target_revision": proposal.engine_revision,
                    "parameter_deltas": validated_deltas,
                    "rationale": proposal.rationale,
                }
            ).encode("utf-8")
        ).hexdigest()
        if (
            proposal.policy_patch_digest is None
            or not hmac.compare_digest(
                proposal.policy_patch_digest, recomputed_patch_digest
            )
            or not hmac.compare_digest(verdict.proposal_digest, recomputed_patch_digest)
        ):
            return "llm_offline_eval_proposal_binding_mismatch"
        if verdict.evaluated_at_ns > proposal.decision_at_ns:
            return "llm_offline_eval_lookahead_violation"
        if verdict.sample_count < self._min_llm_eval_samples:
            return "llm_offline_eval_insufficient_samples"
        if verdict.holdout_margin < self._min_llm_holdout_margin:
            return "llm_offline_eval_insufficient_margin"
        return None

    def commit(
        self,
        writer: WriterLease,
        claim: ExecutionClaim,
        proposal: PlaneExecutionProposal,
        *,
        flow_guard: ShiftFlowGuard | None = None,
        timeout_seconds: float = 15.0,
        now_ns: int | None = None,
    ) -> PlaneExecutionOutcome:
        """Validate authority, restrictions, limits, persist work_decision, and dispatch connector effects."""
        if not isinstance(proposal, PlaneExecutionProposal):
            raise HybridExecutionRefused("proposal must be a PlaneExecutionProposal")
        company_id = writer.company_id
        if claim.company_id != company_id:
            raise HybridControlGateDenied("Execution claim company_id mismatch")
        ts = time_ns() if now_ns is None else now_ns
        action_id = f"hyb:{proposal.plane}:{proposal.proposal_id}"
        agent_id = claim.founder_id

        # ------------------------------------------------------------------
        # Phase 1: Atomic authority, restriction, risk, pre-flight connector,
        #          work_decision, and service-owned admission issuance
        # ------------------------------------------------------------------
        denial_reason: str | None = None
        decision: WorkDecision | None = None
        issued_admission_id: str | None = None
        effect = proposal.connector_effect

        with write_uow(writer, tx_lock=self._tx_lock) as db:
            # 1. Live WorkRunClaim fencing & active standing_grant
            try:
                authority = resolve_run(db, claim)
                authority.require_current(claim.purpose)
            except (DutyRefused, RunFenced) as error:
                raise HybridControlGateDenied(str(error)) from error
            if authority.current_scope == "revoked":
                raise HybridControlGateDenied("Agent standing grant is revoked")

            # 2. Plane-specific invariants (LLM offline-eval gate & notional ceiling)
            llm_err = self._verify_llm_eval_gate(proposal)
            if llm_err is not None:
                denial_reason = llm_err
            elif (
                self._max_notional_minor_units > 0
                and proposal.proposed_notional_minor_units
                > self._max_notional_minor_units
            ):
                denial_reason = (
                    f"notional_cap_exceeded:{proposal.proposed_notional_minor_units}"
                    f">{self._max_notional_minor_units}"
                )

            # 3. Active restriction_cause gate (uniform across ALL 3 planes)
            if denial_reason is None and (
                proposal.action_kind not in DEFENSIVE_ACTION_KINDS or effect is not None
            ):
                checked_scopes = {
                    "company:all",
                    "company",
                    "global",
                    "all",
                    f"company:{company_id}".lower(),
                    proposal.target_scope_ref.lower(),
                }
                if effect is not None:
                    checked_scopes.add(f"connector:{effect.binding_id}".lower())
                active_restrictions = db.execute(
                    """SELECT restriction_id, scope_ref, reason
                       FROM restriction_cause
                       WHERE company_id = ? AND active = 1
                       ORDER BY restriction_id""",
                    (company_id,),
                ).fetchall()
                blocking = [
                    (str(r[0]), str(r[1]), str(r[2]))
                    for r in active_restrictions
                    if str(r[1]).lower() in checked_scopes
                ]
                if blocking:
                    rid, sref, _ = blocking[0]
                    denial_reason = f"active_restriction:{rid}:{sref}"

            # 4. Pre-flight connector authorization & taint validation BEFORE persisting decision
            binding = None
            binding_op = None
            if denial_reason is None and effect is not None:
                if self._connector_proxy is None:
                    denial_reason = "connector_proxy_not_configured"
                else:
                    binding = read_connector_binding(
                        db, company_id=company_id, binding_id=effect.binding_id
                    )
                    if binding is None or binding.status != "active":
                        denial_reason = (
                            f"connector_binding_inactive:{effect.binding_id}"
                        )
                    elif (
                        effect.expected_environment is not None
                        and binding.environment != effect.expected_environment
                    ):
                        denial_reason = (
                            f"connector_environment_mismatch:{binding.environment}"
                        )
                    else:
                        binding_op = binding.operation(effect.operation)
                        if binding_op is None:
                            denial_reason = (
                                f"connector_operation_unregistered:{effect.operation}"
                            )
                        else:
                            try:
                                adapter = self._connector_proxy._adapters.get(
                                    binding.connector_kind
                                )
                                manifest_op = adapter.manifest.operation(
                                    effect.operation
                                )
                                if (
                                    manifest_op is None
                                    or manifest_op.mode != binding_op.mode
                                ):
                                    denial_reason = f"connector_operation_mismatch:{effect.operation}"
                                else:
                                    validate_payload_against_schema(
                                        effect.payload, manifest_op.request_schema
                                    )
                                    validate_payload_against_schema(
                                        effect.payload, binding_op.request_schema
                                    )
                            except ConnectorRefused:
                                denial_reason = (
                                    f"connector_payload_invalid:{effect.operation}"
                                )
                    if (
                        denial_reason is None
                        and binding is not None
                        and binding_op is not None
                    ):
                        grants = read_connector_grants(
                            db,
                            company_id=company_id,
                            agent_id=agent_id,
                            binding_id=binding.binding_id,
                            active_only=True,
                        )
                        matching_grant = next(
                            (
                                g
                                for g in grants
                                if g.standing_grant_id == claim.grant_id
                                and g.binding_revision == binding.revision
                                and binding_op.name in g.allowed_operations
                            ),
                            None,
                        )
                        if matching_grant is None:
                            denial_reason = f"connector_grant_missing:{binding.binding_id}:{binding_op.name}"
                    if (
                        denial_reason is None
                        and flow_guard is not None
                        and binding is not None
                        and binding_op is not None
                    ):
                        if (
                            flow_guard.read_private_desk
                            and binding.environment != "internal"
                        ):
                            denial_reason = "shift_flow_guard_private_desk_taint"
                        elif flow_guard.read_peer_input and binding_op.mode in (
                            "write",
                            "effectful",
                        ):
                            denial_reason = "shift_flow_guard_peer_input_taint"

            if denial_reason is not None:
                append_audit_event(
                    db,
                    company_id=company_id,
                    domain="work",
                    entity_type="hybrid_proposal",
                    entity_id=action_id,
                    event_type="hybrid.proposal_denied",
                    actor_id=agent_id,
                    run_id=claim.run_id,
                    payload={
                        "plane": proposal.plane,
                        "engine_id": proposal.engine_id,
                        "engine_revision": proposal.engine_revision,
                        "action_kind": proposal.action_kind,
                        "target_scope_ref": proposal.target_scope_ref,
                        "reason": denial_reason,
                    },
                    recorded_at_ns=ts,
                )
            else:
                # 5. Persist evidence-backed work_decision atomically, binding replay
                #    to the complete proposal terms including connector intent.
                terms_digest = sha256(
                    _canonical_json(
                        {
                            "plane": proposal.plane,
                            "engine_id": proposal.engine_id,
                            "engine_revision": proposal.engine_revision,
                            "action_kind": proposal.action_kind,
                            "target_scope_ref": proposal.target_scope_ref,
                            "proposed_notional_minor_units": proposal.proposed_notional_minor_units,
                            "parameters": dict(proposal.parameters),
                            "eval_verdict_digest": proposal.eval_verdict_digest,
                            "policy_patch_digest": proposal.policy_patch_digest,
                            "connector_effect": (
                                None
                                if effect is None
                                else {
                                    "binding_id": effect.binding_id,
                                    "operation": effect.operation,
                                    "request_id": effect.request_id,
                                    "payload": dict(effect.payload),
                                    "expected_environment": effect.expected_environment,
                                }
                            ),
                        }
                    ).encode("utf-8")
                ).hexdigest()
                try:
                    decision = record_work_decision(
                        db,
                        company_id=company_id,
                        work_item_id=claim.duty_id,
                        run_id=claim.run_id,
                        fencing_token=claim.fencing_token,
                        agent_id=agent_id,
                        action_kind=proposal.action_kind,
                        rationale=(
                            f"[{proposal.plane}:{proposal.engine_id}@{proposal.engine_revision}"
                            f"|terms={terms_digest[:16]}] {proposal.rationale}"
                        ),
                        evidence=proposal.evidence,
                        decision_at_ns=proposal.decision_at_ns,
                        action_id=action_id,
                        active_snapshot_id=proposal.active_snapshot_id,
                        domain_plugins=self._domain_plugins,
                    )
                except (WorkKernelRefused, RunFenced) as error:
                    raise HybridControlGateDenied(str(error)) from error

                if proposal.fallback_used:
                    append_audit_event(
                        db,
                        company_id=company_id,
                        domain="work",
                        entity_type="work_decision",
                        entity_id=decision.decision_id,
                        event_type="hybrid.fallback_triggered",
                        actor_id=agent_id,
                        run_id=claim.run_id,
                        payload={
                            "plane": proposal.plane,
                            "engine_id": proposal.engine_id,
                            "engine_revision": proposal.engine_revision,
                            "fallback_reason": proposal.fallback_reason,
                            "action_kind": proposal.action_kind,
                            "reproducibility_digest": proposal.reproducibility_digest,
                        },
                        recorded_at_ns=ts,
                    )

                # 6. Atomically issue service-owned connector admission for effectful operations
                #    keyed strictly to (company_id, agent_id, action_id) so one proposal can
                #    never mint multiple admissions even across replay attempts.
                if (
                    effect is not None
                    and binding is not None
                    and binding_op is not None
                    and binding_op.mode == "effectful"
                ):
                    det_aid = (
                        "hybadm-"
                        + sha256(
                            f"{company_id}:{agent_id}:{action_id}".encode("utf-8")
                        ).hexdigest()[:24]
                    )
                    try:
                        adm = issue_connector_admission_in_tx(
                            db,
                            company_id=company_id,
                            binding_id=binding.binding_id,
                            binding_revision=binding.revision,
                            operation=binding_op.name,
                            request_id=effect.request_id,
                            payload=effect.payload,
                            issued_by_service="core.hybrid_control_gate",
                            actor_id=agent_id,
                            admission_id=det_aid,
                            now_ns=ts,
                        )
                    except ConnectorRefused as error:
                        raise HybridControlGateDenied(str(error)) from error
                    issued_admission_id = adm.admission_id

                append_audit_event(
                    db,
                    company_id=company_id,
                    domain="work",
                    entity_type="work_decision",
                    entity_id=decision.decision_id,
                    event_type=(
                        "hybrid.proposal_admitted"
                        if effect is not None
                        else "hybrid.proposal_committed"
                    ),
                    actor_id=agent_id,
                    run_id=claim.run_id,
                    command_id=issued_admission_id,
                    payload={
                        "plane": proposal.plane,
                        "engine_id": proposal.engine_id,
                        "engine_revision": proposal.engine_revision,
                        "action_kind": proposal.action_kind,
                        "target_scope_ref": proposal.target_scope_ref,
                        "proposed_notional_minor_units": proposal.proposed_notional_minor_units,
                        "token_input": proposal.token_usage.input_tokens,
                        "token_output": proposal.token_usage.output_tokens,
                        "fallback_used": proposal.fallback_used,
                        "reproducibility_digest": proposal.reproducibility_digest,
                        "eval_verdict_digest": proposal.eval_verdict_digest,
                        "connector_binding_id": (
                            effect.binding_id if effect is not None else None
                        ),
                    },
                    recorded_at_ns=ts,
                )

        if denial_reason is not None:
            raise HybridControlGateDenied(
                f"DeterministicControlPlaneGate denied proposal '{action_id}': {denial_reason}"
            )
        assert decision is not None

        # ------------------------------------------------------------------
        # Phase 2: Outbound connector dispatch (NO SQLite write lock held!)
        # ------------------------------------------------------------------
        connector_response: ConnectorResponse | None = None
        if effect is not None:
            assert self._connector_proxy is not None
            try:
                connector_response = self._connector_proxy.invoke(
                    writer,
                    claim,
                    binding_id=effect.binding_id,
                    operation=effect.operation,
                    request_id=effect.request_id,
                    payload=effect.payload,
                    flow_guard=flow_guard,
                    admission_id=issued_admission_id,
                    expected_environment=effect.expected_environment,
                    timeout_seconds=timeout_seconds,
                    now_ns=ts,
                )
            except Exception as error:
                fail_ts = time_ns() if now_ns is None else (now_ns + 2)
                if issued_admission_id is not None:
                    try:
                        revoke_connector_admission(
                            writer,
                            company_id=company_id,
                            admission_id=issued_admission_id,
                            actor_id=agent_id,
                            now_ns=fail_ts,
                            tx_lock=self._tx_lock,
                        )
                    except ConnectorRefused:
                        pass
                with write_uow(writer, tx_lock=self._tx_lock) as db:
                    append_audit_event(
                        db,
                        company_id=company_id,
                        domain="work",
                        entity_type="work_decision",
                        entity_id=decision.decision_id,
                        event_type="hybrid.proposal_connector_failed",
                        actor_id=agent_id,
                        run_id=claim.run_id,
                        command_id=issued_admission_id,
                        payload={
                            "plane": proposal.plane,
                            "binding_id": effect.binding_id,
                            "operation": effect.operation,
                            "error_type": type(error).__name__,
                        },
                        recorded_at_ns=fail_ts,
                    )
                raise

            # Phase 3: Record final committed audit linking decision & connector outcome
            commit_ts = time_ns() if now_ns is None else (now_ns + 2)
            with write_uow(writer, tx_lock=self._tx_lock) as db:
                append_audit_event(
                    db,
                    company_id=company_id,
                    domain="work",
                    entity_type="work_decision",
                    entity_id=decision.decision_id,
                    event_type="hybrid.proposal_committed",
                    actor_id=agent_id,
                    run_id=claim.run_id,
                    command_id=issued_admission_id,
                    payload={
                        "plane": proposal.plane,
                        "engine_id": proposal.engine_id,
                        "engine_revision": proposal.engine_revision,
                        "action_kind": proposal.action_kind,
                        "connector_binding_id": effect.binding_id,
                        "connector_operation": effect.operation,
                        "connector_status": connector_response.status,
                        "connector_source_ref": connector_response.source_ref,
                        "reproducibility_digest": proposal.reproducibility_digest,
                    },
                    recorded_at_ns=commit_ts,
                )

        return PlaneExecutionOutcome(
            decision=decision,
            proposal=proposal,
            connector_response=connector_response,
            admission_id=issued_admission_id,
        )


__all__ = [
    "DEFENSIVE_ACTION_KINDS",
    "MAX_MODEL_ARTIFACT_BYTES",
    "CodePlaneExecutor",
    "CodePlaneRuntimeAdapter",
    "CodeRuleDecision",
    "ConnectorEffectIntent",
    "DecisionModelPlaneExecutor",
    "DecisionModelRuntimeAdapter",
    "DeterministicControlPlaneGate",
    "FeaturePoint",
    "FeatureSnapshot",
    "HybridAntiLookaheadViolation",
    "HybridControlGateDenied",
    "HybridExecutionRefused",
    "HybridOfflineEvalRequired",
    "LlmMetaPlaneExecutor",
    "ModelScore",
    "OfflineEvalVerdict",
    "ParameterBound",
    "PlaneExecutionOutcome",
    "PlaneExecutionProposal",
    "PolicyPatchProposal",
]
