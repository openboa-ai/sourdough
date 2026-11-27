"""Deliver the booked paper fee through the company accounting boundary."""

from __future__ import annotations

import json
import re
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from hashlib import sha256
from sqlite3 import Connection
from typing import TYPE_CHECKING

from ..company import _identity
from ..general_ledger import (
    JournalEntry,
    JournalLine,
    JournalRefused,
    post_journal_in_tx,
)
from ..storage.db import open_writer, write_tx
from ..storage.kernel import append_audit_event
from ..storage.schema import is_domain_installed, require_domain
from ..writer_lease import WriterLease

if TYPE_CHECKING:
    from .nautilus_paper import NautilusPaperVenue


PAPER_FILL_FEE_SOURCE = "paper-fill-fee"
FILL_ACCOUNTING_MAPPING_REVISION = "paper-quote-fee-v1"


@dataclass(frozen=True)
class FillAccountingSource:
    destination_id: str
    account_id: str
    fill_id: str
    order_id: str
    binding_revision: str
    currency: str
    mapping_revision: str
    recorded_at_ns: int
    fee_minor_units: int


def read_fill_accounting_source_in_tx(
    db: Connection, company_id: str, fill_id: str
) -> FillAccountingSource | None:
    if _identity(db) != company_id:
        raise JournalRefused("Company identity mismatch")
    if not is_domain_installed(db, "trading"):
        return None
    row = db.execute(
        """SELECT b.destination_id, b.account_id, f.fill_id, o.order_id,
                  COALESCE(o.mandate_revision, b.binding_revision),
                  b.currency, ?, f.filled_at_ns, f.fee_minor_units
           FROM trading_book_fill f
           JOIN trading_book_order o ON o.order_id = f.order_id
           JOIN execution_environment_binding b
             ON b.binding_id = f.binding_id AND b.environment = f.environment
           WHERE f.company_id = ? AND f.fill_id = ?""",
        (FILL_ACCOUNTING_MAPPING_REVISION, company_id, fill_id),
    ).fetchone()
    return FillAccountingSource(*row) if row is not None else None


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


def deliver_paper_fee(
    writer: WriterLease,
    verified: VerifiedFee,
    chart: FeeChart,
    *,
    order_id: str | None = None,
) -> FeeDelivery:
    """Atomically record trading_fee_delivery, GL journal_entry/journal_line, and audit_event in one write_tx."""
    writer.verify()
    if (
        type(verified.fee_minor_units) is not int
        or not 0 <= verified.fee_minor_units < 2**63
        or type(verified.effective_date) is not date
        or not isinstance(verified.currency, str)
        or verified.currency != chart.currency
        or re.fullmatch(r"[A-Z][A-Z0-9]{1,11}", verified.currency) is None
        or type(verified.units_per_whole) is not int
        or type(chart.units_per_whole) is not int
        or not 0 < verified.units_per_whole == chart.units_per_whole < 2**63
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
    delivery_digest = _digest((asdict(verified), asdict(chart)))

    with (
        closing(open_writer(writer.store, lease=writer)) as database,
        write_tx(database),
    ):
        writer.verify()
        if _identity(database) != writer.company_id:
            raise JournalRefused("Fee delivery belongs to another company")
        source = read_fill_accounting_source_in_tx(
            database, writer.company_id, verified.fill_id
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
            raise JournalRefused(
                "Verified fee does not match its original paper source"
            )
        if order_id is not None and order_id != source.order_id:
            raise JournalRefused("Verified fee does not match its original paper order")
        require_domain(database, "trading")
        source_event = _digest(
            (source.destination_id, source.account_id, source.fill_id)
        )
        amount = source.fee_minor_units
        saved = database.execute(
            """SELECT delivery_digest, acknowledged, journal_id
               FROM trading_fee_delivery WHERE fill_id=?""",
            (source.fill_id,),
        ).fetchone()
        if saved is not None:
            if saved[0] != delivery_digest:
                raise JournalRefused("Fee delivery conflicts with its original terms")
            if bool(saved[1]):
                writer.verify()
                return FeeDelivery(source_event, saved[2], amount)
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
                units_per_whole=chart.units_per_whole,
            )
            if amount
            else None
        )
        journal_id = (
            post_journal_in_tx(database, entry, actor_id="trading_fee_bridge")
            if entry is not None
            else None
        )
        if saved is None:
            database.execute(
                "INSERT INTO trading_fee_delivery VALUES (?,?,1,?)",
                (source.fill_id, delivery_digest, journal_id),
            )
        else:
            database.execute(
                """UPDATE trading_fee_delivery SET acknowledged=1, journal_id=?
                   WHERE fill_id=? AND delivery_digest=? AND acknowledged=0""",
                (journal_id, source.fill_id, delivery_digest),
            )
        append_audit_event(
            database,
            company_id=writer.company_id,
            domain="trading",
            entity_type="trading_fee_delivery",
            entity_id=source.fill_id,
            event_type="trading.fee_delivered",
            actor_id="trading_fee_bridge",
            payload={
                "fill_id": source.fill_id,
                "source_event": source_event,
                "journal_id": journal_id,
                "amount_minor": amount,
                "currency": chart.currency,
                "units_per_whole": chart.units_per_whole,
            },
        )
        writer.verify()
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
        raise JournalRefused(
            "Managed paper fee requires an independently timed original fill"
        )
    try:
        effective_day = date(1970, 1, 1) + timedelta(
            days=evidence.effective_ns // (86_400 * 10**9)
        )
    except OverflowError as error:
        raise JournalRefused(
            "Managed paper fee timestamp is outside calendar range"
        ) from error
    revision = _digest(asdict(evidence))
    return deliver_paper_fee(
        writer,
        VerifiedFee(
            evidence.destination_id,
            evidence.account_id,
            evidence.fill_id,
            revision,
            effective_day,
            evidence.currency,
            evidence.units_per_whole,
            evidence.fee_atoms,
        ),
        chart,
        order_id=evidence.order_id,
    )
