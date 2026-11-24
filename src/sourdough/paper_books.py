"""Post identified paper fills and retain exposure on unfinished orders."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from time import time
from typing import TYPE_CHECKING

from .company import _connect, _identity
from .paper_account import read_applied_binding, reopen_binding
from .paper_execution import TERMINAL_OUTCOMES, observe_bound_position
from .storage.schema import is_domain_installed, require_domain

if TYPE_CHECKING:
    from .paper_execution import (
        PaperExecution,
        TerminalOutcome,
        VenueCancellation,
        VenueExecution,
        VenuePosition,
    )
    from .writer_lease import WriterLease


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


@dataclass(frozen=True)
class NoFillObservation:
    order_id: str
    event_id: str
    account_id: str
    instrument_rules_digest: str
    position_quantity: int
    available_at: int
    fill_quantity: int = 0
    status: TerminalOutcome = "cancelled"
    position_units: str = "instrument_units"

    @property
    def revision(self) -> str:
        return sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class BindingPosition:
    binding_revision: str
    account_id: str
    instrument_rules_digest: str
    available_at: int
    position_quantity: int = 0
    position_units: str = "instrument_units"

    @property
    def revision(self) -> str:
        return sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def read_binding_position(
    connection: sqlite3.Connection, company_id: str, revision: str
) -> BindingPosition | None:
    evidence = read_applied_binding(connection, company_id, revision)
    if evidence is None:
        return None
    account, effective_ns = evidence
    return BindingPosition(
        revision,
        account.account_id,
        account.instrument_rules_digest,
        (effective_ns + 10**9 - 1) // 10**9,
    )


FILLED_QUANTITY = (
    "COALESCE((SELECT SUM(quantity) FROM paper_fill WHERE order_id=o.order_id),0)"
)


def _post_fill(db: sqlite3.Connection, order_id: str, observed: VenueExecution) -> None:
    if not isinstance(observed.fill_id, str) or not observed.fill_id:
        raise PostingRefused("Invalid fill identity")
    price = observed.fill_price_minor_units
    filled = (
        observed.quantity
        if observed.filled_quantity is None
        else observed.filled_quantity
    )
    if (
        type(observed.quantity) is not int
        or type(filled) is not int
        or not 0 < filled <= observed.quantity
    ):
        raise PostingRefused("Invalid fill quantity")
    if (
        type(observed.limit_minor_units) is not int
        or type(price) is not int
        or not 0 < price <= observed.limit_minor_units
    ):
        raise PostingRefused("Invalid fill price")
    fee = observed.fee_minor_units
    if type(fee) is not int or not 0 <= fee <= 2**63 - 1:
        raise PostingRefused("Invalid fill fee")
    terms = (observed.fill_id, order_id, filled, price)
    existing = db.execute(
        "SELECT * FROM paper_fill WHERE fill_id = ?", (observed.fill_id,)
    ).fetchone()
    if existing is not None:
        prior_fee = db.execute(
            "SELECT fee_minor_units FROM paper_fill_fee WHERE fill_id = ?",
            (observed.fill_id,),
        ).fetchone()
        if existing != terms or (prior_fee or (0,)) != (fee,):
            raise PostingRefused("Fill conflicts with an existing posting")
        return
    booked, spent = db.execute(
        """SELECT COALESCE(SUM(quantity),0),COALESCE(SUM(quantity*price_minor_units),0)
           FROM paper_fill WHERE order_id=?""",
        (order_id,),
    ).fetchone()
    remaining = observed.quantity - booked - filled
    if remaining < 0:
        raise PostingRefused("Cumulative fills exceed the admitted order")
    db.execute("INSERT INTO paper_fill VALUES (?, ?, ?, ?)", terms)
    db.execute("INSERT INTO paper_fill_fee VALUES (?, ?)", (observed.fill_id, fee))
    db.execute(
        "UPDATE paper_order SET reserved_minor_units = ? WHERE order_id = ?",
        (spent + filled * price + remaining * observed.limit_minor_units, order_id),
    )


def _apply_cancellation(
    db: sqlite3.Connection,
    order_id: str,
    order: tuple,
    evidence: VenueCancellation | None,
    position: VenuePosition | None,
) -> None:
    if evidence is not None:
        if (
            evidence.order_id != order_id
            or (evidence.account_id, evidence.instrument_rules_digest) != order[1:3]
            or not isinstance(evidence.event_id, str)
            or not evidence.event_id
            or type(evidence.filled_quantity) is not int
            or not 0 <= evidence.filled_quantity < order[3]
            or evidence.outcome not in TERMINAL_OUTCOMES
            or (evidence.outcome in ("rejected", "denied") and evidence.filled_quantity)
        ):
            raise PostingRefused("Cancellation does not match the original order")
        previous = db.execute(
            """SELECT order_id,event_id,filled_quantity,outcome
               FROM paper_cancellation WHERE order_id=? OR event_id=?""",
            (order_id, evidence.event_id),
        ).fetchall()
        terms = (
            order_id,
            evidence.event_id,
            evidence.filled_quantity,
            evidence.outcome,
        )
        if not previous:
            db.execute(
                """INSERT INTO paper_cancellation
                   (order_id,event_id,filled_quantity,outcome,released) VALUES (?,?,?,?,0)""",
                terms,
            )
        elif previous != [terms]:
            raise PostingRefused(
                "Cancellation conflicts with recorded terminal evidence"
            )
    pending = db.execute(
        "SELECT filled_quantity,released,available_at FROM paper_cancellation WHERE order_id=?",
        (order_id,),
    ).fetchone()
    if pending is None:
        return
    cumulative, released, available_at = pending
    booked, spent = db.execute(
        """SELECT COALESCE(SUM(quantity),0),COALESCE(SUM(quantity*price_minor_units),0)
           FROM paper_fill WHERE order_id=?""",
        (order_id,),
    ).fetchone()
    if booked > cumulative:
        raise PostingRefused("Fill quantity exceeds recorded terminal evidence")
    if booked == cumulative and not released:
        db.execute(
            "UPDATE paper_order SET reserved_minor_units=? WHERE order_id=?",
            (spent, order_id),
        )
        db.execute(
            "UPDATE paper_cancellation SET released=1 WHERE order_id=?", (order_id,)
        )
    if booked == cumulative == 0 and available_at is None and position is not None:
        db.execute(
            "UPDATE paper_cancellation SET position_quantity=?,available_at=? WHERE order_id=?",
            (position.position_quantity, int(time()), order_id),
        )


def read_no_fill(
    connection: sqlite3.Connection, company_id: str, order_id: str
) -> NoFillObservation | None:
    if _identity(connection) != company_id:
        raise PostingRefused("Company identity mismatch")
    if not is_domain_installed(connection, "paper"):
        return None
    row = connection.execute(
        """SELECT o.order_id,c.event_id,o.account_id,o.instrument_rules_digest,
                  c.position_quantity,c.available_at,c.outcome
           FROM paper_cancellation c JOIN paper_order o USING(order_id)
           WHERE c.order_id=? AND c.released=1 AND c.filled_quantity=0 AND c.available_at IS NOT NULL""",
        (order_id,),
    ).fetchone()
    return NoFillObservation(*row[:-1], status=row[-1]) if row else None


def read_booked_no_fill(
    store: Path, company_id: str, order_id: str
) -> NoFillObservation | None:
    with closing(_connect(store, "ro")) as db:
        db.execute("BEGIN")
        return read_no_fill(db, company_id, order_id)


def read_book_observation(
    connection: sqlite3.Connection, company_id: str, kind: str, resource_id: str
) -> PositionObservation | NoFillObservation | BindingPosition:
    if kind == "paper_binding":
        baseline = read_binding_position(connection, company_id, resource_id)
        if baseline is not None:
            return baseline
    if kind == "paper_position":
        return read_position(connection, company_id, resource_id)
    if kind == "paper_no_fill":
        no_fill = read_no_fill(connection, company_id, resource_id)
        if no_fill is not None:
            return no_fill
    raise PostingRefused("Requested book observation does not exist")


def resolved_order_sql(db: sqlite3.Connection) -> str:
    if not is_domain_installed(db, "paper"):
        return "0"
    return f"({FILLED_QUANTITY}=o.quantity OR EXISTS (SELECT 1 FROM paper_cancellation c WHERE c.order_id=o.order_id AND c.released=1))"


def pending_no_fill_observation_sql(db: sqlite3.Connection) -> str:
    if not is_domain_installed(db, "paper"):
        return "0"
    return """EXISTS (
        SELECT 1 FROM paper_cancellation c JOIN duty_run r ON r.run_id=o.run_id
        JOIN duty d ON d.id=r.duty_id
        WHERE c.order_id=o.order_id AND c.released=1 AND c.filled_quantity=0
          AND c.available_at IS NULL AND d.state='running'
    )"""


def post_confirmed_fill(
    store: Path,
    company_id: str,
    order_id: str,
    venue: PaperExecution,
    *,
    account_sync: bool = False,
    writer: WriterLease | None = None,
) -> Posting | None:
    binding = reopen_binding(store, company_id)
    if venue.observe_account(binding.account.account_id) != binding.account:
        raise PostingRefused("Destination account mismatch")
    observations = venue.read_fills(
        binding.account.account_id, order_id, account_sync=account_sync
    )
    cancellation = venue.read_cancellation(
        binding.account.account_id, order_id, account_sync=account_sync
    )
    if not observations and cancellation is None:
        return None
    position = (
        observe_bound_position(venue, binding.account, order_id)
        if cancellation is not None
        and (cancellation.filled_quantity == 0 or cancellation.outcome != "cancelled")
        else None
    )
    if (
        cancellation is not None
        and cancellation.outcome != "cancelled"
        and position is None
    ):
        cancellation = None
    with closing(_connect(store, "rw")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if writer is not None:
            writer.verify_for(store, company_id)
        if _identity(db) != company_id or reopen_binding(store, company_id) != binding:
            raise PostingRefused("Company binding changed")
        require_domain(db, "paper")
        order = db.execute(
            """SELECT mandate_revision, account_id, instrument_rules_digest,
                      quantity, limit_minor_units
               FROM paper_order WHERE order_id = ?""",
            (order_id,),
        ).fetchone()
        if order is None or order[:2] != (binding.revision, binding.account.account_id):
            raise PostingRefused("Order evidence does not match an admitted order")
        for observed in observations:
            if observed.order_id != order_id or order != (
                binding.revision,
                observed.account_id,
                observed.instrument_rules_digest,
                observed.quantity,
                observed.limit_minor_units,
            ):
                raise PostingRefused("Fill does not match admitted order")
            _post_fill(db, order_id, observed)
        _apply_cancellation(db, order_id, order, cancellation, position)
        return _completed_posting(db, order_id)


def position_quantity(store: Path, company_id: str) -> int:
    with closing(_connect(store, "ro")) as db:
        db.execute("BEGIN")
        if _identity(db) != company_id:
            raise PostingRefused("Company identity mismatch")
        if not is_domain_installed(db, "paper"):
            return 0
        return db.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM paper_fill"
        ).fetchone()[0]


def _completed_posting(connection: sqlite3.Connection, order_id: str) -> Posting | None:
    if not is_domain_installed(connection, "paper"):
        return None
    row = connection.execute(
        f"""SELECT p.fill_id,p.order_id,p.quantity,p.price_minor_units
            FROM paper_fill p JOIN paper_order o USING(order_id)
            WHERE o.order_id=? AND {resolved_order_sql(connection)}
            ORDER BY p.rowid DESC LIMIT 1""",
        (order_id,),
    ).fetchone()
    if row is None:
        return None
    fee = connection.execute(
        "SELECT fee_minor_units FROM paper_fill_fee WHERE fill_id=?", (row[0],)
    ).fetchone()
    return Posting(*row, fee[0] if fee else 0)


def read_booked_fill(store: Path, company_id: str, order_id: str) -> Posting | None:
    with closing(_connect(store, "ro")) as db:
        db.execute("BEGIN")
        if _identity(db) != company_id:
            raise PostingRefused("Company identity mismatch")
        return _completed_posting(db, order_id)


def capture_position(
    store: Path, company_id: str, fill_id: str, source: VenuePosition, *, now: int
) -> PositionObservation:
    with closing(_connect(store, "rw")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if _identity(db) != company_id:
            raise PostingRefused("Company identity mismatch")
        require_domain(db, "paper")
        db.execute(
            """INSERT INTO paper_position_observation
               SELECT target.fill_id, ?, ?
               FROM paper_fill target
               JOIN paper_order source ON source.order_id = target.order_id
               WHERE target.fill_id = ? AND source.order_id = ? AND source.account_id = ?
                 AND source.instrument_rules_digest = ?
               ON CONFLICT(fill_id) DO NOTHING""",
            (
                source.position_quantity,
                now,
                fill_id,
                source.order_id,
                source.account_id,
                source.instrument_rules_digest,
            ),
        )
        recorded = read_position(db, company_id, fill_id)
        if (
            recorded.order_id,
            recorded.account_id,
            recorded.instrument_rules_digest,
            recorded.position_quantity,
        ) != (
            source.order_id,
            source.account_id,
            source.instrument_rules_digest,
            source.position_quantity,
        ):
            raise PostingRefused(
                "Position evidence conflicts with the recorded observation"
            )
        return recorded


def read_position(
    connection: sqlite3.Connection, company_id: str, fill_id: str
) -> PositionObservation:
    observation = _lookup_position(connection, company_id, fill_id)
    if observation is None:
        raise PostingRefused("Requested book observation does not exist")
    return observation


def read_captured_position(
    store: Path, company_id: str, fill_id: str
) -> PositionObservation | None:
    with closing(_connect(store, "ro")) as db:
        db.execute("BEGIN")
        return _lookup_position(db, company_id, fill_id)


def _lookup_position(
    connection: sqlite3.Connection, company_id: str, fill_id: str
) -> PositionObservation | None:
    if _identity(connection) != company_id:
        raise PostingRefused("Company identity mismatch")
    if not is_domain_installed(connection, "paper"):
        return None
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
    return PositionObservation(*row) if row else None
