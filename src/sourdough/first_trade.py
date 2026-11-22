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


@dataclass(frozen=True)
class TradeResult:
    claim: DutyClaim
    order: ToolResult | None
    position_quantity: int


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
        receipt = submit_paper_order(store, claim, intent, venue, writer=writer)
        posting = post_confirmed_fill(
            store, company_id, receipt.order_id, venue, writer=writer
        )
        result = ToolResult(receipt.order_id, posting)
        return result

    runtime.run(context, order_tool)
    return TradeResult(claim, result, position_quantity(store, company_id))
