"""Transfer a company-only duty without rebinding its historical owner."""

from contextlib import closing
from dataclasses import dataclass, replace
from hashlib import sha256
from sqlite3 import Connection
from uuid import uuid4

from .agent_desk import require_desk
from .company import _connect
from .company_assets import SharedAsset, shared_asset_for
from .duty_run import (
    ASSESSMENT_REVIEW,
    DutyRefused,
    ExecutionClaim,
    WorkRunClaim,
    has_turn_capacity,
)
from .runtime.work_kernel import (
    create_work_item,
    has_active_grant,
    sync_duty_terminal,
    sync_work_item,
)
from .storage.kernel import append_audit_event
from .writer_lease import WriterLease

_NS_PER_SECOND = 1_000_000_000


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


def _with_clock(claim: ExecutionClaim, now: int) -> ExecutionClaim:
    if isinstance(claim, WorkRunClaim) and claim.current_now_ns() is None:
        return replace(claim, _clock=now * _NS_PER_SECOND)
    return claim


def offer_handoff(
    writer: WriterLease,
    claim: ExecutionClaim,
    offer: HandoffOffer,
    *,
    now: int,
) -> str:
    writer.verify_for(writer.store, claim.company_id)
    if (
        type(now) is not int
        or offer.recipient_id == claim.founder_id
        or not 0 <= now < offer.expires_at
    ):
        raise DutyRefused("Handoff requires a distinct recipient and a current offer")
    if isinstance(claim, WorkRunClaim) and claim.purpose in (
        "peer_response",
        "work.peer_response",
    ):
        raise DutyRefused("Peer response duty cannot offer a handoff")
    eff_purpose = (
        claim.purpose if isinstance(claim, WorkRunClaim) else ASSESSMENT_REVIEW
    )
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        require_desk(database, _with_clock(claim, now), eff_purpose)
        if not has_active_grant(
            database,
            claim.company_id,
            offer.recipient_id,
            required_scope="assessment",
        ):
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
               FROM work_handoff WHERE company_id=? AND sender_id=? AND request_id=?""",
            (claim.company_id, claim.founder_id, offer.request_id),
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
                   expected_result,successor_work_item_id,accepted_run_id,
                   state,state_version
               ) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL,'offered',1)""",
            (offer_id, claim.company_id, offer.request_id, claim.founder_id, *terms),
        )
        append_audit_event(
            database,
            company_id=claim.company_id,
            domain="work",
            entity_type="work_handoff",
            entity_id=offer_id,
            event_type="work.handoff_offered",
            actor_id=claim.founder_id,
            run_id=claim.run_id,
            payload={
                "recipient_id": offer.recipient_id,
                "asset_id": offer.asset_id,
                "expires_at": offer.expires_at,
            },
            recorded_at_ns=now * _NS_PER_SECOND,
        )
        writer.verify()
        return offer_id


def _authorized_recipient(
    database: Connection, claim: ExecutionClaim, purpose: str
) -> None:
    allowed = (
        (
            "assessment",
            ASSESSMENT_REVIEW,
            "work.assessment_review",
            "handoff_followup",
            "work.handoff_followup",
        )
        if isinstance(claim, WorkRunClaim)
        else ("assessment", ASSESSMENT_REVIEW)
    )
    if purpose not in allowed:
        raise DutyRefused("Only a company assessment duty can receive this handoff")
    require_desk(database, claim, purpose)
    if not has_active_grant(
        database,
        claim.company_id,
        claim.founder_id,
        required_scope="assessment",
    ) or database.execute(
        "SELECT scope FROM standing_grant WHERE id=? AND agent_id=?",
        (claim.grant_id, claim.founder_id),
    ).fetchone() != ("assessment",):
        raise DutyRefused("Recipient has no current assessment grant")


def accepted_handoff(
    database: Connection, claim: ExecutionClaim, asset_id: str
) -> dict[str, str] | None:
    row = database.execute(
        """SELECT offer_id,sender_id,expected_result FROM work_handoff
           WHERE company_id=? AND successor_work_item_id=? AND recipient_id=?
             AND asset_id=? AND state='accepted'""",
        (claim.company_id, claim.duty_id, claim.founder_id, asset_id),
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
    WHERE o.company_id=? AND o.recipient_id=? AND o.state='offered'
      AND o.expires_at>? AND o.successor_work_item_id IS NULL AND w.purpose=?
      AND EXISTS (SELECT 1 FROM json_each(p.members) WHERE value=o.recipient_id)
      AND EXISTS (SELECT 1 FROM json_each(a.audience) WHERE value=o.recipient_id)"""

_READY_CANONICAL_OFFERS = """FROM work_handoff o
    JOIN work_items oi ON oi.work_item_id=o.original_work_item_id AND oi.company_id=o.company_id
    JOIN work_runs sr ON sr.run_id=o.sender_run_id AND sr.company_id=o.company_id
    JOIN shared_publication a ON a.asset_id=o.asset_id AND a.company_id=o.company_id
    JOIN shared_project p ON p.project_id=a.project_id AND p.company_id=o.company_id
    WHERE o.company_id=? AND o.recipient_id=? AND o.state='offered'
      AND o.expires_at>? AND o.successor_work_item_id IS NULL
      AND NOT EXISTS (SELECT 1 FROM duty_run dr WHERE dr.run_id=o.sender_run_id)
      AND oi.state NOT IN ('cancelled')
      AND sr.status!='fenced'
      AND EXISTS (SELECT 1 FROM json_each(p.members) WHERE value=o.recipient_id)
      AND EXISTS (SELECT 1 FROM json_each(a.audience) WHERE value=o.recipient_id)"""


def discover_handoffs(
    writer: WriterLease,
    claim: ExecutionClaim,
    purpose: str,
    *,
    now: int,
) -> list[dict]:
    if type(now) is not int or now < 0:
        raise DutyRefused("Invalid handoff discovery time")
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        _authorized_recipient(database, _with_clock(claim, now), purpose)
        names = (
            "offer_id",
            "sender_id",
            "asset_id",
            "expected_result",
            "due_at",
            "expires_at",
        )
        legacy_rows = database.execute(
            """SELECT o.offer_id,o.sender_id,o.asset_id,o.expected_result,w.due_at,o.expires_at """
            + _READY_OFFERS
            + " ORDER BY w.due_at,o.offer_id LIMIT 32",
            (claim.company_id, claim.founder_id, now, ASSESSMENT_REVIEW),
        ).fetchall()
        canonical_rows = database.execute(
            """SELECT o.offer_id,o.sender_id,o.asset_id,o.expected_result,
                      oi.created_at_ns / 1000000000,o.expires_at """
            + _READY_CANONICAL_OFFERS
            + " ORDER BY o.expires_at,o.offer_id LIMIT 32",
            (claim.company_id, claim.founder_id, now),
        ).fetchall()
        combined = sorted(
            (*legacy_rows, *canonical_rows),
            key=lambda row: (int(row[4]), str(row[0])),
        )[:32]
        return [dict(zip(names, row, strict=True)) for row in combined]


def accept_handoff(
    writer: WriterLease,
    claim: ExecutionClaim,
    purpose: str,
    offer_id: str,
    *,
    now: int,
    expected_state_version: int | None = None,
) -> str:
    if not isinstance(offer_id, str) or not 0 < len(offer_id) <= 128:
        raise DutyRefused("Invalid handoff offer reference")
    if expected_state_version is not None and (
        type(expected_state_version) is not int or expected_state_version < 1
    ):
        raise DutyRefused("Invalid handoff state version")
    if type(now) is not int or now < 0:
        raise DutyRefused("Handoff is outside the recipient's work allocation")
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        _authorized_recipient(database, _with_clock(claim, now), purpose)
        prior = database.execute(
            """SELECT state, state_version, successor_work_item_id, accepted_run_id,
                      original_work_item_id, sender_run_id, expires_at, asset_id
               FROM work_handoff
               WHERE company_id=? AND offer_id=? AND recipient_id=?""",
            (claim.company_id, offer_id, claim.founder_id),
        ).fetchone()
        if prior is None:
            raise DutyRefused("Handoff offer is unavailable")
        (
            state,
            state_version,
            successor_id,
            accepted_run_id,
            orig_item_id,
            sender_run_id,
            expires_at,
            asset_id,
        ) = prior
        if (
            state == "accepted"
            and successor_id is not None
            and accepted_run_id == claim.run_id
        ):
            if expected_state_version is not None and expected_state_version not in (
                int(state_version),
                int(state_version) - 1,
            ):
                raise DutyRefused("Handoff offer state version mismatch")
            return str(successor_id)
        if state != "offered" or now >= int(expires_at):
            raise DutyRefused("Handoff offer is not actionable")
        if (
            expected_state_version is not None
            and int(state_version) != expected_state_version
        ):
            raise DutyRefused("Handoff offer state version mismatch")
        if not has_turn_capacity(database, claim.founder_id):
            raise DutyRefused("Handoff is outside the recipient's work allocation")

        is_legacy = (
            database.execute(
                "SELECT 1 FROM duty_run WHERE run_id=?", (sender_run_id,)
            ).fetchone()
            is not None
        )
        now_ns = now * _NS_PER_SECOND
        if is_legacy:
            if (
                database.execute(
                    """SELECT 1 FROM work_review_decision
                       WHERE duty_id=? AND run_id=?""",
                    (orig_item_id, sender_run_id),
                ).fetchone()
                is None
            ):
                raise DutyRefused("Handoff offer is unavailable")
            current = database.execute(
                "SELECT d.id,w.due_at,o.asset_id "
                + _READY_OFFERS
                + " AND o.offer_id=?",
                (claim.company_id, claim.founder_id, now, ASSESSMENT_REVIEW, offer_id),
            ).fetchone()
            if current is None:
                raise DutyRefused("Handoff offer is not actionable")
            original, due_at, current_asset_id = current
            asset = shared_asset_for(database, claim.founder_id, current_asset_id)
            successor = uuid4().hex
            database.execute(
                "INSERT INTO duty VALUES (?,?,'scheduled')",
                (successor, claim.founder_id),
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
                now_ns=now_ns,
            )
            database.execute(
                """INSERT INTO work_follow_up VALUES (?,?,?,?,?,'shared_asset',?,?,?,?)""",
                (
                    original,
                    successor,
                    claim.grant_id,
                    "assessment",
                    max(due_at, now),
                    current_asset_id,
                    shared_revision(asset),
                    now,
                    ASSESSMENT_REVIEW,
                ),
            )
            database.execute(
                "UPDATE duty SET state='handed_off' WHERE id=?", (original,)
            )
            database.execute(
                "UPDATE duty_run SET is_current=0 WHERE duty_id=?", (original,)
            )
            sync_duty_terminal(
                database,
                original,
                item_state="cancelled",
                run_status="fenced",
                now_ns=now_ns,
            )
        else:
            ready = database.execute(
                "SELECT o.asset_id " + _READY_CANONICAL_OFFERS + " AND o.offer_id=?",
                (claim.company_id, claim.founder_id, now, offer_id),
            ).fetchone()
            if ready is None:
                raise DutyRefused("Handoff offer is not actionable")
            shared_asset_for(database, claim.founder_id, str(asset_id))
            succ_item, _ = create_work_item(
                database,
                company_id=claim.company_id,
                owner_agent_id=claim.founder_id,
                duty_kind="work.assessment_review",
                source_kind="handoff",
                source_id=offer_id,
                parent_work_item_id=str(orig_item_id),
                payload={
                    "offer_id": offer_id,
                    "asset_id": str(asset_id),
                    "sender_run_id": str(sender_run_id),
                },
                state="due",
                actor_id=claim.founder_id,
                now_ns=now_ns,
            )
            successor = succ_item.work_item_id
            sync_duty_terminal(
                database,
                str(orig_item_id),
                item_state="cancelled",
                run_status="fenced",
                now_ns=now_ns,
            )

        changed = database.execute(
            """UPDATE work_handoff
               SET state='accepted', state_version=state_version+1,
                   successor_work_item_id=?, accepted_run_id=?
               WHERE company_id=? AND offer_id=? AND recipient_id=?
                 AND state='offered' AND state_version=? AND expires_at>?""",
            (
                successor,
                claim.run_id,
                claim.company_id,
                offer_id,
                claim.founder_id,
                int(state_version),
                now,
            ),
        )
        if changed.rowcount != 1:
            raise DutyRefused("Handoff offer is not actionable")
        append_audit_event(
            database,
            company_id=claim.company_id,
            domain="work",
            entity_type="work_handoff",
            entity_id=offer_id,
            event_type="work.handoff_accepted",
            actor_id=claim.founder_id,
            run_id=claim.run_id,
            payload={
                "successor_work_item_id": successor,
                "original_work_item_id": str(orig_item_id),
            },
            recorded_at_ns=now_ns,
        )
        writer.verify()
        return successor


def decline_handoff(
    writer: WriterLease,
    claim: ExecutionClaim,
    purpose: str,
    offer_id: str,
    *,
    now: int,
    reason: str = "",
) -> str:
    if not isinstance(offer_id, str) or not 0 < len(offer_id) <= 128:
        raise DutyRefused("Invalid handoff offer reference")
    if type(now) is not int or now < 0:
        raise DutyRefused("Invalid handoff decline time")
    if not isinstance(reason, str) or len(reason) > 500:
        raise DutyRefused("Invalid handoff decline reason")
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        _authorized_recipient(database, _with_clock(claim, now), purpose)
        prior = database.execute(
            """SELECT state, state_version, expires_at
               FROM work_handoff
               WHERE company_id=? AND offer_id=? AND recipient_id=?""",
            (claim.company_id, offer_id, claim.founder_id),
        ).fetchone()
        if prior is None:
            raise DutyRefused("Handoff offer is unavailable")
        state, state_version, expires_at = prior
        if state != "offered" or now >= int(expires_at):
            raise DutyRefused("Handoff offer is not actionable")
        changed = database.execute(
            """UPDATE work_handoff
               SET state='declined', state_version=state_version+1
               WHERE company_id=? AND offer_id=? AND recipient_id=?
                 AND state='offered' AND state_version=? AND expires_at>?""",
            (claim.company_id, offer_id, claim.founder_id, int(state_version), now),
        )
        if changed.rowcount != 1:
            raise DutyRefused("Handoff offer is not actionable")
        append_audit_event(
            database,
            company_id=claim.company_id,
            domain="work",
            entity_type="work_handoff",
            entity_id=offer_id,
            event_type="work.handoff_declined",
            actor_id=claim.founder_id,
            run_id=claim.run_id,
            payload={"reason": reason},
            recorded_at_ns=now * _NS_PER_SECOND,
        )
        writer.verify()
        return offer_id


def withdraw_handoff(
    writer: WriterLease,
    claim: ExecutionClaim,
    offer_id: str,
    *,
    now: int,
) -> str:
    if not isinstance(offer_id, str) or not 0 < len(offer_id) <= 128:
        raise DutyRefused("Invalid handoff offer reference")
    if type(now) is not int or now < 0:
        raise DutyRefused("Invalid handoff withdrawal time")
    writer.verify_for(writer.store, claim.company_id)
    eff_purpose = (
        claim.purpose if isinstance(claim, WorkRunClaim) else ASSESSMENT_REVIEW
    )
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        require_desk(database, _with_clock(claim, now), eff_purpose)
        prior = database.execute(
            """SELECT state, state_version
               FROM work_handoff
               WHERE company_id=? AND offer_id=? AND sender_id=?""",
            (claim.company_id, offer_id, claim.founder_id),
        ).fetchone()
        if prior is None:
            raise DutyRefused("Handoff offer is unavailable")
        state, state_version = prior
        if state != "offered":
            raise DutyRefused("Handoff offer is not actionable")
        changed = database.execute(
            """UPDATE work_handoff
               SET state='withdrawn', state_version=state_version+1
               WHERE company_id=? AND offer_id=? AND sender_id=?
                 AND state='offered' AND state_version=?""",
            (claim.company_id, offer_id, claim.founder_id, int(state_version)),
        )
        if changed.rowcount != 1:
            raise DutyRefused("Handoff offer is not actionable")
        append_audit_event(
            database,
            company_id=claim.company_id,
            domain="work",
            entity_type="work_handoff",
            entity_id=offer_id,
            event_type="work.handoff_withdrawn",
            actor_id=claim.founder_id,
            run_id=claim.run_id,
            payload={},
            recorded_at_ns=now * _NS_PER_SECOND,
        )
        writer.verify()
        return offer_id


def expire_handoffs(writer: WriterLease, *, now: int) -> tuple[str, ...]:
    if type(now) is not int or not 0 <= now < 2**63:
        raise DutyRefused("Invalid handoff expiry time")
    writer.verify()
    expired_ids: list[str] = []
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        rows = database.execute(
            """SELECT offer_id, state_version
               FROM work_handoff
               WHERE company_id=? AND state='offered' AND expires_at<=?
               ORDER BY expires_at, offer_id""",
            (writer.company_id, now),
        ).fetchall()
        for offer_id, state_version in rows:
            changed = database.execute(
                """UPDATE work_handoff
                   SET state='expired', state_version=state_version+1
                   WHERE company_id=? AND offer_id=? AND state='offered'
                     AND state_version=? AND expires_at<=?""",
                (writer.company_id, str(offer_id), int(state_version), now),
            )
            if changed.rowcount == 1:
                append_audit_event(
                    database,
                    company_id=writer.company_id,
                    domain="work",
                    entity_type="work_handoff",
                    entity_id=str(offer_id),
                    event_type="work.handoff_expired",
                    actor_id="work_scheduler",
                    payload={"expired_at": now},
                    recorded_at_ns=now * _NS_PER_SECOND,
                )
                expired_ids.append(str(offer_id))
        writer.verify()
    return tuple(expired_ids)
