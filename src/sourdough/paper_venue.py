"""An isolated, fixed-quote paper destination for the first trade."""

import sqlite3
from contextlib import closing
from dataclasses import astuple, replace
from pathlib import Path
from uuid import uuid4

from .paper_account import PaperAccount
from .paper_execution import (
    PaperIntent,
    VenueExecution,
    VenueRefused,
    require_separate_journal,
)


class FixedQuoteVenue:
    def __init__(self, journal: Path):
        self.journal = journal

    def require_independent_from(self, company_store: Path) -> None:
        require_separate_journal(company_store, self.journal)

    def _open(self, mode: str):
        connection = sqlite3.connect(
            f"{self.journal.absolute().as_uri()}?mode={mode}", uri=True
        )
        connection.execute("PRAGMA foreign_keys = ON")
        return closing(connection)

    def provision(
        self, account: PaperAccount, quote_minor_units: int, *, fee_bps: int = 0
    ) -> PaperAccount:
        if (
            account.environment != "paper"
            or type(quote_minor_units) is not int
            or not 0 < quote_minor_units <= 2**63 - 1
            or type(fee_bps) is not int
            or not 0 <= fee_bps <= 10000
        ):
            raise VenueRefused("Expected paper account, positive quote and valid fee")
        with self._open("rwc") as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """CREATE TABLE IF NOT EXISTS venue_account (
                   singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                   account_id TEXT NOT NULL, connector_revision TEXT NOT NULL,
                   rules TEXT NOT NULL, currency TEXT NOT NULL,
                   starting_minor_units INTEGER NOT NULL, environment TEXT NOT NULL,
                   destination_id TEXT NOT NULL, quote INTEGER NOT NULL)"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS venue_execution (
                   order_id TEXT PRIMARY KEY NOT NULL, account_id TEXT NOT NULL,
                   rules TEXT NOT NULL, quantity INTEGER NOT NULL,
                   limit_minor_units INTEGER NOT NULL, fill_id TEXT UNIQUE,
                   fill_price_minor_units INTEGER)"""
            )
            existing = db.execute("SELECT * FROM venue_account").fetchone()
            if existing is not None and self._fee_rate(db) != fee_bps:
                raise VenueRefused("Paper venue fee policy conflicts")
            db.execute(
                """CREATE TABLE IF NOT EXISTS venue_fee_policy (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1), basis_points INTEGER NOT NULL
                )"""
            )
            db.execute(
                "INSERT OR IGNORE INTO venue_fee_policy VALUES (1, ?)", (fee_bps,)
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS venue_fill_fee (
                    order_id TEXT PRIMARY KEY NOT NULL REFERENCES venue_execution(order_id),
                    fee_minor_units INTEGER NOT NULL
                )"""
            )
            if existing is None:
                if account.destination_id:
                    raise VenueRefused(
                        "Cannot provision an existing destination identity"
                    )
                account = replace(account, destination_id=uuid4().hex)
                db.execute(
                    "INSERT INTO venue_account VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*astuple(account), quote_minor_units),
                )
            elif (
                existing[1:-2] != astuple(account)[:-1]
                or existing[-1] != quote_minor_units
                or account.destination_id not in ("", existing[-2])
            ):
                raise VenueRefused("Paper venue configuration conflicts")
            else:
                account = PaperAccount(*existing[1:-1])
        return account

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
            if fill_id is not None and (rate := self._fee_rate(db)):
                fee = (intent.quantity * price * rate + 5000) // 10000
                if fee > 2**63 - 1:
                    raise VenueRefused("Paper venue fee is out of range")
                db.execute("INSERT INTO venue_fill_fee VALUES (?, ?)", (order_id, fee))

    def observe_quote(self, account_id: str) -> int:
        with self._open("ro") as db:
            row = db.execute(
                "SELECT quote FROM venue_account WHERE account_id = ?", (account_id,)
            ).fetchone()
        if row is None:
            raise VenueRefused("Unknown destination account")
        return row[0]

    def observe_order_sizing(self, account_id: str) -> None:
        self.observe_account(account_id)

    def observe_fee_bps(self, account_id: str) -> int:
        with self._open("ro") as db:
            db.execute("BEGIN")
            if not db.execute(
                "SELECT 1 FROM venue_account WHERE account_id = ?", (account_id,)
            ).fetchone():
                raise VenueRefused("Unknown destination account for fee terms")
            return self._fee_rate(db)

    def observe_execution(self, order_id: str) -> VenueExecution | None:
        with self._open("ro") as db:
            db.execute("BEGIN")
            row = db.execute(
                "SELECT * FROM venue_execution WHERE order_id = ?", (order_id,)
            ).fetchone()
            return self._execution_with_fee(db, row)

    def sync_account_execution(
        self, account_id: str, order_id: str
    ) -> VenueExecution | None:
        with self._open("ro") as db:
            db.execute("BEGIN")
            row = db.execute(
                "SELECT * FROM venue_execution WHERE account_id = ? AND order_id = ?",
                (account_id, order_id),
            ).fetchone()
            return self._execution_with_fee(db, row)

    @staticmethod
    def _fee_rate(db: sqlite3.Connection) -> int:
        if not db.execute(
            "SELECT 1 FROM sqlite_schema WHERE name = 'venue_fee_policy' AND type = 'table'"
        ).fetchone():
            return 0
        row = db.execute(
            "SELECT basis_points FROM venue_fee_policy WHERE singleton = 1"
        ).fetchone()
        if row is None or type(row[0]) is not int or not 0 <= row[0] <= 10000:
            raise VenueRefused("Paper venue fee policy is unverified")
        return row[0]

    def _execution_with_fee(
        self, db: sqlite3.Connection, row: tuple | None
    ) -> VenueExecution | None:
        if row is None:
            return None
        if row[-2] is None or self._fee_rate(db) == 0:
            return VenueExecution(*row)
        fee = db.execute(
            "SELECT fee_minor_units FROM venue_fill_fee WHERE order_id = ?", (row[0],)
        ).fetchone()
        if fee is None or type(fee[0]) is not int or fee[0] < 0:
            raise VenueRefused("Missing authoritative fee for confirmed paper fill")
        return VenueExecution(*row, fee[0])
