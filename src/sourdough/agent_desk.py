"""Own private draft custody independently of a provider's ephemeral workspace."""

import json
import re
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from sqlite3 import Connection

from .company import _connect
from .duty_run import DutyClaim, DutyRefused, resolve_run
from .writer_lease import WriterLease


_NAME = re.compile(r"[a-z][a-z0-9-]{0,47}")
_MAX_DRAFTS = 6
_MAX_CONTENT_BYTES = 800


@dataclass(frozen=True)
class Draft:
    name: str
    revision: str
    content: str


def ensure_private_desk(
    database: Connection, company_id: str, agent_id: str, execution_profile: str
) -> None:
    revision = sha256(
        f"{company_id}:{agent_id}:{execution_profile}".encode()
    ).hexdigest()
    database.execute(
        "INSERT INTO private_desk VALUES (?,?) ON CONFLICT(agent_id) DO NOTHING",
        (agent_id, revision),
    )


def require_desk(database: Connection, claim: DutyClaim, purpose: str) -> str:
    authority = resolve_run(database, claim)
    authority.require_current(purpose)
    if authority.duty_state != "running":
        raise DutyRefused("Scoped workflow requires the current running duty")
    row = database.execute(
        "SELECT manifest_revision FROM private_desk WHERE agent_id=?",
        (claim.founder_id,),
    ).fetchone()
    if not row:
        raise DutyRefused("The current Agent has no private Desk")
    return row[0]


def _has_drafts(database: Connection) -> bool:
    return (
        database.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='desk_draft'"
        ).fetchone()
        is not None
    )


def draft_at_revision(
    database: Connection, agent_id: str, name: str, revision: str
) -> Draft | None:
    if not _has_drafts(database):
        return None
    row = database.execute(
        "SELECT name,revision,content FROM desk_draft WHERE agent_id=? AND name=? AND revision=?",
        (agent_id, name, revision),
    ).fetchone()
    return Draft(*row) if row else None


def resolve_desk(
    writer: WriterLease, claim: DutyClaim, purpose: str, *, include_drafts: bool = False
) -> tuple[str, tuple[Draft, ...]]:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        revision = require_desk(database, claim, purpose)
        drafts = ()
        if include_drafts and _has_drafts(database):
            drafts = tuple(
                Draft(*row)
                for row in database.execute(
                    """SELECT name,revision,content FROM desk_draft
                       WHERE agent_id=? ORDER BY name LIMIT ?""",
                    (claim.founder_id, _MAX_DRAFTS + 1),
                )
            )
            if len(drafts) > _MAX_DRAFTS or any(
                not _NAME.fullmatch(draft.name)
                or len(draft.content.encode()) > _MAX_CONTENT_BYTES
                for draft in drafts
            ):
                raise DutyRefused("Private Desk snapshot exceeds its admitted bounds")
        return revision, drafts


def save_draft(
    writer: WriterLease,
    claim: DutyClaim,
    purpose: str,
    *,
    name: str,
    content: str,
    expected_revision: str | None,
) -> Draft:
    """Persist an authorized private draft; this operation cannot publish or grant access."""
    if (
        not isinstance(name, str)
        or not _NAME.fullmatch(name)
        or not isinstance(content, str)
        or not 0 < len(content.encode()) <= _MAX_CONTENT_BYTES
    ):
        raise DutyRefused("Invalid private draft name or content")
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        desk = require_desk(database, claim, purpose)
        database.execute(
            """CREATE TABLE IF NOT EXISTS desk_draft (
                   agent_id TEXT NOT NULL REFERENCES private_desk(agent_id),
                   name TEXT NOT NULL, revision TEXT NOT NULL, content TEXT NOT NULL,
                   PRIMARY KEY(agent_id,name)
               )"""
        )
        revision = sha256(
            json.dumps(
                (desk, name, expected_revision, content), separators=(",", ":")
            ).encode()
        ).hexdigest()
        draft = Draft(name, revision, content)
        key = (claim.founder_id, name)
        previous = database.execute(
            "SELECT revision,content FROM desk_draft WHERE agent_id=? AND name=?", key
        ).fetchone()
        if previous == (revision, content):
            return draft
        if (previous[0] if previous else None) != expected_revision:
            raise DutyRefused("Private draft revision changed")
        if (
            previous is None
            and database.execute(
                "SELECT count(*) FROM desk_draft WHERE agent_id=?", (claim.founder_id,)
            ).fetchone()[0]
            >= _MAX_DRAFTS
        ):
            raise DutyRefused("Private Desk draft capacity is exhausted")
        database.execute(
            """INSERT INTO desk_draft VALUES (?,?,?,?)
               ON CONFLICT(agent_id,name) DO UPDATE
               SET revision=excluded.revision, content=excluded.content""",
            (*key, revision, content),
        )
        writer.verify()
        return draft
