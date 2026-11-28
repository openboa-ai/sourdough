"""Order-blind pairwise judging, identity redaction, and rubric digest verification."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..agent_runtime.protocol import (
    AgentProfileBinding,
    AgentRuntimeAdapter,
    OuterToolBroker,
    ShiftRequest,
    hydrate_shift_context,
)
from ..trial_registry import (
    BlindedTrial,
    BlindPairwisePacket,
    PairwiseBlindResult,
    PairwiseJudgment,
    TrialRefused,
    blind_pairwise_packets,
    commit_position_swapped_pairwise_grade,
)
from .sandbox_workspace import _canonical_digest, _canonical_json

_BLIND_REDACTED_KEYS = frozenset(
    {
        "arm",
        "slot_id",
        "run_id",
        "executed_runs",
        "agent_id",
        "owner_agent_id",
        "company_id",
        "procedure_id",
        "method_id",
        "delta_id",
        "snapshot_id",
        "active_snapshot_id",
        "desk_path",
        "store_path",
        "workspace_path",
        "decision_id",
        "work_item_id",
        "created_at_ns",
        "updated_at_ns",
        "recorded_at_ns",
        "started_at_ns",
        "completed_at_ns",
    }
)
_BLIND_PATH_RE = re.compile(
    r"(?:(?:/(?:Users|home|tmp|var|private|Volumes)/[^\s\"',;)\]}]+)|(?:\.desk/[^\s\"',;)\]}]+))"
)
_MAX_BLIND_JUDGE_CONTEXT_BYTES = 131_072


def _redact_blinded_value(value: Any) -> Any:
    """Recursively strip identifying arm/run/agent/path metadata from blinded judge evidence."""
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            if key in _BLIND_REDACTED_KEYS:
                continue
            cleaned[key] = _redact_blinded_value(v)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_redact_blinded_value(item) for item in value]
    if isinstance(value, str):
        return _BLIND_PATH_RE.sub("[redacted-path]", value)
    return value


def _verify_and_redact_rubric(
    rubric_document: str | Mapping[str, Any] | None,
    expected_digest: str,
) -> Any:
    if rubric_document is None:
        raise TrialRefused(
            "Model-backed blind judge requires rubric_document matching sealed rubric_digest"
        )
    if isinstance(rubric_document, str):
        actual = sha256(rubric_document.encode("utf-8")).hexdigest()
        if actual != expected_digest:
            raise TrialRefused(
                "Rubric document digest does not match sealed rubric_digest"
            )
        return _redact_blinded_value(rubric_document)
    if isinstance(rubric_document, Mapping):
        if _canonical_digest(rubric_document) != expected_digest:
            raise TrialRefused(
                "Rubric document digest does not match sealed rubric_digest"
            )
        return _redact_blinded_value(rubric_document)
    raise TrialRefused("Unsupported rubric_document type for blind judge")


def _verify_and_redact_blinded_evidence(
    trial: BlindedTrial,
    raw_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    needs_any = any(
        d is not None
        for d in (
            trial.decision_digest,
            trial.tool_trace_digest,
            trial.service_effects_digest,
        )
    )
    if needs_any and not isinstance(raw_evidence, Mapping):
        raise TrialRefused(
            "Model-backed blind judge requires verified evidence matching sealed packet digests"
        )
    evidence_map: Mapping[str, Any] = raw_evidence or {}
    redacted: dict[str, Any] = {}
    for field_name, expected_digest in (
        ("decision", trial.decision_digest),
        ("tool_trace", trial.tool_trace_digest),
        ("service_effects", trial.service_effects_digest),
    ):
        if expected_digest is None:
            continue
        payload = evidence_map.get(field_name)
        if not isinstance(payload, Mapping):
            raise TrialRefused(
                f"Model-backed blind judge is missing verified '{field_name}' payload"
            )
        if _canonical_digest(payload) != expected_digest:
            raise TrialRefused(
                f"Blinded '{field_name}' payload digest does not match sealed packet"
            )
        redacted[field_name] = _redact_blinded_value(payload)
    return redacted


def _judge_via_adapter(
    adapter: AgentRuntimeAdapter,
    profile: AgentProfileBinding,
    packet: BlindPairwisePacket,
    *,
    rubric_content: Any,
    first_evidence: Mapping[str, Any],
    second_evidence: Mapping[str, Any],
) -> PairwiseJudgment:
    run_id = f"judge-{packet.pair_id[:16]}-{packet.ordering}"
    judge_note = _canonical_json(
        {
            "pair_id": packet.pair_id,
            "ordering": packet.ordering,
            "rubric_digest": packet.rubric_digest,
            "rubric": rubric_content,
            "evaluator_digest": packet.evaluator_digest,
            "first": {
                "result_id": packet.first.result_id,
                "outcome": packet.first.outcome,
                "decision_digest": packet.first.decision_digest,
                "tool_trace_digest": packet.first.tool_trace_digest,
                "service_effects_digest": packet.first.service_effects_digest,
                "evidence": dict(first_evidence),
            },
            "second": {
                "result_id": packet.second.result_id,
                "outcome": packet.second.outcome,
                "decision_digest": packet.second.decision_digest,
                "tool_trace_digest": packet.second.tool_trace_digest,
                "service_effects_digest": packet.second.service_effects_digest,
                "evidence": dict(second_evidence),
            },
        }
    )
    required_bytes = len(judge_note.encode("utf-8")) + 4096
    if required_bytes > _MAX_BLIND_JUDGE_CONTEXT_BYTES:
        raise TrialRefused("Blinded judge evidence exceeds maximum safe context budget")
    ctx = hydrate_shift_context(
        company_id="offline-evaluator",
        agent_id="blind-judge",
        run_id=run_id,
        fencing_token=1,
        grant_scope="read_only_judge",
        mandate_summary=(
            "Score blinded trial results strictly on rubric grounding, "
            "constraint satisfaction, and service effects."
        ),
        history_notes=(judge_note,),
        max_context_bytes=max(16_384, required_bytes),
    )
    if ctx.compacted or not ctx.history_notes:
        raise TrialRefused(
            "Blinded judge evidence was compacted or evicted before judging"
        )
    req = ShiftRequest(
        company_id="offline-evaluator",
        agent_id="blind-judge",
        run_id=run_id,
        turn_id=f"turn-{packet.ordering}",
        fencing_token=1,
        instruction=(
            "Compare first and second blinded results using the supplied rubric and "
            "identity-redacted evidence. Emit JSON with keys 'rationale' (non-empty "
            "string) and 'preferred' ('first' | 'second' | 'tie')."
        ),
        context=ctx,
        tools=(),
        budget_ceiling_minor_units=1000,
        max_tool_calls=0,
    )
    broker = OuterToolBroker(
        company_id="offline-evaluator",
        agent_id="blind-judge",
        run_id=run_id,
        fencing_token=1,
        handlers={},
        max_tool_calls=0,
    )
    res = adapter.run_shift(profile, req, broker)
    try:
        parsed = json.loads(res.output_text)
    except (json.JSONDecodeError, TypeError) as error:
        raise TrialRefused(
            "Blind judge adapter did not return valid JSON judgment"
        ) from error
    if not isinstance(parsed, Mapping):
        raise TrialRefused("Blind judge output must be a JSON object")
    return PairwiseJudgment(
        rationale=str(parsed.get("rationale", "")),
        preferred=str(parsed.get("preferred", "")),
    )


def run_blind_pairwise_judge(
    evaluator_store: Path,
    *,
    revision: str,
    judge_fn: Callable[[BlindPairwisePacket], PairwiseJudgment] | None = None,
    judge_adapter: AgentRuntimeAdapter | None = None,
    judge_profile: AgentProfileBinding | None = None,
    rubric_document: str | Mapping[str, Any] | None = None,
    evidence_by_result_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[PairwiseBlindResult, ...]:
    """Run order-blind position-swapped (AB, BA) pairwise judging over a sealed recursive suite."""
    if judge_fn is None and (judge_adapter is None or judge_profile is None):
        raise TrialRefused(
            "Blind pairwise judge requires judge_fn or (judge_adapter, judge_profile)"
        )
    pairs = blind_pairwise_packets(evaluator_store, revision=revision)
    results: list[PairwiseBlindResult] = []
    for ab, ba in pairs:
        if judge_fn is not None:
            ab_j = judge_fn(ab)
            ba_j = judge_fn(ba)
        else:
            assert judge_adapter is not None and judge_profile is not None
            redacted_rubric = _verify_and_redact_rubric(
                rubric_document, ab.rubric_digest
            )
            ev_map = evidence_by_result_id or {}
            ab_first_ev = _verify_and_redact_blinded_evidence(
                ab.first, ev_map.get(ab.first.result_id)
            )
            ab_second_ev = _verify_and_redact_blinded_evidence(
                ab.second, ev_map.get(ab.second.result_id)
            )
            ab_j = _judge_via_adapter(
                judge_adapter,
                judge_profile,
                ab,
                rubric_content=redacted_rubric,
                first_evidence=ab_first_ev,
                second_evidence=ab_second_ev,
            )
            ba_j = _judge_via_adapter(
                judge_adapter,
                judge_profile,
                ba,
                rubric_content=redacted_rubric,
                first_evidence=ab_second_ev,
                second_evidence=ab_first_ev,
            )
        results.append(
            commit_position_swapped_pairwise_grade(
                evaluator_store,
                revision=revision,
                ab_packet=ab,
                ab_judgment=ab_j,
                ba_packet=ba,
                ba_judgment=ba_j,
            )
        )
    return tuple(results)
