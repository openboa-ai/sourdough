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
from .duty_run import DutyClaim, DutyRefused
from .writer_lease import WriterLease


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
    database: Connection, claim: DutyClaim, purpose: str, project: str
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
    writer: WriterLease, claim: DutyClaim, purpose: str, post: Post
) -> Message:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        revision, members = _membership(database, claim, purpose, post.project_id)
        if revision != post.project_revision:
            raise DutyRefused("Project discussion membership changed")
        asset = post.asset_id or None
        if asset is not None:
            shared_asset_for(database, claim.founder_id, asset)
        prior = database.execute(
            f"""SELECT {_FIELDS},membership_revision FROM project_message
                WHERE project_id=? AND sender_id=? AND request_id=?""",
            (post.project_id, claim.founder_id, post.request_id),
        ).fetchone()
        if prior is not None:
            if prior[-3:] != (post.body, asset, revision):
                raise DutyRefused("Message request conflicts with the original post")
            return Message(*prior[:-1])
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
    writer: WriterLease, claim: DutyClaim, purpose: str, project_id: str, after: str
) -> tuple[tuple[Message, ...], str]:
    if not _reference(project_id) or (after != "" and not _reference(after)):
        raise DutyRefused("Invalid project discussion reference")
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        _membership(database, claim, purpose, project_id)
        page = database.execute(
            f"""WITH visible AS (
                    SELECT rowid AS position,{_FIELDS} FROM project_message
                    WHERE project_id=? AND EXISTS (SELECT 1 FROM json_each(audience) WHERE value=?)
                )
                SELECT {_FIELDS} FROM visible
                WHERE ?='' OR position>(SELECT position FROM visible WHERE message_id=?)
                ORDER BY position LIMIT 17""",
            (project_id, claim.founder_id, after, after),
        ).fetchall()
        messages = []
        for row in page[:16]:
            message = Message(*row)
            if message.asset_id is not None:
                try:
                    shared_asset_for(database, claim.founder_id, message.asset_id)
                except DutyRefused:
                    message = replace(message, asset_id=None)
            messages.append(message)
        return tuple(messages), messages[-1].message_id if len(page) > 16 else ""
