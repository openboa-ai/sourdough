"""Core connector contracts: manifests, bindings, grants, requests, responses, and adapter protocol."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import re
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
class ConnectorAdmission:
    """Service-issued immutable admission authorizing one effectful connector invocation."""

    admission_id: str
    company_id: str
    binding_id: str
    binding_revision: int
    operation: str
    request_id: str
    payload_sha256: str
    environment: ConnectorEnvironment
    endpoint_url: str
    issued_by_service: str
    actor_id: str
    status: str
    expires_at_ns: int | None
    created_at_ns: int
    consumed_at_ns: int | None


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


def _matches_json_type(value: Any, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, Mapping)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return type(value) is int
    if expected_type == "number":
        return type(value) in (int, float)
    if expected_type == "boolean":
        return type(value) is bool
    if expected_type == "null":
        return value is None
    return False


def validate_payload_against_schema(
    value: Any,
    schema: Mapping[str, Any] | None,
    *,
    path: str = "payload",
) -> None:
    """Validate a connector request payload against a pinned JSON Schema subset."""
    if not schema:
        return
    if not isinstance(schema, Mapping):
        raise ConnectorRefused(f"Invalid request schema at {path}")

    expected_type = schema.get("type")
    if isinstance(expected_type, str):
        if not _matches_json_type(value, expected_type):
            raise ConnectorRefused(
                f"Connector payload validation failed at {path}: expected type '{expected_type}'"
            )
    elif isinstance(expected_type, list) and expected_type:
        if not any(
            isinstance(t, str) and _matches_json_type(value, t) for t in expected_type
        ):
            raise ConnectorRefused(
                f"Connector payload validation failed at {path}: value does not match allowed types"
            )

    if "const" in schema and value != schema["const"]:
        raise ConnectorRefused(
            f"Connector payload validation failed at {path}: value does not match pinned constant"
        )
    if "enum" in schema:
        allowed_enum = schema["enum"]
        if not isinstance(allowed_enum, list) or value not in allowed_enum:
            raise ConnectorRefused(
                f"Connector payload validation failed at {path}: value is not in pinned enum"
            )

    if isinstance(value, str):
        min_len = schema.get("minLength")
        if type(min_len) is int and len(value) < min_len:
            raise ConnectorRefused(
                f"Connector payload validation failed at {path}: shorter than minLength {min_len}"
            )
        max_len = schema.get("maxLength")
        if type(max_len) is int and len(value) > max_len:
            raise ConnectorRefused(
                f"Connector payload validation failed at {path}: exceeds maxLength {max_len}"
            )
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                if re.search(pattern, value) is None:
                    raise ConnectorRefused(
                        f"Connector payload validation failed at {path}: does not match pinned pattern"
                    )
            except re.error as error:
                raise ConnectorRefused(
                    f"Invalid regex pattern in schema at {path}"
                ) from error

    if type(value) in (int, float):
        minimum = schema.get("minimum")
        if type(minimum) in (int, float) and value < minimum:
            raise ConnectorRefused(
                f"Connector payload validation failed at {path}: value below minimum {minimum}"
            )
        maximum = schema.get("maximum")
        if type(maximum) in (int, float) and value > maximum:
            raise ConnectorRefused(
                f"Connector payload validation failed at {path}: value exceeds maximum {maximum}"
            )
        exc_min = schema.get("exclusiveMinimum")
        if type(exc_min) in (int, float) and value <= exc_min:
            raise ConnectorRefused(
                f"Connector payload validation failed at {path}: value must be > {exc_min}"
            )
        exc_max = schema.get("exclusiveMaximum")
        if type(exc_max) in (int, float) and value >= exc_max:
            raise ConnectorRefused(
                f"Connector payload validation failed at {path}: value must be < {exc_max}"
            )

    if isinstance(value, list):
        min_items = schema.get("minItems")
        if type(min_items) is int and len(value) < min_items:
            raise ConnectorRefused(
                f"Connector payload validation failed at {path}: fewer than {min_items} items"
            )
        max_items = schema.get("maxItems")
        if type(max_items) is int and len(value) > max_items:
            raise ConnectorRefused(
                f"Connector payload validation failed at {path}: exceeds {max_items} items"
            )
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for idx, elem in enumerate(value):
                validate_payload_against_schema(
                    elem, item_schema, path=f"{path}[{idx}]"
                )

    if isinstance(value, Mapping):
        req_keys = schema.get("required")
        if isinstance(req_keys, list):
            for req_key in req_keys:
                if isinstance(req_key, str) and req_key not in value:
                    raise ConnectorRefused(
                        f"Connector payload validation failed at {path}: missing required field '{req_key}'"
                    )
        props = schema.get("properties")
        props_map = props if isinstance(props, Mapping) else {}
        addl = schema.get("additionalProperties", True)
        for key, prop_val in value.items():
            if key in props_map:
                prop_schema = props_map[key]
                if isinstance(prop_schema, Mapping):
                    validate_payload_against_schema(
                        prop_val, prop_schema, path=f"{path}.{key}"
                    )
            elif addl is False:
                raise ConnectorRefused(
                    f"Connector payload validation failed at {path}: unexpected property '{key}'"
                )
            elif isinstance(addl, Mapping):
                validate_payload_against_schema(prop_val, addl, path=f"{path}.{key}")


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
    "ConnectorAdmission",
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
    "validate_payload_against_schema",
]
