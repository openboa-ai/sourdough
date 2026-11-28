"""Data models, constants, validation helpers, and content-addressing utilities for method learning."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from ..agent_runtime.protocol import (
    skill_slug as _skill_slug,
    summarize_when_to_use as _summarize_when_to_use,
)
from ..duty_run import (
    DutyRefused,
    ExecutionClaim,
    employment_cutoff,
    resolve_run,
)

METHOD_CONTRACT_VERSION = "method-learning-v1"
EVALUATOR_REVISION = "method-evaluator-v1"

VALID_DELTA_KINDS = frozenset({"ADD", "PATCH", "DEPRECATE"})
VALID_ITEM_KINDS = frozenset({"work_method", "improvement_procedure", "helper_script"})
VALID_AUDIENCE_SCOPES = frozenset({"one_agent", "project", "company"})

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_BODY_BYTES = 65_536
_MAX_NOTE_CHARS = 2000

_PRIVATE_DESK_PATH_RE = re.compile(
    r"(?:"
    r"\.desk(?:/|\\|%2[fF]|%5[cC])(?:drafts|scratch|logs|mounts)(?:(?:/|\\|%2[fF]|%5[cC])[^\s,;)\]}\"']*)?|"
    r"(?<![A-Za-z0-9_])(?i:desks)(?:/|\\|%2[fF]|%5[cC])[A-Za-z0-9._-]+(?:(?:/|\\|%2[fF]|%5[cC])[^\s,;)\]}\"']*)?|"
    r"private_(?:desk|draft):[A-Za-z0-9._:/\\%-]*"
    r")"
)
_SECRET_TOKEN_RE = re.compile(
    r"(?:"
    r"\bsk-(?:live|test|proj|ant|canary)?-?[A-Za-z0-9_-]{6,}\b|"
    r"\bghp_[A-Za-z0-9]{10,}\b|"
    r"\bAKIA[0-9A-Z]{8,}\b|"
    r"\bBearer\s+[A-Za-z0-9._-]{8,}|"
    r"(?i:\b(?:api_key|secret_key|password|access_token)\s*[:=]\s*['\"]?[A-Za-z0-9._-]{4,}['\"]?)"
    r")"
)
_INJECTION_PATTERNS = (
    re.compile(
        r"\b(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|existing)\s+"
        r"(?:grants?|instructions?|rules?|policies|system)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:bypass|circumvent|disable|skip)\s+(?:the\s+)?"
        r"(?:outertoolbroker|tool\s*broker|sandbox|fencing|pre-?commit\s*gate|gate|grants?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\boverride\s+(?:system|grants?|authority|sandbox|policy)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:reveal|exfiltrate|dump|leak)\s+(?:the\s+)?"
        r"(?:system\s+prompt|secrets?|api\s*keys?|tokens?|private\s+desk)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bsystem\s+prompt\s*[:=]", re.IGNORECASE),
    re.compile(
        r"\bspoof\s+(?:agent_id|company_id|run_id|fencing_token|actor_id|created_by_agent_id)\b",
        re.IGNORECASE,
    ),
)

_SCOPE_ORDER = {"company": 0, "project": 1, "one_agent": 2}

_DELTA_COLUMNS = """company_id, method_id, revision_sha256, delta_kind, item_kind,
    trigger_spec_json, body_text, audience_scope, audience_target_id,
    author_agent_id, parent_method_ids_json, active_snapshot_id,
    source_refs_json, procedure_meta_json, uncertainty_note,
    falsification_criteria, superseded_revision, redaction_passed,
    redaction_report_json, status, created_at_ns"""


class MethodRefused(ValueError):
    """Method learning, redaction, qualification, or activation operation was refused."""


@dataclass(frozen=True)
class RedactionResult:
    redacted_body: str
    redaction_receipt_id: str
    stripped_items: tuple[str, ...] = ()
    retained_source_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class MethodDelta:
    delta_id: str
    company_id: str
    method_id: str
    revision_sha256: str
    delta_kind: str
    item_kind: str
    target_kind: str
    trigger_spec: dict[str, Any]
    body_text: str
    audience_scope: str
    audience_target_id: str | None
    author_agent_id: str
    parent_method_ids: tuple[str, ...]
    active_snapshot_id: str | None
    source_refs: tuple[str, ...]
    procedure_meta: dict[str, Any] | None
    uncertainty_note: str
    falsification_criteria: str
    superseded_revision: str | None
    redaction_passed: bool
    redaction_report: dict[str, Any] | None
    status: str
    created_at_ns: int

    @property
    def body(self) -> str:
        return self.body_text


@dataclass(frozen=True)
class MethodQualification:
    company_id: str
    qualification_id: str
    method_id: str
    revision_sha256: str
    delta_id: str
    baseline_revision_sha256: str | None
    stage1_passed: bool
    stage2_passed: bool
    stage3_passed: bool
    trial_count: int
    metrics: dict[str, Any]
    evaluator_revision: str
    passed: bool
    recorded_at_ns: int


@dataclass(frozen=True)
class MethodActivation:
    activation_id: str
    request_id: str
    company_id: str
    operation: str
    method_id: str
    revision_sha256: str
    delta_id: str
    qualification_id: str
    audience_scope: str
    audience_target_id: str | None
    activation_seq: int
    active_snapshot_id: str
    principal_id: str
    grant_id: str
    quarantined: bool
    quarantine_reason: str | None
    recorded_at_ns: int


@dataclass(frozen=True)
class MethodQuarantineReceipt:
    request_id: str
    company_id: str
    faulted_method_id: str
    faulted_revision_sha256: str
    quarantined_methods: tuple[str, ...]
    quarantined_revisions: tuple[tuple[str, str], ...]
    restored_method_id: str | None
    restored_revision_sha256: str | None
    active_snapshot_id: str | None
    reason: str
    principal_id: str
    grant_id: str


@dataclass(frozen=True)
class ActiveMethod:
    method_id: str
    revision_sha256: str
    delta_kind: str
    item_kind: str
    trigger_spec: dict[str, Any]
    body_text: str
    audience_scope: str
    audience_target_id: str | None
    active_snapshot_id: str | None
    parent_method_ids: tuple[str, ...]
    activation_seq: int
    procedure_meta: dict[str, Any] | None = None
    uncertainty_note: str = ""
    falsification_criteria: str = ""

    @property
    def target_kind(self) -> str:
        return self.item_kind

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "method_id": self.method_id,
            "revision_sha256": self.revision_sha256,
            "delta_kind": self.delta_kind,
            "item_kind": self.item_kind,
            "target_kind": self.item_kind,
            "trigger_spec": dict(self.trigger_spec),
            "body_text": self.body_text,
            "audience_scope": self.audience_scope,
            "audience_target_id": self.audience_target_id,
            "active_snapshot_id": self.active_snapshot_id,
            "parent_method_ids": list(self.parent_method_ids),
            "activation_seq": self.activation_seq,
            "uncertainty_note": self.uncertainty_note,
            "falsification_criteria": self.falsification_criteria,
        }
        if self.procedure_meta is not None:
            payload["procedure_meta"] = dict(self.procedure_meta)
        return payload

    def to_index_dict(self, *, slug: str | None = None) -> dict[str, Any]:
        eff_slug = slug or _skill_slug(self.method_id)
        return {
            "method_id": self.method_id,
            "skill_name": eff_slug,
            "item_kind": self.item_kind,
            "when_to_use": _summarize_when_to_use({"trigger_spec": self.trigger_spec}),
            "skill_path": f"skills/{eff_slug}/SKILL.md",
            "revision_sha256": self.revision_sha256,
            "audience_scope": self.audience_scope,
        }


@dataclass(frozen=True)
class CausalMethodTrace:
    company_id: str
    agent_id: str
    run_id: str
    duty_kind: str
    active_snapshot_id: str | None
    available_methods: tuple[str, ...]
    retrieved_methods: tuple[str, ...]
    invoked_methods: tuple[str, ...]
    outcome_delta: dict[str, Any]
    stable_prefix_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "company_id": self.company_id,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "duty_kind": self.duty_kind,
            "active_snapshot_id": self.active_snapshot_id,
            "available": list(self.available_methods),
            "retrieved": list(self.retrieved_methods),
            "invoked": list(self.invoked_methods),
            "outcome_delta": dict(self.outcome_delta),
        }
        if self.stable_prefix_sha256:
            payload["stable_prefix_sha256"] = self.stable_prefix_sha256
        return payload


@dataclass(frozen=True)
class DreamingAuditResult:
    company_id: str
    agent_id: str
    episodes_audited: int
    patterns_detected: tuple[dict[str, Any], ...]
    drafted_deltas: tuple[MethodDelta, ...]
    skipped_reason: str | None = None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _format_delta_id(method_id: str, revision_sha256: str) -> str:
    return f"{method_id}:{revision_sha256}"


def opaque_one_agent_method_id(method_id: str) -> str:
    """Return a deterministic opaque Board-safe identifier for an Agent-private method_id."""
    return f"one_agent_method:{sha256(str(method_id).encode('utf-8')).hexdigest()[:16]}"


def opaque_one_agent_delta_id(method_id: str, revision_sha256: str) -> str:
    """Return a deterministic opaque Board-safe delta_id for an Agent-private method delta."""
    return _format_delta_id(opaque_one_agent_method_id(method_id), revision_sha256)


def _require_safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value) or ".." in value:
        raise MethodRefused(f"{label} must be a valid identifier")
    return value


def _require_nonempty_str(
    value: Any, label: str, *, max_bytes: int = _MAX_BODY_BYTES
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MethodRefused(f"{label} must be a nonempty string")
    if len(value.encode("utf-8")) > max_bytes:
        raise MethodRefused(f"{label} exceeds maximum byte length ({max_bytes})")
    return value


def _validate_claim(
    db: sqlite3.Connection,
    claim: ExecutionClaim,
    *,
    company_id: str,
) -> None:
    if not isinstance(claim, ExecutionClaim):
        raise MethodRefused("A valid DutyClaim or WorkRunClaim is required")
    if claim.company_id != company_id:
        raise MethodRefused("Claim company_id does not match store identity")
    if employment_cutoff(db, claim.founder_id) is not None:
        raise MethodRefused("Agent employment is fenced")
    try:
        authority = resolve_run(db, claim)
    except DutyRefused as exc:
        raise MethodRefused(str(exc)) from exc
    if not authority.is_current or authority.duty_state != "running":
        raise MethodRefused("Method learning requires a current running claim")


def _row_to_delta(row: tuple[Any, ...]) -> MethodDelta:
    (
        company_id,
        method_id,
        revision_sha256,
        delta_kind,
        item_kind,
        trigger_spec_json,
        body_text,
        audience_scope,
        audience_target_id,
        author_agent_id,
        parent_method_ids_json,
        active_snapshot_id,
        source_refs_json,
        procedure_meta_json,
        uncertainty_note,
        falsification_criteria,
        superseded_revision,
        redaction_passed,
        redaction_report_json,
        status,
        created_at_ns,
    ) = row
    return MethodDelta(
        delta_id=_format_delta_id(str(method_id), str(revision_sha256)),
        company_id=str(company_id),
        method_id=str(method_id),
        revision_sha256=str(revision_sha256),
        delta_kind=str(delta_kind),
        item_kind=str(item_kind),
        target_kind=str(item_kind),
        trigger_spec=json.loads(str(trigger_spec_json)),
        body_text=str(body_text),
        audience_scope=str(audience_scope),
        audience_target_id=None
        if audience_target_id is None
        else str(audience_target_id),
        author_agent_id=str(author_agent_id),
        parent_method_ids=tuple(json.loads(str(parent_method_ids_json))),
        active_snapshot_id=None
        if active_snapshot_id is None
        else str(active_snapshot_id),
        source_refs=tuple(json.loads(str(source_refs_json))),
        procedure_meta=(
            None
            if procedure_meta_json is None
            else json.loads(str(procedure_meta_json))
        ),
        uncertainty_note=str(uncertainty_note),
        falsification_criteria=str(falsification_criteria),
        superseded_revision=(
            None if superseded_revision is None else str(superseded_revision)
        ),
        redaction_passed=bool(redaction_passed),
        redaction_report=(
            None
            if redaction_report_json is None
            else json.loads(str(redaction_report_json))
        ),
        status=str(status),
        created_at_ns=int(created_at_ns),
    )


def _lookup_delta(
    db: sqlite3.Connection, company_id: str, delta_id: str
) -> MethodDelta:
    if not isinstance(delta_id, str) or not delta_id.strip():
        raise MethodRefused("delta_id must be a nonempty string")
    raw = delta_id.strip()
    row = None
    for sep in (":", "@"):
        if sep in raw:
            mid, rev = raw.rsplit(sep, 1)
            if _SHA256_RE.fullmatch(rev):
                row = db.execute(
                    f"""SELECT {_DELTA_COLUMNS}
                        FROM method_delta
                        WHERE company_id = ? AND method_id = ? AND revision_sha256 = ?""",
                    (company_id, mid, rev),
                ).fetchone()
                if row is not None:
                    return _row_to_delta(row)
    if _SHA256_RE.fullmatch(raw):
        rows = db.execute(
            f"""SELECT {_DELTA_COLUMNS}
                FROM method_delta
                WHERE company_id = ? AND revision_sha256 = ?""",
            (company_id, raw),
        ).fetchall()
        if len(rows) == 1:
            return _row_to_delta(rows[0])
    raise MethodRefused(f"Method delta '{delta_id}' not found")


def compute_active_snapshot_id(
    active_methods: Sequence[ActiveMethod | Mapping[str, Any]],
) -> str | None:
    """Compute a deterministic content-addressed snapshot ID from an active method set."""
    pairs: list[tuple[str, str]] = []
    for item in active_methods:
        if isinstance(item, ActiveMethod):
            pairs.append((item.method_id, item.revision_sha256))
        elif isinstance(item, Mapping):
            mid = item.get("method_id")
            rev = item.get("revision_sha256")
            if isinstance(mid, str) and isinstance(rev, str):
                pairs.append((mid, rev))
    if not pairs:
        return None
    pairs.sort()
    return "snap-" + _sha256_json(pairs)[:24]


def skill_precedence_key(method: ActiveMethod) -> tuple[int, int, str, str]:
    """Precedence key favoring one_agent > project > company, then latest activation."""
    return (
        _SCOPE_ORDER.get(method.audience_scope, -1),
        method.activation_seq,
        method.method_id,
        method.revision_sha256,
    )


def _trigger_matches_duty(
    trigger_spec: Mapping[str, Any], duty_kind: str | None
) -> bool:
    if duty_kind is None:
        return True
    expected = trigger_spec.get("duty_kind")
    if expected is None or expected == "*":
        return True
    if isinstance(expected, str):
        return expected == duty_kind
    if isinstance(expected, list):
        return duty_kind in expected or "*" in expected
    return True


def _safe_token(value: str, *, fallback: str = "item") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    return cleaned[:48] if cleaned else fallback
