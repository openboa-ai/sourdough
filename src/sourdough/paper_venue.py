"""An isolated, fixed-quote paper destination for the first trade."""

import sqlite3
from contextlib import closing
from dataclasses import astuple, dataclass
from pathlib import Path
from uuid import uuid4

from .paper_account import PaperAccount
from .paper_order import PaperIntent


class VenueRefused(Exception):
    """The paper destination cannot accept the command."""


@dataclass(frozen=True)
class VenueExecution:
    order_id: str
    account_id: str
    instrument_rules_digest: str
    quantity: int
    limit_minor_units: int
    fill_id: str | None
    fill_price_minor_units: int | None


class FixedQuoteVenue:
    def __init__(self, journal: Path):
        self.journal = journal

    def _open(self, mode: str):
        return closing(
            sqlite3.connect(f"{self.journal.absolute().as_uri()}?mode={mode}", uri=True)
        )

    def provision(self, account: PaperAccount, quote_minor_units: int) -> None:
        if (
            account.environment != "paper"
            or type(quote_minor_units) is not int
            or not 0 < quote_minor_units <= 2**63 - 1
        ):
            raise VenueRefused("Expected paper account and positive quote")
        with self._open("rwc") as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """CREATE TABLE IF NOT EXISTS venue_account (
                   singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                   account_id TEXT NOT NULL, connector_revision TEXT NOT NULL,
                   rules TEXT NOT NULL, currency TEXT NOT NULL,
                   starting_minor_units INTEGER NOT NULL, environment TEXT NOT NULL,
                   quote INTEGER NOT NULL)"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS venue_execution (
                   order_id TEXT PRIMARY KEY NOT NULL, account_id TEXT NOT NULL,
                   rules TEXT NOT NULL, quantity INTEGER NOT NULL,
                   limit_minor_units INTEGER NOT NULL, fill_id TEXT UNIQUE,
                   fill_price_minor_units INTEGER)"""
            )
            terms = (*astuple(account), quote_minor_units)
            existing = db.execute("SELECT * FROM venue_account").fetchone()
            if existing is None:
                db.execute(
                    "INSERT INTO venue_account VALUES (1, ?, ?, ?, ?, ?, ?, ?)", terms
                )
            elif existing[1:] != terms:
                raise VenueRefused("Paper venue configuration conflicts")

    def observe_account(self, account_id: str) -> PaperAccount:
        with self._open("ro") as db:
            row = db.execute(
                "SELECT * FROM venue_account WHERE account_id = ?", (account_id,)
            ).fetchone()
        if row is None:
            raise VenueRefused("Unknown destination account")
        return PaperAccount(*row[1:-1])

    def submit(self, order_id: str, account_id: str, intent: PaperIntent) -> None:
        if (
            not order_id
            or type(intent.quantity) is not int
            or type(intent.limit_minor_units) is not int
            or not 0 < intent.quantity <= 2**63 - 1
            or not 0 < intent.limit_minor_units <= 2**63 - 1
        ):
            raise VenueRefused("Invalid paper order terms")
        with self._open("rw") as db, db:
            db.execute("BEGIN IMMEDIATE")
            account = db.execute(
                "SELECT rules, quote FROM venue_account WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if account is None or account[0] != intent.instrument_rules_digest:
                raise VenueRefused("Unknown destination account or instrument")
            terms = (account_id, account[0], intent.quantity, intent.limit_minor_units)
            existing = db.execute(
                "SELECT * FROM venue_execution WHERE order_id = ?", (order_id,)
            ).fetchone()
            if existing is not None:
                if existing[1:5] != terms:
                    raise VenueRefused("Conflicting destination order identity")
                return
            price = account[1] if 0 < account[1] <= intent.limit_minor_units else None
            fill_id = uuid4().hex if price is not None else None
            db.execute(
                "INSERT INTO venue_execution VALUES (?, ?, ?, ?, ?, ?, ?)",
                (order_id, *terms, fill_id, price),
            )

    def observe_execution(self, order_id: str) -> VenueExecution | None:
        with self._open("ro") as db:
            row = db.execute(
                "SELECT * FROM venue_execution WHERE order_id = ?", (order_id,)
            ).fetchone()
        return None if row is None else VenueExecution(*row)
