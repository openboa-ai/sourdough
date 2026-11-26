"""Unified Trading risk gating over scoped Core restriction_cause records."""

from __future__ import annotations

from pathlib import Path
from sqlite3 import Connection
from typing import Any

from ..board_control import BoardControlRefused
from ..storage.schema import is_domain_installed
from .domain import ENV_LIVE, ENV_PAPER, ExecutionTierRefused

TRADING_RISK_CAUSE_CATEGORY = "trading_risk_pause"
SCOPE_EXECUTION_ALL = "execution:all"
SCOPE_EXECUTION_PAPER = "execution:paper"
SCOPE_EXECUTION_LIVE = "execution:live"
TRADING_RISK_SCOPES = frozenset(
    {SCOPE_EXECUTION_ALL, SCOPE_EXECUTION_PAPER, SCOPE_EXECUTION_LIVE}
)
QUOTE_FRESHNESS_MAX_NS = 15 * 10**9


def scopes_for_environment(environment: str) -> tuple[str, ...]:
    """Return all restriction_cause scope_refs that gate the given execution environment."""
    if environment == ENV_PAPER:
        return ("company:all", SCOPE_EXECUTION_ALL, SCOPE_EXECUTION_PAPER)
    if environment == ENV_LIVE:
        return ("company:all", SCOPE_EXECUTION_ALL, SCOPE_EXECUTION_LIVE)
    return ()


def active_trading_risk_restrictions(
    db: Connection, company_id: str, environment: str
) -> list[dict[str, Any]]:
    """Return active restriction_cause rows gating the target environment."""
    scopes = scopes_for_environment(environment)
    if not scopes:
        return []
    placeholders = ", ".join("?" for _ in scopes)
    rows = db.execute(
        f"""SELECT restriction_id, cause_category, scope_ref, restriction_revision,
                   operator_releasable, reason, recorded_at_ns
            FROM restriction_cause
            WHERE company_id = ? AND active = 1
              AND scope_ref IN ({placeholders})
            ORDER BY restriction_revision DESC, restriction_id ASC""",
        (company_id, *scopes),
    ).fetchall()
    return [
        {
            "restriction_id": str(r[0]),
            "cause_category": str(r[1]),
            "scope_ref": str(r[2]),
            "restriction_revision": int(r[3]),
            "operator_releasable": bool(r[4]),
            "reason": str(r[5]),
            "recorded_at_ns": int(r[6]),
        }
        for r in rows
    ]


def is_environment_suspended(db: Connection, company_id: str, environment: str) -> bool:
    """Return True if any active restriction gates the specified environment."""
    return bool(active_trading_risk_restrictions(db, company_id, environment))


def require_trading_risk_clear(
    db: Connection, company_id: str, environment: str
) -> None:
    """Raise ExecutionTierRefused if any active restriction gates the target environment."""
    active = active_trading_risk_restrictions(db, company_id, environment)
    if active:
        first = active[0]
        raise ExecutionTierRefused(
            f"{environment} execution blocked by active restriction "
            f"{first['restriction_id']} ({first['scope_ref']}): {first['reason']}"
        )


def _target_environments_for_scope(scope_ref: str) -> tuple[str, ...]:
    if scope_ref == SCOPE_EXECUTION_PAPER:
        return (ENV_PAPER,)
    if scope_ref == SCOPE_EXECUTION_LIVE:
        return (ENV_LIVE,)
    if scope_ref in (SCOPE_EXECUTION_ALL, "company:all"):
        return (ENV_PAPER, ENV_LIVE)
    raise BoardControlRefused(f"Unsupported execution restriction scope '{scope_ref}'")


def verify_trading_restriction_clear(
    db: Connection,
    *,
    company_id: str,
    scope_ref: str,
    cause_category: str,
    now_ns: int,
    engine_directory: Path | None = None,
) -> None:
    """Enforce fail-closed preconditions before clearing an execution:* or trading_risk_pause restriction."""
    norm_scope = scope_ref.strip().lower()
    norm_category = cause_category.strip().lower()
    if not (
        norm_scope.startswith("execution:")
        or norm_category == TRADING_RISK_CAUSE_CATEGORY
    ):
        return
    if not is_domain_installed(db, "trading"):
        raise BoardControlRefused(
            "Domain 'trading' is not installed on this company store"
        )
    envs = _target_environments_for_scope(norm_scope)
    env_placeholders = ", ".join("?" for _ in envs)

    open_orders = int(
        db.execute(
            f"""SELECT COUNT(*) FROM trading_book_order
                WHERE company_id = ?
                  AND environment IN ({env_placeholders})
                  AND status = 'open'""",
            (company_id, *envs),
        ).fetchone()[0]
    )
    if open_orders > 0:
        raise BoardControlRefused(
            f"Cannot resume trading execution while {open_orders} open order(s) remain unsettled"
        )

    if ENV_LIVE in envs:
        unresolved_ext = int(
            db.execute(
                """SELECT COUNT(*) FROM external_actions
                   WHERE company_id = ?
                     AND status IN ('prepared', 'in_flight', 'unknown')""",
                (company_id,),
            ).fetchone()[0]
        )
        if unresolved_ext > 0:
            raise BoardControlRefused(
                f"Cannot resume live trading while {unresolved_ext} external action(s) remain unresolved"
            )

    # Enforce <= 15s quote freshness precondition when clearing execution:* / trading_risk_pause.
    if engine_directory is not None:
        from .engine import VERSION
        from .execution import VenueRefused
        from .nautilus_paper import NautilusPaperVenue

        sandbox_db = (
            engine_directory.resolve() / f"nautilus-{VERSION}" / "sandbox.sqlite"
        )
        if sandbox_db.is_file():
            try:
                venue = NautilusPaperVenue(sandbox_db)
                with venue._connect("ro") as vconn:
                    vconn.execute("BEGIN")
                    qrow = vconn.execute(
                        "SELECT event_ns, exchange_trade_ns FROM sandbox_quote LIMIT 1"
                    ).fetchone()
                if qrow is None:
                    raise BoardControlRefused(
                        "Cannot resume trading execution without fresh quote (<= 15s)"
                    )
                observed = min(int(qrow[0]), int(qrow[1]))
                if observed > now_ns or now_ns - observed > QUOTE_FRESHNESS_MAX_NS:
                    raise BoardControlRefused(
                        "Cannot resume trading execution without fresh quote (<= 15s)"
                    )
                return
            except VenueRefused as error:
                raise BoardControlRefused(
                    f"Cannot resume trading execution: {error}"
                ) from error

    active_bindings = int(
        db.execute(
            f"""SELECT COUNT(*) FROM execution_environment_binding
                WHERE company_id = ?
                  AND environment IN ({env_placeholders})
                  AND status = 'active'""",
            (company_id, *envs),
        ).fetchone()[0]
    )
    if active_bindings == 0:
        return

    latest_order_quote = db.execute(
        f"""SELECT MAX(quote_observed_at_ns) FROM trading_book_order
            WHERE company_id = ? AND environment IN ({env_placeholders})""",
        (company_id, *envs),
    ).fetchone()[0]
    latest_val_ns = db.execute(
        f"""SELECT MAX(recorded_at_ns) FROM trading_book_valuation
            WHERE company_id = ? AND environment IN ({env_placeholders})""",
        (company_id, *envs),
    ).fetchone()[0]

    candidates = [int(v) for v in (latest_order_quote, latest_val_ns) if v is not None]
    if not candidates:
        raise BoardControlRefused(
            "Cannot resume trading execution without fresh quote (<= 15s)"
        )
    latest_quote_ns = max(candidates)
    if latest_quote_ns > now_ns or now_ns - latest_quote_ns > QUOTE_FRESHNESS_MAX_NS:
        raise BoardControlRefused(
            "Cannot resume trading execution without fresh quote (<= 15s)"
        )


class TradingRestrictionGuard:
    """Trading-registered RestrictionGuard validating execution:* restriction apply/clear."""

    def handles_scope(self, scope_ref: str) -> bool:
        norm_scope = scope_ref.strip().lower()
        return norm_scope.startswith("execution:") or norm_scope == "company:all"

    def validate_apply(
        self,
        db: Connection,
        *,
        company_id: str,
        scope_ref: str,
        cause_category: str,
    ) -> None:
        del company_id
        norm_scope = scope_ref.strip().lower()
        norm_category = cause_category.strip().lower()
        if not (
            norm_scope.startswith("execution:")
            or norm_category == TRADING_RISK_CAUSE_CATEGORY
        ):
            return
        if not is_domain_installed(db, "trading"):
            raise BoardControlRefused(
                "Domain 'trading' is not installed on this company store"
            )
        if norm_scope not in (
            "company:all",
            SCOPE_EXECUTION_ALL,
            SCOPE_EXECUTION_PAPER,
            SCOPE_EXECUTION_LIVE,
        ):
            raise BoardControlRefused(
                f"Unsupported execution restriction scope '{scope_ref}'"
            )

    def validate_clear(
        self,
        db: Connection,
        *,
        company_id: str,
        scope_ref: str,
        cause_category: str,
        now_ns: int,
        engine_directory: Path | None = None,
    ) -> None:
        verify_trading_restriction_clear(
            db,
            company_id=company_id,
            scope_ref=scope_ref,
            cause_category=cause_category,
            now_ns=now_ns,
            engine_directory=engine_directory,
        )
