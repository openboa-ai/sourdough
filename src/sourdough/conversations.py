"""Append and reopen project discussions without changing work state."""

import json
from contextlib import closing
from dataclasses import dataclass, replace
from sqlite3 import Connection
from uuid import uuid4

from .agent_desk import require_desk
from .company import _connect
from .company_assets import project_membership, shared_asset_for
from .conversation_delivery import enqueue_deliveries
from .duty_run import DutyRefused, ExecutionClaim
from .writer_lease import WriterLease


PER_RUN_MESSAGE_CAP = 8
_FIELDS = "message_id,project_id,sender_id,run_id,body,asset_id"


def _reference(value: str) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128 and bool(value.strip())


@dataclass(frozen=True)
class Message:
    message_id: str
    project_id: str
    sender_id: str
    run_id: str
    body: str
    asset_id: str | None


@dataclass(frozen=True)
class Post:
    project_id: str
    project_revision: str
    request_id: str
    body: str
    asset_id: str

    def __post_init__(self) -> None:
        if (
            not all(
                _reference(value)
                for value in (self.project_id, self.project_revision, self.request_id)
            )
            or (self.asset_id != "" and not _reference(self.asset_id))
            or not isinstance(self.body, str)
            or not self.body.strip()
            or len(self.body.encode()) > 1600
        ):
            raise DutyRefused("Invalid project message")


def _membership(
    database: Connection,
    claim: ExecutionClaim,
    purpose: str,
    project: str,
) -> tuple[str, tuple[str, ...]]:
    require_desk(database, claim, purpose)
    try:
        revision, members, _ = project_membership(database, project)
    except DutyRefused as error:
        raise DutyRefused("Project discussion is unavailable") from error
    if claim.founder_id not in members:
        raise DutyRefused("Project discussion is unavailable")
    return revision, members


def append_message(
    writer: WriterLease,
    claim: ExecutionClaim,
    purpose: str,
    post: Post,
    *,
    expected_membership_revision: str | None = None,
) -> Message:
    if expected_membership_revision is not None and not _reference(
        expected_membership_revision
    ):
        raise DutyRefused("Invalid project discussion reference")
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        revision, members = _membership(database, claim, purpose, post.project_id)
        if revision != post.project_revision or (
            expected_membership_revision is not None
            and revision != expected_membership_revision
        ):
            raise DutyRefused("Project discussion membership changed")
        asset = post.asset_id or None
        if asset is not None:
            linked = shared_asset_for(database, claim.founder_id, asset)
            if linked.project_id != post.project_id:
                raise DutyRefused("Shared asset is unavailable")
        prior = database.execute(
            f"""SELECT {_FIELDS},membership_revision FROM project_message
                WHERE company_id=? AND project_id=? AND sender_id=? AND request_id=?""",
            (claim.company_id, post.project_id, claim.founder_id, post.request_id),
        ).fetchone()
        if prior is not None:
            if prior[-3:] != (post.body, asset, revision):
                raise DutyRefused("Message request conflicts with the original post")
            return Message(*prior[:-1])
        sent_count = int(
            database.execute(
                "SELECT COUNT(*) FROM project_message WHERE company_id=? AND run_id=?",
                (claim.company_id, claim.run_id),
            ).fetchone()[0]
        )
        if sent_count >= PER_RUN_MESSAGE_CAP:
            raise DutyRefused("Per-run project message cap exceeded")
        message = Message(
            uuid4().hex,
            post.project_id,
            claim.founder_id,
            claim.run_id,
            post.body,
            asset,
        )
        database.execute(
            """INSERT INTO project_message (
                   message_id,company_id,project_id,sender_id,run_id,body,
                   asset_id,audience,membership_revision,request_id
               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                message.message_id,
                claim.company_id,
                message.project_id,
                message.sender_id,
                message.run_id,
                message.body,
                message.asset_id,
                json.dumps(members),
                revision,
                post.request_id,
            ),
        )
        enqueue_deliveries(
            database, claim.company_id, message.message_id, message.sender_id, members
        )
        writer.verify()
        return message


def read_messages(
    writer: WriterLease,
    claim: ExecutionClaim,
    purpose: str,
    project_id: str,
    after: str,
    *,
    expected_membership_revision: str | None = None,
) -> tuple[tuple[Message, ...], str]:
    if (
        not _reference(project_id)
        or (after != "" and not _reference(after))
        or (
            expected_membership_revision is not None
            and not _reference(expected_membership_revision)
        )
    ):
        raise DutyRefused("Invalid project discussion reference")
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        revision, _ = _membership(database, claim, purpose, project_id)
        if (
            expected_membership_revision is not None
            and revision != expected_membership_revision
        ):
            raise DutyRefused("Project discussion membership changed")
        after_position: int | None = None
        if after != "":
            cursor_row = database.execute(
                """SELECT rowid
                   FROM project_message
                   WHERE company_id=? AND project_id=? AND message_id=?
                     AND EXISTS (SELECT 1 FROM json_each(audience) WHERE value=?)""",
                (claim.company_id, project_id, after, claim.founder_id),
            ).fetchone()
            if cursor_row is None:
                raise DutyRefused("Project discussion cursor is unavailable")
            after_position = int(cursor_row[0])
        page = database.execute(
            f"""WITH visible AS (
                    SELECT rowid AS position,{_FIELDS} FROM project_message
                    WHERE company_id=? AND project_id=?
                      AND EXISTS (SELECT 1 FROM json_each(audience) WHERE value=?)
                )
                SELECT {_FIELDS} FROM visible
                WHERE ? IS NULL OR position > ?
                ORDER BY position LIMIT 17""",
            (
                claim.company_id,
                project_id,
                claim.founder_id,
                after_position,
                after_position,
            ),
        ).fetchall()
        messages = []
        for row in page[:16]:
            message = Message(*row)
            if message.asset_id is not None:
                try:
                    linked = shared_asset_for(
                        database, claim.founder_id, message.asset_id
                    )
                    if linked.project_id != project_id:
                        message = replace(message, asset_id=None)
                except DutyRefused:
                    message = replace(message, asset_id=None)
            messages.append(message)
        return tuple(messages), messages[-1].message_id if len(page) > 16 else ""
