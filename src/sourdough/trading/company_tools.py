"""Server-bound Outer Capability Broker tools for autonomous Company Trading shifts."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from time import time_ns
from typing import Any
from uuid import uuid4

from ..agent_runtime.protocol import ToolSpec
from ..runtime.work_kernel import register_responsibility, set_condition
from ..storage.db import open_reader, open_writer, read_tx, write_tx
from ..writer_lease import WriterLease
from .domain import (
    ENV_LIVE,
    ENV_PAPER,
    ExecutionTierRefused,
    TIER_COMPANY_LIVE,
    TIER_COMPANY_PAPER,
)
from .external_venue import ExternalVenueAdapter
from .ledger import (
    cancel_company_paper_book_order,
    read_trading_book_state_from_db,
    submit_company_paper_book_order,
)
from .migration import propose_paper_to_live_migration
from .quotes import MarketQuoteProvider, QuoteRefused, normalize_instrument_id
from .reconcile import submit_company_live_book_order

_ALLOWED_SCHEDULE_DUTIES = frozenset(
    {
        "quant.paper_decision",
        "trading.decision",
        "quant.reconcile",
        "trading.reconcile",
        "quant.agent_sandbox",
        "trading.agent_sandbox",
    }
)
_MIN_SHIFT_DELAY_SECONDS = 60
_MAX_SHIFT_DELAY_SECONDS = 86_400
_ALLOWED_CANCEL_REASONS = frozenset(
    {
        "agent_cancelled",
        "risk_limit",
        "quote_stale",
        "rebalance",
        "manual_cancel",
    }
)


def opaque_trading_action_id(company_id: str, scope_id: str, raw_action_id: str) -> str:
    """Derive a deterministic opaque order action ID so raw LLM text never reaches ledgers or Board projections."""
    if not isinstance(raw_action_id, str) or not raw_action_id.strip():
        raise ExecutionTierRefused("action_id must be a non-empty string")
    digest = sha256(
        f"sourdough:trading-action-id:v1\x1f{company_id}\x1f{scope_id}\x1f{raw_action_id.strip()}".encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    return f"ordact:{digest}"


READ_MARKET_QUOTE_SPEC = ToolSpec(
    name="read_market_quote",
    description="Read the latest authoritative market price quote for an instrument from the host quote feed.",
    parameters={
        "type": "object",
        "properties": {
            "instrument_id": {
                "type": "string",
                "description": "Instrument identifier such as BTC-USDT or ETH-USDT.",
            }
        },
        "required": ["instrument_id"],
    },
)

READ_TRADING_BOOKS_SPEC = ToolSpec(
    name="read_trading_books",
    description="Inspect active Company Paper or Company Live trading books, NAV, cash, exposure, and open positions.",
    parameters={
        "type": "object",
        "properties": {
            "environment": {
                "type": "string",
                "enum": ["paper", "live"],
                "description": "Execution environment to inspect ('paper' or 'live').",
            }
        },
        "required": [],
    },
)

COMPANY_TRADING_TOOL_SPECS: tuple[ToolSpec, ...] = (
    READ_MARKET_QUOTE_SPEC,
    READ_TRADING_BOOKS_SPEC,
    ToolSpec(
        name="submit_company_paper_order",
        description="Submit a simulated order into an active Company Paper Book using authoritative host market quotes.",
        parameters={
            "type": "object",
            "properties": {
                "binding_id": {"type": "string"},
                "action_id": {"type": "string"},
                "instrument_id": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "quantity": {"type": "integer"},
                "limit_minor_units": {"type": "integer"},
                "fill_mode": {"type": "string", "enum": ["immediate", "resting"]},
            },
            "required": [
                "binding_id",
                "action_id",
                "instrument_id",
                "side",
                "quantity",
                "limit_minor_units",
            ],
        },
        flow_class="external_write_sink",
    ),
    ToolSpec(
        name="cancel_company_paper_order",
        description="Cancel an open resting order in an active Company Paper Book.",
        parameters={
            "type": "object",
            "properties": {
                "binding_id": {"type": "string"},
                "order_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["binding_id", "order_id"],
        },
        flow_class="external_write_sink",
    ),
    ToolSpec(
        name="submit_company_live_order",
        description="Submit a real-capital order to a Board-activated Company Live venue through the crash-safe two-phase outbox.",
        parameters={
            "type": "object",
            "properties": {
                "binding_id": {"type": "string"},
                "action_id": {"type": "string"},
                "instrument_id": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "quantity": {"type": "integer"},
                "limit_minor_units": {"type": "integer"},
                "time_in_force": {"type": "string", "enum": ["IOC", "GTC"]},
            },
            "required": [
                "binding_id",
                "action_id",
                "instrument_id",
                "side",
                "quantity",
                "limit_minor_units",
            ],
        },
        flow_class="external_write_sink",
    ),
    ToolSpec(
        name="propose_live_migration",
        description="Recommend a soak-ready Company Paper Book for Board-approved Clean Start Live migration (never activates live directly).",
        parameters={
            "type": "object",
            "properties": {
                "source_paper_binding_id": {"type": "string"},
                "target_live_binding_id": {"type": "string"},
                "qualification_receipt_id": {"type": "string"},
                "reason": {"type": "string"},
                "migration_mode": {
                    "type": "string",
                    "enum": ["clean_start", "drain_incumbent"],
                },
                "source_paper_disposition": {
                    "type": "string",
                    "enum": ["keep_shadow", "retire_source"],
                },
            },
            "required": [
                "source_paper_binding_id",
                "target_live_binding_id",
                "qualification_receipt_id",
            ],
        },
        flow_class="external_write_sink",
    ),
    ToolSpec(
        name="schedule_next_shift",
        description="Arm a bounded timer condition so the resident runtime wakes this agent for its next autonomous trading or reconciliation shift.",
        parameters={
            "type": "object",
            "properties": {
                "delay_seconds": {"type": "integer"},
                "duty_kind": {
                    "type": "string",
                    "enum": [
                        "quant.paper_decision",
                        "trading.decision",
                        "quant.reconcile",
                        "trading.reconcile",
                    ],
                },
            },
            "required": ["delay_seconds", "duty_kind"],
        },
    ),
)

_FORBIDDEN_IDENTITY_KEYS = frozenset(
    {"agent_id", "actor_id", "company_id", "run_id", "created_by_agent_id"}
)


def read_trading_books_summary(
    store_path: Any, company_id: str, *, environment: str = ENV_PAPER
) -> dict[str, Any]:
    """Read-only summary of all trading books in the requested environment."""
    if environment not in (ENV_PAPER, ENV_LIVE):
        raise ExecutionTierRefused("environment must be 'paper' or 'live'")
    target_tier = TIER_COMPANY_LIVE if environment == ENV_LIVE else TIER_COMPANY_PAPER
    with closing(open_reader(store_path)) as db, read_tx(db):
        rows = db.execute(
            """SELECT binding_id FROM execution_environment_binding
               WHERE company_id = ? AND tier = ? AND environment = ?
               ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, created_at_ns ASC""",
            (company_id, target_tier, environment),
        ).fetchall()
        books: list[dict[str, Any]] = []
        for (bid,) in rows:
            st = read_trading_book_state_from_db(db, company_id, str(bid))
            if st is None:
                continue
            books.append(
                {
                    "binding_id": st["binding_id"],
                    "account_id": st["account_id"],
                    "book_name": st["book_name"],
                    "environment": st["environment"],
                    "role": st["role"],
                    "status": st["status"],
                    "currency": st["currency"],
                    "nav_minor_units": st["nav_minor_units"],
                    "cash_minor_units": st["cash_minor_units"],
                    "gross_exposure_minor_units": st["gross_exposure_minor_units"],
                    "exposure_limit_minor_units": st["exposure_limit_minor_units"],
                    "return_pct": st["return_pct"],
                    "fill_count": st["fill_count"],
                    "soak_ready": st["soak_ready"],
                    "positions": st["positions"],
                }
            )
    return {"environment": environment, "books": books}


@dataclass(frozen=True)
class CompanyTradingTools:
    """Server-bound Outer Capability Broker handler for autonomous Company Trading shifts.

    Identity (`agent_id`, `company_id`, `run_id`) and market quotes are bound strictly by
    the host runtime so agents can never spoof actors, fabricate market prices, or bypass
    Board approval for Live activation.
    """

    writer: WriterLease
    agent_id: str
    run_id: str
    quote_provider: MarketQuoteProvider | None = None
    live_venue_resolver: Callable[[str], ExternalVenueAdapter | None] | None = None
    max_quote_age_ns: int = 60 * 10**9
    clock: Callable[[], int] = time_ns

    def _require_fresh_quote(
        self, instrument_id: str, *, max_age_ns: int
    ) -> dict[str, Any]:
        if self.quote_provider is None:
            raise ExecutionTierRefused(
                "No authoritative MarketQuoteProvider is configured on this runtime"
            )
        try:
            norm = normalize_instrument_id(instrument_id)
        except QuoteRefused as exc:
            raise ExecutionTierRefused(str(exc)) from exc
        quote = self.quote_provider.latest_quote(norm)
        now = self.clock()
        if quote is None:
            raise ExecutionTierRefused(
                f"No market quote available for '{norm}' from host quote provider"
            )
        if not quote.is_fresh(now_ns=now, max_age_ns=max_age_ns):
            raise ExecutionTierRefused(
                f"Market quote for '{norm}' is stale and cannot be used for execution"
            )
        return {
            "instrument_id": quote.instrument_id,
            "price_minor_units": quote.price_minor_units,
            "observed_at_ns": quote.observed_at_ns,
            "source": quote.source,
        }

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ExecutionTierRefused("Expected JSON object tool arguments")
        if _FORBIDDEN_IDENTITY_KEYS & set(arguments):
            raise ExecutionTierRefused(
                "Server-bound identity fields cannot be supplied in trading tool arguments"
            )

        if name == "read_market_quote":
            if set(arguments) != {"instrument_id"}:
                raise ExecutionTierRefused("Invalid read_market_quote arguments")
            return self._require_fresh_quote(
                arguments["instrument_id"], max_age_ns=self.max_quote_age_ns
            )

        if name == "read_trading_books":
            if not set(arguments).issubset({"environment"}):
                raise ExecutionTierRefused("Invalid read_trading_books arguments")
            env = str(arguments.get("environment", ENV_PAPER)).strip().lower()
            return read_trading_books_summary(
                self.writer.store, self.writer.company_id, environment=env
            )

        if name == "submit_company_paper_order":
            allowed = {
                "binding_id",
                "action_id",
                "instrument_id",
                "side",
                "quantity",
                "limit_minor_units",
                "fill_mode",
            }
            required = {
                "binding_id",
                "action_id",
                "instrument_id",
                "side",
                "quantity",
                "limit_minor_units",
            }
            if not set(arguments).issubset(allowed) or not required.issubset(arguments):
                raise ExecutionTierRefused(
                    "Invalid submit_company_paper_order arguments"
                )
            quote = self._require_fresh_quote(
                arguments["instrument_id"], max_age_ns=self.max_quote_age_ns
            )
            opaque_action_id = opaque_trading_action_id(
                self.writer.company_id,
                str(arguments["binding_id"]),
                arguments["action_id"],
            )
            return submit_company_paper_book_order(
                self.writer,
                binding_id=arguments["binding_id"],
                actor_id=self.agent_id,
                action_id=opaque_action_id,
                instrument_id=quote["instrument_id"],
                side=arguments["side"],
                quantity=arguments["quantity"],
                limit_minor_units=arguments["limit_minor_units"],
                market_price_minor_units=quote["price_minor_units"],
                quote_observed_at_ns=quote["observed_at_ns"],
                fill_mode=arguments.get("fill_mode", "immediate"),
                run_id=self.run_id,
                now_ns=self.clock(),
            )

        if name == "cancel_company_paper_order":
            allowed = {"binding_id", "order_id", "reason"}
            if not set(arguments).issubset(allowed) or not {
                "binding_id",
                "order_id",
            }.issubset(arguments):
                raise ExecutionTierRefused(
                    "Invalid cancel_company_paper_order arguments"
                )
            raw_reason = arguments.get("reason", "agent_cancelled")
            if (
                not isinstance(raw_reason, str)
                or raw_reason not in _ALLOWED_CANCEL_REASONS
            ):
                raise ExecutionTierRefused(
                    "cancel_company_paper_order reason must be one of the allowed closed cancel reasons"
                )
            return cancel_company_paper_book_order(
                self.writer,
                binding_id=arguments["binding_id"],
                actor_id=self.agent_id,
                order_id=arguments["order_id"],
                reason=raw_reason,
                now_ns=self.clock(),
            )

        if name == "submit_company_live_order":
            allowed = {
                "binding_id",
                "action_id",
                "instrument_id",
                "side",
                "quantity",
                "limit_minor_units",
                "time_in_force",
            }
            required = {
                "binding_id",
                "action_id",
                "instrument_id",
                "side",
                "quantity",
                "limit_minor_units",
            }
            if not set(arguments).issubset(allowed) or not required.issubset(arguments):
                raise ExecutionTierRefused(
                    "Invalid submit_company_live_order arguments"
                )
            if self.live_venue_resolver is None:
                raise ExecutionTierRefused(
                    "No external live venue adapter resolver is configured on this runtime"
                )
            adapter = self.live_venue_resolver(arguments["binding_id"])
            if adapter is None:
                raise ExecutionTierRefused(
                    f"No external venue adapter bound for live binding '{arguments['binding_id']}'"
                )
            opaque_action_id = opaque_trading_action_id(
                self.writer.company_id,
                str(arguments["binding_id"]),
                arguments["action_id"],
            )
            return submit_company_live_book_order(
                self.writer,
                binding_id=arguments["binding_id"],
                actor_id=self.agent_id,
                action_id=opaque_action_id,
                instrument_id=arguments["instrument_id"],
                side=arguments["side"],
                quantity=arguments["quantity"],
                limit_minor_units=arguments["limit_minor_units"],
                adapter=adapter,
                quote_provider=self.quote_provider,
                time_in_force=arguments.get("time_in_force", "IOC"),
                run_id=self.run_id,
                now_ns=self.clock(),
            )

        if name == "propose_live_migration":
            allowed = {
                "source_paper_binding_id",
                "target_live_binding_id",
                "qualification_receipt_id",
                "reason",
                "migration_mode",
                "source_paper_disposition",
            }
            required = {
                "source_paper_binding_id",
                "target_live_binding_id",
                "qualification_receipt_id",
            }
            if not set(arguments).issubset(allowed) or not required.issubset(arguments):
                raise ExecutionTierRefused("Invalid propose_live_migration arguments")
            qid = f"q-mig-{uuid4().hex[:12]}"
            return propose_paper_to_live_migration(
                self.writer,
                question_id=qid,
                source_paper_binding_id=arguments["source_paper_binding_id"],
                target_live_binding_id=arguments["target_live_binding_id"],
                qualification_receipt_id=arguments["qualification_receipt_id"],
                created_by_agent_id=self.agent_id,
                migration_mode=arguments.get("migration_mode", "clean_start"),
                source_paper_disposition=arguments.get(
                    "source_paper_disposition", "keep_shadow"
                ),
                summary=arguments.get("reason"),
            )

        if name == "schedule_next_shift":
            if set(arguments) != {"delay_seconds", "duty_kind"}:
                raise ExecutionTierRefused("Invalid schedule_next_shift arguments")
            delay = arguments["delay_seconds"]
            duty_kind = arguments["duty_kind"]
            if (
                type(delay) is not int
                or not _MIN_SHIFT_DELAY_SECONDS <= delay <= _MAX_SHIFT_DELAY_SECONDS
            ):
                raise ExecutionTierRefused(
                    f"delay_seconds must be an integer between {_MIN_SHIFT_DELAY_SECONDS} and {_MAX_SHIFT_DELAY_SECONDS}"
                )
            if duty_kind not in _ALLOWED_SCHEDULE_DUTIES:
                raise ExecutionTierRefused(
                    f"Unsupported trading shift duty_kind '{duty_kind}'"
                )
            now = self.clock()
            due_at_ns = now + delay * 1_000_000_000
            with (
                closing(open_writer(self.writer.store, lease=self.writer)) as db,
                write_tx(db),
            ):
                self.writer.verify()
                resp_row = db.execute(
                    """SELECT responsibility_id FROM work_responsibilities
                       WHERE company_id = ? AND owner_agent_id = ?
                         AND duty_kind = ? AND status = 'active'
                       LIMIT 1""",
                    (self.writer.company_id, self.agent_id, duty_kind),
                ).fetchone()
                if resp_row is not None:
                    resp_id = str(resp_row[0])
                else:
                    resp = register_responsibility(
                        db,
                        company_id=self.writer.company_id,
                        owner_agent_id=self.agent_id,
                        duty_kind=duty_kind,
                        remit=f"Autonomous trading shift ({duty_kind})",
                        readable_scope=["trading_books", "market_quotes"],
                        now_ns=now,
                    )
                    resp_id = resp.responsibility_id
                cond = set_condition(
                    db,
                    company_id=self.writer.company_id,
                    owner_agent_id=self.agent_id,
                    responsibility_id=resp_id,
                    trigger_kind="timer",
                    due_at_ns=due_at_ns,
                    now_ns=now,
                )
            return {
                "condition_id": cond.condition_id,
                "responsibility_id": resp_id,
                "duty_kind": duty_kind,
                "due_at_ns": due_at_ns,
                "status": cond.status,
            }

        raise ExecutionTierRefused(f"Unsupported company trading tool: {name}")
