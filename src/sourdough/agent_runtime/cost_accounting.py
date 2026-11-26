"""Deterministic integer LLM compute cost calculation and General Ledger settlement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256

from ..general_ledger import JournalEntry, JournalLine, post_journal
from ..writer_lease import WriterLease
from .protocol import (
    PROVIDER_ANTHROPIC,
    PROVIDER_CODEX_CLI,
    PROVIDER_GEMINI,
    PROVIDER_OPENAI,
    AgentBudgetExceeded,
    AgentProfileBinding,
    TokenUsage,
)

# Costs expressed in micro-units per 1,000 tokens (1 minor unit = 10,000 micro-units when USD cents).
# Example: $2.50 / 1M input tokens = 0.25 cents / 1K tokens = 2,500 micro-cents / 1K tokens.
_MICRO_SCALE = 10_000


@dataclass(frozen=True)
class ModelPricing:
    """Exact integer pricing per 1,000 tokens in micro-minor-units."""

    input_micro_per_1k: int
    output_micro_per_1k: int
    currency: str = "USD"
    minimum_charge_minor: int = 1


DEFAULT_MODEL_PRICING: dict[str, ModelPricing] = {
    PROVIDER_CODEX_CLI: ModelPricing(
        input_micro_per_1k=2_500, output_micro_per_1k=10_000
    ),
    PROVIDER_OPENAI: ModelPricing(input_micro_per_1k=2_500, output_micro_per_1k=10_000),
    PROVIDER_ANTHROPIC: ModelPricing(
        input_micro_per_1k=3_000, output_micro_per_1k=15_000
    ),
    PROVIDER_GEMINI: ModelPricing(input_micro_per_1k=1_250, output_micro_per_1k=5_000),
}


@dataclass(frozen=True)
class TurnCostSettlement:
    run_id: str
    turn_id: str
    provider: str
    model: str
    usage: TokenUsage
    cost_minor_units: int
    released_unused_minor_units: int
    journal_entry_id: str | None


def compute_usage_cost_minor_units(usage: TokenUsage, pricing: ModelPricing) -> int:
    """Compute rounded-up integer minor-unit cost from exact token counts."""
    if usage.input_tokens == 0 and usage.output_tokens == 0:
        return 0
    total_micro = (
        usage.input_tokens * pricing.input_micro_per_1k
        + usage.output_tokens * pricing.output_micro_per_1k
    )
    # Divide by (1000 * _MICRO_SCALE) with ceiling rounding.
    denom = 1_000 * _MICRO_SCALE
    minor = (total_micro + denom - 1) // denom
    return max(pricing.minimum_charge_minor, minor)


def settle_shift_cost_to_ledger(
    writer: WriterLease,
    *,
    profile: AgentProfileBinding,
    run_id: str,
    turn_id: str,
    usage: TokenUsage,
    budget_ceiling_minor_units: int,
    pricing: ModelPricing | None = None,
    effective_date: date | None = None,
) -> TurnCostSettlement:
    """Settle shift token usage into General Ledger idempotently and verify budget ceiling."""
    resolved_pricing = pricing or DEFAULT_MODEL_PRICING[profile.provider]
    cost_minor = compute_usage_cost_minor_units(usage, resolved_pricing)
    if budget_ceiling_minor_units > 0 and cost_minor > budget_ceiling_minor_units:
        raise AgentBudgetExceeded(
            f"Shift compute cost ({cost_minor}) exceeded authorized ceiling ({budget_ceiling_minor_units})"
        )
    released = max(0, budget_ceiling_minor_units - cost_minor)
    if cost_minor == 0:
        return TurnCostSettlement(
            run_id=run_id,
            turn_id=turn_id,
            provider=profile.provider,
            model=profile.model,
            usage=usage,
            cost_minor_units=0,
            released_unused_minor_units=released,
            journal_entry_id=None,
        )

    evidence_rev = sha256(
        f"{profile.profile_id()}:{run_id}:{turn_id}:{usage.input_tokens}:{usage.output_tokens}:{cost_minor}".encode(
            "utf-8"
        )
    ).hexdigest()
    entry_date = datetime.now(UTC).date() if effective_date is None else effective_date
    entry = JournalEntry(
        company_id=writer.company_id,
        source=f"agent_llm:{profile.provider}",
        source_event=f"llm-turn:{run_id}:{turn_id}",
        evidence_revision=evidence_rev,
        effective_date=entry_date,
        currency=resolved_pricing.currency,
        mapping_revision="agent-llm-cost-v1",
        lines=(
            JournalLine("expense:agent_llm_compute", cost_minor),
            JournalLine("liability:accounts_payable", -cost_minor),
        ),
    )
    journal_id = post_journal(writer, entry)
    return TurnCostSettlement(
        run_id=run_id,
        turn_id=turn_id,
        provider=profile.provider,
        model=profile.model,
        usage=usage,
        cost_minor_units=cost_minor,
        released_unused_minor_units=released,
        journal_entry_id=journal_id,
    )
