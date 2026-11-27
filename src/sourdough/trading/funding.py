"""Deterministic 8-hour perpetual funding settlement booking into the company GL."""

from __future__ import annotations

import json
import re
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
from time import time_ns

from ..company import _identity
from ..general_ledger import (
    JournalEntry,
    JournalLine,
    JournalRefused,
    post_journal_in_tx,
)
from ..storage.db import open_reader, open_writer, read_tx, write_tx
from ..storage.kernel import append_audit_event
from ..storage.schema import enable_domain_v1, is_domain_installed
from ..writer_lease import WriterLease

FUNDING_INTERVAL_NS = 8 * 3600 * 10**9
RATE_SCALE = 10**9
MAX_MARK_AGE_NS = 15 * 60 * 10**9
PAPER_FUNDING_SOURCE = "paper-perpetual-funding"
_DAY_NS = 86_400 * 10**9
_EPOCH = date(1970, 1, 1)
_MAX_I64 = 2**63 - 1


class FundingRefused(Exception):
    """The perpetual funding settlement inputs are invalid or conflict with prior books."""


@dataclass(frozen=True)
class FundingRateEvidence:
    """Normalized FundingRateUpdate evidence matching nautilus_trader==1.231.0."""

    instrument_id: str
    interval_end_ns: int
    funding_rate_e9: int
    available_at_ns: int
    source_revision: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.instrument_id, str)
            or not 0 < len(self.instrument_id) <= 128
            or not self.instrument_id.strip()
            or not isinstance(self.source_revision, str)
            or not 0 < len(self.source_revision) <= 128
            or not self.source_revision.strip()
            or type(self.interval_end_ns) is not int
            or not 0 < self.interval_end_ns <= _MAX_I64
            or self.interval_end_ns % FUNDING_INTERVAL_NS != 0
            or type(self.funding_rate_e9) is not int
            or not -RATE_SCALE <= self.funding_rate_e9 <= RATE_SCALE
            or type(self.available_at_ns) is not int
            or not 0 < self.available_at_ns <= _MAX_I64
        ):
            raise FundingRefused("Invalid funding rate evidence")


@dataclass(frozen=True)
class MarkPriceEvidence:
    """Normalized MarkPriceUpdate evidence matching nautilus_trader==1.231.0."""

    instrument_id: str
    observed_ns: int
    mark_price_atoms: int
    quote_units_per_whole: int
    source_revision: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.instrument_id, str)
            or not 0 < len(self.instrument_id) <= 128
            or not self.instrument_id.strip()
            or not isinstance(self.source_revision, str)
            or not 0 < len(self.source_revision) <= 128
            or not self.source_revision.strip()
            or type(self.observed_ns) is not int
            or not 0 < self.observed_ns <= _MAX_I64
            or type(self.mark_price_atoms) is not int
            or not 0 < self.mark_price_atoms <= _MAX_I64
            or type(self.quote_units_per_whole) is not int
            or not 0 < self.quote_units_per_whole <= _MAX_I64
        ):
            raise FundingRefused("Invalid mark price evidence")


@dataclass(frozen=True)
class FundingChart:
    revision: str
    currency: str
    units_per_whole: int
    funding_expense_account: str
    funding_income_account: str
    paper_cash_account: str

    def __post_init__(self) -> None:
        accounts = (
            self.funding_expense_account,
            self.funding_income_account,
            self.paper_cash_account,
        )
        if (
            not isinstance(self.currency, str)
            or re.fullmatch(r"[A-Z][A-Z0-9]{1,11}", self.currency) is None
            or type(self.units_per_whole) is not int
            or not 0 < self.units_per_whole <= _MAX_I64
            or len(set(accounts)) != 3
            or not all(
                isinstance(token, str) and 0 < len(token) <= 128 and bool(token.strip())
                for token in (self.revision, *accounts)
            )
        ):
            raise FundingRefused("Invalid funding chart configuration")


@dataclass(frozen=True)
class FundingSettlement:
    settlement_id: str
    company_id: str
    destination_id: str
    account_id: str
    instrument_id: str
    interval_end_ns: int
    funding_rate_e9: int | None
    mark_price_atoms: int | None
    signed_position_lots: int
    funding_cashflow_atoms: int | None
    status: str
    unavailable_reason: str | None
    evidence_sha256: str
    journal_id: str | None
    recorded_at_ns: int


def _digest(payload: object) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def compute_funding_cashflow_atoms(
    signed_position_lots: int,
    mark_price_atoms: int,
    funding_rate_e9: int,
) -> int:
    """Compute exact signed quote-atom funding cashflow (long pays positive rate)."""
    if (
        type(signed_position_lots) is not int
        or abs(signed_position_lots) >= 2**62
        or type(mark_price_atoms) is not int
        or not 0 < mark_price_atoms <= _MAX_I64
        or type(funding_rate_e9) is not int
        or not -RATE_SCALE <= funding_rate_e9 <= RATE_SCALE
    ):
        raise FundingRefused("Funding cashflow requires bounded exact integer inputs")
    if signed_position_lots == 0 or funding_rate_e9 == 0:
        return 0
    exact = Fraction(
        -signed_position_lots * mark_price_atoms * funding_rate_e9, RATE_SCALE
    )
    cashflow = int(round(exact))
    if abs(cashflow) > _MAX_I64:
        raise FundingRefused("Funding cashflow exceeds 64-bit integer ledger bounds")
    return cashflow


def book_funding_settlement(
    writer: WriterLease,
    *,
    destination_id: str,
    account_id: str,
    instrument_id: str,
    interval_end_ns: int,
    signed_position_lots: int,
    funding: FundingRateEvidence | None,
    mark: MarkPriceEvidence | None,
    chart: FundingChart,
) -> FundingSettlement:
    """Atomically book one 8h funding interval into trading_funding_settlement and GL.

    Missing, stale, or lookahead funding/mark inputs record explicit status='unavailable'
    with funding_cashflow_atoms=None (never substituting 0).
    """
    writer.verify()
    if (
        not all(
            isinstance(value, str) and 0 < len(value) <= 128 and bool(value.strip())
            for value in (destination_id, account_id, instrument_id)
        )
        or type(interval_end_ns) is not int
        or not 0 < interval_end_ns <= _MAX_I64
        or interval_end_ns % FUNDING_INTERVAL_NS != 0
        or type(signed_position_lots) is not int
        or abs(signed_position_lots) >= 2**62
    ):
        raise FundingRefused(
            "Funding settlement requires valid scope and exact 8h UTC boundary"
        )
    if mark is not None and mark.quote_units_per_whole != chart.units_per_whole:
        raise FundingRefused("Mark price units do not match funding chart units")

    try:
        effective_day = _EPOCH + timedelta(days=interval_end_ns // _DAY_NS)
    except OverflowError as error:
        raise FundingRefused(
            "Funding interval timestamp is outside calendar range"
        ) from error

    unavailable_reason: str | None = None
    if funding is None:
        unavailable_reason = "missing-funding-rate"
    elif mark is None:
        unavailable_reason = "missing-mark-price"
    elif (
        funding.instrument_id != instrument_id
        or mark.instrument_id != instrument_id
        or funding.interval_end_ns != interval_end_ns
    ):
        unavailable_reason = "mismatched-funding-evidence"
    elif funding.available_at_ns > interval_end_ns:
        unavailable_reason = "lookahead-funding-rate"
    elif mark.observed_ns > interval_end_ns:
        unavailable_reason = "lookahead-mark-price"
    elif interval_end_ns - mark.observed_ns > MAX_MARK_AGE_NS:
        unavailable_reason = "stale-mark-price"

    if unavailable_reason is not None:
        status = "unavailable"
        cashflow: int | None = None
        # Do not persist rejected/mismatched/lookahead values as authoritative interval figures.
        rate_e9: int | None = None
        mark_atoms: int | None = None
    else:
        assert funding is not None and mark is not None
        rate_e9 = funding.funding_rate_e9
        mark_atoms = mark.mark_price_atoms
        cashflow = compute_funding_cashflow_atoms(
            signed_position_lots, mark_atoms, rate_e9
        )
        status = "zero" if cashflow == 0 else "booked"

    settlement_id = _digest(
        (
            writer.company_id,
            destination_id,
            account_id,
            instrument_id,
            interval_end_ns,
        )
    )
    evidence_sha256 = _digest(
        {
            "destination_id": destination_id,
            "account_id": account_id,
            "instrument_id": instrument_id,
            "interval_end_ns": interval_end_ns,
            "signed_position_lots": signed_position_lots,
            "funding": asdict(funding) if funding is not None else None,
            "mark": asdict(mark) if mark is not None else None,
            "chart": asdict(chart),
            "status": status,
            "unavailable_reason": unavailable_reason,
            "funding_cashflow_atoms": cashflow,
        }
    )

    with (
        closing(open_writer(writer.store, lease=writer)) as db,
        write_tx(db),
    ):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise FundingRefused("Funding settlement belongs to another company")
        enable_domain_v1(
            db, "trading", company_id=writer.company_id, actor_id="trading_funding"
        )
        existing = db.execute(
            """SELECT settlement_id, company_id, destination_id, account_id,
                      instrument_id, interval_end_ns, funding_rate_e9,
                      mark_price_atoms, signed_position_lots, funding_cashflow_atoms,
                      status, unavailable_reason, evidence_sha256, journal_id,
                      recorded_at_ns
               FROM trading_funding_settlement
               WHERE settlement_id = ?""",
            (settlement_id,),
        ).fetchone()
        if existing is not None:
            prior = FundingSettlement(*existing)
            if prior.evidence_sha256 != evidence_sha256:
                raise FundingRefused(
                    "Funding settlement conflicts with previously recorded interval"
                )
            writer.verify()
            return prior

        journal_id: str | None = None
        if status == "booked":
            assert cashflow is not None and cashflow != 0
            if cashflow < 0:
                lines = (
                    JournalLine(chart.funding_expense_account, -cashflow),
                    JournalLine(chart.paper_cash_account, cashflow),
                )
            else:
                lines = (
                    JournalLine(chart.paper_cash_account, cashflow),
                    JournalLine(chart.funding_income_account, -cashflow),
                )
            try:
                entry = JournalEntry(
                    company_id=writer.company_id,
                    source=PAPER_FUNDING_SOURCE,
                    source_event=settlement_id,
                    evidence_revision=evidence_sha256,
                    effective_date=effective_day,
                    currency=chart.currency,
                    mapping_revision=chart.revision,
                    lines=lines,
                    units_per_whole=chart.units_per_whole,
                )
                journal_id = post_journal_in_tx(db, entry, actor_id="trading_funding")
            except JournalRefused as error:
                raise FundingRefused(str(error)) from error

        now_ns = time_ns()
        db.execute(
            """INSERT INTO trading_funding_settlement (
                   settlement_id, company_id, destination_id, account_id,
                   instrument_id, interval_end_ns, funding_rate_e9,
                   mark_price_atoms, signed_position_lots, funding_cashflow_atoms,
                   status, unavailable_reason, evidence_sha256, journal_id,
                   recorded_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                settlement_id,
                writer.company_id,
                destination_id,
                account_id,
                instrument_id,
                interval_end_ns,
                rate_e9,
                mark_atoms,
                signed_position_lots,
                cashflow,
                status,
                unavailable_reason,
                evidence_sha256,
                journal_id,
                now_ns,
            ),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="trading",
            entity_type="trading_funding_settlement",
            entity_id=settlement_id,
            event_type=(
                "trading.funding_unavailable"
                if status == "unavailable"
                else "trading.funding_settled"
            ),
            actor_id="trading_funding",
            payload={
                "settlement_id": settlement_id,
                "instrument_id": instrument_id,
                "interval_end_ns": interval_end_ns,
                "status": status,
                "funding_cashflow_atoms": cashflow,
                "unavailable_reason": unavailable_reason,
                "journal_id": journal_id,
            },
            recorded_at_ns=now_ns,
        )
        writer.verify()
        return FundingSettlement(
            settlement_id=settlement_id,
            company_id=writer.company_id,
            destination_id=destination_id,
            account_id=account_id,
            instrument_id=instrument_id,
            interval_end_ns=interval_end_ns,
            funding_rate_e9=rate_e9,
            mark_price_atoms=mark_atoms,
            signed_position_lots=signed_position_lots,
            funding_cashflow_atoms=cashflow,
            status=status,
            unavailable_reason=unavailable_reason,
            evidence_sha256=evidence_sha256,
            journal_id=journal_id,
            recorded_at_ns=now_ns,
        )


def read_funding_settlements(
    store: Path,
    company_id: str,
    *,
    instrument_id: str | None = None,
) -> tuple[FundingSettlement, ...]:
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise FundingRefused("Funding ledger belongs to another company")
        if not is_domain_installed(db, "trading"):
            return ()
        if instrument_id is None:
            rows = db.execute(
                """SELECT settlement_id, company_id, destination_id, account_id,
                          instrument_id, interval_end_ns, funding_rate_e9,
                          mark_price_atoms, signed_position_lots, funding_cashflow_atoms,
                          status, unavailable_reason, evidence_sha256, journal_id,
                          recorded_at_ns
                   FROM trading_funding_settlement
                   WHERE company_id = ?
                   ORDER BY interval_end_ns ASC, settlement_id ASC""",
                (company_id,),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT settlement_id, company_id, destination_id, account_id,
                          instrument_id, interval_end_ns, funding_rate_e9,
                          mark_price_atoms, signed_position_lots, funding_cashflow_atoms,
                          status, unavailable_reason, evidence_sha256, journal_id,
                          recorded_at_ns
                   FROM trading_funding_settlement
                   WHERE company_id = ? AND instrument_id = ?
                   ORDER BY interval_end_ns ASC, settlement_id ASC""",
                (company_id, instrument_id),
            ).fetchall()
        return tuple(FundingSettlement(*row) for row in rows)
