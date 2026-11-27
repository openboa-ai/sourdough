"""Deliver project messages to a durable inbox without transferring work."""

from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from sqlite3 import Connection

from .agent_desk import require_desk
from .company import _connect
from .duty_run import DutyClaim, DutyRefused, WorkRunClaim
from .writer_lease import WriterLease


_VISIBLE = """FROM project_delivery d JOIN project_message m ON m.message_id=d.message_id
    JOIN shared_project p ON p.project_id=m.project_id
    WHERE d.company_id=? AND d.recipient_id=? AND EXISTS
       (SELECT 1 FROM json_each(p.members) WHERE value=d.recipient_id)"""


@dataclass(frozen=True)
class Delivery:
    delivery_id: str
    message_id: str
    project_id: str
    sender_id: str
    read_run: str | None


def enqueue_deliveries(
    database: Connection,
    company_id: str,
    message_id: str,
    sender: str,
    members: tuple[str, ...],
) -> None:
    database.executemany(
        """INSERT INTO project_delivery (
               delivery_id,company_id,message_id,recipient_id,read_run_id
           ) VALUES (?,?,?,?,NULL)""",
        (
            (
                sha256(f"{message_id}:{recipient}".encode()).hexdigest(),
                company_id,
                message_id,
                recipient,
            )
            for recipient in members
            if recipient != sender
        ),
    )


def _check_cursor(value: str, *, allow_empty: bool = False) -> None:
    if allow_empty and value == "":
        return
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DutyRefused("Invalid project delivery reference")


def _check_reference(value: str) -> None:
    if not isinstance(value, str) or not 0 < len(value) <= 128 or not value.strip():
        raise DutyRefused("Invalid project discussion reference")


def read_inbox(
    writer: WriterLease,
    claim: DutyClaim | WorkRunClaim,
    purpose: str,
    after: str,
) -> tuple[tuple[Delivery, ...], str]:
    _check_cursor(after, allow_empty=True)
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        require_desk(database, claim, purpose)
        rows = database.execute(
            """SELECT d.delivery_id,d.message_id,m.project_id,m.sender_id,d.read_run_id """
            + _VISIBLE
            + """ AND (?='' OR d.rowid>(
                SELECT rowid FROM project_delivery
                WHERE company_id=? AND recipient_id=? AND delivery_id=?
               )) ORDER BY d.rowid LIMIT 17""",
            (
                claim.company_id,
                claim.founder_id,
                after,
                claim.company_id,
                claim.founder_id,
                after,
            ),
        ).fetchall()
        page = tuple(Delivery(*row) for row in rows[:16])
        return page, page[-1].delivery_id if len(rows) > 16 else ""


def acknowledge_delivery(
    writer: WriterLease,
    claim: DutyClaim | WorkRunClaim,
    purpose: str,
    delivery_id: str,
    *,
    expected_membership_revision: str | None = None,
) -> Delivery:
    _check_cursor(delivery_id)
    if expected_membership_revision is not None:
        _check_reference(expected_membership_revision)
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        require_desk(database, claim, purpose)
        row = database.execute(
            """SELECT d.delivery_id,d.message_id,m.project_id,m.sender_id,d.read_run_id,p.revision """
            + _VISIBLE
            + " AND d.delivery_id=?",
            (claim.company_id, claim.founder_id, delivery_id),
        ).fetchone()
        if row is None:
            raise DutyRefused("Project delivery is unavailable")
        if (
            expected_membership_revision is not None
            and row[5] != expected_membership_revision
        ):
            raise DutyRefused("Project discussion membership changed")
        if row[4] is not None:
            return Delivery(*row[:5])
        database.execute(
            "UPDATE project_delivery SET read_run_id=? WHERE company_id=? AND delivery_id=?",
            (claim.run_id, claim.company_id, delivery_id),
        )
        writer.verify()
        return Delivery(*row[:4], claim.run_id)
