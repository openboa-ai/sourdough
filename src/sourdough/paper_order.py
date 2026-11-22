"""Admit a bounded order before handing it to the paper destination."""

from contextlib import closing
from dataclasses import astuple, dataclass
from pathlib import Path
from sqlite3 import Connection
from typing import Protocol
from uuid import uuid4

from .company import _connect
from .duty_run import DutyClaim
from .first_duty import require_historical_claim, require_paper_authority
from .paper_account import PaperAccount, reopen_binding
from .paper_decision import has_matching_paper_decision
from .paper_research import allows_research_order
from .writer_lease import WriterLease


class OrderRefused(Exception):
    """The order does not fit the paper mandate."""


@dataclass(frozen=True)
class PaperIntent:
    action_id: str
    instrument_rules_digest: str
    quantity: int
    limit_minor_units: int


@dataclass(frozen=True)
class OrderReceipt:
    order_id: str
    dispatched_now: bool


class PaperDestination(Protocol):
    def observe_account(self, account_id: str) -> PaperAccount: ...

    def submit(self, order_id: str, account_id: str, intent: PaperIntent) -> None: ...


def reserved_exposure(connection: Connection, account_id: str) -> int:
    return connection.execute(
        """SELECT COALESCE(SUM(reserved_minor_units), 0)
           FROM paper_order WHERE account_id = ?""",
        (account_id,),
    ).fetchone()[0]


def _historical_receipt(connection, claim, intent):
    if not connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'paper_order'"
    ).fetchone():
        return None
    previous = connection.execute(
        """SELECT o.order_id, o.instrument_rules_digest, o.quantity, o.limit_minor_units
           FROM paper_order o JOIN duty_run r ON r.run_id = o.run_id
           WHERE r.duty_id = ? AND o.action_id = ?""",
        (claim.duty_id, intent.action_id),
    ).fetchone()
    if previous is None:
        return None
    require_historical_claim(connection, claim)
    if previous[1:] != astuple(intent)[1:]:
        raise OrderRefused("Action identity reused with different terms")
    return OrderReceipt(previous[0], False)


def submit_paper_order(
    store: Path,
    claim: DutyClaim,
    intent: PaperIntent,
    destination: PaperDestination,
    *,
    writer: WriterLease | None = None,
) -> OrderReceipt:
    if writer is not None:
        writer.verify_for(store, claim.company_id)
    if (
        not isinstance(intent.action_id, str)
        or not intent.action_id.strip()
        or type(intent.quantity) is not int
        or type(intent.limit_minor_units) is not int
        or min(intent.quantity, intent.limit_minor_units) <= 0
    ):
        raise OrderRefused("Invalid paper order terms")
    with closing(_connect(store, "ro")) as connection:
        connection.execute("BEGIN")
        receipt = _historical_receipt(connection, claim, intent)
        if receipt is not None:
            return receipt
    observed_binding = reopen_binding(store, claim.company_id)
    account = observed_binding.account
    if destination.observe_account(account.account_id) != account:
        raise OrderRefused("Paper destination mismatch")
    with closing(_connect(store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if writer is not None:
            writer.verify_for(store, claim.company_id)
        receipt = _historical_receipt(connection, claim, intent)
        if receipt is not None:
            return receipt
        require_paper_authority(connection, claim)
        if reopen_binding(store, claim.company_id) != observed_binding:
            raise OrderRefused("Paper binding changed")
        if intent.instrument_rules_digest != account.instrument_rules_digest:
            raise OrderRefused("Instrument does not match paper binding")
        first = connection.execute(
            "SELECT 1 FROM founding_receipt WHERE company_id = ? AND due_work_id = ?",
            (claim.company_id, claim.duty_id),
        ).fetchone()
        if first is not None and not allows_research_order(
            connection,
            claim,
            intent.action_id,
            intent.quantity,
            intent.limit_minor_units,
        ):
            raise OrderRefused(
                "Opening order requires its committed research decision and exact terms"
            )
        if first is None and not has_matching_paper_decision(
            connection,
            claim,
            intent.action_id,
            account.account_id,
            intent.instrument_rules_digest,
            intent.quantity,
            intent.limit_minor_units,
        ):
            raise OrderRefused(
                "Later paper order requires its persisted decision and exact terms"
            )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS paper_order (
                order_id TEXT PRIMARY KEY NOT NULL,
                run_id TEXT NOT NULL REFERENCES duty_run(run_id),
                mandate_revision TEXT NOT NULL,
                account_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                instrument_rules_digest TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                limit_minor_units INTEGER NOT NULL,
                reserved_minor_units INTEGER NOT NULL,
                UNIQUE (run_id, action_id)
            )"""
        )
        reservation = intent.quantity * intent.limit_minor_units
        pending = reserved_exposure(connection, account.account_id)
        if reservation + pending > observed_binding.exposure_limit_minor_units:
            raise OrderRefused("Paper exposure limit exceeded")
        order_id = uuid4().hex
        connection.execute(
            "INSERT INTO paper_order VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                order_id,
                claim.run_id,
                observed_binding.revision,
                account.account_id,
                *astuple(intent),
                reservation,
            ),
        )
    if writer is not None:
        writer.verify_for(store, claim.company_id)
    destination.submit(order_id, account.account_id, intent)
    return OrderReceipt(order_id, True)
