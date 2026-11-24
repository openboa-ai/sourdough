"""Transfer a company-only duty without rebinding its historical owner."""

from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from sqlite3 import Connection
from uuid import uuid4

from .agent_desk import require_desk
from .company import _connect
from .company_assets import SharedAsset, shared_asset_for
from .duty_run import ASSESSMENT_REVIEW, DutyClaim, DutyRefused, has_turn_capacity
from .runtime.work_kernel import sync_duty_terminal, sync_work_item
from .writer_lease import WriterLease


def shared_revision(asset: SharedAsset) -> str:
    return sha256(asset.content.encode()).hexdigest()


@dataclass(frozen=True)
class HandoffOffer:
    request_id: str
    recipient_id: str
    asset_id: str
    expires_at: int
    expected_result: str

    def __post_init__(self):
        if not all(
            isinstance(value, str) and 0 < len(value) <= 128
            for value in (self.request_id, self.recipient_id, self.asset_id)
        ) or (
            type(self.expires_at) is not int
            or not 0 <= self.expires_at < 2**63
            or not isinstance(self.expected_result, str)
            or not 0 < len(self.expected_result.strip()) <= 500
        ):
            raise DutyRefused("Invalid handoff offer")


def offer_handoff(
    writer: WriterLease, claim: DutyClaim, offer: HandoffOffer, *, now: int
) -> str:
    writer.verify_for(writer.store, claim.company_id)
    if offer.recipient_id == claim.founder_id or not 0 <= now < offer.expires_at:
        raise DutyRefused("Handoff requires a distinct recipient and a current offer")
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        require_desk(database, claim, ASSESSMENT_REVIEW)
        recipient = database.execute(
            """SELECT 1 FROM agent a JOIN standing_grant g ON g.agent_id=a.id
               WHERE a.company_id=? AND a.id=? AND g.scope='assessment'""",
            (claim.company_id, offer.recipient_id),
        ).fetchone()
        if recipient is None:
            raise DutyRefused("Recipient has no compatible assessment grant")
        asset = shared_asset_for(database, offer.recipient_id, offer.asset_id)
        if (asset.author_id, asset.duty_id) != (claim.founder_id, claim.duty_id):
            raise DutyRefused("Handoff requires a shared result of the original duty")
        terms = (
            claim.run_id,
            claim.duty_id,
            offer.recipient_id,
            offer.asset_id,
            offer.expires_at,
            offer.expected_result,
        )
        prior = database.execute(
            """SELECT offer_id,sender_run_id,original_work_item_id,recipient_id,
                      asset_id,expires_at,expected_result
               FROM work_handoff WHERE sender_id=? AND request_id=?""",
            (claim.founder_id, offer.request_id),
        ).fetchone()
        if prior is not None:
            if prior[1:] != terms:
                raise DutyRefused("Handoff request conflicts with the original offer")
            return prior[0]
        offer_id = uuid4().hex
        database.execute(
            """INSERT INTO work_handoff (
                   offer_id,company_id,request_id,sender_id,sender_run_id,
                   original_work_item_id,recipient_id,asset_id,expires_at,
                   expected_result,successor_work_item_id,accepted_run_id
               ) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)""",
            (offer_id, claim.company_id, offer.request_id, claim.founder_id, *terms),
        )
        writer.verify()
        return offer_id


def _authorized_recipient(database: Connection, claim: DutyClaim, purpose: str) -> None:
    if purpose not in ("assessment", ASSESSMENT_REVIEW):
        raise DutyRefused("Only a company assessment duty can receive this handoff")
    require_desk(database, claim, purpose)
    if database.execute(
        "SELECT scope FROM standing_grant WHERE id=? AND agent_id=?",
        (claim.grant_id, claim.founder_id),
    ).fetchone() != ("assessment",):
        raise DutyRefused("Recipient has no current assessment grant")


def accepted_handoff(
    database: Connection, claim: DutyClaim, asset_id: str
) -> dict[str, str] | None:
    row = database.execute(
        """SELECT offer_id,sender_id,expected_result FROM work_handoff
           WHERE successor_work_item_id=? AND recipient_id=? AND asset_id=?""",
        (claim.duty_id, claim.founder_id, asset_id),
    ).fetchone()
    if row is None:
        return None
    return dict(zip(("offer_id", "sender_id", "expected_result"), row, strict=True))


_READY_OFFERS = """FROM work_handoff o
    JOIN work_review_decision review ON review.duty_id=o.original_work_item_id AND review.run_id=o.sender_run_id
    JOIN work_follow_up w ON w.previous_duty_id=review.duty_id AND w.grant_scope='assessment'
    JOIN duty d ON d.id=w.duty_id AND d.agent_id=o.sender_id AND d.state='scheduled'
    JOIN standing_grant grant ON grant.id=w.grant_id AND grant.agent_id=d.agent_id AND grant.scope=w.grant_scope
    JOIN shared_publication a ON a.asset_id=o.asset_id
    JOIN shared_project p ON p.project_id=a.project_id
    WHERE o.recipient_id=? AND o.expires_at>? AND o.successor_work_item_id IS NULL AND w.purpose=?
      AND EXISTS (SELECT 1 FROM json_each(p.members) WHERE value=o.recipient_id)
      AND EXISTS (SELECT 1 FROM json_each(a.audience) WHERE value=o.recipient_id)"""


def discover_handoffs(
    writer: WriterLease, claim: DutyClaim, purpose: str, *, now: int
) -> list[dict]:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        _authorized_recipient(database, claim, purpose)
        names = (
            "offer_id",
            "sender_id",
            "asset_id",
            "expected_result",
            "due_at",
            "expires_at",
        )
        return [
            dict(zip(names, row, strict=True))
            for row in database.execute(
                """SELECT o.offer_id,o.sender_id,o.asset_id,o.expected_result,w.due_at,o.expires_at """
                + _READY_OFFERS
                + " ORDER BY w.due_at,o.offer_id LIMIT 32",
                (claim.founder_id, now, ASSESSMENT_REVIEW),
            )
        ]


def accept_handoff(
    writer: WriterLease,
    claim: DutyClaim,
    purpose: str,
    offer_id: str,
    *,
    now: int,
) -> str:
    if not isinstance(offer_id, str) or not 0 < len(offer_id) <= 128:
        raise DutyRefused("Invalid handoff offer reference")
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        _authorized_recipient(database, claim, purpose)
        prior = database.execute(
            """SELECT successor_work_item_id, original_work_item_id, sender_run_id
               FROM work_handoff WHERE offer_id=? AND recipient_id=?""",
            (offer_id, claim.founder_id),
        ).fetchone()
        if prior is None:
            raise DutyRefused("Handoff offer is unavailable")
        if prior[0] is not None:
            return prior[0]
        if (
            database.execute(
                """SELECT 1 FROM work_review_decision
                   WHERE duty_id=? AND run_id=?""",
                (prior[1], prior[2]),
            ).fetchone()
            is None
        ):
            raise DutyRefused("Handoff offer is unavailable")
        if now < 0 or not has_turn_capacity(database, claim.founder_id):
            raise DutyRefused("Handoff is outside the recipient's work allocation")
        current = database.execute(
            "SELECT d.id,w.due_at,o.asset_id " + _READY_OFFERS + " AND o.offer_id=?",
            (claim.founder_id, now, ASSESSMENT_REVIEW, offer_id),
        ).fetchone()
        if current is None:
            raise DutyRefused("Handoff offer is not actionable")
        original, due_at, asset_id = current
        asset = shared_asset_for(database, claim.founder_id, asset_id)
        successor = uuid4().hex
        database.execute(
            "INSERT INTO duty VALUES (?,?,'scheduled')", (successor, claim.founder_id)
        )
        sync_work_item(
            database,
            company_id=claim.company_id,
            work_item_id=successor,
            owner_agent_id=claim.founder_id,
            duty_kind="work.assessment_review",
            state="scheduled",
            source_kind="handoff",
            source_id=offer_id,
        )
        database.execute(
            """INSERT INTO work_follow_up VALUES (?,?,?,?,?,'shared_asset',?,?,?,?)""",
            (
                original,
                successor,
                claim.grant_id,
                "assessment",
                max(due_at, now),
                asset_id,
                shared_revision(asset),
                now,
                ASSESSMENT_REVIEW,
            ),
        )
        database.execute("UPDATE duty SET state='handed_off' WHERE id=?", (original,))
        database.execute(
            "UPDATE duty_run SET is_current=0 WHERE duty_id=?", (original,)
        )
        sync_duty_terminal(
            database, original, item_state="cancelled", run_status="fenced"
        )
        database.execute(
            "UPDATE work_handoff SET successor_work_item_id=?,accepted_run_id=? WHERE offer_id=?",
            (successor, claim.run_id, offer_id),
        )
        writer.verify()
        return successor
