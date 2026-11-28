"""Trusted outer-host ConnectorProxy enforcing fencing, grants, admissions, taint, rate/budget, and GL settlement."""

from __future__ import annotations

import json
import threading
import urllib.parse
from collections.abc import Mapping
from contextlib import nullcontext
from datetime import UTC, date, datetime
from hashlib import sha256
from time import time_ns
from typing import Any

from ..agent_runtime.protocol import ToolHandler, ToolSpec
from ..duty_run import DutyRefused, ExecutionClaim, resolve_run
from ..general_ledger import JournalEntry, JournalLine, post_journal
from ..runtime.kernel import RunFenced, create_work_item
from ..runtime.shift.flow_guard import ShiftFlowGuard
from ..secret_vault import (
    CredentialRefused,
    HostSecretVault,
    MaskedSecret,
    MaskedSecretBundle,
    SecretVault,
    redact_secret_structure,
    redact_secret_values,
)
from ..storage.db import scoped_reader, write_uow
from ..storage.kernel import (
    ExternalActionConflict,
    append_audit_event,
    prepare_external_action,
    settle_external_action,
)
from ..writer_lease import WriterLease
from .protocol import (
    ConnectorAdapter,
    ConnectorBinding,
    ConnectorBudgetExceeded,
    ConnectorEnvironmentMismatch,
    ConnectorGrant,
    ConnectorIndeterminate,
    ConnectorRateLimited,
    ConnectorRefused,
    ConnectorRequest,
    ConnectorResponse,
    ConnectorUnauthorized,
    OperationSpec,
    validate_payload_against_schema,
)
from .registry import (
    ConnectorAdapterRegistry,
    read_connector_binding,
    read_connector_grants,
    validate_endpoint_and_hosts,
    verify_and_consume_connector_admission,
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_NS_PER_SECOND = 1_000_000_000
_SAFE_LOOKUP_CONTEXT_KEYS: tuple[str, ...] = (
    "symbol",
    "client_order_id",
    "action_id",
    "order_id",
)


def _extract_lookup_context(
    payload: Mapping[str, Any], request_id: str
) -> dict[str, Any]:
    """Extract immutable non-secret target identifiers used to bind recovery lookups."""
    ctx: dict[str, Any] = {}
    for key in _SAFE_LOOKUP_CONTEXT_KEYS:
        val = payload.get(key)
        if isinstance(val, (str, int)) and not isinstance(val, bool):
            ctx[key] = val
    if "client_order_id" not in ctx:
        ctx["client_order_id"] = str(ctx.get("action_id") or request_id)
    return ctx


def _origin_run_is_live(
    db: Any, company_id: str, run_id: str | None, now_ns: int
) -> bool:
    """Return True when the originating work_run is still actively running with an unexpired lease."""
    if not isinstance(run_id, str) or not run_id.strip():
        return False
    row = db.execute(
        """SELECT status, lease_expires_at_ns
           FROM work_runs
           WHERE company_id = ? AND run_id = ?""",
        (company_id, run_id),
    ).fetchone()
    if row is None:
        return False
    return str(row[0]) == "running" and int(row[1]) > now_ns


def _parse_receipt_meta(raw_receipt: str | None) -> dict[str, Any]:
    if not isinstance(raw_receipt, str) or not raw_receipt:
        return {}
    try:
        parsed = json.loads(raw_receipt)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _compute_request_sha256(
    *,
    agent_id: str,
    binding_id: str,
    binding_revision: int,
    operation: str,
    request_id: str,
    payload: Mapping[str, Any],
    admission_id: str | None,
    environment: str,
    endpoint_url: str,
) -> str:
    canonical = _canonical_json(
        {
            "agent_id": agent_id,
            "binding_id": binding_id,
            "binding_revision": binding_revision,
            "operation": operation,
            "request_id": request_id,
            "payload": dict(payload),
            "admission_id": admission_id,
            "environment": environment,
            "endpoint_url": endpoint_url,
        }
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_secret_for_binding(
    vault: SecretVault,
    binding: ConnectorBinding,
    adapter: ConnectorAdapter,
) -> MaskedSecret | MaskedSecretBundle | None:
    handle = binding.credential_handle_ref
    if handle is None:
        if adapter.manifest.requires_credential:
            raise ConnectorRefused(
                f"Connector '{binding.connector_kind}' requires an opaque credential_handle_ref"
            )
        return None
    try:
        if adapter.manifest.credential_fields:
            return vault.resolve_bundle(
                handle,
                adapter.manifest.credential_fields,
                expected_environment=binding.environment,
            )
        return vault.resolve_secret(
            handle,
            expected_environment=binding.environment,
        )
    except CredentialRefused as error:
        raise ConnectorEnvironmentMismatch(str(error)) from error


def _scrub_response(
    response: ConnectorResponse, raw_secrets: tuple[str, ...]
) -> ConnectorResponse:
    if not raw_secrets:
        return response
    clean_payload = redact_secret_structure(response.payload, raw_secrets)
    if not isinstance(clean_payload, dict):
        clean_payload = {"value": clean_payload}
    clean_ref = (
        None
        if response.source_ref is None
        else redact_secret_values(response.source_ref, raw_secrets)
    )
    clean_usage = (
        None
        if response.usage is None
        else redact_secret_structure(response.usage, raw_secrets)
    )
    return ConnectorResponse(
        status=response.status,
        payload=clean_payload,
        source_ref=clean_ref,
        indeterminate=response.indeterminate,
        cost_minor_units=response.cost_minor_units,
        usage=clean_usage if isinstance(clean_usage, dict) else None,
    )


def _response_from_cached_receipt(
    status: str, receipt_json: str | None
) -> ConnectorResponse:
    parsed: dict[str, Any] = {}
    if receipt_json:
        try:
            loaded = json.loads(receipt_json)
            if isinstance(loaded, dict):
                parsed = loaded
        except json.JSONDecodeError:
            parsed = {}
    resp_status = "observed" if status == "confirmed" else "rejected"
    raw_payload = parsed.get("response_payload")
    payload = dict(raw_payload) if isinstance(raw_payload, Mapping) else {}
    raw_cost = parsed.get("cost_minor_units", 0)
    cost = int(raw_cost) if type(raw_cost) is int and raw_cost >= 0 else 0
    raw_usage = parsed.get("usage")
    usage = dict(raw_usage) if isinstance(raw_usage, Mapping) else None
    raw_ref = parsed.get("source_ref")
    source_ref = None if raw_ref is None else str(raw_ref)
    return ConnectorResponse(
        status=resp_status,  # type: ignore[arg-type]
        payload=payload,
        source_ref=source_ref,
        indeterminate=False,
        cost_minor_units=cost,
        usage=usage,
    )


class ConnectorProxy:
    """Trusted outer-host gateway executing all scoped connector invocations."""

    def __init__(
        self,
        adapter_registry: ConnectorAdapterRegistry,
        secret_vault: SecretVault | None = None,
        *,
        loopback_only: bool = False,
        tx_lock: threading.RLock | None = None,
    ) -> None:
        self._adapters = adapter_registry
        self._vault: SecretVault = secret_vault or HostSecretVault()
        self._loopback_only = loopback_only
        self._tx_lock = tx_lock

    def _assert_dispatch_still_authorized(
        self,
        writer: WriterLease,
        claim: ExecutionClaim,
        *,
        binding_id: str,
        binding_revision: int,
        grant_binding_id: str,
        operation: str,
    ) -> None:
        """Re-check sender fencing, standing grant, active binding, and connector grant right before send."""
        with scoped_reader(writer.store) as ro_db:
            try:
                authority = resolve_run(ro_db, claim)
                authority.require_current(claim.purpose)
            except (DutyRefused, RunFenced) as error:
                raise ConnectorUnauthorized(str(error)) from error
            if authority.current_scope == "revoked":
                raise ConnectorUnauthorized("Agent standing grant is revoked")
            cur_binding = read_connector_binding(
                ro_db,
                company_id=writer.company_id,
                binding_id=binding_id,
                revision=binding_revision,
            )
            if cur_binding is None or cur_binding.status != "active":
                raise ConnectorUnauthorized(
                    f"Connector binding '{binding_id}:r{binding_revision}' is no longer active"
                )
            g_row = ro_db.execute(
                """SELECT status, allowed_operations_json
                   FROM connector_grant
                   WHERE company_id = ? AND grant_binding_id = ?""",
                (writer.company_id, grant_binding_id),
            ).fetchone()
            if g_row is None or str(g_row[0]) != "active":
                raise ConnectorUnauthorized(
                    f"Connector grant '{grant_binding_id}' is no longer active"
                )
            try:
                allowed_ops = json.loads(str(g_row[1]))
            except json.JSONDecodeError:
                allowed_ops = []
            if not isinstance(allowed_ops, list) or operation not in allowed_ops:
                raise ConnectorUnauthorized(
                    f"Connector grant '{grant_binding_id}' no longer authorizes '{operation}'"
                )

    def invoke(
        self,
        writer: WriterLease,
        claim: ExecutionClaim,
        *,
        binding_id: str,
        operation: str,
        request_id: str,
        payload: Mapping[str, Any],
        flow_guard: ShiftFlowGuard | None = None,
        admission_id: str | None = None,
        expected_environment: str | None = None,
        timeout_seconds: float = 15.0,
        effective_date: date | None = None,
        now_ns: int | None = None,
    ) -> ConnectorResponse:
        """Authorize, outbox-persist, credential-inject, recheck fencing, dispatch, and settle.

        SQLite write locks are strictly released before secret resolution and outbound I/O.
        """
        if not isinstance(binding_id, str) or not binding_id.strip():
            raise ConnectorRefused("binding_id must be a nonempty string")
        if not isinstance(operation, str) or not operation.strip():
            raise ConnectorRefused("operation must be a nonempty string")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ConnectorRefused("request_id must be a nonempty string")
        if not isinstance(payload, Mapping):
            raise ConnectorRefused("payload must be a JSON object mapping")
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ConnectorRefused("timeout_seconds must be positive")

        company_id = writer.company_id
        if claim.company_id != company_id:
            raise ConnectorUnauthorized(
                "Execution claim company_id does not match writer company_id"
            )
        ts = time_ns() if now_ns is None else now_ns

        # ------------------------------------------------------------------
        # Phase 1: Short pre-dispatch authorization & outbox preparation TX
        # ------------------------------------------------------------------
        binding: ConnectorBinding
        grant: ConnectorGrant
        op_spec: OperationSpec
        adapter: ConnectorAdapter
        agent_id: str
        idempotency_key: str
        req_sha256: str
        exec_gen: int = 1
        mode_action: str = "execute"  # "execute" | "lookup" | "replay"
        replay_response: ConnectorResponse | None = None

        with write_uow(writer, tx_lock=self._tx_lock) as db:
            try:
                authority = resolve_run(db, claim)
                authority.require_current(claim.purpose)
            except (DutyRefused, RunFenced) as error:
                raise ConnectorUnauthorized(str(error)) from error
            if authority.current_scope == "revoked":
                raise ConnectorUnauthorized("Agent standing grant is revoked")
            agent_id = claim.founder_id
            standing_grant_id = claim.grant_id

            loaded_binding = read_connector_binding(
                db, company_id=company_id, binding_id=binding_id.strip()
            )
            if loaded_binding is None or loaded_binding.status != "active":
                raise ConnectorRefused(
                    f"Active connector binding '{binding_id}' not found"
                )
            binding = loaded_binding

            if (
                expected_environment is not None
                and binding.environment != expected_environment
            ):
                raise ConnectorEnvironmentMismatch(
                    f"Connector binding '{binding_id}' environment '{binding.environment}' "
                    f"does not match expected '{expected_environment}'"
                )

            # Re-verify pinned host allowlist (and loopback-only guard when enabled) at use time.
            clean_url, clean_hosts = validate_endpoint_and_hosts(
                binding.endpoint_url,
                binding.allowed_hosts,
                allow_loopback=True,
            )
            parsed_host = (urllib.parse.urlparse(clean_url).hostname or "").lower()
            if self._loopback_only and parsed_host not in _LOOPBACK_HOSTS:
                raise ConnectorEnvironmentMismatch(
                    f"ConnectorProxy loopback_only mode refused non-loopback host '{parsed_host}'"
                )
            if not self._loopback_only and clean_url.startswith("http://"):
                if parsed_host not in _LOOPBACK_HOSTS:
                    raise ConnectorEnvironmentMismatch(
                        "Plaintext http:// connector target is prohibited for non-loopback hosts"
                    )

            adapter = self._adapters.get(binding.connector_kind)
            binding_op = binding.operation(operation.strip())
            manifest_op = adapter.manifest.operation(operation.strip())
            if binding_op is None or manifest_op is None:
                raise ConnectorRefused(
                    f"Operation '{operation}' is not registered on connector '{binding_id}'"
                )
            if binding_op.mode != manifest_op.mode:
                raise ConnectorRefused(
                    f"Operation '{operation}' mode mismatch between binding and adapter manifest"
                )
            # Enforce BOTH the adapter manifest schema and the binding's pinned restrictions
            # (e.g. model allowlists, token caps, additionalProperties=False) before dispatch.
            validate_payload_against_schema(payload, manifest_op.request_schema)
            validate_payload_against_schema(payload, binding_op.request_schema)
            op_spec = manifest_op

            # Check explicit active connector_grant bound to this exact standing grant & binding revision.
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
                    if g.standing_grant_id == standing_grant_id
                    and g.binding_revision == binding.revision
                    and op_spec.name in g.allowed_operations
                ),
                None,
            )
            if matching_grant is None:
                raise ConnectorUnauthorized(
                    f"Agent '{agent_id}' lacks an active connector_grant for "
                    f"'{binding.binding_id}:r{binding.revision}' operation '{op_spec.name}'"
                )
            grant = matching_grant

            # Enforce ShiftFlowGuard information-flow taint isolation.
            if flow_guard is not None:
                if flow_guard.read_private_desk and binding.environment != "internal":
                    raise ConnectorUnauthorized(
                        "Cannot invoke external connector in the same shift after reading private Desk data"
                    )
                if flow_guard.read_peer_input and op_spec.mode in (
                    "write",
                    "effectful",
                ):
                    raise ConnectorUnauthorized(
                        "Cannot invoke write or effectful connector in the same shift "
                        "after reading peer-authored messages, handoffs, or shared assets"
                    )

            # Effectful invocations MUST carry an authoritative service-issued admission
            # bound to exact immutable payload, binding, environment, and endpoint terms.
            clean_admission = (
                admission_id.strip()
                if isinstance(admission_id, str) and admission_id.strip()
                else None
            )
            if op_spec.mode == "effectful" and clean_admission is None:
                raise ConnectorUnauthorized(
                    f"Effectful operation '{op_spec.name}' on '{binding.binding_id}' "
                    "requires an authoritative service-issued admission"
                )

            if op_spec.mode == "effectful":
                idempotency_key = (
                    f"conn:{company_id}:{binding.binding_id}:r{binding.revision}:"
                    f"{op_spec.name}:{request_id.strip()}"
                )
            else:
                # Namespace non-effectful read/write actions by Agent so two authorized
                # Agents using the same request_id execute independently with zero cross-Agent disclosure.
                idempotency_key = (
                    f"conn:{company_id}:{agent_id}:{binding.binding_id}:r{binding.revision}:"
                    f"{op_spec.name}:{request_id.strip()}"
                )
            req_sha256 = _compute_request_sha256(
                agent_id=agent_id,
                binding_id=binding.binding_id,
                binding_revision=binding.revision,
                operation=op_spec.name,
                request_id=request_id.strip(),
                payload=payload,
                admission_id=clean_admission,
                environment=binding.environment,
                endpoint_url=clean_url,
            )
            connector_col = f"{binding.binding_id}:r{binding.revision}"
            try:
                ext_action, replayed = prepare_external_action(
                    db,
                    idempotency_key=idempotency_key,
                    company_id=company_id,
                    connector=connector_col,
                    operation=op_spec.name,
                    request_sha256=req_sha256,
                    run_id=claim.run_id,
                    command_id=clean_admission,
                    now_ns=ts,
                )
            except ExternalActionConflict as error:
                raise ConnectorRefused(str(error)) from error

            if op_spec.mode == "effectful":
                assert clean_admission is not None
                verify_and_consume_connector_admission(
                    db,
                    company_id=company_id,
                    admission_id=clean_admission,
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    operation=op_spec.name,
                    request_id=request_id.strip(),
                    payload=payload,
                    environment=binding.environment,
                    endpoint_url=clean_url,
                    actor_id=agent_id,
                    allow_already_consumed=replayed,
                    now_ns=ts,
                )

            if replayed:
                prior_obj = _parse_receipt_meta(ext_action.receipt_json)
                prior_owner = (
                    prior_obj.get("agent_id")
                    if isinstance(prior_obj.get("agent_id"), str)
                    else None
                )
                prior_gen_raw = prior_obj.get("execution_generation", 0)
                prior_gen = int(prior_gen_raw) if type(prior_gen_raw) is int else 0
                is_owner = prior_owner == agent_id
                is_recovery_owner = (
                    db.execute(
                        """SELECT 1 FROM work_items
                           WHERE company_id = ?
                             AND source_kind = 'connector_recovery'
                             AND source_id = ?
                             AND owner_agent_id = ?
                           LIMIT 1""",
                        (company_id, idempotency_key, agent_id),
                    ).fetchone()
                    is not None
                )
                if not (is_owner or is_recovery_owner):
                    raise ConnectorUnauthorized(
                        f"Agent '{agent_id}' cannot replay or inspect another Agent's external action"
                    )
                if ext_action.status in ("confirmed", "rejected"):
                    mode_action = "replay"
                    replay_response = _response_from_cached_receipt(
                        ext_action.status, ext_action.receipt_json
                    )
                elif ext_action.status in ("prepared", "in_flight") and (
                    op_spec.mode == "read"
                    or _origin_run_is_live(db, company_id, ext_action.run_id, ts)
                ):
                    # Preserve active execution state and budget serialization while
                    # an attempt is still actively in flight.
                    mode_action = "replay"
                    replay_response = ConnectorResponse(
                        status="unknown",
                        payload={
                            "idempotency_key": idempotency_key,
                            "reason": "action_in_flight",
                        },
                        indeterminate=True,
                    )
                elif op_spec.mode in ("write", "effectful"):
                    # Never blindly resend an unresolved write/effectful action!
                    exec_gen = prior_gen
                    if op_spec.supports_lookup or adapter.manifest.supports_lookup:
                        mode_action = "lookup"
                    else:
                        mode_action = "replay"
                        replay_response = ConnectorResponse(
                            status="unknown",
                            payload={
                                "idempotency_key": idempotency_key,
                                "reason": "prior_action_unresolved",
                            },
                            indeterminate=True,
                        )
                else:
                    # Settled 'unknown' read retry: atomically claims 'in_flight' below
                    # with an incremented execution_generation and enforces rate/budget!
                    exec_gen = prior_gen + 1
                    mode_action = "execute"

            if mode_action == "execute":
                # Account for EVERY outbound execute attempt (including unresolved read retries).
                window_start_ns = ts - (binding.window_seconds * _NS_PER_SECOND)
                recent_attempts = int(
                    db.execute(
                        """SELECT COUNT(*) FROM audit_event
                           WHERE company_id = ?
                             AND domain = 'connectors'
                             AND event_type = 'connectors.dispatch_attempt'
                             AND json_extract(payload_json, '$.binding_id') = ?
                             AND recorded_at_ns >= ?""",
                        (company_id, binding.binding_id, window_start_ns),
                    ).fetchone()[0]
                )
                if recent_attempts >= binding.max_requests_per_window:
                    raise ConnectorRateLimited(
                        f"Connector '{binding.binding_id}' exceeded rate limit "
                        f"({binding.max_requests_per_window} per {binding.window_seconds}s)"
                    )

                prefix = f"{binding.binding_id}:r"
                if binding.budget_ceiling_minor_units > 0:
                    in_flight_binding = db.execute(
                        """SELECT 1 FROM external_actions
                           WHERE company_id = ?
                             AND status = 'in_flight'
                             AND idempotency_key != ?
                             AND substr(connector, 1, ?) = ?
                           LIMIT 1""",
                        (company_id, idempotency_key, len(prefix), prefix),
                    ).fetchone()
                    if in_flight_binding is not None:
                        raise ConnectorBudgetExceeded(
                            f"Another budgeted request on connector binding '{binding.binding_id}' "
                            "is currently in flight; await settlement before dispatching another"
                        )
                    spent_binding = int(
                        db.execute(
                            """SELECT COALESCE(SUM(
                                   CAST(json_extract(receipt_json, '$.cost_minor_units') AS INTEGER)
                               ), 0)
                               FROM external_actions
                               WHERE company_id = ?
                                 AND status = 'confirmed'
                                 AND substr(connector, 1, ?) = ?""",
                            (company_id, len(prefix), prefix),
                        ).fetchone()[0]
                    )
                    if spent_binding >= binding.budget_ceiling_minor_units:
                        raise ConnectorBudgetExceeded(
                            f"Connector binding '{binding.binding_id}' budget ceiling "
                            f"({binding.budget_ceiling_minor_units}) exhausted"
                        )
                if grant.max_cost_minor_units > 0:
                    in_flight_grant = db.execute(
                        """SELECT 1 FROM external_actions
                           WHERE company_id = ?
                             AND status = 'in_flight'
                             AND idempotency_key != ?
                             AND json_extract(receipt_json, '$.grant_binding_id') = ?
                           LIMIT 1""",
                        (company_id, idempotency_key, grant.grant_binding_id),
                    ).fetchone()
                    if in_flight_grant is not None:
                        raise ConnectorBudgetExceeded(
                            f"Another budgeted request under connector grant '{grant.grant_binding_id}' "
                            "is currently in flight; await settlement before dispatching another"
                        )
                    spent_grant = int(
                        db.execute(
                            """SELECT COALESCE(SUM(
                                   CAST(json_extract(receipt_json, '$.cost_minor_units') AS INTEGER)
                               ), 0)
                               FROM external_actions
                               WHERE company_id = ?
                                 AND status = 'confirmed'
                                 AND json_extract(receipt_json, '$.grant_binding_id') = ?""",
                            (company_id, grant.grant_binding_id),
                        ).fetchone()[0]
                    )
                    if spent_grant >= grant.max_cost_minor_units:
                        raise ConnectorBudgetExceeded(
                            f"Connector grant '{grant.grant_binding_id}' cost ceiling "
                            f"({grant.max_cost_minor_units}) exhausted"
                        )

                lookup_context = _extract_lookup_context(payload, request_id.strip())
                settle_external_action(
                    db,
                    idempotency_key,
                    status="in_flight",
                    receipt={
                        "binding_id": binding.binding_id,
                        "binding_revision": binding.revision,
                        "connector_kind": binding.connector_kind,
                        "operation": op_spec.name,
                        "request_id": request_id.strip(),
                        "agent_id": agent_id,
                        "grant_binding_id": grant.grant_binding_id,
                        "admission_id": clean_admission,
                        "request_sha256": req_sha256,
                        "execution_generation": exec_gen,
                        "lookup_context": lookup_context,
                    },
                    now_ns=ts,
                )
                append_audit_event(
                    db,
                    company_id=company_id,
                    domain="connectors",
                    entity_type="external_action",
                    entity_id=idempotency_key,
                    event_type="connectors.dispatch_attempt",
                    actor_id=agent_id,
                    run_id=claim.run_id,
                    command_id=clean_admission,
                    payload={
                        "binding_id": binding.binding_id,
                        "binding_revision": binding.revision,
                        "operation": op_spec.name,
                        "retry": replayed,
                    },
                    recorded_at_ns=ts,
                )

        if mode_action == "replay":
            assert replay_response is not None
            return replay_response

        # ------------------------------------------------------------------
        # Phase 2: Trusted-host memory-only secret injection, fencing recheck,
        #          and outbound dispatch (NO SQLite write lock held here!)
        # ------------------------------------------------------------------
        try:
            secret = _resolve_secret_for_binding(self._vault, binding, adapter)
        except ConnectorRefused as error:
            fail_ts = time_ns() if now_ns is None else (now_ns + 1)
            with write_uow(writer, tx_lock=self._tx_lock) as db:
                settle_external_action(
                    db,
                    idempotency_key,
                    status="rejected",
                    receipt={
                        "binding_id": binding.binding_id,
                        "binding_revision": binding.revision,
                        "connector_kind": binding.connector_kind,
                        "operation": op_spec.name,
                        "request_id": request_id.strip(),
                        "agent_id": agent_id,
                        "grant_binding_id": grant.grant_binding_id,
                        "admission_id": clean_admission,
                        "request_sha256": req_sha256,
                        "response_payload": {"error": str(error)},
                    },
                    now_ns=fail_ts,
                )
                append_audit_event(
                    db,
                    company_id=company_id,
                    domain="connectors",
                    entity_type="external_action",
                    entity_id=idempotency_key,
                    event_type="connectors.operation_rejected",
                    actor_id=agent_id,
                    run_id=claim.run_id,
                    command_id=clean_admission,
                    payload={
                        "binding_id": binding.binding_id,
                        "binding_revision": binding.revision,
                        "operation": op_spec.name,
                        "reason": "secret_resolution_failed",
                    },
                    recorded_at_ns=fail_ts,
                )
            raise
        raw_secrets = secret.raw_values() if secret is not None else ()

        # Recheck sender fencing, active binding, and connector grant immediately before
        # outbound execute so a run fenced or revoked during blocking secret resolution
        # (e.g. Keychain lookup) cannot newly send.
        if mode_action == "execute":
            try:
                self._assert_dispatch_still_authorized(
                    writer,
                    claim,
                    binding_id=binding.binding_id,
                    binding_revision=binding.revision,
                    grant_binding_id=grant.grant_binding_id,
                    operation=op_spec.name,
                )
            except ConnectorUnauthorized:
                secret = None
                del raw_secrets
                fenced_ts = time_ns() if now_ns is None else (now_ns + 1)
                with write_uow(writer, tx_lock=self._tx_lock) as db:
                    settle_external_action(
                        db,
                        idempotency_key,
                        status="unknown",
                        receipt={
                            "binding_id": binding.binding_id,
                            "binding_revision": binding.revision,
                            "connector_kind": binding.connector_kind,
                            "operation": op_spec.name,
                            "request_id": request_id.strip(),
                            "agent_id": agent_id,
                            "grant_binding_id": grant.grant_binding_id,
                            "admission_id": clean_admission,
                            "request_sha256": req_sha256,
                            "execution_generation": exec_gen,
                            "lookup_context": _extract_lookup_context(
                                payload, request_id.strip()
                            ),
                            "response_payload": {
                                "idempotency_key": idempotency_key,
                                "reason": "sender_fenced_before_dispatch",
                            },
                        },
                        now_ns=fenced_ts,
                    )
                    if op_spec.mode in ("write", "effectful"):
                        create_work_item(
                            db,
                            company_id=company_id,
                            owner_agent_id=agent_id,
                            duty_kind="connector.reconcile",
                            source_kind="connector_recovery",
                            source_id=idempotency_key,
                            payload={
                                "idempotency_key": idempotency_key,
                                "binding_id": binding.binding_id,
                                "binding_revision": binding.revision,
                                "operation": op_spec.name,
                                "request_id": request_id.strip(),
                            },
                            now_ns=fenced_ts,
                        )
                    append_audit_event(
                        db,
                        company_id=company_id,
                        domain="connectors",
                        entity_type="external_action",
                        entity_id=idempotency_key,
                        event_type="connectors.operation_fenced",
                        actor_id=agent_id,
                        run_id=claim.run_id,
                        command_id=clean_admission,
                        payload={
                            "binding_id": binding.binding_id,
                            "binding_revision": binding.revision,
                            "operation": op_spec.name,
                            "reason": "sender_fenced_before_dispatch",
                        },
                        recorded_at_ns=fenced_ts,
                    )
                raise

        conn_request = ConnectorRequest(
            company_id=company_id,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
            operation=op_spec.name,
            request_id=request_id.strip(),
            idempotency_key=idempotency_key,
            payload=dict(payload),
            environment=binding.environment,
            endpoint_url=clean_url,
            allowed_hosts=clean_hosts,
            admission_id=clean_admission,
        )

        try:
            if mode_action == "lookup":
                raw_response = adapter.lookup(
                    conn_request, secret=secret, timeout_seconds=float(timeout_seconds)
                )
            else:
                raw_response = adapter.execute(
                    conn_request, secret=secret, timeout_seconds=float(timeout_seconds)
                )
        except (ConnectorIndeterminate, TimeoutError, OSError) as error:
            raw_response = ConnectorResponse(
                status="unknown",
                payload={
                    "idempotency_key": idempotency_key,
                    "error": redact_secret_values(str(error), raw_secrets),
                },
                indeterminate=True,
            )
        except ConnectorRefused as error:
            raw_response = ConnectorResponse(
                status="rejected",
                payload={
                    "idempotency_key": idempotency_key,
                    "error": redact_secret_values(str(error), raw_secrets),
                },
                indeterminate=False,
            )
        except Exception as error:
            # Never allow unexpected transport/client exceptions to leak resolved credentials.
            raw_response = ConnectorResponse(
                status="unknown",
                payload={
                    "idempotency_key": idempotency_key,
                    "error": f"{type(error).__name__}: {redact_secret_values(str(error), raw_secrets)}",
                },
                indeterminate=True,
            )
        finally:
            secret = None

        clean_response = _scrub_response(raw_response, raw_secrets)
        del raw_secrets

        # ------------------------------------------------------------------
        # Phase 3: Short post-dispatch settlement, recovery duty & GL posting
        # ------------------------------------------------------------------
        settle_ts = time_ns() if now_ns is None else (now_ns + 1)
        ext_status = (
            "confirmed"
            if clean_response.status == "observed"
            else "rejected"
            if clean_response.status == "rejected"
            else "unknown"
        )
        lookup_context = _extract_lookup_context(payload, request_id.strip())
        receipt_envelope = {
            "binding_id": binding.binding_id,
            "binding_revision": binding.revision,
            "connector_kind": binding.connector_kind,
            "operation": op_spec.name,
            "request_id": request_id.strip(),
            "agent_id": agent_id,
            "grant_binding_id": grant.grant_binding_id,
            "admission_id": clean_admission,
            "request_sha256": req_sha256,
            "execution_generation": exec_gen,
            "lookup_context": lookup_context,
            "source_ref": clean_response.source_ref,
            "cost_minor_units": clean_response.cost_minor_units,
            "usage": clean_response.usage,
            "response_payload": clean_response.payload,
        }

        recovery_work_item_id: str | None = None
        with write_uow(writer, tx_lock=self._tx_lock) as db:
            settle_external_action(
                db,
                idempotency_key,
                status=ext_status,
                receipt=receipt_envelope,
                now_ns=settle_ts,
            )
            if ext_status == "unknown" and op_spec.mode in ("write", "effectful"):
                rec_item, _ = create_work_item(
                    db,
                    company_id=company_id,
                    owner_agent_id=agent_id,
                    duty_kind="connector.reconcile",
                    source_kind="connector_recovery",
                    source_id=idempotency_key,
                    payload={
                        "idempotency_key": idempotency_key,
                        "binding_id": binding.binding_id,
                        "binding_revision": binding.revision,
                        "operation": op_spec.name,
                        "request_id": request_id.strip(),
                    },
                    now_ns=settle_ts,
                )
                recovery_work_item_id = rec_item.work_item_id
            append_audit_event(
                db,
                company_id=company_id,
                domain="connectors",
                entity_type="external_action",
                entity_id=idempotency_key,
                event_type=f"connectors.operation_{ext_status}",
                actor_id=agent_id,
                run_id=claim.run_id,
                command_id=clean_admission,
                payload={
                    "binding_id": binding.binding_id,
                    "binding_revision": binding.revision,
                    "connector_kind": binding.connector_kind,
                    "operation": op_spec.name,
                    "status": ext_status,
                    "indeterminate": clean_response.indeterminate,
                    "source_ref": clean_response.source_ref,
                    "cost_minor_units": clean_response.cost_minor_units,
                    "recovery_work_item_id": recovery_work_item_id,
                },
                recorded_at_ns=settle_ts,
            )

        if ext_status == "confirmed" and clean_response.cost_minor_units > 0:
            entry_date = (
                datetime.now(UTC).date() if effective_date is None else effective_date
            )
            evidence_rev = sha256(
                f"{idempotency_key}:{clean_response.cost_minor_units}:{req_sha256}".encode(
                    "utf-8"
                )
            ).hexdigest()
            entry = JournalEntry(
                company_id=company_id,
                source=f"connector:{binding.connector_kind}",
                source_event=idempotency_key,
                evidence_revision=evidence_rev,
                effective_date=entry_date,
                currency="USD",
                mapping_revision="connector-usage-v1",
                lines=(
                    JournalLine(
                        "expense:connector_usage", clean_response.cost_minor_units
                    ),
                    JournalLine(
                        "liability:accounts_payable", -clean_response.cost_minor_units
                    ),
                ),
            )
            lock_ctx = self._tx_lock if self._tx_lock is not None else nullcontext()
            with lock_ctx:
                post_journal(writer, entry)

        if (
            recovery_work_item_id is not None
            and "recovery_work_item_id" not in clean_response.payload
        ):
            return ConnectorResponse(
                status=clean_response.status,
                payload={
                    **clean_response.payload,
                    "recovery_work_item_id": recovery_work_item_id,
                },
                source_ref=clean_response.source_ref,
                indeterminate=clean_response.indeterminate,
                cost_minor_units=clean_response.cost_minor_units,
                usage=clean_response.usage,
            )
        return clean_response

    def reconcile_unknown_action(
        self,
        writer: WriterLease,
        claim: ExecutionClaim,
        *,
        idempotency_key: str,
        lookup_payload: Mapping[str, Any] | None = None,
        timeout_seconds: float = 15.0,
        now_ns: int | None = None,
    ) -> ConnectorResponse:
        """Reconcile an existing unresolved external_actions record via adapter.lookup.

        Restricted strictly to the action's originating Agent or explicitly assigned
        `connector_recovery` work-item owner, while still succeeding after the
        original connector_grant or connector_binding is revoked/disabled.
        """
        company_id = writer.company_id
        if claim.company_id != company_id:
            raise ConnectorUnauthorized("Execution claim company_id mismatch")
        ts = time_ns() if now_ns is None else now_ns

        with write_uow(writer, tx_lock=self._tx_lock) as db:
            try:
                authority = resolve_run(db, claim)
                authority.require_current(claim.purpose)
            except (DutyRefused, RunFenced) as error:
                raise ConnectorUnauthorized(str(error)) from error
            if authority.current_scope == "revoked":
                raise ConnectorUnauthorized("Agent standing grant is revoked")
            agent_id = claim.founder_id

            row = db.execute(
                """SELECT company_id, connector, operation, status, receipt_json, command_id, run_id
                   FROM external_actions
                   WHERE idempotency_key = ?""",
                (idempotency_key,),
            ).fetchone()
            if row is None or str(row[0]) != company_id:
                raise ConnectorRefused(
                    f"External action '{idempotency_key}' not found for company"
                )
            (
                connector_col,
                op_name,
                current_status,
                raw_receipt,
                cmd_id,
                origin_run_id,
            ) = (
                str(row[1]),
                str(row[2]),
                str(row[3]),
                row[4],
                None if row[5] is None else str(row[5]),
                None if row[6] is None else str(row[6]),
            )

            prior_meta = _parse_receipt_meta(raw_receipt)
            prior_gen_raw = prior_meta.get("execution_generation", 0)
            expected_gen = int(prior_gen_raw) if type(prior_gen_raw) is int else 0

            # Enforce strict ownership or explicit recovery work-item assignment BEFORE
            # exposing cached receipts or invoking destination lookup.
            origin_agent = prior_meta.get("agent_id")
            is_originator = isinstance(origin_agent, str) and origin_agent == agent_id
            is_assigned_recovery_owner = (
                db.execute(
                    """SELECT 1 FROM work_items
                       WHERE company_id = ?
                         AND source_kind = 'connector_recovery'
                         AND source_id = ?
                         AND owner_agent_id = ?
                       LIMIT 1""",
                    (company_id, idempotency_key, agent_id),
                ).fetchone()
                is not None
            )
            if not (is_originator or is_assigned_recovery_owner):
                raise ConnectorUnauthorized(
                    f"Agent '{agent_id}' is neither the originator nor the assigned "
                    f"recovery owner for external action '{idempotency_key}'"
                )

            if current_status in ("confirmed", "rejected"):
                return _response_from_cached_receipt(current_status, raw_receipt)

            # Never clear or overwrite an actively executing in_flight action while its
            # originating run is still live; wait for settlement or lease expiry.
            if current_status in ("prepared", "in_flight") and _origin_run_is_live(
                db, company_id, origin_run_id, ts
            ):
                raise ConnectorRefused(
                    f"External action '{idempotency_key}' is actively in flight on a live run; "
                    "wait for settlement or lease expiry before reconciling"
                )

            if ":r" not in connector_col:
                raise ConnectorRefused(
                    f"Malformed connector reference on external_action '{idempotency_key}'"
                )
            b_id, rev_str = connector_col.rsplit(":r", 1)
            b_rev = int(rev_str)
            loaded_binding = read_connector_binding(
                db, company_id=company_id, binding_id=b_id, revision=b_rev
            )
            if loaded_binding is None:
                raise ConnectorRefused(
                    f"Connector binding '{b_id}:r{b_rev}' not found for reconciliation"
                )
            binding = loaded_binding
            clean_url, clean_hosts = validate_endpoint_and_hosts(
                binding.endpoint_url,
                binding.allowed_hosts,
                allow_loopback=True,
            )
            parsed_host = (urllib.parse.urlparse(clean_url).hostname or "").lower()
            if self._loopback_only and parsed_host not in _LOOPBACK_HOSTS:
                raise ConnectorEnvironmentMismatch(
                    f"ConnectorProxy loopback_only mode refused non-loopback host '{parsed_host}'"
                )
            adapter = self._adapters.get(binding.connector_kind)
            request_id = str(prior_meta.get("request_id") or idempotency_key)
            raw_persisted_lookup = prior_meta.get("lookup_context")
            persisted_lookup: dict[str, Any] = (
                dict(raw_persisted_lookup)
                if isinstance(raw_persisted_lookup, Mapping)
                else {"client_order_id": request_id}
            )
            if lookup_payload is not None:
                if not isinstance(lookup_payload, Mapping):
                    raise ConnectorRefused("lookup_payload must be a mapping")
                # Reject any caller-supplied substitution that contradicts or injects
                # target identifiers outside the originally admitted action.
                for k, v in lookup_payload.items():
                    if k not in persisted_lookup or persisted_lookup[k] != v:
                        raise ConnectorUnauthorized(
                            f"Recovery lookup_payload cannot substitute field '{k}' "
                            "outside the originally admitted action"
                        )

        secret = _resolve_secret_for_binding(self._vault, binding, adapter)
        raw_secrets = secret.raw_values() if secret is not None else ()
        lookup_req = ConnectorRequest(
            company_id=company_id,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
            operation=op_name,
            request_id=request_id,
            idempotency_key=idempotency_key,
            payload=persisted_lookup,
            environment=binding.environment,
            endpoint_url=clean_url,
            allowed_hosts=clean_hosts,
            admission_id=cmd_id,
        )
        try:
            raw_response = adapter.lookup(
                lookup_req, secret=secret, timeout_seconds=float(timeout_seconds)
            )
        except (ConnectorIndeterminate, TimeoutError, OSError) as error:
            raw_response = ConnectorResponse(
                status="unknown",
                payload={
                    "idempotency_key": idempotency_key,
                    "error": redact_secret_values(str(error), raw_secrets),
                },
                indeterminate=True,
            )
        except ConnectorRefused as error:
            raw_response = ConnectorResponse(
                status="rejected",
                payload={
                    "idempotency_key": idempotency_key,
                    "error": redact_secret_values(str(error), raw_secrets),
                },
                indeterminate=False,
            )
        except Exception as error:
            raw_response = ConnectorResponse(
                status="unknown",
                payload={
                    "idempotency_key": idempotency_key,
                    "error": f"{type(error).__name__}: {redact_secret_values(str(error), raw_secrets)}",
                },
                indeterminate=True,
            )
        finally:
            secret = None

        clean_response = _scrub_response(raw_response, raw_secrets)
        del raw_secrets

        ext_status = (
            "confirmed"
            if clean_response.status == "observed"
            else "rejected"
            if clean_response.status == "rejected"
            else "unknown"
        )
        merged_receipt = {
            **prior_meta,
            "source_ref": clean_response.source_ref,
            "cost_minor_units": clean_response.cost_minor_units,
            "usage": clean_response.usage,
            "response_payload": clean_response.payload,
            "reconciled_at_ns": ts,
        }
        with write_uow(writer, tx_lock=self._tx_lock) as db:
            # Condition settlement on execution_generation and non-live in_flight state
            # so a concurrent lookup can never clear an active attempt's budget exclusion.
            cur_row = db.execute(
                """SELECT status, receipt_json, run_id
                   FROM external_actions
                   WHERE idempotency_key = ?""",
                (idempotency_key,),
            ).fetchone()
            if cur_row is not None:
                latest_status, latest_raw, latest_run_id = (
                    str(cur_row[0]),
                    cur_row[1],
                    None if cur_row[2] is None else str(cur_row[2]),
                )
                if latest_status in ("confirmed", "rejected"):
                    return _response_from_cached_receipt(latest_status, latest_raw)
                latest_meta = _parse_receipt_meta(latest_raw)
                latest_gen_raw = latest_meta.get("execution_generation", 0)
                latest_gen = int(latest_gen_raw) if type(latest_gen_raw) is int else 0
                if latest_status in ("prepared", "in_flight") and (
                    latest_gen != expected_gen
                    or _origin_run_is_live(db, company_id, latest_run_id, ts)
                ):
                    raise ConnectorRefused(
                        f"External action '{idempotency_key}' advanced to an active execution "
                        "during reconciliation lookup"
                    )
            settle_external_action(
                db,
                idempotency_key,
                status=ext_status,
                receipt=merged_receipt,
                now_ns=ts,
            )
            append_audit_event(
                db,
                company_id=company_id,
                domain="connectors",
                entity_type="external_action",
                entity_id=idempotency_key,
                event_type="connectors.operation_reconciled",
                actor_id=agent_id,
                run_id=claim.run_id,
                command_id=cmd_id,
                payload={
                    "binding_id": binding.binding_id,
                    "binding_revision": binding.revision,
                    "operation": op_name,
                    "resolved_status": ext_status,
                    "source_ref": clean_response.source_ref,
                },
                recorded_at_ns=ts,
            )
        if ext_status == "confirmed" and clean_response.cost_minor_units > 0:
            evidence_rev = sha256(
                f"{idempotency_key}:{clean_response.cost_minor_units}:reconciled".encode(
                    "utf-8"
                )
            ).hexdigest()
            entry = JournalEntry(
                company_id=company_id,
                source=f"connector:{binding.connector_kind}",
                source_event=idempotency_key,
                evidence_revision=evidence_rev,
                effective_date=datetime.now(UTC).date(),
                currency="USD",
                mapping_revision="connector-usage-v1",
                lines=(
                    JournalLine(
                        "expense:connector_usage", clean_response.cost_minor_units
                    ),
                    JournalLine(
                        "liability:accounts_payable", -clean_response.cost_minor_units
                    ),
                ),
            )
            lock_ctx = self._tx_lock if self._tx_lock is not None else nullcontext()
            with lock_ctx:
                post_journal(writer, entry)
        return clean_response

    @staticmethod
    def tool_spec() -> ToolSpec:
        """Return OuterToolBroker ToolSpec declaring `invoke_connector` as an external write sink.

        Note: `admission_id` is intentionally omitted from model-visible arguments;
        effectful operations require a trusted domain service to issue and pass a
        server-owned `connector_admission`.
        """
        return ToolSpec(
            name="invoke_connector",
            description=(
                "Invoke a company-registered scoped external connector by binding_id and operation. "
                "Credentials are injected exclusively by the outer host; never pass API keys."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "binding_id": {"type": "string"},
                    "operation": {"type": "string"},
                    "request_id": {"type": "string"},
                    "payload": {"type": "object"},
                    "expected_environment": {"type": "string"},
                },
                "required": ["binding_id", "operation", "request_id", "payload"],
            },
            flow_class="external_write_sink",
        )

    def build_tool_handler(
        self,
        writer: WriterLease,
        claim: ExecutionClaim,
        *,
        flow_guard: ShiftFlowGuard | None = None,
        timeout_seconds: float = 15.0,
    ) -> ToolHandler:
        """Create an OuterToolBroker tool handler bound to a live fenced claim.

        Never forwards caller-supplied `admission_id` values so Agents cannot
        fabricate domain admissions for effectful operations.
        """

        def _handle(args: dict[str, Any]) -> dict[str, Any]:
            if not isinstance(args, Mapping):
                raise ConnectorRefused("invoke_connector args must be a JSON object")
            resp = self.invoke(
                writer,
                claim,
                binding_id=str(args.get("binding_id") or ""),
                operation=str(args.get("operation") or ""),
                request_id=str(args.get("request_id") or ""),
                payload=args.get("payload")
                if isinstance(args.get("payload"), Mapping)
                else {},
                flow_guard=flow_guard,
                admission_id=None,
                expected_environment=(
                    str(args["expected_environment"])
                    if isinstance(args.get("expected_environment"), str)
                    else None
                ),
                timeout_seconds=timeout_seconds,
            )
            return {
                "status": resp.status,
                "indeterminate": resp.indeterminate,
                "source_ref": resp.source_ref,
                "cost_minor_units": resp.cost_minor_units,
                "usage": resp.usage,
                "payload": resp.payload,
            }

        return _handle


__all__ = ["ConnectorProxy"]
