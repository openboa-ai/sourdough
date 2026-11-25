"""Use an offline SDK engine to replay a registered quote tape."""

import argparse
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from .market_worker import journal_key
from .nautilus_paper import NautilusPaperVenue, SandboxBalance
from .nautilus_paper_worker import capture_fill, capture_terminal, submit_limit
from .paper_execution import VenueRefused
from .paper_sender_lease import verify_worker_sender
from .replay_tape import ReplayTape
from .spot_cash import QUOTE_SCALE, SpotCashUnits


def run(
    venue: NautilusPaperVenue, tape: ReplayTape, ready_fd: int, seconds: int
) -> None:
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.data import QuoteTick
    from nautilus_trader.model.enums import AccountType, OmsType
    from nautilus_trader.model.identifiers import InstrumentId, Symbol
    from nautilus_trader.model.instruments import CurrencyPair
    from nautilus_trader.model.objects import Currency, Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    _, plan = venue.configuration()
    if plan.tape_revision != tape.revision or plan.terms != tape.terms:
        raise VenueRefused(
            "Offline quote tape differs from the original replay binding"
        )
    units = SpotCashUnits(tape.terms)
    generation = uuid4().hex
    key = journal_key(venue.journal)
    base = Currency.from_str(tape.terms.base_currency)
    quote = Currency.from_str(tape.terms.quote_currency)
    price_increment = Price.from_str(
        format(Decimal(tape.terms.price_increment).normalize(), "f")
    )
    size_increment = Quantity.from_str(
        format(Decimal(tape.terms.quantity_increment).normalize(), "f")
    )
    instrument = CurrencyPair(
        instrument_id=InstrumentId.from_str(f"{tape.terms.symbol}.BINANCE"),
        raw_symbol=Symbol(tape.terms.symbol),
        base_currency=base,
        quote_currency=quote,
        price_precision=price_increment.precision,
        size_precision=size_increment.precision,
        price_increment=price_increment,
        size_increment=size_increment,
        min_quantity=Quantity.from_str(tape.terms.minimum_quantity),
        min_notional=Money(Decimal(tape.terms.minimum_notional), quote),
        margin_init=Decimal(0),
        margin_maint=Decimal(0),
        maker_fee=Decimal(0),
        taker_fee=Decimal(0),
        ts_event=0,
        ts_init=0,
    )

    class ReplayStrategy(Strategy):
        acted = False
        order_id = None
        error = None

        def on_start(self):
            self.subscribe_quote_ticks(instrument.id)

        def on_quote_tick(self, tick):
            if self.acted:
                return
            self.acted = True
            try:
                account = self.portfolio.account(instrument.id.venue)
                if account is None or not account.is_cash_account:
                    raise VenueRefused("Backtest did not initialize its virtual cash")
                initial = account.balance_total(quote)
                venue.prove_account(
                    generation,
                    str(account.id),
                    str(quote),
                    SandboxBalance(0, units.quote_atoms(initial.as_decimal())),
                )
                if self.clock.timestamp_ns() != tick.ts_init:
                    raise VenueRefused("Replay clock differs from quote availability")
                venue.publish_replay_cut(
                    generation,
                    units.price_per_lot(tick.ask_price.as_decimal()),
                    tick.ts_event,
                    tick.ts_init,
                )
                payload = {
                    "pid": os.getpid(),
                    "company_id": plan.company_id,
                    "store_key": key,
                }
                os.write(ready_fd, json.dumps(payload).encode() + b"\n")
                deadline = monotonic() + seconds
                while monotonic() < deadline:
                    command = venue.claim_command(generation)
                    if command is not None:
                        submit_limit(self, instrument, units, command)
                        return
                    sleep(0.025)
                raise VenueRefused(
                    "Replay had no admitted command at the sealed first cut"
                )
            except Exception as error:
                self.error = error

        def on_order_filled(self, event):
            try:
                self.order_id = capture_fill(venue, generation, units, event)
            except Exception as error:
                self.error = error

        def terminal_event(self, event):
            try:
                self.order_id = capture_terminal(
                    venue,
                    generation,
                    units,
                    self.cache.order(event.client_order_id),
                    event,
                )
            except Exception as error:
                self.error = error

        on_order_canceled = terminal_event
        on_order_rejected = terminal_event
        on_order_denied = terminal_event
        on_order_expired = terminal_event

    engine = BacktestEngine(
        config=BacktestEngineConfig(logging=LoggingConfig(log_level="ERROR"))
    )
    strategy = ReplayStrategy()
    try:
        engine.add_venue(
            venue=instrument.id.venue,
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            starting_balances=[
                Money(Decimal(plan.starting_atoms) / QUOTE_SCALE, quote)
            ],
        )
        engine.add_instrument(instrument)
        engine.add_data(
            [
                QuoteTick(
                    instrument.id,
                    instrument.make_price(Decimal(frame.bid)),
                    instrument.make_price(Decimal(frame.ask)),
                    instrument.make_qty(Decimal(frame.bid_size)),
                    instrument.make_qty(Decimal(frame.ask_size)),
                    frame.event_ns,
                    frame.available_ns,
                )
                for frame in tape.quotes
            ]
        )
        engine.add_strategy(strategy)
        engine.run()
        if strategy.error is not None:
            raise strategy.error
        if strategy.order_id is None:
            raise VenueRefused("Offline venue did not confirm the original order")
        account = strategy.portfolio.account(instrument.id.venue)
        base_balance = account.balance_total(base)
        base_amount = (
            base_balance.as_decimal() if base_balance is not None else Decimal(0)
        )
        if not venue.record_account(
            generation,
            strategy.order_id,
            0 if base_amount.is_zero() else units.lots(base_amount),
            units.quote_atoms(account.balance_total(quote).as_decimal()),
        ):
            raise VenueRefused(
                "Offline account could not corroborate the confirmed fill"
            )
    finally:
        venue.invalidate_quote()
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("journal", type=Path)
    parser.add_argument("tape", type=Path)
    parser.add_argument("seconds", type=int)
    parser.add_argument("--ready-fd", required=True, type=int)
    parser.add_argument("--store-key", required=True)
    parser.add_argument("--sender-lease-fd", required=True, type=int)
    args = parser.parse_args()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    try:
        verify_worker_sender(args.journal, args.sender_lease_fd, args.store_key)
        if not 1 <= args.seconds <= 600:
            raise VenueRefused("Invalid replay sandbox activation bounds")
        venue = NautilusPaperVenue(args.journal)
        venue.start_generation()
        run(venue, ReplayTape.load(args.tape), args.ready_fd, args.seconds)
    finally:
        os.close(args.ready_fd)
        os.close(args.sender_lease_fd)
