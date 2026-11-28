"""Core connector contracts: manifests, bindings, grants, requests, responses, and adapter protocol."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ..secret_vault import MaskedSecret, MaskedSecretBundle

ConnectorEnvironment = Literal["paper", "external_test", "live", "internal"]
OperationMode = Literal["read", "write", "effectful"]
ConnectorResponseStatus = Literal["observed", "rejected", "unknown"]

VALID_CONNECTOR_ENVIRONMENTS: frozenset[str] = frozenset(
    {"paper", "external_test", "live", "internal"}
)
VALID_OPERATION_MODES: frozenset[str] = frozenset({"read", "write", "effectful"})
VALID_RESPONSE_STATUSES: frozenset[str] = frozenset({"observed", "rejected", "unknown"})


class ConnectorRefused(Exception):
    """Connector registration, authorization, policy check, or dispatch was refused."""


class ConnectorUnauthorized(ConnectorRefused):
    """Actor lacks an active grant, operation scope, admission, or taint clearance."""


class ConnectorEnvironmentMismatch(ConnectorRefused):
    """Connector environment, destination host, or credential scope mismatched."""


class ConnectorRateLimited(ConnectorRefused):
    """Connector invocation exceeded its sliding-window rate limit."""


class ConnectorBudgetExceeded(ConnectorRefused):
    """Connector invocation exceeded the binding or grant cost ceiling."""


class ConnectorIndeterminate(Exception):
    """Connector transport reached an unknown/indeterminate outcome requiring reconciliation."""


@dataclass(frozen=True)
class OperationSpec:
    """Versioned schema and side-effect classification for one connector operation."""

    name: str
    mode: OperationMode
    request_schema: Mapping[str, Any] = field(default_factory=dict)
    supports_lookup: bool = False
    idempotent: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ConnectorRefused("OperationSpec.name must be a nonempty string")
        if self.mode not in VALID_OPERATION_MODES:
            raise ConnectorRefused(f"Unsupported OperationSpec.mode: {self.mode}")
        if not isinstance(self.request_schema, Mapping):
            raise ConnectorRefused("OperationSpec.request_schema must be a mapping")
        object.__setattr__(self, "request_schema", dict(self.request_schema))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mode": self.mode,
            "request_schema": dict(self.request_schema),
            "supports_lookup": bool(self.supports_lookup),
            "idempotent": bool(self.idempotent),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OperationSpec:
        return cls(
            name=str(data.get("name", "")),
            mode=str(data.get("mode", "")),  # type: ignore[arg-type]
            request_schema=data.get("request_schema") or {},
            supports_lookup=bool(data.get("supports_lookup", False)),
            idempotent=bool(data.get("idempotent", False)),
        )


@dataclass(frozen=True)
class ConnectorManifest:
    """Static capability and credential-shape declaration for a ConnectorAdapter."""

    connector_kind: str
    adapter_revision: str
    operations: tuple[OperationSpec, ...]
    supports_lookup: bool = False
    seals_absent: bool = False
    requires_credential: bool = True
    credential_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.connector_kind, str) or not self.connector_kind.strip():
            raise ConnectorRefused("connector_kind must be nonempty")
        if (
            not isinstance(self.adapter_revision, str)
            or not self.adapter_revision.strip()
        ):
            raise ConnectorRefused("adapter_revision must be nonempty")
        if not self.operations:
            raise ConnectorRefused("ConnectorManifest requires at least one operation")
        names = [op.name for op in self.operations]
        if len(set(names)) != len(names):
            raise ConnectorRefused("Duplicate operation name in ConnectorManifest")

    def operation(self, name: str) -> OperationSpec | None:
        for op in self.operations:
            if op.name == name:
                return op
        return None


@dataclass(frozen=True)
class ConnectorBinding:
    """Authoritative SQLite-persisted connector binding pinned to one revision."""

    binding_id: str
    revision: int
    company_id: str
    connector_kind: str
    environment: ConnectorEnvironment
    endpoint_url: str
    allowed_hosts: tuple[str, ...]
    operations: tuple[OperationSpec, ...]
    credential_handle_ref: str | None
    recovery_capabilities: Mapping[str, Any]
    status: str
    max_requests_per_window: int
    window_seconds: int
    budget_ceiling_minor_units: int
    registered_by_actor_id: str
    created_at_ns: int
    updated_at_ns: int

    def operation(self, name: str) -> OperationSpec | None:
        for op in self.operations:
            if op.name == name:
                return op
        return None

    def safe_dict(self) -> dict[str, Any]:
        """Serialize binding metadata with strictly opaque credential handle only."""
        return {
            "binding_id": self.binding_id,
            "revision": self.revision,
            "company_id": self.company_id,
            "connector_kind": self.connector_kind,
            "environment": self.environment,
            "endpoint_url": self.endpoint_url,
            "allowed_hosts": list(self.allowed_hosts),
            "operations": [op.to_dict() for op in self.operations],
            "credential_handle_present": self.credential_handle_ref is not None,
            "recovery_capabilities": dict(self.recovery_capabilities),
            "status": self.status,
            "max_requests_per_window": self.max_requests_per_window,
            "window_seconds": self.window_seconds,
            "budget_ceiling_minor_units": self.budget_ceiling_minor_units,
            "created_at_ns": self.created_at_ns,
            "updated_at_ns": self.updated_at_ns,
        }


@dataclass(frozen=True)
class ConnectorGrant:
    """Explicit actor grant authorizing specific operations on one pinned binding revision."""

    grant_binding_id: str
    company_id: str
    agent_id: str
    standing_grant_id: str
    binding_id: str
    binding_revision: int
    allowed_operations: tuple[str, ...]
    max_cost_minor_units: int
    status: str
    granted_by_actor_id: str
    created_at_ns: int
    revoked_at_ns: int | None


@dataclass(frozen=True)
class ConnectorRequest:
    """Validated, host-assembled connector invocation envelope."""

    company_id: str
    binding_id: str
    binding_revision: int
    operation: str
    request_id: str
    idempotency_key: str
    payload: Mapping[str, Any]
    environment: ConnectorEnvironment
    endpoint_url: str
    allowed_hosts: tuple[str, ...]
    admission_id: str | None = None


@dataclass(frozen=True)
class ConnectorResponse:
    """Observed, rejected, or indeterminate outcome from a connector adapter."""

    status: ConnectorResponseStatus
    payload: dict[str, Any]
    source_ref: str | None = None
    indeterminate: bool = False
    cost_minor_units: int = 0
    usage: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.status not in VALID_RESPONSE_STATUSES:
            raise ConnectorRefused(
                f"Unsupported ConnectorResponse.status: {self.status}"
            )
        if not isinstance(self.payload, dict):
            raise ConnectorRefused("ConnectorResponse.payload must be a dict")
        if type(self.cost_minor_units) is not int or self.cost_minor_units < 0:
            raise ConnectorRefused(
                "ConnectorResponse.cost_minor_units must be a non-negative integer"
            )
        if self.status == "unknown" and not self.indeterminate:
            object.__setattr__(self, "indeterminate", True)


class ConnectorAdapter(Protocol):
    """Outer-host adapter executing scoped operations with memory-only secrets."""

    @property
    def manifest(self) -> ConnectorManifest: ...

    def execute(
        self,
        request: ConnectorRequest,
        *,
        secret: MaskedSecret | MaskedSecretBundle | None,
        timeout_seconds: float,
    ) -> ConnectorResponse: ...

    def lookup(
        self,
        request: ConnectorRequest,
        *,
        secret: MaskedSecret | MaskedSecretBundle | None,
        timeout_seconds: float,
    ) -> ConnectorResponse: ...


__all__ = [
    "ConnectorAdapter",
    "ConnectorBinding",
    "ConnectorBudgetExceeded",
    "ConnectorEnvironment",
    "ConnectorEnvironmentMismatch",
    "ConnectorGrant",
    "ConnectorIndeterminate",
    "ConnectorManifest",
    "ConnectorRateLimited",
    "ConnectorRefused",
    "ConnectorRequest",
    "ConnectorResponse",
    "ConnectorResponseStatus",
    "ConnectorUnauthorized",
    "OperationMode",
    "OperationSpec",
    "VALID_CONNECTOR_ENVIRONMENTS",
    "VALID_OPERATION_MODES",
    "VALID_RESPONSE_STATUSES",
]
