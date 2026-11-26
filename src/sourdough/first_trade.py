"""Run the first company duty through a scoped paper-order tool."""

from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .company import _connect, founding_execution_profile
from .duty_run import DutyClaim
from .employee_process import EmployeeProcess, EmployeeProcessRefused
from .paper_account import reopen_binding
from .paper_books import Posting, post_confirmed_fill
from .paper_execution import (
    PaperExecution,
    PaperIntent,
    PaperOrderSizing,
    observe_bound_position,
)
from .paper_order import OrderReplyUnknown, submit_paper_order
from .paper_research import (
    ResearchDecisionReceipt,
    ResearchTools,
    find_unsubmitted_opening,
    reopen_research_decision,
    requires_research,
)
from .trading.duty import claim_paper_first_duty
from .writer_lease import WriterLease


class TradeRefused(EmployeeProcessRefused):
    """The initial trading duty cannot use this operating configuration."""


@dataclass(frozen=True)
class TradingContext:
    claim: DutyClaim
    account_id: str
    instrument_rules_digest: str
    quote_minor_units: int
    exposure_limit_minor_units: int
    order_sizing: PaperOrderSizing | None = None


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
    position_quantity: int | None
    research: ResearchDecisionReceipt | None = None


def run_first_trade(
    store: Path,
    company_id: str,
    venue: PaperExecution,
    runtime: EmployeeRuntime,
    *,
    writer: WriterLease | None = None,
) -> TradeResult:
    if writer is None:
        from .runtime.engine import CompanyRuntime, build_default_registry
        from .runtime.registry import DutyOutcome

        registry = build_default_registry(
            {"paper.first_trade": lambda _ctx: DutyOutcome("completed")}
        )
        return CompanyRuntime(store, company_id, registry).run_bound_handler(
            "paper.first_trade",
            lambda lease: _run_first_trade_bound(
                store, company_id, venue, runtime, writer=lease
            ),
            process_guard=venue,
        )
    return _run_first_trade_bound(store, company_id, venue, runtime, writer=writer)


def _run_first_trade_bound(
    store: Path,
    company_id: str,
    venue: PaperExecution,
    runtime: EmployeeRuntime,
    *,
    writer: WriterLease | None = None,
) -> TradeResult:
    venue.require_independent_from(store)
    binding = reopen_binding(store, company_id)
    account = binding.account
    if venue.observe_account(account.account_id) != account:
        raise TradeRefused("Paper destination does not match the bound account")
    sizing = venue.observe_order_sizing(account.account_id)
    with closing(_connect(store, "ro")) as db:
        configured = founding_execution_profile(db, company_id)
        research_required = requires_research(db, company_id)
    quote, replay_cut = (
        venue.observe_research_quote(account.account_id)
        if research_required
        else (venue.observe_quote(account.account_id), None)
    )
    if configured != runtime.profile:
        raise TradeRefused("Employee process does not match the configured profile")
    runtime.check_ready()
    if writer is not None:
        writer.verify_for(store, company_id)
    claim = claim_paper_first_duty(store, company_id, runtime.profile)
    context = TradingContext(
        claim,
        account.account_id,
        account.instrument_rules_digest,
        quote,
        binding.exposure_limit_minor_units,
        sizing,
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
            sizing=sizing,
            replay_cut=replay_cut,
        )
        try:
            if isinstance(runtime, EmployeeProcess):
                runtime.scoped_tool_exchange("research_duty", tools.invoke, timeout=100)
            else:
                runtime.research(tools.invoke)
        except EmployeeProcessRefused:
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
        if isinstance(runtime, EmployeeProcess):
            response = runtime.run_first_duty_exchange(
                context=asdict(context), tools=("paper_buy",)
            )
            if response != {"action": "hold"}:
                if (
                    set(response)
                    != {
                        "tool",
                        "call_id",
                        "quantity",
                        "limit_minor_units",
                    }
                    or response["tool"] != "paper_buy"
                ):
                    raise TradeRefused("Unexpected employee tool request")
                order_tool(
                    response["call_id"],
                    response["quantity"],
                    response["limit_minor_units"],
                )
        else:
            runtime.run(context, order_tool)
    return _trade_result(store, claim, result, venue, research)


def resume_opening_research(
    writer: WriterLease, venue: PaperExecution, *, hired: bool = False
) -> TradeResult | None:
    writer.verify()
    store, company_id = writer.store, writer.company_id
    pending = find_unsubmitted_opening(store, company_id, hired=hired)
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
    return _trade_result(store, claim, result, venue, research)


def _trade_result(
    store: Path,
    claim: DutyClaim,
    result: ToolResult | None,
    venue: PaperExecution,
    research: ResearchDecisionReceipt | None,
) -> TradeResult:
    position = None
    if result is not None:
        account = reopen_binding(store, claim.company_id).account
        position = observe_bound_position(venue, account, result.order_id)
    return TradeResult(
        claim,
        result,
        position.position_quantity if position is not None else None,
        research,
    )


def _apply_paper_order(
    store: Path,
    claim: DutyClaim,
    intent: PaperIntent,
    venue: PaperExecution,
    writer: WriterLease | None,
) -> ToolResult:
    try:
        receipt = submit_paper_order(store, claim, intent, venue, writer=writer)
    except OrderReplyUnknown as error:
        return ToolResult(error.order_id, None)
    try:
        posting = post_confirmed_fill(
            store, claim.company_id, receipt.order_id, venue, writer=writer
        )
    except TimeoutError:
        posting = None
    return ToolResult(receipt.order_id, posting)
