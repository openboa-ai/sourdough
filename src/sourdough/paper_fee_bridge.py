"""Deliver the booked paper fee through the company accounting boundary."""

from __future__ import annotations

import json
import re
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING

from .company import _connect
from .general_ledger import JournalEntry, JournalLine, JournalRefused, post_journal
from .paper_books import read_fill_accounting_source
from .writer_lease import WriterLease

if TYPE_CHECKING:
    from .nautilus_paper import NautilusPaperVenue


PAPER_FILL_FEE_SOURCE = "paper-fill-fee"


PAPER_FILL_FEE_SOURCE = "paper-fill-fee"


@dataclass(frozen=True)
class VerifiedFee:
    destination_id: str
    account_id: str
    fill_id: str
    revision: str
    effective_date: date
    currency: str
    units_per_whole: int
    fee_minor_units: int


@dataclass(frozen=True)
class FeeChart:
    revision: str
    source_mapping_revision: str
    currency: str
    units_per_whole: int
    fee_expense: str
    paper_cash: str


@dataclass(frozen=True)
class FeeDelivery:
    source_event: str
    journal_id: str | None
    amount_minor: int


def _digest(value) -> str:
    body = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=date.isoformat
    )
    return sha256(body.encode()).hexdigest()


def _prepare(
    writer: WriterLease, fill_id: str, delivery_digest: str
) -> tuple[bool, str | None]:
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        writer.verify()
        database.execute(
            """CREATE TABLE IF NOT EXISTS paper_fee_delivery (
                fill_id TEXT PRIMARY KEY NOT NULL REFERENCES paper_fill_accounting_source(fill_id),
                delivery_digest TEXT NOT NULL,
                acknowledged INTEGER NOT NULL CHECK(acknowledged IN (0, 1)),
                journal_id TEXT
            )"""
        )
        saved = database.execute(
            """SELECT delivery_digest,acknowledged,journal_id
               FROM paper_fee_delivery WHERE fill_id=?""",
            (fill_id,),
        ).fetchone()
        if saved:
            if saved[0] != delivery_digest:
                raise JournalRefused("Fee delivery conflicts with its original terms")
            writer.verify()
            return bool(saved[1]), saved[2]
        database.execute(
            "INSERT INTO paper_fee_delivery VALUES (?,?,0,NULL)",
            (fill_id, delivery_digest),
        )
        writer.verify()
    return False, None


def _acknowledge(
    writer: WriterLease, fill_id: str, delivery_digest: str, journal_id: str | None
) -> None:
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        writer.verify()
        effect = database.execute(
            """UPDATE paper_fee_delivery SET acknowledged=1,journal_id=?
               WHERE fill_id=? AND delivery_digest=? AND acknowledged=0""",
            (journal_id, fill_id, delivery_digest),
        )
        if effect.rowcount != 1:
            raise JournalRefused("Expected the original unacknowledged delivery")
        writer.verify()


def deliver_paper_fee(
    writer: WriterLease,
    verified: VerifiedFee,
    chart: FeeChart,
    *,
    order_id: str | None = None,
) -> FeeDelivery:
    """Internal delivery; the caller supplies verified venue evidence and its admitted chart."""
    writer.verify()
    source = read_fill_accounting_source(
        writer.store, writer.company_id, verified.fill_id
    )
    if source is None or (
        source.destination_id,
        source.account_id,
        source.currency,
        source.fee_minor_units,
        source.mapping_revision,
    ) != (
        verified.destination_id,
        verified.account_id,
        verified.currency,
        verified.fee_minor_units,
        chart.source_mapping_revision,
    ):
        raise JournalRefused("Verified fee does not match its original paper source")
    if order_id is not None and order_id != source.order_id:
        raise JournalRefused("Verified fee does not match its original paper order")
    if (
        type(verified.fee_minor_units) is not int
        or type(verified.effective_date) is not date
        or not isinstance(verified.currency, str)
        or verified.currency != chart.currency
        or re.fullmatch(r"[A-Z][A-Z0-9]{1,11}", verified.currency) is None
        or type(verified.units_per_whole) is not int
        or type(chart.units_per_whole) is not int
        or not 0 < verified.units_per_whole == chart.units_per_whole
        or chart.fee_expense == chart.paper_cash
        or not all(
            isinstance(value, str) and 0 < len(value) <= 256 and bool(value.strip())
            for value in (
                verified.revision,
                chart.revision,
                chart.fee_expense,
                chart.paper_cash,
            )
        )
    ):
        raise JournalRefused(
            "Fee delivery requires a verified cut and matching chart units"
        )
    source_event = _digest((source.destination_id, source.account_id, source.fill_id))
    delivery_digest = _digest((asdict(verified), asdict(chart)))
    amount = source.fee_minor_units
    entry = (
        JournalEntry(
            writer.company_id,
            PAPER_FILL_FEE_SOURCE,
            source_event,
            verified.revision,
            verified.effective_date,
            chart.currency,
            chart.revision,
            (
                JournalLine(chart.fee_expense, amount),
                JournalLine(chart.paper_cash, -amount),
            ),
        )
        if amount
        else None
    )
    acknowledged, previous_journal = _prepare(writer, source.fill_id, delivery_digest)
    if acknowledged:
        return FeeDelivery(source_event, previous_journal, amount)
    journal_id = post_journal(writer, entry) if entry is not None else None
    _acknowledge(writer, source.fill_id, delivery_digest, journal_id)
    return FeeDelivery(source_event, journal_id, amount)


def deliver_managed_paper_fee(
    writer: WriterLease,
    venue: NautilusPaperVenue,
    account_id: str,
    fill_id: str,
    chart: FeeChart,
) -> FeeDelivery:
    venue.require_independent_from(writer.store)
    evidence = venue.read_fill_fee(account_id, fill_id)
    if evidence is None:
        raise JournalRefused("No independently timed current-paper fee evidence")
    effective_date = date(1970, 1, 1) + timedelta(
        days=evidence.effective_ns // (86400 * 10**9)
    )
    return deliver_paper_fee(
        writer,
        VerifiedFee(
            evidence.destination_id,
            evidence.account_id,
            evidence.fill_id,
            _digest(asdict(evidence)),
            effective_date,
            evidence.currency,
            evidence.units_per_whole,
            evidence.fee_atoms,
        ),
        chart,
        order_id=evidence.order_id,
    )
