"""Normalized AgentRuntimeAdapter wire contract, profile pinning, context pack, and OuterToolBroker."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from hashlib import sha256
from pathlib import Path
from time import time_ns
from typing import Any, Literal, Protocol

ToolFlowClass = Literal[
    "neutral",
    "private_desk_read",
    "private_desk_write",
    "peer_input",
    "peer_outbound",
    "external_write_sink",
]
VALID_TOOL_FLOW_CLASSES: frozenset[str] = frozenset(
    {
        "neutral",
        "private_desk_read",
        "private_desk_write",
        "peer_input",
        "peer_outbound",
        "external_write_sink",
    }
)
EXTERNAL_ISOLATION_FLOW_CLASSES: frozenset[str] = frozenset(
    {"peer_input", "peer_outbound", "external_write_sink"}
)
LEGACY_EXTERNAL_ISOLATION_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "read_messages",
        "read_inbox",
        "discover_handoffs",
        "read_shared_asset",
        "append_message",
        "request_peer_response",
        "respond_peer_request",
        "offer_handoff",
        "decline_handoff",
        "submit_company_paper_order",
        "cancel_company_paper_order",
        "submit_company_live_order",
        "propose_live_migration",
        "open_agent_sandbox",
        "submit_agent_sandbox_order",
        "cancel_agent_sandbox_order",
    }
)

PROVIDER_CODEX_CLI = "codex_cli"
PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_GEMINI = "gemini"
VALID_PROVIDERS = frozenset(
    {PROVIDER_CODEX_CLI, PROVIDER_OPENAI, PROVIDER_ANTHROPIC, PROVIDER_GEMINI}
)

_FORBIDDEN_TOOL_ARG_KEYS = frozenset(
    {
        "agent_id",
        "company_id",
        "run_id",
        "fencing_token",
        "tier",
        "actor_id",
        "created_by_agent_id",
    }
)
_SECRET_HANDLE_RE = re.compile(
    r"^(?P<scheme>keychain|envref):(?P<target>[A-Za-z0-9][A-Za-z0-9._:/-]{2,120})$"
)
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class AgentRuntimeRefused(Exception):
    """Agent runtime configuration, tool request, or secret resolution was refused."""


class AgentProfileMismatch(AgentRuntimeRefused):
    """Requested runtime/provider does not match the Agent's immutable execution profile."""


class AgentBudgetExceeded(AgentRuntimeRefused):
    """Agent shift exceeded its authorized compute budget ceiling."""


class AgentTransportInterrupted(Exception):
    """Provider HTTP/CLI transport failed mid-shift; already-committed receipts remain intact."""


@dataclass(frozen=True)
class MaskedSecret:
    """In-memory single API secret that masks itself in repr/str."""

    value: str
    handle_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise AgentRuntimeRefused("Resolved secret value is empty")

    def __repr__(self) -> str:
        return f"MaskedSecret(handle_ref={self.handle_ref!r}, value='***')"

    def __str__(self) -> str:
        return self.__repr__()


class SecretHandleResolver(Protocol):
    def resolve_secret(self, handle_ref: str) -> MaskedSecret: ...


def _parse_secret_handle(handle_ref: str) -> tuple[str, str]:
    if not isinstance(handle_ref, str):
        raise AgentRuntimeRefused("credential_handle_ref must be a string")
    match = _SECRET_HANDLE_RE.fullmatch(handle_ref)
    if match is None:
        raise AgentRuntimeRefused(
            "credential_handle_ref must be an opaque 'envref:...' or 'keychain:...' handle"
        )
    return match.group("scheme"), match.group("target")


class EnvSecretResolver:
    """Resolve `envref:<VAR>` from host environment or `keychain:<svc>/<acct>` via macOS Keychain."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def resolve_secret(self, handle_ref: str) -> MaskedSecret:
        scheme, target = _parse_secret_handle(handle_ref)
        if scheme == "envref":
            var_name = re.sub(r"[^A-Za-z0-9_]", "_", target).upper()
            raw = self._environ.get(var_name, "").strip()
            if not raw:
                raise AgentRuntimeRefused(
                    f"Host environment variable for '{handle_ref}' is not set"
                )
            return MaskedSecret(value=raw, handle_ref=handle_ref)
        if "/" not in target:
            raise AgentRuntimeRefused(
                "Keychain handle must use 'keychain:<service>/<account>' format"
            )
        service, account = target.split("/", 1)
        try:
            proc = subprocess.run(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s",
                    service,
                    "-a",
                    account,
                    "-w",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AgentRuntimeRefused("macOS Keychain lookup failed") from error
        if proc.returncode != 0 or not proc.stdout.strip():
            raise AgentRuntimeRefused(
                f"macOS Keychain secret unavailable for '{service}'"
            )
        return MaskedSecret(value=proc.stdout.strip(), handle_ref=handle_ref)


class InMemorySecretResolver:
    """Test-only secret resolver mapping opaque handles to synthetic values."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = dict(mapping)

    def resolve_secret(self, handle_ref: str) -> MaskedSecret:
        _parse_secret_handle(handle_ref)
        val = self._mapping.get(handle_ref)
        if not val:
            raise AgentRuntimeRefused(f"Secret handle '{handle_ref}' is not registered")
        return MaskedSecret(value=val, handle_ref=handle_ref)


@dataclass(frozen=True)
class AdapterManifest:
    provider: str
    adapter_revision: str
    transport: str  # "local_cli_uds" | "https_json"
    supports_tool_mediation: bool
    supports_usage_accounting: bool
    supports_sandbox_desk: bool


@dataclass(frozen=True)
class AgentProfileBinding:
    """Immutable execution profile pinned to one Agent at hiring/founding."""

    provider: str
    model: str
    revision: str
    sandbox_backend: str = "local-macos"
    credential_handle_ref: str | None = None

    def __post_init__(self) -> None:
        if self.provider not in VALID_PROVIDERS:
            raise AgentRuntimeRefused(f"Unsupported provider: {self.provider}")
        for value, label in (
            (self.model, "model"),
            (self.revision, "revision"),
            (self.sandbox_backend, "sandbox_backend"),
        ):
            if not isinstance(value, str) or _SAFE_SEGMENT_RE.fullmatch(value) is None:
                raise AgentRuntimeRefused(f"Invalid {label} in AgentProfileBinding")
        if self.provider != PROVIDER_CODEX_CLI:
            if self.credential_handle_ref is None:
                raise AgentRuntimeRefused(
                    f"Provider '{self.provider}' requires an opaque credential_handle_ref"
                )
            _parse_secret_handle(self.credential_handle_ref)
        elif self.credential_handle_ref is not None:
            _parse_secret_handle(self.credential_handle_ref)

    def profile_id(self) -> str:
        payload = json.dumps(
            {
                "provider": self.provider,
                "model": self.model,
                "revision": self.revision,
                "sandbox_backend": self.sandbox_backend,
                "credential_handle_ref": self.credential_handle_ref,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = sha256(payload).hexdigest()[:12]
        return f"adapter-v1:{self.provider}:{self.model}:{digest}"

    def verify_matches_agent(self, persisted_profile: str) -> None:
        expected = self.profile_id()
        if persisted_profile != expected:
            raise AgentProfileMismatch(
                f"Agent execution_profile '{persisted_profile}' does not match bound profile '{expected}'"
            )


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    flow_class: ToolFlowClass = "neutral"

    def __post_init__(self) -> None:
        if self.flow_class not in VALID_TOOL_FLOW_CLASSES:
            raise AgentRuntimeRefused(
                f"Unsupported ToolSpec.flow_class '{self.flow_class}' for tool '{self.name}'"
            )

    def requires_desk_isolation(self) -> bool:
        """Return True when exposing this tool requires isolating the shift from private Desk files."""
        return (
            self.flow_class in EXTERNAL_ISOLATION_FLOW_CLASSES
            or self.name in LEGACY_EXTERNAL_ISOLATION_TOOL_NAMES
        )


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0

    def __post_init__(self) -> None:
        if (
            type(self.input_tokens) is not int
            or type(self.output_tokens) is not int
            or type(self.cached_input_tokens) is not int
            or self.input_tokens < 0
            or self.output_tokens < 0
            or self.cached_input_tokens < 0
        ):
            raise AgentRuntimeRefused(
                "Token usage counts must be non-negative integers"
            )

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
        )


@dataclass(frozen=True)
class AdapterEvent:
    kind: str  # "started" | "output" | "tool_request" | "tool_result" | "usage" | "terminal"
    recorded_at_ns: int
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextPack:
    """Versioned shift context pack preserving active grants and open-effect receipts."""

    company_id: str
    agent_id: str
    run_id: str
    fencing_token: int
    grant_scope: str
    mandate_summary: str
    open_receipts: tuple[dict[str, Any], ...]
    history_notes: tuple[str, ...]
    digest: str
    compacted: bool
    desk_manifest: tuple[dict[str, Any], ...] = ()
    active_methods: tuple[dict[str, Any], ...] = ()
    active_snapshot_id: str | None = None
    recent_episodes: tuple[dict[str, Any], ...] = ()


def skill_slug(method_id: str) -> str:
    """Return a safe case-normalized directory slug for an active method/skill ID."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", method_id.strip()).strip(".-").lower()
    return slug or "skill"


_skill_slug = skill_slug


def assign_skill_slugs(method_ids: Sequence[str]) -> dict[str, str]:
    """Map distinct method_ids to collision-free, case-folded directory slugs."""
    unique_ids = sorted({str(mid).strip() for mid in method_ids if str(mid).strip()})
    groups: dict[str, list[str]] = {}
    for mid in unique_ids:
        groups.setdefault(skill_slug(mid), []).append(mid)

    assigned: dict[str, str] = {}
    used: set[str] = set()
    for base, mids in sorted(groups.items()):
        if len(mids) == 1 and base not in used:
            assigned[mids[0]] = base
            used.add(base)
            continue
        for mid in mids:
            digest = sha256(mid.encode("utf-8")).hexdigest()
            width = 8
            candidate = f"{base}-{digest[:width]}"
            while candidate in used:
                width += 4
                candidate = f"{base}-{digest[:width]}"
            assigned[mid] = candidate
            used.add(candidate)
    return assigned


def summarize_when_to_use(raw: Mapping[str, Any]) -> str:
    """Format a bounded single-line trigger summary for Skill Index projections."""
    explicit = raw.get("when_to_use")
    if isinstance(explicit, str) and explicit.strip():
        return " ".join(explicit.strip().split())[:160]
    trig = raw.get("trigger_spec")
    if isinstance(trig, Mapping) and trig:
        parts: list[str] = []
        for k in sorted(str(key) for key in trig.keys()):
            v = trig[k]
            if isinstance(v, (str, int, float, bool)):
                parts.append(f"{k}={v}")
            elif isinstance(v, (list, tuple)):
                parts.append(f"{k}={','.join(str(x) for x in v[:4])}")
            else:
                parts.append(
                    f"{k}={json.dumps(v, sort_keys=True, separators=(',', ':'))}"
                )
        summary = "; ".join(parts)
        if summary:
            return summary[:160]
    for fallback_key in ("title", "description"):
        val = raw.get(fallback_key)
        if isinstance(val, str) and val.strip():
            return " ".join(val.strip().split())[:160]
    return ""


_summarize_when_to_use = summarize_when_to_use


def project_skill_index_entry(item: Any) -> dict[str, Any]:
    """Project an ActiveMethod or mapping into a lean Skill Index entry without body_text."""
    if isinstance(item, dict):
        raw: Mapping[str, Any] = item
    elif hasattr(item, "to_index_dict") and callable(item.to_index_dict):
        raw = dict(item.to_index_dict())
    elif hasattr(item, "to_dict") and callable(item.to_dict):
        raw = dict(item.to_dict())
    elif is_dataclass(item) and not isinstance(item, type):
        raw = asdict(item)
    elif isinstance(item, Mapping):
        raw = dict(item)
    else:
        raise AgentRuntimeRefused("Unsupported active_methods entry type")

    method_id = str(raw.get("method_id") or "").strip()
    if not method_id:
        raise AgentRuntimeRefused(
            "active_methods entry must include nonempty method_id"
        )
    raw_skill_name = raw.get("skill_name")
    skill_name = (
        str(raw_skill_name).strip()
        if isinstance(raw_skill_name, str) and raw_skill_name.strip()
        else _skill_slug(method_id)
    )
    raw_item_kind = raw.get("item_kind") or raw.get("target_kind") or "work_method"
    item_kind = str(raw_item_kind).strip() or "work_method"
    raw_skill_path = raw.get("skill_path")
    skill_path = (
        str(raw_skill_path).strip()
        if isinstance(raw_skill_path, str) and raw_skill_path.strip()
        else f"skills/{skill_name}/SKILL.md"
    )
    revision_sha256 = str(raw.get("revision_sha256") or "").strip()
    audience_scope = str(raw.get("audience_scope") or "").strip()
    when_to_use = _summarize_when_to_use(raw)
    return {
        "method_id": method_id,
        "skill_name": skill_name,
        "item_kind": item_kind,
        "when_to_use": when_to_use,
        "skill_path": skill_path,
        "revision_sha256": revision_sha256,
        "audience_scope": audience_scope,
    }


def project_skill_index(
    active_methods: tuple[Any, ...] | list[Any],
) -> list[dict[str, Any]]:
    """Return a deterministically sorted lean Skill Index list for ContextPack v2."""
    entries = [project_skill_index_entry(item) for item in active_methods]
    slug_map = assign_skill_slugs([e["method_id"] for e in entries])
    for entry in entries:
        disambiguated = slug_map.get(entry["method_id"])
        if disambiguated and disambiguated != entry["skill_name"]:
            default_slug = _skill_slug(entry["method_id"])
            if (
                entry["skill_name"] == default_slug
                or entry["skill_name"].lower() == default_slug
            ):
                entry["skill_name"] = disambiguated
                entry["skill_path"] = f"skills/{disambiguated}/SKILL.md"
    entries.sort(
        key=lambda e: (e["method_id"], e["revision_sha256"], e["audience_scope"])
    )
    return entries


def _normalize_active_methods(
    active_methods: tuple[Any, ...] | list[Any],
) -> list[dict[str, Any]]:
    return project_skill_index(active_methods)


def hydrate_shift_context(
    *,
    company_id: str,
    agent_id: str,
    run_id: str,
    fencing_token: int,
    grant_scope: str,
    mandate_summary: str,
    open_receipts: tuple[dict[str, Any], ...] = (),
    history_notes: tuple[str, ...] = (),
    desk_manifest: tuple[dict[str, Any], ...] = (),
    active_methods: tuple[Any, ...] = (),
    active_snapshot_id: str | None = None,
    recent_episodes: tuple[dict[str, Any], ...] = (),
    max_context_bytes: int = 16_384,
) -> ContextPack:
    """Build a bounded ContextPack without EVER evicting open_receipts, grants, or active methods."""
    kept_history = list(history_notes)
    kept_episodes = list(recent_episodes)
    normalized_methods = _normalize_active_methods(active_methods)
    compacted = False
    while True:
        body = {
            "company_id": company_id,
            "agent_id": agent_id,
            "run_id": run_id,
            "fencing_token": fencing_token,
            "grant_scope": grant_scope,
            "mandate_summary": mandate_summary,
            "open_receipts": list(open_receipts),
            "history_notes": kept_history,
            "desk_manifest": list(desk_manifest),
            "active_methods": normalized_methods,
            "active_snapshot_id": active_snapshot_id,
            "recent_episodes": kept_episodes,
        }
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(encoded) <= max_context_bytes:
            break
        if kept_history:
            kept_history.pop(0)
            compacted = True
        elif kept_episodes:
            kept_episodes.pop()
            compacted = True
        else:
            break
    digest = sha256(encoded).hexdigest()
    return ContextPack(
        company_id=company_id,
        agent_id=agent_id,
        run_id=run_id,
        fencing_token=fencing_token,
        grant_scope=grant_scope,
        mandate_summary=mandate_summary,
        open_receipts=tuple(open_receipts),
        history_notes=tuple(kept_history),
        digest=digest,
        compacted=compacted,
        desk_manifest=tuple(desk_manifest),
        active_methods=tuple(normalized_methods),
        active_snapshot_id=active_snapshot_id,
        recent_episodes=tuple(kept_episodes),
    )


@dataclass(frozen=True)
class ShiftRequest:
    company_id: str
    agent_id: str
    run_id: str
    turn_id: str
    fencing_token: int
    instruction: str
    context: ContextPack
    tools: tuple[ToolSpec, ...]
    budget_ceiling_minor_units: int
    max_tool_calls: int = 8
    timeout_seconds: float = 30.0
    desk_path: Path | None = None
    store_path: Path | None = None

    def __post_init__(self) -> None:
        sorted_tools = tuple(sorted(self.tools, key=lambda spec: spec.name))
        object.__setattr__(self, "tools", sorted_tools)


@dataclass(frozen=True)
class ShiftResult:
    provider: str
    model: str
    run_id: str
    turn_id: str
    output_text: str
    usage: TokenUsage
    tool_calls_executed: int
    events: tuple[AdapterEvent, ...]


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


def _is_fatal_tool_exception(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            SystemExit,
            KeyboardInterrupt,
            AgentRuntimeRefused,
            AgentTransportInterrupted,
        ),
    ):
        return True
    from ..company import FoundingRefused
    from ..runtime.registry import RuntimeRefused
    from ..runtime.work_kernel import RunFenced
    from ..storage.checkpoint import CheckpointRefused
    from ..storage.db import StorageRefused
    from ..writer_lease import WriterRefused

    # Check RunFenced BEFORE WorkKernelRefused because RunFenced subclasses WorkKernelRefused.
    return isinstance(
        exc,
        (
            RunFenced,
            StorageRefused,
            FoundingRefused,
            WriterRefused,
            CheckpointRefused,
            RuntimeRefused,
        ),
    )


def _is_recoverable_tool_exception(
    exc: BaseException,
    extra_recoverable: tuple[type[BaseException], ...],
) -> bool:
    if _is_fatal_tool_exception(exc):
        return False
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return True
    if extra_recoverable and isinstance(exc, extra_recoverable):
        return True
    from ..board_control import BoardControlRefused
    from ..duty_run import DutyRefused
    from ..episode_index import EpisodeIndexRefused
    from ..general_ledger import JournalRefused
    from ..memory import MemoryRefused
    from ..method_learning import MethodRefused
    from ..runtime.domains import DomainActionRefused
    from ..runtime.work_kernel import WorkKernelRefused

    return isinstance(
        exc,
        (
            WorkKernelRefused,
            DutyRefused,
            BoardControlRefused,
            DomainActionRefused,
            JournalRefused,
            MemoryRefused,
            EpisodeIndexRefused,
            MethodRefused,
        ),
    )


class OuterToolBroker:
    """Trusted Outer Capability Broker mediating every model-requested tool call.

    Enforces strict allowlisting, rejects spoofed server-identity keys, bounds total
    invocations, prevents trajectory loops, classifies recoverable domain refusals vs
    fatal aborts, and caches receipts by `call_id` so retries never duplicate effects.
    """

    def __init__(
        self,
        *,
        company_id: str,
        agent_id: str,
        run_id: str,
        fencing_token: int,
        handlers: dict[str, ToolHandler],
        max_tool_calls: int = 8,
        fencing_check: Callable[[], None] | None = None,
        receipt_recorder: Callable[[dict[str, Any]], None] | None = None,
        trace_recorder: Callable[[dict[str, Any]], None] | None = None,
        offload_handler: Callable[[int, str, dict[str, Any]], dict[str, Any]]
        | None = None,
        recoverable_exceptions: tuple[type[BaseException], ...] = (),
        max_repeated_identical_calls: int = 3,
        clock: Callable[[], int] = time_ns,
    ) -> None:
        self._company_id = company_id
        self._agent_id = agent_id
        self._run_id = run_id
        self._fencing_token = fencing_token
        self._handlers = dict(handlers)
        self._max_tool_calls = max_tool_calls
        self._fencing_check = fencing_check
        self._receipt_recorder = receipt_recorder
        self._trace_recorder = trace_recorder
        self._offload_handler = offload_handler
        self._recoverable_exceptions = tuple(recoverable_exceptions)
        self._max_repeated_identical_calls = max(1, int(max_repeated_identical_calls))
        self._clock = clock
        self._call_cache: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self._identical_call_counts: dict[tuple[str, str], int] = {}
        self._call_count = 0
        self._accrued_usage = TokenUsage(0, 0)
        self._events: list[AdapterEvent] = []

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def accrued_usage(self) -> TokenUsage:
        return self._accrued_usage

    def record_usage(self, delta: TokenUsage) -> None:
        self._accrued_usage = self._accrued_usage + delta

    @property
    def events(self) -> tuple[AdapterEvent, ...]:
        return tuple(self._events)

    def _emit_records(
        self,
        *,
        seq: int,
        call_id: str,
        tool_name: str,
        arg_digest: str,
        arg_keys: list[str],
        status: str,
        result: dict[str, Any],
        error_type: str | None = None,
        error_message: str | None = None,
        best_effort: bool = False,
    ) -> None:
        now_ns = max(1, int(self._clock()))
        result_json = json.dumps(result, sort_keys=True, separators=(",", ":"))
        result_digest = sha256(result_json.encode("utf-8")).hexdigest()
        offloaded_path = (
            str(result["log_path"])
            if result.get("offloaded") is True
            and isinstance(result.get("log_path"), str)
            else None
        )
        if self._receipt_recorder is not None:
            receipt_payload = {
                "company_id": self._company_id,
                "run_id": self._run_id,
                "agent_id": self._agent_id,
                "call_id": call_id,
                "tool_name": tool_name,
                "arg_digest": arg_digest,
                "status": status,
                "result_digest": result_digest,
                "result_json": result_json,
                "error_type": error_type,
                "error_message": error_message,
                "offloaded_log_path": offloaded_path,
                "recorded_at_ns": now_ns,
            }
            if best_effort:
                try:
                    self._receipt_recorder(receipt_payload)
                except Exception:
                    pass
            else:
                self._receipt_recorder(receipt_payload)
        if self._trace_recorder is not None:
            trace_payload = {
                "seq": seq,
                "recorded_at_ns": now_ns,
                "company_id": self._company_id,
                "agent_id": self._agent_id,
                "run_id": self._run_id,
                "fencing_token": self._fencing_token,
                "call_id": call_id,
                "tool": tool_name,
                "status": status,
                "arg_keys": arg_keys,
                "arg_digest": arg_digest,
                "result_sha256": result_digest,
                "error_kind": error_type,
                "offloaded_log_path": offloaded_path,
            }
            if best_effort:
                try:
                    self._trace_recorder(trace_payload)
                except Exception:
                    pass
            else:
                self._trace_recorder(trace_payload)

    def invoke(
        self, call_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        # Step A1: Live fencing check BEFORE cache replay so fenced runs cannot replay.
        if self._fencing_check is not None:
            self._fencing_check()

        # Step A2: Structural argument checks (always fatal).
        if not isinstance(call_id, str) or not call_id.strip():
            raise AgentRuntimeRefused("Tool call_id is required")
        if not isinstance(arguments, dict):
            raise AgentRuntimeRefused("Tool arguments must be a JSON object")
        arg_digest = sha256(
            json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        arg_keys = sorted(str(k) for k in arguments.keys())

        # Step A3: Idempotent replay cache check.
        cached = self._call_cache.get(call_id)
        if cached is not None:
            prior_name, prior_digest, prior_result = cached
            if prior_name != tool_name or prior_digest != arg_digest:
                raise AgentRuntimeRefused(
                    f"Tool call_id '{call_id}' reused with conflicting arguments"
                )
            return prior_result

        # Step A4: Tool cap, allowlist, and identity-spoofing checks (always fatal).
        if self._call_count >= self._max_tool_calls:
            raise AgentRuntimeRefused("Shift exceeded maximum permitted tool calls")
        if tool_name not in self._handlers:
            raise AgentRuntimeRefused(
                f"Tool '{tool_name}' is not admitted for this shift"
            )
        spoofed = _FORBIDDEN_TOOL_ARG_KEYS.intersection(arguments.keys())
        if spoofed:
            raise AgentRuntimeRefused(
                f"Server-bound identity fields cannot be supplied in tool arguments: {sorted(spoofed)}"
            )

        # Step B: Trajectory loop-breaker on repeated identical (tool_name, arg_digest) calls.
        loop_key = (tool_name, arg_digest)
        prior_identical = self._identical_call_counts.get(loop_key, 0)
        if prior_identical >= self._max_repeated_identical_calls + 1:
            err_msg = (
                f"Trajectory loop-breaker aborted shift after repeated identical "
                f"calls to '{tool_name}'"
            )
            self._emit_records(
                seq=self._call_count + 1,
                call_id=call_id,
                tool_name=tool_name,
                arg_digest=arg_digest,
                arg_keys=arg_keys,
                status="fatal",
                result={
                    "ok": False,
                    "error": err_msg,
                    "error_type": "AgentRuntimeRefused",
                },
                error_type="AgentRuntimeRefused",
                error_message=err_msg,
                best_effort=True,
            )
            raise AgentRuntimeRefused(err_msg)

        now_ns = max(1, int(self._clock()))
        self._events.append(
            AdapterEvent(
                kind="tool_request",
                recorded_at_ns=now_ns,
                payload={
                    "call_id": call_id,
                    "tool": tool_name,
                    "arg_digest": arg_digest,
                },
            )
        )
        self._call_count += 1
        seq = self._call_count
        self._identical_call_counts[loop_key] = prior_identical + 1

        if prior_identical >= self._max_repeated_identical_calls:
            refusal_result: dict[str, Any] = {
                "ok": False,
                "error": (
                    f"Trajectory loop detected for tool '{tool_name}' with identical "
                    "arguments; choose a different action or conclude."
                ),
                "error_type": "TrajectoryLoopDetected",
            }
            self._call_cache[call_id] = (tool_name, arg_digest, refusal_result)
            self._emit_records(
                seq=seq,
                call_id=call_id,
                tool_name=tool_name,
                arg_digest=arg_digest,
                arg_keys=arg_keys,
                status="refused",
                result=refusal_result,
                error_type="TrajectoryLoopDetected",
                error_message=refusal_result["error"],
            )
            self._events.append(
                AdapterEvent(
                    kind="tool_result",
                    recorded_at_ns=max(1, int(self._clock())),
                    payload={
                        "call_id": call_id,
                        "tool": tool_name,
                        "ok": False,
                        "error_type": "TrajectoryLoopDetected",
                        "result_keys": sorted(refusal_result.keys()),
                    },
                )
            )
            return refusal_result

        # Step C: Handler execution and fatal vs recoverable classification.
        try:
            raw_result = self._handlers[tool_name](arguments)
        except BaseException as exc:
            if _is_fatal_tool_exception(exc):
                self._emit_records(
                    seq=seq,
                    call_id=call_id,
                    tool_name=tool_name,
                    arg_digest=arg_digest,
                    arg_keys=arg_keys,
                    status="fatal",
                    result={
                        "ok": False,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:1000],
                    best_effort=True,
                )
                raise
            if _is_recoverable_tool_exception(exc, self._recoverable_exceptions):
                recoverable_result: dict[str, Any] = {
                    "ok": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
                self._call_cache[call_id] = (tool_name, arg_digest, recoverable_result)
                self._emit_records(
                    seq=seq,
                    call_id=call_id,
                    tool_name=tool_name,
                    arg_digest=arg_digest,
                    arg_keys=arg_keys,
                    status="refused",
                    result=recoverable_result,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:1000],
                )
                self._events.append(
                    AdapterEvent(
                        kind="tool_result",
                        recorded_at_ns=max(1, int(self._clock())),
                        payload={
                            "call_id": call_id,
                            "tool": tool_name,
                            "ok": False,
                            "error_type": type(exc).__name__,
                            "result_keys": sorted(recoverable_result.keys()),
                        },
                    )
                )
                return recoverable_result
            self._emit_records(
                seq=seq,
                call_id=call_id,
                tool_name=tool_name,
                arg_digest=arg_digest,
                arg_keys=arg_keys,
                status="error",
                result={
                    "ok": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
                error_type=type(exc).__name__,
                error_message=str(exc)[:1000],
                best_effort=True,
            )
            raise

        if not isinstance(raw_result, dict):
            raise AgentRuntimeRefused("Tool handler must return a JSON object")
        result = (
            self._offload_handler(seq, tool_name, raw_result)
            if self._offload_handler is not None
            else raw_result
        )
        if not isinstance(result, dict):
            raise AgentRuntimeRefused("Tool offload handler must return a JSON object")
        self._call_cache[call_id] = (tool_name, arg_digest, result)
        self._emit_records(
            seq=seq,
            call_id=call_id,
            tool_name=tool_name,
            arg_digest=arg_digest,
            arg_keys=arg_keys,
            status="ok",
            result=result,
        )
        self._events.append(
            AdapterEvent(
                kind="tool_result",
                recorded_at_ns=max(1, int(self._clock())),
                payload={
                    "call_id": call_id,
                    "tool": tool_name,
                    "ok": result.get("ok", True),
                    "result_keys": sorted(result.keys()),
                },
            )
        )
        return result


class AgentRuntimeAdapter(Protocol):
    @property
    def manifest(self) -> AdapterManifest: ...

    def run_shift(
        self,
        profile: AgentProfileBinding,
        request: ShiftRequest,
        broker: OuterToolBroker,
    ) -> ShiftResult: ...
