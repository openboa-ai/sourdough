"""Authoritative SQLite connector binding/grant registry and in-process adapter registry."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import urllib.parse
from collections.abc import Mapping, Sequence
from time import time_ns
from typing import Any
from uuid import uuid4

from ..secret_vault import (
    CredentialRefused,
    assert_handle_environment_isolation,
)
from ..storage.db import write_uow
from ..storage.kernel import append_audit_event
from ..writer_lease import WriterLease
from .protocol import (
    VALID_CONNECTOR_ENVIRONMENTS,
    ConnectorAdapter,
    ConnectorBinding,
    ConnectorEnvironment,
    ConnectorEnvironmentMismatch,
    ConnectorGrant,
    ConnectorRefused,
    ConnectorUnauthorized,
    OperationSpec,
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,95}$")
_SAFE_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,252}$")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def validate_endpoint_and_hosts(
    endpoint_url: str,
    allowed_hosts: Sequence[str],
    *,
    allow_loopback: bool = False,
) -> tuple[str, tuple[str, ...]]:
    """Validate pinned connector endpoint URL and explicit host allowlist."""
    if not isinstance(endpoint_url, str) or not endpoint_url.strip():
        raise ConnectorRefused("endpoint_url must be a nonempty URL string")
    parsed = urllib.parse.urlparse(endpoint_url.strip())
    if parsed.username is not None or parsed.password is not None:
        raise ConnectorRefused(
            "endpoint_url must not embed credentials in URL userinfo"
        )
    if parsed.fragment:
        raise ConnectorRefused("endpoint_url must not contain URL fragments")
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ConnectorRefused("endpoint_url must contain a valid hostname")
    if parsed.scheme == "https":
        pass
    elif parsed.scheme == "http":
        if not allow_loopback or hostname not in _LOOPBACK_HOSTS:
            raise ConnectorEnvironmentMismatch(
                "Plaintext http:// connector endpoints are restricted to loopback with allow_loopback=True"
            )
    else:
        raise ConnectorRefused(
            f"Unsupported connector endpoint scheme: {parsed.scheme!r}"
        )

    if not allowed_hosts:
        raise ConnectorRefused("allowed_hosts must contain at least one pinned host")
    normalized_hosts: list[str] = []
    for raw_host in allowed_hosts:
        if not isinstance(raw_host, str) or not raw_host.strip():
            raise ConnectorRefused("allowed_hosts entries must be nonempty strings")
        host = raw_host.strip().lower()
        if "*" in host or "/" in host or ":" in host and host != "::1":
            raise ConnectorRefused(
                f"Invalid host entry in allowed_hosts (no wildcards/ports): {raw_host!r}"
            )
        if host != "::1" and _SAFE_HOST_RE.fullmatch(host) is None:
            raise ConnectorRefused(f"Invalid hostname in allowed_hosts: {raw_host!r}")
        if host not in normalized_hosts:
            normalized_hosts.append(host)
    if hostname not in normalized_hosts:
        raise ConnectorEnvironmentMismatch(
            f"Endpoint host '{hostname}' is not in pinned allowed_hosts {normalized_hosts}"
        )
    return endpoint_url.strip().rstrip("/"), tuple(sorted(normalized_hosts))


def _row_to_binding(row: tuple[Any, ...]) -> ConnectorBinding:
    ops_raw = json.loads(str(row[7]))
    return ConnectorBinding(
        binding_id=str(row[0]),
        revision=int(row[1]),
        company_id=str(row[2]),
        connector_kind=str(row[3]),
        environment=str(row[4]),  # type: ignore[arg-type]
        endpoint_url=str(row[5]),
        allowed_hosts=tuple(str(h) for h in json.loads(str(row[6]))),
        operations=tuple(OperationSpec.from_dict(item) for item in ops_raw),
        credential_handle_ref=None if row[8] is None else str(row[8]),
        recovery_capabilities=dict(json.loads(str(row[9]))),
        status=str(row[10]),
        max_requests_per_window=int(row[11]),
        window_seconds=int(row[12]),
        budget_ceiling_minor_units=int(row[13]),
        registered_by_actor_id=str(row[14]),
        created_at_ns=int(row[15]),
        updated_at_ns=int(row[16]),
    )


def _row_to_grant(row: tuple[Any, ...]) -> ConnectorGrant:
    return ConnectorGrant(
        grant_binding_id=str(row[0]),
        company_id=str(row[1]),
        agent_id=str(row[2]),
        standing_grant_id=str(row[3]),
        binding_id=str(row[4]),
        binding_revision=int(row[5]),
        allowed_operations=tuple(str(op) for op in json.loads(str(row[6]))),
        max_cost_minor_units=int(row[7]),
        status=str(row[8]),
        granted_by_actor_id=str(row[9]),
        created_at_ns=int(row[10]),
        revoked_at_ns=None if row[11] is None else int(row[11]),
    )


_BINDING_SELECT = """SELECT binding_id, revision, company_id, connector_kind,
       environment, endpoint_url, allowed_hosts_json, operations_json,
       credential_handle_ref, recovery_capabilities_json, status,
       max_requests_per_window, window_seconds, budget_ceiling_minor_units,
       registered_by_actor_id, created_at_ns, updated_at_ns
FROM connector_binding"""


def register_connector_binding(
    writer: WriterLease,
    *,
    company_id: str,
    binding_id: str,
    connector_kind: str,
    environment: ConnectorEnvironment,
    endpoint_url: str,
    allowed_hosts: Sequence[str],
    operations: Sequence[OperationSpec],
    credential_handle_ref: str | None,
    actor_id: str,
    recovery_capabilities: Mapping[str, Any] | None = None,
    max_requests_per_window: int = 60,
    window_seconds: int = 60,
    budget_ceiling_minor_units: int = 0,
    allow_loopback: bool = False,
    now_ns: int | None = None,
    tx_lock: threading.RLock | None = None,
) -> ConnectorBinding:
    """Register or revise a scoped connector binding.

    Replacement always produces a strictly higher revision and refuses environment
    or connector_kind mutation under the same binding_id. Registering a binding
    never grants any Agent permission to invoke it.
    """
    if writer.company_id != company_id:
        raise ConnectorRefused("Writer company_id does not match binding company_id")
    if not isinstance(binding_id, str) or _SAFE_ID_RE.fullmatch(binding_id) is None:
        raise ConnectorRefused("Invalid connector binding_id")
    if (
        not isinstance(connector_kind, str)
        or _SAFE_ID_RE.fullmatch(connector_kind) is None
    ):
        raise ConnectorRefused("Invalid connector_kind")
    if environment not in VALID_CONNECTOR_ENVIRONMENTS:
        raise ConnectorRefused(f"Unsupported connector environment: {environment}")
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ConnectorRefused("actor_id must be nonempty")
    if not operations:
        raise ConnectorRefused("At least one OperationSpec must be registered")
    op_names = [op.name for op in operations]
    if len(set(op_names)) != len(op_names):
        raise ConnectorRefused("Duplicate operation name in connector binding")
    if type(max_requests_per_window) is not int or max_requests_per_window < 1:
        raise ConnectorRefused("max_requests_per_window must be >= 1")
    if type(window_seconds) is not int or window_seconds < 1:
        raise ConnectorRefused("window_seconds must be >= 1")
    if type(budget_ceiling_minor_units) is not int or budget_ceiling_minor_units < 0:
        raise ConnectorRefused("budget_ceiling_minor_units must be >= 0")

    clean_url, clean_hosts = validate_endpoint_and_hosts(
        endpoint_url, allowed_hosts, allow_loopback=allow_loopback
    )
    if credential_handle_ref is not None:
        try:
            assert_handle_environment_isolation(
                credential_handle_ref,
                environment,
                allow_paper=(connector_kind != "trading_venue"),
            )
        except CredentialRefused as error:
            raise ConnectorEnvironmentMismatch(str(error)) from error

    recovery_map = dict(recovery_capabilities or {})
    timestamp = time_ns() if now_ns is None else now_ns

    with write_uow(writer, tx_lock=tx_lock) as db:
        prior_rows = db.execute(
            f"{_BINDING_SELECT} WHERE binding_id = ? ORDER BY revision DESC",
            (binding_id,),
        ).fetchall()
        next_revision = 1
        if prior_rows:
            latest = _row_to_binding(prior_rows[0])
            if latest.company_id != company_id:
                raise ConnectorRefused(
                    f"Connector binding '{binding_id}' belongs to another company"
                )
            if latest.environment != environment:
                raise ConnectorEnvironmentMismatch(
                    f"Cannot change connector binding '{binding_id}' environment "
                    f"from '{latest.environment}' to '{environment}'"
                )
            if latest.connector_kind != connector_kind:
                raise ConnectorRefused(
                    f"Cannot change connector binding '{binding_id}' kind "
                    f"from '{latest.connector_kind}' to '{connector_kind}'"
                )
            next_revision = latest.revision + 1
            db.execute(
                """UPDATE connector_binding
                   SET status = 'superseded', updated_at_ns = ?
                   WHERE binding_id = ? AND company_id = ? AND status = 'active'""",
                (timestamp, binding_id, company_id),
            )

        db.execute(
            """INSERT INTO connector_binding (
                   binding_id, revision, company_id, connector_kind, environment,
                   endpoint_url, allowed_hosts_json, operations_json,
                   credential_handle_ref, recovery_capabilities_json, status,
                   max_requests_per_window, window_seconds, budget_ceiling_minor_units,
                   registered_by_actor_id, created_at_ns, updated_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)""",
            (
                binding_id,
                next_revision,
                company_id,
                connector_kind,
                environment,
                clean_url,
                _canonical_json(list(clean_hosts)),
                _canonical_json([op.to_dict() for op in operations]),
                credential_handle_ref,
                _canonical_json(recovery_map),
                max_requests_per_window,
                window_seconds,
                budget_ceiling_minor_units,
                actor_id,
                timestamp,
                timestamp,
            ),
        )
        append_audit_event(
            db,
            company_id=company_id,
            domain="connectors",
            entity_type="connector_binding",
            entity_id=f"{binding_id}:r{next_revision}",
            event_type="connectors.binding_registered",
            actor_id=actor_id,
            payload={
                "binding_id": binding_id,
                "revision": next_revision,
                "connector_kind": connector_kind,
                "environment": environment,
                "endpoint_url": clean_url,
                "allowed_hosts": list(clean_hosts),
                "operations": [op.name for op in operations],
                "credential_handle_present": credential_handle_ref is not None,
            },
            recorded_at_ns=timestamp,
        )
        row = db.execute(
            f"{_BINDING_SELECT} WHERE binding_id = ? AND revision = ?",
            (binding_id, next_revision),
        ).fetchone()
        assert row is not None
        return _row_to_binding(row)


def disable_connector_binding(
    writer: WriterLease,
    *,
    company_id: str,
    binding_id: str,
    revision: int,
    actor_id: str,
    reason: str,
    now_ns: int | None = None,
    tx_lock: threading.RLock | None = None,
) -> ConnectorBinding:
    """Disable only the specified binding revision while preserving outstanding external_actions."""
    if writer.company_id != company_id:
        raise ConnectorRefused("Writer company_id mismatch")
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ConnectorRefused("actor_id must be nonempty")
    if not isinstance(reason, str) or not reason.strip():
        raise ConnectorRefused("reason must be nonempty")
    timestamp = time_ns() if now_ns is None else now_ns
    with write_uow(writer, tx_lock=tx_lock) as db:
        cur = db.execute(
            """UPDATE connector_binding
               SET status = 'disabled', updated_at_ns = ?
               WHERE company_id = ? AND binding_id = ? AND revision = ? AND status = 'active'""",
            (timestamp, company_id, binding_id, revision),
        )
        if cur.rowcount != 1:
            raise ConnectorRefused(
                f"Active connector binding '{binding_id}' revision {revision} not found"
            )
        append_audit_event(
            db,
            company_id=company_id,
            domain="connectors",
            entity_type="connector_binding",
            entity_id=f"{binding_id}:r{revision}",
            event_type="connectors.binding_disabled",
            actor_id=actor_id,
            payload={
                "binding_id": binding_id,
                "revision": revision,
                "reason": reason.strip(),
            },
            recorded_at_ns=timestamp,
        )
        row = db.execute(
            f"{_BINDING_SELECT} WHERE binding_id = ? AND revision = ?",
            (binding_id, revision),
        ).fetchone()
        assert row is not None
        return _row_to_binding(row)


def grant_connector_access(
    writer: WriterLease,
    *,
    company_id: str,
    agent_id: str,
    standing_grant_id: str,
    binding_id: str,
    binding_revision: int,
    allowed_operations: Sequence[str],
    granted_by_actor_id: str,
    max_cost_minor_units: int = 0,
    grant_binding_id: str | None = None,
    now_ns: int | None = None,
    tx_lock: threading.RLock | None = None,
) -> ConnectorGrant:
    """Grant one Agent explicit operation access to a specific active connector binding revision."""
    if writer.company_id != company_id:
        raise ConnectorRefused("Writer company_id mismatch")
    if not allowed_operations:
        raise ConnectorUnauthorized(
            "Connector grant must list at least one allowed operation"
        )
    if type(max_cost_minor_units) is not int or max_cost_minor_units < 0:
        raise ConnectorRefused("max_cost_minor_units must be >= 0")
    if not isinstance(granted_by_actor_id, str) or not granted_by_actor_id.strip():
        raise ConnectorRefused("granted_by_actor_id must be nonempty")
    unique_ops = tuple(sorted({str(op).strip() for op in allowed_operations}))
    if any(not op for op in unique_ops):
        raise ConnectorRefused("allowed_operations must contain nonempty names")

    timestamp = time_ns() if now_ns is None else now_ns
    gid = grant_binding_id or f"cgrant-{uuid4().hex[:16]}"
    with write_uow(writer, tx_lock=tx_lock) as db:
        agent_row = db.execute(
            "SELECT 1 FROM agent WHERE id = ? AND company_id = ?",
            (agent_id, company_id),
        ).fetchone()
        if agent_row is None:
            raise ConnectorUnauthorized("Agent does not belong to company")
        sg_row = db.execute(
            "SELECT scope FROM standing_grant WHERE id = ? AND agent_id = ?",
            (standing_grant_id, agent_id),
        ).fetchone()
        if sg_row is None or str(sg_row[0]) == "revoked":
            raise ConnectorUnauthorized(
                "Agent does not hold an active standing_grant for connector access"
            )
        b_row = db.execute(
            f"{_BINDING_SELECT} WHERE company_id = ? AND binding_id = ? AND revision = ?",
            (company_id, binding_id, binding_revision),
        ).fetchone()
        if b_row is None:
            raise ConnectorRefused(
                f"Connector binding '{binding_id}' revision {binding_revision} not found"
            )
        binding = _row_to_binding(b_row)
        if binding.status != "active":
            raise ConnectorRefused(
                f"Cannot grant access to non-active connector binding revision (status={binding.status})"
            )
        registered_ops = {op.name for op in binding.operations}
        unknown_ops = sorted(set(unique_ops) - registered_ops)
        if unknown_ops:
            raise ConnectorUnauthorized(
                f"Grant references unregistered operations for '{binding_id}': {unknown_ops}"
            )
        db.execute(
            """INSERT INTO connector_grant (
                   grant_binding_id, company_id, agent_id, standing_grant_id,
                   binding_id, binding_revision, allowed_operations_json,
                   max_cost_minor_units, status, granted_by_actor_id,
                   created_at_ns, revoked_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL)""",
            (
                gid,
                company_id,
                agent_id,
                standing_grant_id,
                binding_id,
                binding_revision,
                _canonical_json(list(unique_ops)),
                max_cost_minor_units,
                granted_by_actor_id,
                timestamp,
            ),
        )
        append_audit_event(
            db,
            company_id=company_id,
            domain="connectors",
            entity_type="connector_grant",
            entity_id=gid,
            event_type="connectors.grant_created",
            actor_id=granted_by_actor_id,
            payload={
                "grant_binding_id": gid,
                "agent_id": agent_id,
                "standing_grant_id": standing_grant_id,
                "binding_id": binding_id,
                "binding_revision": binding_revision,
                "allowed_operations": list(unique_ops),
                "max_cost_minor_units": max_cost_minor_units,
            },
            recorded_at_ns=timestamp,
        )
        row = db.execute(
            """SELECT grant_binding_id, company_id, agent_id, standing_grant_id,
                      binding_id, binding_revision, allowed_operations_json,
                      max_cost_minor_units, status, granted_by_actor_id,
                      created_at_ns, revoked_at_ns
               FROM connector_grant WHERE grant_binding_id = ?""",
            (gid,),
        ).fetchone()
        assert row is not None
        return _row_to_grant(row)


def revoke_connector_grant(
    writer: WriterLease,
    *,
    company_id: str,
    grant_binding_id: str,
    actor_id: str,
    now_ns: int | None = None,
    tx_lock: threading.RLock | None = None,
) -> None:
    """Revoke an active connector grant without erasing already-admitted external_actions."""
    if writer.company_id != company_id:
        raise ConnectorRefused("Writer company_id mismatch")
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ConnectorRefused("actor_id must be nonempty")
    timestamp = time_ns() if now_ns is None else now_ns
    with write_uow(writer, tx_lock=tx_lock) as db:
        cur = db.execute(
            """UPDATE connector_grant
               SET status = 'revoked', revoked_at_ns = ?
               WHERE company_id = ? AND grant_binding_id = ? AND status = 'active'""",
            (timestamp, company_id, grant_binding_id),
        )
        if cur.rowcount != 1:
            raise ConnectorRefused(
                f"Active connector grant '{grant_binding_id}' not found"
            )
        append_audit_event(
            db,
            company_id=company_id,
            domain="connectors",
            entity_type="connector_grant",
            entity_id=grant_binding_id,
            event_type="connectors.grant_revoked",
            actor_id=actor_id,
            payload={"grant_binding_id": grant_binding_id},
            recorded_at_ns=timestamp,
        )


def read_connector_binding(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    binding_id: str,
    revision: int | None = None,
) -> ConnectorBinding | None:
    """Read a specific revision or the latest active revision of a connector binding."""
    if revision is not None:
        row = connection.execute(
            f"{_BINDING_SELECT} WHERE company_id = ? AND binding_id = ? AND revision = ?",
            (company_id, binding_id, revision),
        ).fetchone()
    else:
        row = connection.execute(
            f"""{_BINDING_SELECT}
                WHERE company_id = ? AND binding_id = ? AND status = 'active'
                ORDER BY revision DESC LIMIT 1""",
            (company_id, binding_id),
        ).fetchone()
    return None if row is None else _row_to_binding(row)


def list_connector_bindings(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    active_only: bool = True,
) -> tuple[ConnectorBinding, ...]:
    if active_only:
        rows = connection.execute(
            f"""{_BINDING_SELECT}
                WHERE company_id = ? AND status = 'active'
                ORDER BY binding_id ASC, revision DESC""",
            (company_id,),
        ).fetchall()
    else:
        rows = connection.execute(
            f"""{_BINDING_SELECT}
                WHERE company_id = ?
                ORDER BY binding_id ASC, revision DESC""",
            (company_id,),
        ).fetchall()
    return tuple(_row_to_binding(r) for r in rows)


def read_connector_grants(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    agent_id: str | None = None,
    binding_id: str | None = None,
    active_only: bool = True,
) -> tuple[ConnectorGrant, ...]:
    clauses = ["company_id = ?"]
    params: list[Any] = [company_id]
    if agent_id is not None:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    if binding_id is not None:
        clauses.append("binding_id = ?")
        params.append(binding_id)
    if active_only:
        clauses.append("status = 'active'")
    rows = connection.execute(
        f"""SELECT grant_binding_id, company_id, agent_id, standing_grant_id,
                   binding_id, binding_revision, allowed_operations_json,
                   max_cost_minor_units, status, granted_by_actor_id,
                   created_at_ns, revoked_at_ns
            FROM connector_grant
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at_ns DESC, grant_binding_id ASC""",
        tuple(params),
    ).fetchall()
    return tuple(_row_to_grant(r) for r in rows)


class ConnectorAdapterRegistry:
    """In-process registry mapping connector_kind to ConnectorAdapter implementations."""

    def __init__(self, adapters: Sequence[ConnectorAdapter] = ()) -> None:
        self._adapters: dict[str, ConnectorAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: ConnectorAdapter) -> None:
        kind = adapter.manifest.connector_kind
        if kind in self._adapters:
            raise ConnectorRefused(
                f"Duplicate connector adapter registration for kind '{kind}'"
            )
        self._adapters[kind] = adapter

    def register_from_plugins(self, plugins: Sequence[Any]) -> None:
        """Load Seam #9 DomainPlugin.contribute_connectors() into this registry."""
        for plugin in plugins:
            contribute = getattr(plugin, "contribute_connectors", None)
            if callable(contribute):
                for adapter in contribute():
                    self.register(adapter)

    def get(self, connector_kind: str) -> ConnectorAdapter:
        adapter = self._adapters.get(connector_kind)
        if adapter is None:
            raise ConnectorRefused(
                f"No connector adapter registered for kind '{connector_kind}'"
            )
        return adapter

    def registered_kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))


__all__ = [
    "ConnectorAdapterRegistry",
    "disable_connector_binding",
    "grant_connector_access",
    "list_connector_bindings",
    "read_connector_binding",
    "read_connector_grants",
    "register_connector_binding",
    "revoke_connector_grant",
    "validate_endpoint_and_hosts",
]
