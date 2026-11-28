"""Server-bound Outer Capability Broker handler for per-Agent isolated sandboxes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..writer_lease import WriterLease
from .accounts import open_agent_sandbox
from .company_tools import opaque_trading_action_id
from .domain import ExecutionTierRefused
from .ledger import (
    cancel_agent_sandbox_order,
    read_agent_sandbox_portfolio,
    submit_agent_sandbox_order,
)


@dataclass(frozen=True)
class AgentSandboxTools:
    """Server-bound Outer Capability Broker handler for one Agent's private sandbox.

    Identity is strictly fixed to `agent_id` by the host; tool arguments can never
    supply or override `agent_id`, `company_id`, or target company_paper / company_live.
    """

    writer: WriterLease
    agent_id: str

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ExecutionTierRefused("Expected JSON object tool arguments")
        if "agent_id" in arguments or "company_id" in arguments or "tier" in arguments:
            raise ExecutionTierRefused(
                "Server-bound identity fields cannot be supplied in sandbox tool arguments"
            )
        if name == "open_agent_sandbox":
            allowed = {
                "account_id",
                "currency",
                "starting_minor_units",
                "exposure_limit_minor_units",
                "fee_bps",
                "slippage_bps",
                "quote_source",
            }
            if not set(arguments).issubset(allowed) or "account_id" not in arguments:
                raise ExecutionTierRefused("Invalid open_agent_sandbox arguments")
            binding = open_agent_sandbox(
                self.writer, agent_id=self.agent_id, **arguments
            )
            return asdict(binding)
        if name == "submit_agent_sandbox_order":
            allowed = {
                "action_id",
                "instrument_id",
                "side",
                "quantity",
                "limit_minor_units",
                "market_price_minor_units",
                "quote_observed_at_ns",
                "fill_mode",
            }
            required = {
                "action_id",
                "instrument_id",
                "side",
                "quantity",
                "limit_minor_units",
                "market_price_minor_units",
            }
            if not set(arguments).issubset(allowed) or not required.issubset(arguments):
                raise ExecutionTierRefused(
                    "Invalid submit_agent_sandbox_order arguments"
                )
            sanitized_args = dict(arguments)
            sanitized_args["action_id"] = opaque_trading_action_id(
                self.writer.company_id, self.agent_id, arguments["action_id"]
            )
            receipt = submit_agent_sandbox_order(
                self.writer, agent_id=self.agent_id, **sanitized_args
            )
            return asdict(receipt)
        if name == "cancel_agent_sandbox_order":
            if set(arguments) != {"order_id"}:
                raise ExecutionTierRefused(
                    "Invalid cancel_agent_sandbox_order arguments"
                )
            return cancel_agent_sandbox_order(
                self.writer, agent_id=self.agent_id, order_id=arguments["order_id"]
            )
        if name == "read_agent_sandbox_portfolio":
            if arguments:
                raise ExecutionTierRefused(
                    "read_agent_sandbox_portfolio accepts no arguments"
                )
            portfolio = read_agent_sandbox_portfolio(
                self.writer.store, self.writer.company_id, self.agent_id
            )
            return {"portfolio": portfolio}
        raise ExecutionTierRefused(f"Unsupported agent sandbox tool: {name}")
