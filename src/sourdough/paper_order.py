"""Admit a bounded order before handing it to the paper destination."""

from contextlib import closing
from dataclasses import astuple, dataclass
from pathlib import Path
from sqlite3 import Connection
from uuid import uuid4

from .company import _connect
from .duty_run import DutyClaim
from .first_duty import require_historical_claim, require_paper_authority
from .onboarding import is_hired_first_duty
from .paper_account import has_pending_binding_change, reopen_binding
from .paper_decision import has_matching_paper_decision
from .paper_execution import PaperDestination, PaperIntent, VenueRefused
from .paper_research import allows_hired_research_order, allows_research_order
from .storage.schema import is_domain_installed, require_domain
from .writer_lease import WriterLease


class OrderRefused(Exception):
    """The order does not fit the paper mandate."""


class OrderReplyUnknown(TimeoutError):
    def __init__(self, order_id: str):
        self.order_id = order_id
        super().__init__("The original paper order outcome remains unknown")


@dataclass(frozen=True)
class OrderReceipt:
    order_id: str
    dispatched_now: bool


def reserved_exposure(connection: Connection, account_id: str) -> int:
    return connection.execute(
        """SELECT COALESCE(SUM(reserved_minor_units), 0)
           FROM paper_order WHERE account_id = ?""",
        (account_id,),
    ).fetchone()[0]


def _historical_receipt(connection, claim, intent):
    if not is_domain_installed(connection, "paper"):
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
    try:
        checked_account = destination.check_order_terms(account.account_id, intent)
    except VenueRefused as error:
        raise OrderRefused("Paper destination refused the order terms") from error
    if checked_account != account:
        raise OrderRefused("Paper destination mismatch")
    with closing(_connect(store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if writer is not None:
            writer.verify_for(store, claim.company_id)
        receipt = _historical_receipt(connection, claim, intent)
        if receipt is not None:
            return receipt
        require_paper_authority(connection, claim)
        if has_pending_binding_change(connection):
            raise OrderRefused("Paper binding change is pending; new sends are fenced")
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
            account=account,
        ):
            raise OrderRefused(
                "Opening order requires its committed research decision and exact terms"
            )
        hired = first is None and is_hired_first_duty(connection, claim)
        if hired and not allows_hired_research_order(
            connection,
            claim,
            intent.action_id,
            intent.quantity,
            intent.limit_minor_units,
            account=account,
        ):
            raise OrderRefused(
                "First hired order requires acceptance, research and a separate request"
            )
        if (
            first is None
            and not hired
            and not has_matching_paper_decision(
                connection,
                claim,
                intent.action_id,
                account.account_id,
                intent.instrument_rules_digest,
                intent.quantity,
                intent.limit_minor_units,
            )
        ):
            raise OrderRefused(
                "Later paper order requires its persisted decision and exact terms"
            )
        require_domain(connection, "paper")
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
    try:
        destination.submit(order_id, account.account_id, intent)
    except TimeoutError as error:
        raise OrderReplyUnknown(order_id) from error
    return OrderReceipt(order_id, True)
