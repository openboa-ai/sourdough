"""Trading domain Board commands contributed to CompanyDaemon."""

from __future__ import annotations

from sqlite3 import Connection
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
from ..writer_lease import WriterLease
from .accounts import demote_live_environment, promote_execution_environment
from .domain import (
    DEMOTE_LIVE_ENVIRONMENT,
    EXECUTION_TIER_COMMAND,
    PROMOTE_EXECUTION_ENVIRONMENT,
    ExecutionTierRefused,
)
from .strategy_comparison import (
    StrategyComparisonRefused,
    activate_strategy_release,
    rollback_strategy_deployment,
)

STRATEGY_COMMAND = "strategy-command-v1"
ACTIVATE_STRATEGY_RELEASE = "activate_strategy_release"
ROLLBACK_STRATEGY_DEPLOYMENT = "rollback_strategy_deployment"

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
            release_id = _require_text(
                payload.get("release_id"), "release_id", max_len=128
            )
            binding_rev = _require_text(
                preconditions.get("binding_revision"), "binding_revision", max_len=128
            )
            comparison_id = payload.get("comparison_id")
            expected_active = preconditions.get("expected_active_deployment_id")
            deployment = activate_strategy_release(
                writer,
                command_id=safe_cmd,
                release_id=release_id,
                binding_revision=binding_rev,
                authorize_deploy=_deploy_auth,
                comparison_id=comparison_id,
                expected_active_deployment_id=expected_active,
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
        if "already in use" in low or "conflict" in low:
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
) -> BoardCommandOutcome:
    """Delegate execution-tier promotion/demotion commands to trading.accounts."""
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
    except ExecutionTierRefused as error:
        raise BoardControlRefused(str(error)) from error
    raise BoardControlRefused("Unsupported execution tier command")


__all__ = [
    "ACTIVATE_STRATEGY_RELEASE",
    "DEMOTE_LIVE_ENVIRONMENT",
    "DEMOTE_PRECONDITION_KEYS",
    "EXECUTION_TIER_COMMAND",
    "PROMOTE_EXECUTION_ENVIRONMENT",
    "PROMOTE_PRECONDITION_KEYS",
    "ROLLBACK_STRATEGY_DEPLOYMENT",
    "STRATEGY_COMMAND",
    "execute_execution_tier_command",
    "execute_strategy_command",
]
