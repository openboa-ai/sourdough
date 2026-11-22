"""Post a confirmed full fill from the first-trade paper destination."""

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .company import _connect, _identity
from .paper_account import reopen_binding
from .paper_venue import FixedQuoteVenue


class PostingRefused(Exception):
    """Destination evidence does not match company books."""


@dataclass(frozen=True)
class Posting:
    fill_id: str
    order_id: str
    quantity: int
    price_minor_units: int


def post_confirmed_fill(
    store: Path, company_id: str, order_id: str, venue: FixedQuoteVenue
) -> Posting | None:
    binding = reopen_binding(store, company_id)
    if venue.observe_account(binding.account.account_id) != binding.account:
        raise PostingRefused("Destination account mismatch")
    observed = venue.observe_execution(order_id)
    if observed is None or observed.fill_id is None:
        return None
    price = observed.fill_price_minor_units
    if type(price) is not int or not 0 < price <= observed.limit_minor_units:
        raise PostingRefused("Invalid fill price")
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
        terms = (observed.fill_id, order_id, observed.quantity, price)
        existing = db.execute(
            "SELECT * FROM paper_fill WHERE order_id = ? OR fill_id = ?",
            (order_id, observed.fill_id),
        ).fetchall()
        if not existing:
            db.execute("INSERT INTO paper_fill VALUES (?, ?, ?, ?)", terms)
            db.execute(
                "UPDATE paper_order SET reserved_minor_units = ? WHERE order_id = ?",
                (observed.quantity * price, order_id),
            )
        elif existing != [terms]:
            raise PostingRefused("Fill conflicts with an existing posting")
    return Posting(*terms)


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
