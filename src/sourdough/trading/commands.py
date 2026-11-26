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
from ..strategy_comparison import (
    StrategyComparisonRefused,
    activate_strategy_release,
    rollback_strategy_deployment,
)
from ..writer_lease import WriterLease

STRATEGY_COMMAND = "strategy-command-v1"
ACTIVATE_STRATEGY_RELEASE = "activate_strategy_release"
ROLLBACK_STRATEGY_DEPLOYMENT = "rollback_strategy_deployment"


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
