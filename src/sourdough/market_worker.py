"""Read actual public trades into a company-managed, read-only-market journal."""

import argparse
import asyncio
import fcntl
import json
import os
import re
import sqlite3
import stat
import sys
from contextlib import closing
from dataclasses import asdict, astuple, dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from time import time_ns


SOURCE = "binance-public-spot-trades-v1"
_NANOS_PER_MILLISECOND = 1_000_000


class MarketRefused(Exception):
    """The market source or its evidence did not match the registered scope."""


def _positive_decimal(value: str) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9]{1,24}(?:\.[0-9]{1,18})?", value) is not None
        and Decimal(value) > 0
    )


@dataclass(frozen=True)
class SpotInstrumentTerms:
    symbol: str
    base_currency: str
    quote_currency: str
    price_increment: str
    quantity_increment: str
    minimum_quantity: str | None
    minimum_notional: str | None

    def __post_init__(self) -> None:
        if (
            not all(
                isinstance(label, str) and re.fullmatch(r"[A-Z0-9]{2,24}", label)
                for label in (self.symbol, self.base_currency, self.quote_currency)
            )
            or self.symbol != self.base_currency + self.quote_currency
            or not _positive_decimal(self.price_increment)
            or not _positive_decimal(self.quantity_increment)
            or any(
                value is not None and not _positive_decimal(value)
                for value in (self.minimum_quantity, self.minimum_notional)
            )
        ):
            raise MarketRefused("Invalid spot sizing and currency terms")

    @property
    def revision(self) -> str:
        return sha256(_terms_body(self).encode()).hexdigest()


def _terms_body(terms: SpotInstrumentTerms) -> str:
    return json.dumps(asdict(terms), separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True)
class MarketScope:
    company_id: str
    engine_lock_digest: str
    symbol: str
    max_age_seconds: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.company_id, str)
            or not self.company_id
            or not isinstance(self.engine_lock_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.engine_lock_digest)
            or not isinstance(self.symbol, str)
            or not re.fullmatch(r"[A-Z0-9]{4,24}", self.symbol)
            or type(self.max_age_seconds) is not int
            or not 1 <= self.max_age_seconds <= 120
        ):
            raise MarketRefused("Invalid public spot market scope")


@dataclass(frozen=True)
class MarketTrade:
    trade_id: str
    price: str
    quantity: str
    event_ns: int
    received_ns: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.trade_id, str)
            or not re.fullmatch(r"[0-9]{1,32}", self.trade_id)
            or type(self.event_ns) is not int
            or type(self.received_ns) is not int
            or not 0 < self.event_ns <= self.received_ns <= 2**63 - 1
        ):
            raise MarketRefused("Trade has invalid source identity or event times")
        for amount in (self.price, self.quantity):
            if not _positive_decimal(amount):
                raise MarketRefused("Invalid fixed-precision trade amount")


def spot_trade_time_ns(sdk_time_ns: int) -> int:
    if type(sdk_time_ns) is not int or not 0 < sdk_time_ns <= 2**63 - 1:
        raise MarketRefused("Invalid SDK trade timestamp")
    half = _NANOS_PER_MILLISECOND // 2
    millisecond = (sdk_time_ns + half) // _NANOS_PER_MILLISECOND
    normalized = millisecond * _NANOS_PER_MILLISECOND
    if not 0 < normalized <= 2**63 - 1 or abs(normalized - sdk_time_ns) > 256:
        raise MarketRefused(
            "SDK timestamp does not match spot trade millisecond precision"
        )
    return normalized


def _connection(journal: Path, mode: str):
    return closing(
        sqlite3.connect(f"{journal.as_uri()}?mode={mode}", uri=True, timeout=2)
    )


def journal_key(journal: Path) -> tuple[int, int]:
    info = journal.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o077
    ):
        raise MarketRefused("Market journal must be one private file")
    return info.st_dev, info.st_ino


def register(journal: Path, scope: MarketScope) -> None:
    try:
        descriptor = os.open(journal, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    journal_key(journal)
    with _connection(journal, "rw") as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS market_scope (
                 singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                 company_id TEXT NOT NULL, engine_lock_digest TEXT NOT NULL,
                 symbol TEXT NOT NULL, max_age_seconds INTEGER NOT NULL, source TEXT NOT NULL
               )"""
        )
        terms = (*astuple(scope), SOURCE)
        existing = connection.execute(
            """SELECT company_id,engine_lock_digest,symbol,max_age_seconds,source
               FROM market_scope WHERE singleton=1"""
        ).fetchone()
        if existing is not None and existing != terms:
            raise MarketRefused("Market journal cannot change its registered source")
        if existing is None:
            connection.execute(
                "INSERT INTO market_scope VALUES (1, ?, ?, ?, ?, ?)", terms
            )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS market_trade (
                 trade_id TEXT PRIMARY KEY NOT NULL, price TEXT NOT NULL,
                 quantity TEXT NOT NULL, event_ns INTEGER NOT NULL, received_ns INTEGER NOT NULL,
                 activation_id TEXT NOT NULL, worker_pid INTEGER NOT NULL
               )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS spot_instrument_terms (
                 singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                 revision TEXT NOT NULL, terms TEXT NOT NULL
               )"""
        )


def bind_spot_terms(
    journal: Path, key: tuple[int, int], scope: MarketScope, terms: SpotInstrumentTerms
) -> None:
    if terms.symbol != scope.symbol or journal_key(journal) != key:
        raise MarketRefused("Instrument terms do not match the registered market")
    with _connection(journal, "rw") as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT revision,terms FROM spot_instrument_terms WHERE singleton=1"
        ).fetchone()
        receipt = terms.revision, _terms_body(terms)
        if existing is not None and existing != receipt:
            raise MarketRefused("Instrument terms changed from the registered revision")
        if existing is None:
            connection.execute(
                "INSERT INTO spot_instrument_terms VALUES (1, ?, ?)", receipt
            )


def read_spot_terms(journal: Path) -> SpotInstrumentTerms | None:
    with _connection(journal, "ro") as connection:
        row = connection.execute(
            "SELECT revision,terms FROM spot_instrument_terms WHERE singleton=1"
        ).fetchone()
    if row is None:
        return None
    try:
        terms = SpotInstrumentTerms(**json.loads(row[1]))
    except (TypeError, ValueError) as error:
        raise MarketRefused("Instrument terms receipt is invalid") from error
    if terms.revision != row[0]:
        raise MarketRefused("Instrument terms do not match their receipt")
    return terms


def reopen(journal: Path, company_id: str) -> MarketScope:
    with _connection(journal, "ro") as connection:
        row = connection.execute(
            """SELECT company_id,engine_lock_digest,symbol,max_age_seconds,source
               FROM market_scope WHERE singleton=1"""
        ).fetchone()
    if row is None or row[0] != company_id or row[-1] != SOURCE:
        raise MarketRefused("Market journal does not identify the original source")
    return MarketScope(*row[:-1])


def append_trade(
    journal: Path,
    key: tuple[int, int],
    scope: MarketScope,
    trade: MarketTrade,
    activation_id: str,
    worker_pid: int,
) -> bool:
    if journal_key(journal) != key:
        raise MarketRefused("Original market journal changed")
    if trade.received_ns - trade.event_ns > scope.max_age_seconds * 10**9:
        return False
    with _connection(journal, "rw") as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        present = connection.execute(
            "SELECT price,quantity,event_ns FROM market_trade WHERE trade_id=?",
            (trade.trade_id,),
        ).fetchone()
        if present is not None:
            if present != (trade.price, trade.quantity, trade.event_ns):
                raise MarketRefused("Conflicting delivery of the same venue trade")
            return False
        connection.execute(
            "INSERT INTO market_trade VALUES (?, ?, ?, ?, ?, ?, ?)",
            (*astuple(trade), activation_id, worker_pid),
        )
    return True


async def _collect(
    journal: Path,
    key: tuple[int, int],
    scope: MarketScope,
    activation_id: str,
    samples: int,
    ready_fd: int,
) -> None:
    from nautilus_trader.adapters.binance import (
        BinanceDataClientConfig,
        BinanceLiveDataClientFactory,
    )
    from nautilus_trader.common.actor import Actor
    from nautilus_trader.config import InstrumentProviderConfig, LoggingConfig
    from nautilus_trader.live.config import LiveExecEngineConfig, TradingNodeConfig
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.model.instruments import CurrencyPair

    loop = asyncio.get_running_loop()
    completed = loop.create_future()
    instrument = InstrumentId.from_str(f"{scope.symbol}.BINANCE")
    process_id = os.getpid()

    class PublicTrades(Actor):
        count = 0

        def on_start(self):
            try:
                metadata = self.cache.instrument(instrument)
                if not isinstance(metadata, CurrencyPair):
                    raise MarketRefused("Public spot instrument was not loaded")
                terms = SpotInstrumentTerms(
                    str(metadata.raw_symbol),
                    str(metadata.base_currency),
                    str(metadata.quote_currency),
                    str(metadata.price_increment),
                    str(metadata.size_increment),
                    str(metadata.min_quantity)
                    if metadata.min_quantity is not None
                    else None,
                    format(metadata.min_notional.as_decimal(), "f")
                    if metadata.min_notional is not None
                    else None,
                )
                bind_spot_terms(journal, key, scope, terms)
                self.subscribe_trade_ticks(instrument)
            except (MarketRefused, sqlite3.Error, OSError) as error:
                completed.set_exception(error)

        def on_trade_tick(self, tick):
            if completed.done():
                return
            try:
                if tick.instrument_id != instrument:
                    raise MarketRefused(
                        "Trade instrument differs from registered scope"
                    )
                received = time_ns()
                event = spot_trade_time_ns(tick.ts_event)
                if event > received:
                    return
                trade = MarketTrade(
                    str(tick.trade_id),
                    str(tick.price),
                    str(tick.size),
                    event,
                    received,
                )
                if append_trade(journal, key, scope, trade, activation_id, process_id):
                    self.count += 1
                    if self.count == 1:
                        payload = {
                            "pid": process_id,
                            "company_id": scope.company_id,
                            "store_key": key,
                        }
                        os.write(ready_fd, json.dumps(payload).encode() + b"\n")
                    if self.count >= samples:
                        completed.set_result(None)
            except (MarketRefused, sqlite3.Error, OSError) as error:
                completed.set_exception(error)

    config = TradingNodeConfig(
        data_clients={
            "BINANCE": BinanceDataClientConfig(
                instrument_provider=InstrumentProviderConfig(
                    load_ids=frozenset({instrument})
                ),
                update_instruments_interval_mins=None,
            )
        },
        exec_clients={},
        exec_engine=LiveExecEngineConfig(reconciliation=False),
        timeout_connection=12.0,
        timeout_disconnection=5.0,
        timeout_post_stop=0.5,
        logging=LoggingConfig(log_level="ERROR"),
    )
    node = TradingNode(config=config, loop=loop)
    node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)
    node.trader.add_actor(PublicTrades())
    node.build()
    task = asyncio.create_task(node.run_async())
    try:
        await asyncio.wait_for(completed, timeout=35)
    finally:
        await asyncio.wait_for(node.stop_async(), timeout=8)
        await asyncio.wait_for(task, timeout=8)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("journal", type=Path)
    parser.add_argument("company_id")
    parser.add_argument("activation_id")
    parser.add_argument("samples", type=int)
    parser.add_argument("--ready-fd", type=int, required=True)
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
        if journal_key(lease_file) != (info.st_dev, info.st_ino):
            raise MarketRefused("Market worker lease changed")
        key = journal_key(args.journal)
        if args.store_key != f"{key[0]}:{key[1]}":
            raise MarketRefused("Original market journal is unavailable")
        scope = reopen(args.journal, args.company_id)
        asyncio.run(
            _collect(
                args.journal,
                key,
                scope,
                args.activation_id,
                args.samples,
                args.ready_fd,
            )
        )
    finally:
        os.close(args.ready_fd)
        os.close(descriptor)


if __name__ == "__main__":
    main()
