"""Activate one bounded observation on the installed trading engine."""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path
from uuid import uuid4

from .market_worker import (
    SOURCE,
    MarketRefused,
    MarketScope,
    MarketTrade,
    SpotInstrumentTerms,
    journal_key,
    read_spot_terms,
    register,
)
from .paper_execution import require_separate_journal
from .supervisor import ChildAttempt, supervise_worker
from .trading_engine import _environment, prepare_engine
from .writer_lease import hold_company_writer


@dataclass(frozen=True)
class MarketObservation:
    activation_id: str
    source: str
    symbol: str
    instrument: SpotInstrumentTerms | None
    instrument_revision: str | None
    observations: tuple[MarketTrade, ...]
    attempts: tuple[ChildAttempt, ...]
    completed: bool


def observe_market(
    store: Path,
    company_id: str,
    directory: Path,
    *,
    symbol: str,
    max_age_seconds: int,
    samples: int,
    emit: Callable[[dict], None],
) -> MarketObservation:
    if type(samples) is not int or not 1 <= samples <= 100:
        raise MarketRefused("Market observation requires one to 100 public trades")
    with hold_company_writer(store, company_id) as writer:
        installation = prepare_engine(writer, directory, allow_install=False)
        engine = directory.resolve() / f"nautilus-{installation.distribution_version}"
        journal = engine / "market.sqlite"
        require_separate_journal(store, journal)
        scope = MarketScope(
            company_id, installation.lock_digest, symbol, max_age_seconds
        )
        register(journal, scope)
        key = journal_key(journal)
    activation_id = uuid4().hex
    with as_file(files("sourdough").joinpath("market_worker.py")) as worker:
        trial = supervise_worker(
            [
                str(engine / "runtime" / "bin" / "python"),
                "-IB",
                str(worker),
                str(journal),
                company_id,
                activation_id,
                str(samples),
            ],
            journal,
            company_id,
            max_restarts=1,
            startup_seconds=30,
            max_seconds=100,
            emit=emit,
            environment=_environment(engine / "home"),
            expected_store_key=key,
        )
    if journal_key(journal) != key:
        raise MarketRefused("Original market journal changed during observation")
    successful_pid = trial.attempts[-1].pid if trial.completed else None
    with closing(
        sqlite3.connect(f"{journal.as_uri()}?mode=ro", uri=True)
    ) as connection:
        observations = tuple(
            MarketTrade(*row)
            for row in connection.execute(
                """SELECT trade_id,price,quantity,event_ns,received_ns FROM market_trade
                   WHERE activation_id=? AND (? IS NULL OR worker_pid=?)
                   ORDER BY received_ns,trade_id""",
                (activation_id, successful_pid, successful_pid),
            )
        )
    instrument = read_spot_terms(journal)
    return MarketObservation(
        activation_id,
        SOURCE,
        symbol,
        instrument,
        instrument.revision if instrument is not None else None,
        observations,
        trial.attempts,
        trial.completed and len(observations) == samples and instrument is not None,
    )
