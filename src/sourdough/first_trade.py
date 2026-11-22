"""Run the first company duty through a scoped paper-order tool."""

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .company import _connect
from .first_duty import DutyClaim, claim_first_duty
from .paper_account import reopen_binding
from .paper_books import Posting, position_quantity, post_confirmed_fill
from .paper_order import PaperIntent, submit_paper_order
from .paper_research import (
    ResearchDecisionReceipt,
    ResearchTools,
    find_unsubmitted_opening,
    reopen_research_decision,
    requires_research,
)
from .paper_venue import FixedQuoteVenue
from .writer_lease import WriterLease


class TradeRefused(Exception):
    """The initial trading duty cannot use this operating configuration."""


@dataclass(frozen=True)
class TradingContext:
    claim: DutyClaim
    account_id: str
    instrument_rules_digest: str
    quote_minor_units: int
    exposure_limit_minor_units: int


@dataclass(frozen=True)
class ToolResult:
    order_id: str
    posting: Posting | None


OrderTool = Callable[[str, int, int], ToolResult]


class EmployeeRuntime(Protocol):
    profile: str

    def check_ready(self) -> None: ...

    def run(self, context: TradingContext, order_tool: OrderTool) -> None: ...

    def research(self, invoke: Callable[[str, dict], dict]) -> dict: ...


@dataclass(frozen=True)
class TradeResult:
    claim: DutyClaim
    order: ToolResult | None
    position_quantity: int
    research: ResearchDecisionReceipt | None = None


def run_first_trade(
    store: Path,
    company_id: str,
    venue: FixedQuoteVenue,
    runtime: EmployeeRuntime,
    *,
    writer: WriterLease | None = None,
) -> TradeResult:
    if store.resolve() == venue.journal.resolve() or (
        store.exists() and venue.journal.exists() and store.samefile(venue.journal)
    ):
        raise TradeRefused("Company store and venue journal must be separate")
    binding = reopen_binding(store, company_id)
    account = binding.account
    if venue.observe_account(account.account_id) != account:
        raise TradeRefused("Paper destination does not match the bound account")
    quote = venue.observe_quote(account.account_id)
    with closing(_connect(store, "ro")) as db:
        configured = db.execute(
            "SELECT execution_profile FROM founder WHERE company_id = ?", (company_id,)
        ).fetchone()
        research_required = requires_research(db, company_id)
    if configured != (runtime.profile,):
        raise TradeRefused("Employee process does not match the configured profile")
    runtime.check_ready()
    if writer is not None:
        writer.verify_for(store, company_id)
    claim = claim_first_duty(store, company_id, runtime.profile)
    context = TradingContext(
        claim,
        account.account_id,
        account.instrument_rules_digest,
        quote,
        binding.exposure_limit_minor_units,
    )
    called = False
    result = None

    def order_tool(call_id: str, quantity: int, limit_minor_units: int) -> ToolResult:
        nonlocal called, result
        if called:
            raise TradeRefused("This duty permits at most one order call")
        called = True
        intent = PaperIntent(
            call_id, account.instrument_rules_digest, quantity, limit_minor_units
        )
        result = _apply_paper_order(store, claim, intent, venue, writer)
        return result

    research = None
    if research_required:
        tools = ResearchTools(
            store,
            claim,
            quote,
            venue.observe_fee_bps(account.account_id),
            binding.exposure_limit_minor_units,
            writer,
        )
        try:
            runtime.research(tools.invoke)
        except TradeRefused:
            if find_unsubmitted_opening(store, company_id) is None:
                raise
        research = reopen_research_decision(store, claim)
        if research is None:
            raise TradeRefused("The opening research decision was not committed")
        if research.decision.action == "buy":
            order_tool(
                research.decision_id,
                research.decision.quantity,
                research.decision.limit_minor_units,
            )
    else:
        runtime.run(context, order_tool)
    return TradeResult(claim, result, position_quantity(store, company_id), research)


def resume_opening_research(
    writer: WriterLease, venue: FixedQuoteVenue
) -> TradeResult | None:
    writer.verify()
    store, company_id = writer.store, writer.company_id
    pending = find_unsubmitted_opening(store, company_id)
    if pending is None:
        return None
    claim, research = pending
    result = None
    if research.decision.action == "buy":
        account = reopen_binding(store, company_id).account
        intent = PaperIntent(
            research.decision_id,
            account.instrument_rules_digest,
            research.decision.quantity,
            research.decision.limit_minor_units,
        )
        result = _apply_paper_order(store, claim, intent, venue, writer)
    return TradeResult(claim, result, position_quantity(store, company_id), research)


def _apply_paper_order(
    store: Path,
    claim: DutyClaim,
    intent: PaperIntent,
    venue: FixedQuoteVenue,
    writer: WriterLease | None,
) -> ToolResult:
    receipt = submit_paper_order(store, claim, intent, venue, writer=writer)
    posting = post_confirmed_fill(
        store, claim.company_id, receipt.order_id, venue, writer=writer
    )
    return ToolResult(receipt.order_id, posting)
