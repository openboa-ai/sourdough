"""Isolate the virtual execution client from the company's authority and books."""

import argparse
import asyncio
import fcntl
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from time import monotonic, time_ns
from uuid import uuid4

from .market_worker import describe_spot_instrument, journal_key, spot_trade_time_ns
from .nautilus_paper import NautilusPaperVenue, SandboxCommand
from .paper_execution import VenueRefused
from .spot_cash import QUOTE_SCALE, SpotCashUnits


async def serve(venue: NautilusPaperVenue, ready_fd: int, seconds: int) -> None:
    from nautilus_trader.adapters.binance import (
        BinanceDataClientConfig,
        BinanceLiveDataClientFactory,
    )
    from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
    from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
    from nautilus_trader.config import InstrumentProviderConfig, LoggingConfig
    from nautilus_trader.live.config import LiveExecEngineConfig, TradingNodeConfig
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.model.enums import OrderSide, TimeInForce
    from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId
    from nautilus_trader.model.instruments import CurrencyPair
    from nautilus_trader.trading.strategy import Strategy

    _, plan = venue.configuration()
    units = SpotCashUnits(plan.terms)
    instrument_id = InstrumentId.from_str(f"{plan.terms.symbol}.BINANCE")
    market_venue = instrument_id.venue
    currency = plan.terms.quote_currency
    generation = uuid4().hex
    key = journal_key(venue.journal)
    loop = asyncio.get_running_loop()
    failure = loop.create_future()
    pending_account: set[str] = set()

    class ScopedSandbox(Strategy):
        quote = None
        trade_ns = 0
        instrument = None

        def on_start(self):
            try:
                instrument = self.cache.instrument(instrument_id)
                if (
                    not isinstance(instrument, CurrencyPair)
                    or describe_spot_instrument(instrument) != plan.terms
                    or instrument.maker_fee != 0
                    or instrument.taker_fee != 0
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
                if quote.instrument_id == instrument_id:
                    self.quote = (
                        units.price_per_lot(quote.ask_price.as_decimal()),
                        time_ns(),
                    )
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

        def execute_command(self, command: SandboxCommand) -> None:
            quantity, limit = units.limit_order(
                command.quantity, command.limit_minor_units
            )
            self.submit_order(
                self.order_factory.limit(
                    instrument_id,
                    OrderSide.BUY,
                    self.instrument.make_qty(quantity),
                    self.instrument.make_price(limit),
                    time_in_force=TimeInForce.IOC,
                    client_order_id=ClientOrderId(command.order_id),
                )
            )

        def on_order_filled(self, event):
            try:
                if str(event.commission.currency) != currency:
                    raise VenueRefused("Sandbox fill fee has unexpected currency")
                venue.record_fill(
                    generation,
                    str(event.client_order_id),
                    str(event.trade_id),
                    units.lots(event.last_qty.as_decimal()),
                    units.price_per_lot(event.last_px.as_decimal()),
                    units.quote_atoms(event.commission.as_decimal()),
                )
                pending_account.add(str(event.client_order_id))
            except Exception as error:
                if not failure.done():
                    failure.set_result(error)

        def on_order_rejected(self, event):
            venue.mark_unresolved(generation, str(event.client_order_id))

        on_order_canceled = on_order_rejected
        on_order_denied = on_order_rejected
        on_order_expired = on_order_rejected

    cash = Decimal(plan.starting_atoms) / QUOTE_SCALE
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
                starting_balances=[f"{cash:f} {currency}"],
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
    ready = False
    start_deadline = monotonic() + 30
    deadline = start_deadline
    last_quote = None
    try:
        while monotonic() < deadline:
            if failure.done():
                raise failure.result()
            if task.done():
                await task
                raise VenueRefused("Sandbox stopped before completing its activation")
            quote = strategy.quote
            if quote is not None and 0 < time_ns() - strategy.trade_ns <= 15 * 10**9:
                if not ready:
                    account = strategy.portfolio.account(market_venue)
                    if account is None or not account.is_cash_account:
                        raise VenueRefused(
                            "SDK did not create the scoped virtual cash account"
                        )
                    balance = account.balance_total(strategy.instrument.quote_currency)
                    venue.prove_account(
                        str(account.id),
                        currency,
                        units.quote_atoms(balance.as_decimal()),
                    )
                if quote != last_quote:
                    venue.publish_quote(generation, *quote, strategy.trade_ns)
                    last_quote = quote
                if not ready:
                    payload = {
                        "pid": os.getpid(),
                        "company_id": plan.company_id,
                        "store_key": key,
                    }
                    os.write(ready_fd, json.dumps(payload).encode() + b"\n")
                    ready = True
                    deadline = monotonic() + seconds
                if pending_account:
                    account = strategy.portfolio.account(market_venue)
                    quote_cash = account.balance_total(
                        strategy.instrument.quote_currency
                    )
                    base_balance = account.balance_total(
                        strategy.instrument.base_currency
                    )
                    if base_balance is not None and base_balance.as_decimal() > 0:
                        for order_id in tuple(pending_account):
                            if venue.record_account(
                                generation,
                                order_id,
                                units.lots(base_balance.as_decimal()),
                                units.quote_atoms(quote_cash.as_decimal()),
                            ):
                                pending_account.remove(order_id)
                command = venue.claim_command(generation)
                if command is not None:
                    strategy.execute_command(command)
            await asyncio.sleep(0.1)
        if not ready:
            raise VenueRefused(
                "No fresh public quote, source trade, and virtual cash account"
            )
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
    args = parser.parse_args()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    lease_file = args.journal.with_suffix(".writer-lock")
    descriptor = os.open(
        lease_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(descriptor)
        identity = journal_key(args.journal)
        if (
            journal_key(lease_file) != (info.st_dev, info.st_ino)
            or args.store_key != f"{identity[0]}:{identity[1]}"
            or not 1 <= args.seconds <= 600
        ):
            raise VenueRefused(
                "Original sandbox journal or activation bounds do not match"
            )
        venue = NautilusPaperVenue(args.journal)
        venue.start_generation()
        asyncio.run(serve(venue, args.ready_fd, args.seconds))
    finally:
        os.close(args.ready_fd)
        os.close(descriptor)
