"""Trading domain module plugging Trading capabilities into Core Company OS."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .duty import (
    FIRST_DUTY_SCOPE,
    PAPER_EXECUTION,
    POSITION_REVIEW,
    TRADING_WORK_POLICY,
    allow_trading_review_transition,
    claim_bound_position_review,
    claim_paper_first_duty,
    claim_paper_follow_up,
    finish_paper_first_duty,
    has_trading_committed_result,
    require_historical_paper_claim,
    require_paper_authority,
    resolve_paper_first_run,
    settle_paper_review,
)

if TYPE_CHECKING:
    from .plugin import (
        DUTY_QUANT_AGENT_SANDBOX,
        DUTY_QUANT_PAPER_DECISION,
        DUTY_QUANT_RECONCILE,
        TradingDomainPlugin,
        create_domain_plugin,
    )

_PLUGIN_EXPORTS = frozenset(
    {
        "DUTY_QUANT_AGENT_SANDBOX",
        "DUTY_QUANT_PAPER_DECISION",
        "DUTY_QUANT_RECONCILE",
        "TradingDomainPlugin",
        "create_domain_plugin",
    }
)


def __getattr__(name: str) -> Any:
    if name in _PLUGIN_EXPORTS:
        from . import plugin

        return getattr(plugin, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DUTY_QUANT_AGENT_SANDBOX",
    "DUTY_QUANT_PAPER_DECISION",
    "DUTY_QUANT_RECONCILE",
    "FIRST_DUTY_SCOPE",
    "PAPER_EXECUTION",
    "POSITION_REVIEW",
    "TRADING_WORK_POLICY",
    "TradingDomainPlugin",
    "allow_trading_review_transition",
    "claim_bound_position_review",
    "claim_paper_first_duty",
    "claim_paper_follow_up",
    "create_domain_plugin",
    "finish_paper_first_duty",
    "has_trading_committed_result",
    "require_historical_paper_claim",
    "require_paper_authority",
    "resolve_paper_first_run",
    "settle_paper_review",
]
