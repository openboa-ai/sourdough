"""Trading domain Board commands contributed to CompanyDaemon."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import closing
from sqlite3 import Connection
from time import time_ns
from typing import Any

from ..board_control import (
    AuthorizeBoard,
    BoardCommandOutcome,
    BoardControlConflict,
    BoardControlForbidden,
    BoardControlRefused,
    _require_text,
    make_board_authorizer,
)
from ..company import _identity
from ..storage.db import open_writer, write_tx
from ..storage.kernel import CommandConflict, admit_command_receipt, append_audit_event
from ..storage.schema import is_domain_installed
from ..writer_lease import WriterLease
from .accounts import (
    demote_live_environment,
    open_company_paper_book,
    promote_execution_environment,
)
from .domain import (
    DEMOTE_LIVE_ENVIRONMENT,
    EXECUTION_TIER_COMMAND,
    PROMOTE_EXECUTION_ENVIRONMENT,
    ExecutionTierRefused,
    _sha256_json,
)
from .external_venue import ExternalVenueAdapter
from .quotes import MarketQuoteProvider
from .reconcile import reconcile_trading_books
from .strategy_comparison import (
    StrategyComparisonRefused,
    activate_strategy_release,
    rollback_strategy_deployment,
)

STRATEGY_COMMAND = "strategy-command-v1"
ACTIVATE_STRATEGY_RELEASE = "activate_strategy_release"
ROLLBACK_STRATEGY_DEPLOYMENT = "rollback_strategy_deployment"
ACTIVATE_PRECONDITION_KEYS = frozenset(
    {"binding_revision", "expected_active_deployment_id"}
)
ROLLBACK_PRECONDITION_KEYS = frozenset({"expected_active_deployment_id"})

OPEN_COMPANY_PAPER_BOOK = "open_company_paper_book"
RECONCILE_TRADING_BOOKS = "reconcile_trading_books"
OPEN_PAPER_BOOK_PRECONDITION_KEYS = frozenset({"charter_revision"})
RECONCILE_TRADING_PRECONDITION_KEYS = frozenset({"charter_revision"})

PROMOTE_PRECONDITION_KEYS = frozenset(
    {
        "charter_revision",
        "target_binding_id",
        "qualification_receipt_id",
        "expected_payload_hash",
    }
)
DEMOTE_PRECONDITION_KEYS = frozenset({"charter_revision", "target_binding_id"})


def execute_strategy_command(
    writer: WriterLease,
    *,
    command_id: str,
    command_type: str,
    preconditions: dict[str, Any],
    payload: dict[str, Any],
    authorize: AuthorizeBoard | None = None,
    principal_hint: str | None = None,
    peer_pid: int | None = None,
) -> BoardCommandOutcome:
    """Delegate strategy activation or forward rollback to strategy_comparison.py."""
    safe_cmd = _require_text(command_id, "command_id", max_len=128)
    auth_fn = make_board_authorizer(
        authorize_board=authorize, principal_hint=principal_hint
    )

    def _deploy_auth(
        db: Connection, company_id: str, op: str, _release_id: str
    ) -> str | None:
        return auth_fn(db, company_id, op)

    try:
        if command_type == ACTIVATE_STRATEGY_RELEASE:
            if not set(preconditions.keys()).issubset(ACTIVATE_PRECONDITION_KEYS):
                raise BoardControlRefused(
                    "Command preconditions contain unsupported or missing fields"
                )
            release_id = _require_text(
                payload.get("release_id"), "release_id", max_len=128
            )
            binding_rev = _require_text(
                preconditions.get("binding_revision"), "binding_revision", max_len=128
            )
            comparison_id = payload.get("comparison_id")
            expected_active = preconditions.get("expected_active_deployment_id")
            if expected_active is not None:
                expected_active = _require_text(
                    expected_active, "expected_active_deployment_id", max_len=128
                )
            deployment = activate_strategy_release(
                writer,
                command_id=safe_cmd,
                release_id=release_id,
                binding_revision=binding_rev,
                authorize_deploy=_deploy_auth,
                comparison_id=comparison_id,
                expected_active_deployment_id=expected_active,
                contract_version=STRATEGY_COMMAND,
                peer_pid=peer_pid,
            )
        elif command_type == ROLLBACK_STRATEGY_DEPLOYMENT:
            target_release = _require_text(
                payload.get("target_release_id"), "target_release_id", max_len=128
            )
            expected_active = _require_text(
                preconditions.get("expected_active_deployment_id"),
                "expected_active_deployment_id",
                max_len=128,
            )
            deployment = rollback_strategy_deployment(
                writer,
                command_id=safe_cmd,
                target_release_id=target_release,
                expected_active_deployment_id=expected_active,
                authorize_deploy=_deploy_auth,
                contract_version=STRATEGY_COMMAND,
                peer_pid=peer_pid,
            )
        else:
            raise BoardControlRefused("Unsupported strategy command")
    except StrategyComparisonRefused as error:
        msg = str(error)
        low = msg.lower()
        if (
            "authority" in low
            or "authorized" in low
            or "grant is required" in low
            or "cannot self-activate" in low
        ):
            raise BoardControlForbidden(msg) from error
        if (
            "already in use" in low
            or "conflict" in low
            or "does not match expected precondition" in low
            or "does not match expected rollback target" in low
        ):
            raise BoardControlConflict(msg) from error
        raise BoardControlRefused(msg) from error

    return BoardCommandOutcome(
        command_id=safe_cmd,
        command_type=command_type,
        state="applied",
        disposition="applied",
        denial_code=None,
        effective_revision=1,
        recorded_at_ns=deployment.activated_at_ns,
        peer_pid=peer_pid,
        result={
            "deployment_id": deployment.deployment_id,
            "release_id": deployment.release_id,
            "status": deployment.status,
        },
    )


def _authorize_board_trading_command(
    db: Connection,
    company_id: str,
    *,
    operation: str,
    authorize: AuthorizeBoard | None,
    principal_hint: str | None,
) -> str:
    auth_fn = make_board_authorizer(
        authorize_board=authorize, principal_hint=principal_hint
    )
    actor_id = auth_fn(db, company_id, operation)
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise BoardControlForbidden(f"Board control authority required for {operation}")
    return actor_id.strip()


def _check_existing_receipt_and_charter(
    writer: WriterLease,
    *,
    command_id: str,
    command_type: str,
    charter_revision: str,
    payload_sha: str,
    preconditions: dict[str, Any],
    authorize: AuthorizeBoard | None,
    principal_hint: str | None,
    peer_pid: int | None,
) -> tuple[str, BoardCommandOutcome | None]:
    """Verify RBAC, idempotent receipt replay/conflict, and charter_revision precondition."""
    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise BoardControlForbidden("Company identity mismatch")
        actor_id = _authorize_board_trading_command(
            db,
            writer.company_id,
            operation=command_type,
            authorize=authorize,
            principal_hint=principal_hint,
        )
        existing = db.execute(
            """SELECT company_id, command_id, actor_id, peer_pid, command_type,
                      contract_version, payload_sha256, preconditions_json, state,
                      denial_code, result_json, applied_seq, created_at_ns, finalized_at_ns
               FROM command_receipt
               WHERE company_id = ? AND command_id = ?""",
            (writer.company_id, command_id),
        ).fetchone()
        if existing is not None:
            try:
                replayed, _ = admit_command_receipt(
                    db,
                    company_id=writer.company_id,
                    command_id=command_id,
                    actor_id=actor_id,
                    peer_pid=peer_pid,
                    command_type=command_type,
                    contract_version=EXECUTION_TIER_COMMAND,
                    payload_sha256=payload_sha,
                    preconditions=preconditions,
                    state=str(existing[8]),
                )
            except CommandConflict as error:
                raise BoardControlConflict(str(error)) from error
            saved_result = (
                json.loads(replayed.result_json) if replayed.result_json else {}
            )
            return actor_id, BoardCommandOutcome(
                command_id=replayed.command_id,
                command_type=replayed.command_type,
                state=replayed.state,
                disposition="applied" if replayed.state == "applied" else "denied",
                denial_code=replayed.denial_code,
                effective_revision=int(replayed.applied_seq or 0),
                recorded_at_ns=replayed.created_at_ns,
                peer_pid=replayed.peer_pid,
                result=saved_result,
            )

        if not is_domain_installed(db, "trading"):
            raise BoardControlRefused("Trading domain is not installed on this store")

        row = db.execute(
            "SELECT charter_revision FROM company WHERE id = ?", (writer.company_id,)
        ).fetchone()
        current_charter = str(row[0]) if row else ""
        if charter_revision != current_charter:
            now = time_ns()
            denied_result = {
                "expected_charter_revision": current_charter,
                "provided_charter_revision": charter_revision,
            }
            seq = append_audit_event(
                db,
                company_id=writer.company_id,
                domain="trading",
                entity_type="company",
                entity_id=writer.company_id,
                event_type=f"trading.{command_type}_denied_conflict",
                actor_id=actor_id,
                command_id=command_id,
                payload=denied_result,
                recorded_at_ns=now,
            )
            admit_command_receipt(
                db,
                company_id=writer.company_id,
                command_id=command_id,
                actor_id=actor_id,
                peer_pid=peer_pid,
                command_type=command_type,
                contract_version=EXECUTION_TIER_COMMAND,
                payload_sha256=payload_sha,
                preconditions=preconditions,
                state="denied",
                denial_code="conflicting_revision",
                result=denied_result,
                applied_seq=seq,
                now_ns=now,
            )
            writer.verify()
            return actor_id, BoardCommandOutcome(
                command_id=command_id,
                command_type=command_type,
                state="denied",
                disposition="denied",
                denial_code="conflicting_revision",
                effective_revision=seq,
                recorded_at_ns=now,
                peer_pid=peer_pid,
                result=denied_result,
            )
        return actor_id, None


def _finalize_applied_board_command(
    writer: WriterLease,
    *,
    command_id: str,
    command_type: str,
    actor_id: str,
    peer_pid: int | None,
    payload_sha: str,
    preconditions: dict[str, Any],
    entity_type: str,
    entity_id: str,
    event_type: str,
    result: dict[str, Any],
) -> BoardCommandOutcome:
    now = time_ns()
    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        seq = append_audit_event(
            db,
            company_id=writer.company_id,
            domain="trading",
            entity_type=entity_type,
            entity_id=entity_id,
            event_type=event_type,
            actor_id=actor_id,
            command_id=command_id,
            payload=result,
            recorded_at_ns=now,
        )
        admit_command_receipt(
            db,
            company_id=writer.company_id,
            command_id=command_id,
            actor_id=actor_id,
            peer_pid=peer_pid,
            command_type=command_type,
            contract_version=EXECUTION_TIER_COMMAND,
            payload_sha256=payload_sha,
            preconditions=preconditions,
            state="applied",
            denial_code=None,
            result=result,
            applied_seq=seq,
            now_ns=now,
        )
        writer.verify()
    return BoardCommandOutcome(
        command_id=command_id,
        command_type=command_type,
        state="applied",
        disposition="applied",
        denial_code=None,
        effective_revision=seq,
        recorded_at_ns=now,
        peer_pid=peer_pid,
        result=result,
    )


def execute_execution_tier_command(
    writer: WriterLease,
    *,
    command_id: str,
    command_type: str,
    preconditions: dict[str, Any],
    payload: dict[str, Any],
    authorize: AuthorizeBoard | None = None,
    principal_hint: str | None = None,
    peer_pid: int | None = None,
    quote_provider: MarketQuoteProvider | None = None,
    live_venue_resolver: Callable[[str], ExternalVenueAdapter | None] | None = None,
) -> BoardCommandOutcome:
    """Delegate execution-tier and Day-2 desk Board commands."""
    try:
        if command_type == PROMOTE_EXECUTION_ENVIRONMENT:
            return promote_execution_environment(
                writer,
                command_id=command_id,
                charter_revision=preconditions.get("charter_revision"),
                target_binding_id=preconditions.get("target_binding_id"),
                qualification_receipt_id=preconditions.get("qualification_receipt_id"),
                expected_payload_hash=preconditions.get("expected_payload_hash"),
                reason=payload.get("reason"),
                authorize=authorize,
                principal_hint=principal_hint,
                peer_pid=peer_pid,
            )
        if command_type == DEMOTE_LIVE_ENVIRONMENT:
            return demote_live_environment(
                writer,
                command_id=command_id,
                charter_revision=preconditions.get("charter_revision"),
                target_binding_id=preconditions.get("target_binding_id"),
                action=payload.get("action"),
                reason=payload.get("reason"),
                authorize=authorize,
                principal_hint=principal_hint,
                peer_pid=peer_pid,
            )
        if command_type == OPEN_COMPANY_PAPER_BOOK:
            cmd_id = _require_text(command_id, "command_id", max_len=128)
            charter = _require_text(
                preconditions.get("charter_revision"), "charter_revision", max_len=128
            )
            account_id = _require_text(
                payload.get("account_id"), "account_id", max_len=128
            )
            book_name = _require_text(
                payload.get("book_name"), "book_name", max_len=128
            )
            starting_minor = payload.get("starting_minor_units")
            exposure_minor = payload.get("exposure_limit_minor_units")
            if type(starting_minor) is not int or starting_minor <= 0:
                raise BoardControlRefused(
                    "starting_minor_units must be a positive integer"
                )
            if type(exposure_minor) is not int or exposure_minor <= 0:
                raise BoardControlRefused(
                    "exposure_limit_minor_units must be a positive integer"
                )
            currency = _require_text(
                payload.get("currency", "USD"), "currency", max_len=32
            )
            raw_release = payload.get("strategy_release_id")
            strategy_release_id = (
                _require_text(raw_release, "strategy_release_id", max_len=128)
                if raw_release is not None and str(raw_release).strip()
                else None
            )
            inst_digest = _require_text(
                payload.get("instrument_rules_digest", "btc-usdt-spot-v1"),
                "instrument_rules_digest",
                max_len=128,
            )
            fee_bps = payload.get("fee_bps", 4)
            slippage_bps = payload.get("slippage_bps", 2)
            if type(fee_bps) is not int or type(slippage_bps) is not int:
                raise BoardControlRefused("fee_bps and slippage_bps must be integers")
            reason = (
                _require_text(payload.get("reason"), "reason", max_len=500)
                if payload.get("reason") is not None
                else f"Board opened paper book '{book_name}'"
            )
            norm_pre = {"charter_revision": charter}
            norm_payload = {
                "account_id": account_id,
                "book_name": book_name,
                "starting_minor_units": starting_minor,
                "exposure_limit_minor_units": exposure_minor,
                "currency": currency,
                "strategy_release_id": strategy_release_id,
                "instrument_rules_digest": inst_digest,
                "fee_bps": fee_bps,
                "slippage_bps": slippage_bps,
                "reason": reason,
            }
            payload_sha = _sha256_json(
                {
                    "command_type": command_type,
                    "preconditions": norm_pre,
                    "payload": norm_payload,
                }
            )
            actor_id, early = _check_existing_receipt_and_charter(
                writer,
                command_id=cmd_id,
                command_type=command_type,
                charter_revision=charter,
                payload_sha=payload_sha,
                preconditions=norm_pre,
                authorize=authorize,
                principal_hint=principal_hint,
                peer_pid=peer_pid,
            )
            if early is not None:
                return early

            binding = open_company_paper_book(
                writer,
                account_id=account_id,
                book_name=book_name,
                strategy_release_id=strategy_release_id,
                instrument_rules_digest=inst_digest,
                currency=currency,
                starting_minor_units=starting_minor,
                exposure_limit_minor_units=exposure_minor,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
            )
            result = {
                "binding_id": binding.binding_id,
                "account_id": binding.account_id,
                "book_name": binding.book_name,
                "strategy_release_id": binding.strategy_release_id,
                "starting_minor_units": binding.starting_minor_units,
                "exposure_limit_minor_units": binding.exposure_limit_minor_units,
                "currency": binding.currency,
                "reason": reason,
            }
            return _finalize_applied_board_command(
                writer,
                command_id=cmd_id,
                command_type=command_type,
                actor_id=actor_id,
                peer_pid=peer_pid,
                payload_sha=payload_sha,
                preconditions=norm_pre,
                entity_type="execution_environment_binding",
                entity_id=binding.binding_id,
                event_type="trading.paper_book_opened_by_board",
                result=result,
            )
        if command_type == RECONCILE_TRADING_BOOKS:
            cmd_id = _require_text(command_id, "command_id", max_len=128)
            charter = _require_text(
                preconditions.get("charter_revision"), "charter_revision", max_len=128
            )
            max_age = payload.get("max_quote_age_seconds", 60)
            if type(max_age) is not int or not 1 <= max_age <= 86400:
                raise BoardControlRefused(
                    "max_quote_age_seconds must be an integer between 1 and 86400"
                )
            reason = (
                _require_text(payload.get("reason"), "reason", max_len=500)
                if payload.get("reason") is not None
                else "Board requested on-demand trading mark-to-market and reconciliation"
            )
            norm_pre = {"charter_revision": charter}
            norm_payload = {
                "max_quote_age_seconds": max_age,
                "reason": reason,
            }
            payload_sha = _sha256_json(
                {
                    "command_type": command_type,
                    "preconditions": norm_pre,
                    "payload": norm_payload,
                }
            )
            actor_id, early = _check_existing_receipt_and_charter(
                writer,
                command_id=cmd_id,
                command_type=command_type,
                charter_revision=charter,
                payload_sha=payload_sha,
                preconditions=norm_pre,
                authorize=authorize,
                principal_hint=principal_hint,
                peer_pid=peer_pid,
            )
            if early is not None:
                return early

            summary = reconcile_trading_books(
                writer,
                quote_provider=quote_provider,
                live_venue_resolver=live_venue_resolver,
                max_quote_age_ns=max_age * 10**9,
            )
            result = summary | {"reason": reason}
            return _finalize_applied_board_command(
                writer,
                command_id=cmd_id,
                command_type=command_type,
                actor_id=actor_id,
                peer_pid=peer_pid,
                payload_sha=payload_sha,
                preconditions=norm_pre,
                entity_type="company",
                entity_id=writer.company_id,
                event_type="trading.reconciled_by_board",
                result=result,
            )
    except ExecutionTierRefused as error:
        raise BoardControlRefused(str(error)) from error
    raise BoardControlRefused("Unsupported execution tier command")


__all__ = [
    "ACTIVATE_STRATEGY_RELEASE",
    "DEMOTE_LIVE_ENVIRONMENT",
    "DEMOTE_PRECONDITION_KEYS",
    "EXECUTION_TIER_COMMAND",
    "OPEN_COMPANY_PAPER_BOOK",
    "OPEN_PAPER_BOOK_PRECONDITION_KEYS",
    "PROMOTE_EXECUTION_ENVIRONMENT",
    "PROMOTE_PRECONDITION_KEYS",
    "RECONCILE_TRADING_BOOKS",
    "RECONCILE_TRADING_PRECONDITION_KEYS",
    "ROLLBACK_STRATEGY_DEPLOYMENT",
    "STRATEGY_COMMAND",
    "execute_execution_tier_command",
    "execute_strategy_command",
]
