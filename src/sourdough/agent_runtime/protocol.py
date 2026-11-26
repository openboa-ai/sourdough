"""Normalized AgentRuntimeAdapter wire contract, profile pinning, context pack, and OuterToolBroker."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from time import time_ns
from typing import Any, Protocol

PROVIDER_CODEX_CLI = "codex_cli"
PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_GEMINI = "gemini"
VALID_PROVIDERS = frozenset(
    {PROVIDER_CODEX_CLI, PROVIDER_OPENAI, PROVIDER_ANTHROPIC, PROVIDER_GEMINI}
)

_FORBIDDEN_TOOL_ARG_KEYS = frozenset(
    {"agent_id", "company_id", "run_id", "fencing_token", "tier"}
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


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if (
            type(self.input_tokens) is not int
            or type(self.output_tokens) is not int
            or self.input_tokens < 0
            or self.output_tokens < 0
        ):
            raise AgentRuntimeRefused(
                "Token usage counts must be non-negative integers"
            )

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
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
    max_context_bytes: int = 16_384,
) -> ContextPack:
    """Build a bounded ContextPack without EVER evicting open_receipts or grant scope."""
    kept_history = list(history_notes)
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
        }
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(encoded) <= max_context_bytes or not kept_history:
            break
        kept_history.pop(0)
        compacted = True
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


class OuterToolBroker:
    """Trusted Outer Capability Broker mediating every model-requested tool call.

    Enforces strict allowlisting, rejects spoofed server-identity keys, bounds total
    invocations, and caches receipts by `call_id` so retries never duplicate effects.
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
    ) -> None:
        self._company_id = company_id
        self._agent_id = agent_id
        self._run_id = run_id
        self._fencing_token = fencing_token
        self._handlers = dict(handlers)
        self._max_tool_calls = max_tool_calls
        self._call_cache: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self._call_count = 0
        self._events: list[AdapterEvent] = []

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def events(self) -> tuple[AdapterEvent, ...]:
        return tuple(self._events)

    def invoke(
        self, call_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(call_id, str) or not call_id.strip():
            raise AgentRuntimeRefused("Tool call_id is required")
        if not isinstance(arguments, dict):
            raise AgentRuntimeRefused("Tool arguments must be a JSON object")
        arg_digest = sha256(
            json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        cached = self._call_cache.get(call_id)
        if cached is not None:
            prior_name, prior_digest, prior_result = cached
            if prior_name != tool_name or prior_digest != arg_digest:
                raise AgentRuntimeRefused(
                    f"Tool call_id '{call_id}' reused with conflicting arguments"
                )
            return prior_result

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

        self._events.append(
            AdapterEvent(
                kind="tool_request",
                recorded_at_ns=time_ns(),
                payload={
                    "call_id": call_id,
                    "tool": tool_name,
                    "arg_digest": arg_digest,
                },
            )
        )
        self._call_count += 1
        result = self._handlers[tool_name](arguments)
        if not isinstance(result, dict):
            raise AgentRuntimeRefused("Tool handler must return a JSON object")
        self._call_cache[call_id] = (tool_name, arg_digest, result)
        self._events.append(
            AdapterEvent(
                kind="tool_result",
                recorded_at_ns=time_ns(),
                payload={
                    "call_id": call_id,
                    "tool": tool_name,
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
