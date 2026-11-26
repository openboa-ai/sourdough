"""Isolate the virtual execution client from the company's authority and books."""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from time import monotonic, time_ns
from uuid import uuid4

from .execution import VenueRefused
from .market_worker import describe_spot_instrument, journal_key, spot_trade_time_ns
from .nautilus_paper import (
    NautilusPaperVenue,
    SandboxBalance,
    SandboxCommand,
    current_quote_times,
)
from .sender_lease import verify_worker_sender
from .spot_cash import QUOTE_SCALE, SpotCashUnits


async def _drain_checkpoint(checkpoint: Callable[[], Awaitable[bool]]) -> None:
    deadline = monotonic() + 5
    while monotonic() < deadline:
        if await checkpoint():
            return
        await asyncio.sleep(max(0, min(0.1, deadline - monotonic())))
    raise VenueRefused("Sandbox stopped with an unresolved account checkpoint")


def submit_limit(
    strategy, instrument, units: SpotCashUnits, command: SandboxCommand
) -> None:
    from nautilus_trader.model.enums import OrderSide, TimeInForce
    from nautilus_trader.model.identifiers import ClientOrderId

    quantity, limit = units.limit_order(command.quantity, command.limit_minor_units)
    strategy.submit_order(
        strategy.order_factory.limit(
            instrument.id,
            OrderSide.BUY,
            instrument.make_qty(quantity),
            instrument.make_price(limit),
            time_in_force=TimeInForce.IOC,
            client_order_id=ClientOrderId(command.order_id),
        )
    )


def capture_fill(venue, generation: str, units: SpotCashUnits, event) -> str:
    if str(event.commission.currency) != units.terms.quote_currency:
        raise VenueRefused("Sandbox fill fee has unexpected currency")
    order_id = str(event.client_order_id)
    venue.record_fill(
        generation,
        order_id,
        str(event.trade_id),
        units.lots(event.last_qty.as_decimal()),
        units.price_per_lot(event.last_px.as_decimal()),
        units.quote_atoms(event.commission.as_decimal()),
        effective_ns=event.ts_event,
    )
    return order_id


def capture_terminal(venue, generation: str, units: SpotCashUnits, order, event) -> str:
    from nautilus_trader.model.enums import OrderStatus
    from nautilus_trader.model.events import (
        OrderCanceled,
        OrderDenied,
        OrderExpired,
        OrderRejected,
    )

    match event:
        case OrderCanceled():
            outcome, status = "cancelled", OrderStatus.CANCELED
        case OrderDenied():
            outcome, status = "denied", OrderStatus.DENIED
        case OrderExpired():
            outcome, status = "expired", OrderStatus.EXPIRED
        case OrderRejected():
            outcome, status = "rejected", OrderStatus.REJECTED
        case _:
            raise VenueRefused("SDK event does not prove a terminal order outcome")

    order_id = str(event.client_order_id)
    filled_quantity = None
    if (
        order is not None
        and order.client_order_id == event.client_order_id
        and order.status == status
    ):
        amount = order.filled_qty.as_decimal()
        filled_quantity = 0 if amount.is_zero() else units.lots(amount)
    venue.record_cancellation(
        generation, order_id, str(event.id), filled_quantity, outcome=outcome
    )
    return order_id


async def serve(
    venue: NautilusPaperVenue, ready_fd: int, seconds: int, checkpoint: SandboxBalance
) -> None:
    from nautilus_trader.adapters.binance import (
        BinanceDataClientConfig,
        BinanceLiveDataClientFactory,
    )
    from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
    from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
    from nautilus_trader.config import InstrumentProviderConfig, LoggingConfig
    from nautilus_trader.live.config import LiveExecEngineConfig, TradingNodeConfig
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.model.enums import TimeInForce
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.model.instruments import CurrencyPair
    from nautilus_trader.trading.strategy import Strategy

    _, plan = venue.configuration()
    if plan.tape_revision is not None:
        raise VenueRefused(
            "A historical binding cannot start the current market worker"
        )
    units = SpotCashUnits(plan.terms)
    instrument_id = InstrumentId.from_str(f"{plan.terms.symbol}.BINANCE")
    market_venue = instrument_id.venue
    currency = plan.terms.quote_currency
    generation = uuid4().hex
    key = journal_key(venue.journal)
    loop = asyncio.get_running_loop()
    failure = loop.create_future()
    outstanding_order = None

    class ScopedSandbox(Strategy):
        quote = None
        trade_ns = 0
        instrument = None

        def on_start(self):
            try:
                instrument = self.cache.instrument(instrument_id)
                expected_fee = Decimal(plan.fee_bps) / Decimal(10_000)
                if (
                    not isinstance(instrument, CurrencyPair)
                    or describe_spot_instrument(instrument) != plan.terms
                    or instrument.maker_fee != expected_fee
                    or instrument.taker_fee != expected_fee
                ):
                    raise VenueRefused(
                        "Sandbox instrument or fee policy differs from the registered plan"
                    )
                self.instrument = instrument
                self.subscribe_trade_ticks(instrument_id)
                self.subscribe_quote_ticks(instrument_id)
            except Exception as error:
                failure.set_result(error)

        def on_quote_tick(self, quote):
            try:
                event_ns, received_ns = quote.ts_event, quote.ts_init
                if quote.instrument_id != instrument_id or not current_quote_times(
                    event_ns, received_ns, time_ns()
                ):
                    return
                if (
                    self.quote is not None
                    and (event_ns, received_ns) <= self.quote[1:3]
                ):
                    return
                ask = units.price_per_lot(quote.ask_price.as_decimal())
                bid = units.price_per_lot(quote.bid_price.as_decimal())
                if bid <= ask:
                    self.quote = (ask, event_ns, received_ns, bid)
            except Exception as error:
                if not failure.done():
                    failure.set_result(error)

        def on_trade_tick(self, tick):
            if tick.instrument_id != instrument_id:
                return
            try:
                stamp = spot_trade_time_ns(tick.ts_event)
                if stamp <= time_ns():
                    self.trade_ns = max(self.trade_ns, stamp)
            except Exception as error:
                if not failure.done():
                    failure.set_result(error)

        def on_order_accepted(self, event):
            loop.call_soon(self.complete_ioc, event.client_order_id)

        def complete_ioc(self, order_id):
            if failure.done():
                return
            try:
                order = self.cache.order(order_id)
                if (
                    order is not None
                    and order.time_in_force == TimeInForce.IOC
                    and order.is_open
                    and not order.is_pending_cancel
                ):
                    self.cancel_order(order)
            except Exception as error:
                if not failure.done():
                    failure.set_result(error)

        def on_order_filled(self, event):
            try:
                capture_fill(venue, generation, units, event)
            except Exception as error:
                if not failure.done():
                    failure.set_result(error)

        def terminal_event(self, event):
            try:
                capture_terminal(
                    venue,
                    generation,
                    units,
                    self.cache.order(event.client_order_id),
                    event,
                )
            except Exception as error:
                if not failure.done():
                    failure.set_result(error)

        on_order_canceled = terminal_event
        on_order_rejected = terminal_event
        on_order_denied = terminal_event
        on_order_expired = terminal_event

    cash = Decimal(checkpoint.cash_atoms) / QUOTE_SCALE
    balances = [f"{cash:f} {currency}"]
    if checkpoint.position_lots:
        base = Decimal(plan.terms.quantity_increment) * checkpoint.position_lots
        balances.append(f"{base:f} {plan.terms.base_currency}")
    config = TradingNodeConfig(
        data_clients={
            "BINANCE": BinanceDataClientConfig(
                instrument_provider=InstrumentProviderConfig(
                    load_ids=frozenset({instrument_id})
                ),
                update_instruments_interval_mins=None,
            )
        },
        exec_clients={
            "BINANCE": SandboxExecutionClientConfig(
                venue="BINANCE",
                account_type="CASH",
                starting_balances=balances,
                bar_execution=False,
                trade_execution=False,
            )
        },
        exec_engine=LiveExecEngineConfig(reconciliation=False),
        logging=LoggingConfig(log_level="ERROR"),
        timeout_connection=12.0,
        timeout_disconnection=5.0,
        timeout_post_stop=0.5,
    )
    node = TradingNode(config=config, loop=loop)
    node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)
    node.add_exec_client_factory("BINANCE", SandboxLiveExecClientFactory)
    strategy = ScopedSandbox()
    node.trader.add_strategy(strategy)
    node.build()
    task = asyncio.create_task(node.run_async())

    def read_balance() -> tuple[str, SandboxBalance]:
        account = strategy.portfolio.account(market_venue)
        if account is None or not account.is_cash_account:
            raise VenueRefused("SDK did not create the scoped virtual cash account")
        quote_cash = account.balance_total(strategy.instrument.quote_currency)
        base_balance = account.balance_total(strategy.instrument.base_currency)
        base_amount = (
            base_balance.as_decimal() if base_balance is not None else Decimal(0)
        )
        return str(account.id), SandboxBalance(
            0 if base_amount.is_zero() else units.lots(base_amount),
            units.quote_atoms(quote_cash.as_decimal()),
        )

    async def checkpoint() -> bool:
        nonlocal outstanding_order
        if failure.done():
            raise failure.result()
        if task.done():
            await task
            raise VenueRefused("Sandbox stopped before completing its activation")
        if outstanding_order is not None:
            _, balance = read_balance()
            if venue.record_account(
                generation, outstanding_order, balance.position_lots, balance.cash_atoms
            ):
                outstanding_order = None
        return outstanding_order is None

    ready = False
    start_deadline = monotonic() + 30
    deadline = start_deadline
    last_quote = None
    try:
        while monotonic() < deadline:
            await checkpoint()
            quote = strategy.quote
            now = time_ns()
            if (
                quote is not None
                and current_quote_times(*quote[1:3], now)
                and 0 <= now - strategy.trade_ns <= 15 * 10**9
            ):
                if not ready:
                    account_id, balance = read_balance()
                    venue.prove_account(generation, account_id, currency, balance)
                observation = (*quote, strategy.trade_ns)
                if observation != last_quote:
                    ask, event_ns, received_ns, bid, trade_ns = observation
                    venue.publish_quote(
                        generation,
                        ask,
                        received_ns,
                        trade_ns,
                        event_ns=event_ns,
                        bid_atoms_per_lot=bid,
                    )
                    last_quote = observation
                if not ready:
                    payload = {
                        "pid": os.getpid(),
                        "company_id": plan.company_id,
                        "store_key": key,
                    }
                    os.write(ready_fd, json.dumps(payload).encode() + b"\n")
                    ready = True
                    deadline = monotonic() + seconds
                if outstanding_order is None and monotonic() < deadline:
                    command = venue.claim_command(generation)
                    if command is not None:
                        outstanding_order = command.order_id
                        submit_limit(strategy, strategy.instrument, units, command)
            await asyncio.sleep(0.1)
        if not ready:
            raise VenueRefused(
                "No fresh public quote, source trade, and virtual cash account"
            )
        venue.invalidate_quote()
        await _drain_checkpoint(checkpoint)
    finally:
        venue.invalidate_quote()
        await asyncio.wait_for(node.stop_async(), timeout=8)
        await asyncio.wait_for(task, timeout=8)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("journal", type=Path)
    parser.add_argument("seconds", type=int)
    parser.add_argument("--ready-fd", required=True, type=int)
    parser.add_argument("--store-key", required=True)
    parser.add_argument("--sender-lease-fd", required=True, type=int)
    args = parser.parse_args()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    try:
        verify_worker_sender(args.journal, args.sender_lease_fd, args.store_key)
        if not 1 <= args.seconds <= 600:
            raise VenueRefused("Invalid managed sandbox activation bounds")
        venue = NautilusPaperVenue(args.journal)
        checkpoint = venue.start_generation()
        asyncio.run(serve(venue, args.ready_fd, args.seconds, checkpoint))
    finally:
        os.close(args.ready_fd)
        os.close(args.sender_lease_fd)
