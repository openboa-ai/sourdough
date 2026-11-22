"""Admit a bounded order before handing it to the paper destination."""

from contextlib import closing
from dataclasses import astuple, dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .company import _connect
from .first_duty import DutyClaim, require_historical_claim, require_paper_authority
from .paper_account import PaperAccount, reopen_binding


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


def _historical_receipt(connection, claim, intent):
    if not connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'paper_order'"
    ).fetchone():
        return None
    previous = connection.execute(
        """SELECT order_id, instrument_rules_digest, quantity, limit_minor_units
           FROM paper_order WHERE run_id = ? AND action_id = ?""",
        (claim.run_id, intent.action_id),
    ).fetchone()
    if previous is None:
        return None
    require_historical_claim(connection, claim)
    if previous[1:] != astuple(intent)[1:]:
        raise OrderRefused("Action identity reused with different terms")
    return OrderReceipt(previous[0], False)


def submit_paper_order(
    store: Path, claim: DutyClaim, intent: PaperIntent, destination: PaperDestination
) -> OrderReceipt:
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
        receipt = _historical_receipt(connection, claim, intent)
        if receipt is not None:
            return receipt
        require_paper_authority(connection, claim)
        if reopen_binding(store, claim.company_id) != observed_binding:
            raise OrderRefused("Paper binding changed")
        if intent.instrument_rules_digest != account.instrument_rules_digest:
            raise OrderRefused("Instrument does not match paper binding")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS paper_order (
                order_id TEXT PRIMARY KEY NOT NULL,
                run_id TEXT NOT NULL REFERENCES first_duty_claim(run_id),
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
        pending = connection.execute(
            """SELECT COALESCE(SUM(reserved_minor_units), 0)
               FROM paper_order WHERE account_id = ?""",
            (account.account_id,),
        ).fetchone()[0]
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
    destination.submit(order_id, account.account_id, intent)
    return OrderReceipt(order_id, True)
