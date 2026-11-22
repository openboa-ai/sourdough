"""Post a confirmed full fill from the first-trade paper destination."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from .company import _connect, _identity
from .paper_account import reopen_binding

if TYPE_CHECKING:
    from .paper_venue import FixedQuoteVenue


class PostingRefused(Exception):
    """Destination evidence does not match company books."""


@dataclass(frozen=True)
class Posting:
    fill_id: str
    order_id: str
    quantity: int
    price_minor_units: int
    fee_minor_units: int = 0


@dataclass(frozen=True)
class PositionObservation:
    fill_id: str
    order_id: str
    account_id: str
    instrument_rules_digest: str
    fill_quantity: int
    position_quantity: int
    available_at: int
    position_units: str = "instrument_units"

    @property
    def revision(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True).encode()
        return sha256(encoded).hexdigest()


def post_confirmed_fill(
    store: Path,
    company_id: str,
    order_id: str,
    venue: FixedQuoteVenue,
    *,
    account_sync: bool = False,
) -> Posting | None:
    binding = reopen_binding(store, company_id)
    if venue.observe_account(binding.account.account_id) != binding.account:
        raise PostingRefused("Destination account mismatch")
    observed = (
        venue.sync_account_execution(binding.account.account_id, order_id)
        if account_sync
        else venue.observe_execution(order_id)
    )
    if observed is None or observed.fill_id is None:
        return None
    price = observed.fill_price_minor_units
    if type(price) is not int or not 0 < price <= observed.limit_minor_units:
        raise PostingRefused("Invalid fill price")
    fee = observed.fee_minor_units
    if type(fee) is not int or not 0 <= fee <= 2**63 - 1:
        raise PostingRefused("Invalid fill fee")
    with closing(_connect(store, "rw")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if _identity(db) != company_id or reopen_binding(store, company_id) != binding:
            raise PostingRefused("Company binding changed")
        order = db.execute(
            """SELECT mandate_revision, account_id, instrument_rules_digest,
                      quantity, limit_minor_units
               FROM paper_order WHERE order_id = ?""",
            (order_id,),
        ).fetchone()
        if observed.order_id != order_id or order != (
            binding.revision,
            observed.account_id,
            observed.instrument_rules_digest,
            observed.quantity,
            observed.limit_minor_units,
        ):
            raise PostingRefused("Fill does not match admitted order")
        db.execute(
            """CREATE TABLE IF NOT EXISTS paper_fill (
                fill_id TEXT PRIMARY KEY NOT NULL,
                order_id TEXT UNIQUE NOT NULL REFERENCES paper_order(order_id),
                quantity INTEGER NOT NULL,
                price_minor_units INTEGER NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS paper_fill_fee (
                fill_id TEXT PRIMARY KEY NOT NULL REFERENCES paper_fill(fill_id),
                fee_minor_units INTEGER NOT NULL
            )"""
        )
        terms = (observed.fill_id, order_id, observed.quantity, price)
        existing = db.execute(
            "SELECT * FROM paper_fill WHERE order_id = ? OR fill_id = ?",
            (order_id, observed.fill_id),
        ).fetchall()
        if not existing:
            db.execute("INSERT INTO paper_fill VALUES (?, ?, ?, ?)", terms)
            db.execute(
                "INSERT INTO paper_fill_fee VALUES (?, ?)", (observed.fill_id, fee)
            )
            db.execute(
                "UPDATE paper_order SET reserved_minor_units = ? WHERE order_id = ?",
                (observed.quantity * price, order_id),
            )
        else:
            prior_fee = db.execute(
                "SELECT fee_minor_units FROM paper_fill_fee WHERE fill_id = ?",
                (observed.fill_id,),
            ).fetchone()
            if existing != [terms] or (prior_fee or (0,)) != (fee,):
                raise PostingRefused("Fill conflicts with an existing posting")
    return Posting(*terms, fee)


def position_quantity(store: Path, company_id: str) -> int:
    with closing(_connect(store, "ro")) as db:
        db.execute("BEGIN")
        if _identity(db) != company_id:
            raise PostingRefused("Company identity mismatch")
        if not db.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'paper_fill'"
        ).fetchone():
            return 0
        return db.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM paper_fill"
        ).fetchone()[0]


def capture_position(
    store: Path, company_id: str, fill_id: str, *, now: int
) -> PositionObservation:
    with closing(_connect(store, "rw")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if _identity(db) != company_id:
            raise PostingRefused("Company identity mismatch")
        db.execute(
            """CREATE TABLE IF NOT EXISTS paper_position_observation (
                fill_id TEXT PRIMARY KEY NOT NULL REFERENCES paper_fill(fill_id),
                position_quantity INTEGER NOT NULL,
                available_at INTEGER NOT NULL
            )"""
        )
        db.execute(
            """INSERT INTO paper_position_observation
               SELECT target.fill_id, SUM(booked.quantity), ?
               FROM paper_fill target
               JOIN paper_order source ON source.order_id = target.order_id
               JOIN paper_order related ON related.account_id = source.account_id
                   AND related.instrument_rules_digest = source.instrument_rules_digest
               JOIN paper_fill booked ON booked.order_id = related.order_id
               WHERE target.fill_id = ?
               GROUP BY target.fill_id
               ON CONFLICT(fill_id) DO NOTHING""",
            (now, fill_id),
        )
        return read_position(db, company_id, fill_id)


def read_position(
    connection: sqlite3.Connection, company_id: str, fill_id: str
) -> PositionObservation:
    if _identity(connection) != company_id:
        raise PostingRefused("Company identity mismatch")
    row = connection.execute(
        """SELECT fill.fill_id, fill.order_id, source.account_id,
                  source.instrument_rules_digest, fill.quantity,
                  snapshot.position_quantity, snapshot.available_at
           FROM paper_position_observation snapshot
           JOIN paper_fill fill ON fill.fill_id = snapshot.fill_id
           JOIN paper_order source ON source.order_id = fill.order_id
           WHERE snapshot.fill_id = ?""",
        (fill_id,),
    ).fetchone()
    if row is None:
        raise PostingRefused("Requested book observation does not exist")
    return PositionObservation(*row)
