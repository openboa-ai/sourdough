"""Publish an explicitly selected private draft excerpt into company custody."""

import json
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from sqlite3 import Connection
from uuid import uuid4

from .agent_desk import require_desk
from .company import _connect
from .duty_run import DutyClaim, DutyRefused
from .writer_lease import WriterLease


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS shared_project (
           project_id TEXT PRIMARY KEY, revision TEXT NOT NULL,
           members TEXT NOT NULL, publishers TEXT NOT NULL
       )""",
    """CREATE TABLE IF NOT EXISTS shared_publication (
           asset_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES shared_project(project_id),
           author_id TEXT NOT NULL REFERENCES agent(id), duty_id TEXT NOT NULL REFERENCES duty(id),
           request_id TEXT NOT NULL, request_digest TEXT NOT NULL, audience TEXT NOT NULL,
           content TEXT NOT NULL, UNIQUE(author_id,request_id)
       )""",
)


def _label(value: str) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128 and bool(value.strip())


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _revision(value) -> str:
    return sha256(_json(value).encode()).hexdigest()


def _project(
    database: Connection, project_id: str
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    row = database.execute(
        "SELECT revision,members,publishers FROM shared_project WHERE project_id=?",
        (project_id,),
    ).fetchone()
    if row is None:
        raise DutyRefused("Shared project is unavailable")
    revision, members, publishers = row
    return revision, tuple(json.loads(members)), tuple(json.loads(publishers))


def _active_member(database: Connection, company: str, agent: str) -> bool:
    return (
        database.execute(
            """SELECT 1 FROM agent JOIN standing_grant ON standing_grant.agent_id=agent.id
           WHERE agent.company_id=? AND agent.id=? AND standing_grant.scope!='revoked'""",
            (company, agent),
        ).fetchone()
        is not None
    )


def assign_project(
    writer: WriterLease,
    manager_grant: str,
    project_id: str,
    members: tuple[str, ...],
    *,
    publishers: tuple[str, ...],
    expected_revision: str | None = None,
) -> str:
    """Internal company setup operation, never an Agent tool or an implicit membership grant."""
    if (
        not _label(project_id)
        or not 1 <= len(members) <= 32
        or not all(_label(member) for member in (*members, *publishers))
        or len(set(members)) != len(members)
        or len(set(publishers)) != len(publishers)
        or not set(publishers) <= set(members)
    ):
        raise DutyRefused("Invalid shared project scope")
    members = tuple(sorted(members))
    publishers = tuple(sorted(publishers))
    writer.verify()
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        manager = database.execute(
            """SELECT 1 FROM founding_receipt f JOIN standing_grant g
               ON g.id=f.grant_id AND g.agent_id=f.founder_id
               WHERE f.company_id=? AND g.id=? AND g.scope='manage-company'""",
            (writer.company_id, manager_grant),
        ).fetchone()
        if manager is None or not all(
            _active_member(database, writer.company_id, member) for member in members
        ):
            raise DutyRefused("Project membership requires existing company authority")
        for statement in SCHEMA:
            database.execute(statement)
        previous = database.execute(
            "SELECT revision,members,publishers FROM shared_project WHERE project_id=?",
            (project_id,),
        ).fetchone()
        revision = _revision(
            (writer.company_id, project_id, expected_revision, members, publishers)
        )
        if previous == (revision, _json(members), _json(publishers)):
            return revision
        if (previous[0] if previous else None) != expected_revision:
            raise DutyRefused("Shared project revision changed")
        database.execute(
            """INSERT INTO shared_project VALUES (?,?,?,?)
               ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision,
               members=excluded.members,publishers=excluded.publishers""",
            (project_id, revision, _json(members), _json(publishers)),
        )
        writer.verify()
        return revision


@dataclass(frozen=True)
class Selection:
    request_id: str
    draft_name: str
    draft_revision: str
    project_id: str
    project_revision: str
    audience: tuple[str, ...]
    start: int
    end: int

    def __post_init__(self):
        if (
            not all(
                _label(value)
                for value in (
                    self.request_id,
                    self.draft_name,
                    self.draft_revision,
                    self.project_id,
                    self.project_revision,
                )
            )
            or not isinstance(self.audience, tuple)
            or not 1 <= len(self.audience) <= 32
            or not all(_label(member) for member in self.audience)
            or len(set(self.audience)) != len(self.audience)
            or type(self.start) is not int
            or type(self.end) is not int
            or not 0 <= self.start < self.end <= 800
        ):
            raise DutyRefused("Invalid private publication selection")


@dataclass(frozen=True)
class SharedAsset:
    asset_id: str
    project_id: str
    author_id: str
    duty_id: str
    content: str


@dataclass(frozen=True)
class ProjectAccess:
    project_id: str
    revision: str
    members: tuple[str, ...]
    can_publish: bool


@dataclass(frozen=True)
class SharedAssetReference:
    asset_id: str
    project_id: str
    author_id: str
    duty_id: str


def discover_shared_assets(
    writer: WriterLease,
    claim: DutyClaim,
    purpose: str,
    after: str,
) -> tuple[tuple[SharedAssetReference, ...], str]:
    if after and not _label(after):
        raise DutyRefused("Invalid shared asset cursor")
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        require_desk(database, claim, purpose)
        page = tuple(
            SharedAssetReference(*row)
            for row in database.execute(
                """WITH visible AS (
                       SELECT a.rowid AS position,a.asset_id,a.project_id,a.author_id,a.duty_id
                       FROM shared_publication a JOIN shared_project p ON p.project_id=a.project_id
                       WHERE EXISTS (SELECT 1 FROM json_each(p.members) WHERE value=?)
                         AND EXISTS (SELECT 1 FROM json_each(a.audience) WHERE value=?)
                   )
                   SELECT asset_id,project_id,author_id,duty_id FROM visible
                   WHERE ?='' OR position<(SELECT position FROM visible WHERE asset_id=?)
                   ORDER BY position DESC LIMIT 33""",
                (claim.founder_id, claim.founder_id, after, after),
            )
        )
        return page[:32], page[31].asset_id if len(page) > 32 else ""


def project_access(
    writer: WriterLease,
    claim: DutyClaim,
    purpose: str,
) -> tuple[ProjectAccess, ...]:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        require_desk(database, claim, purpose)
        if (
            database.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='shared_project'"
            ).fetchone()
            is None
        ):
            return ()
        result = []
        for project_id, revision, members, publishers in database.execute(
            "SELECT project_id,revision,members,publishers FROM shared_project ORDER BY project_id"
        ):
            audience = tuple(json.loads(members))
            if claim.founder_id in audience:
                result.append(
                    ProjectAccess(
                        project_id,
                        revision,
                        audience,
                        claim.founder_id in json.loads(publishers),
                    )
                )
        return tuple(result)


def publish_selection(
    writer: WriterLease,
    claim: DutyClaim,
    purpose: str,
    selection: Selection,
) -> SharedAsset:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        require_desk(database, claim, purpose)
        revision, members, publishers = _project(database, selection.project_id)
        if (
            revision != selection.project_revision
            or claim.founder_id not in publishers
            or not set(selection.audience) <= set(members)
            or not all(
                _active_member(database, claim.company_id, member)
                for member in selection.audience
            )
        ):
            raise DutyRefused("Publication is outside the current project audience")
        digest = _revision(asdict(selection) | {"duty_id": claim.duty_id})
        prior = database.execute(
            """SELECT asset_id,project_id,author_id,duty_id,content,request_digest
               FROM shared_publication WHERE author_id=? AND request_id=?""",
            (claim.founder_id, selection.request_id),
        ).fetchone()
        if prior is not None:
            if prior[-1] != digest:
                raise DutyRefused(
                    "Publication request conflicts with its original selection"
                )
            return SharedAsset(*prior[:-1])
        draft = database.execute(
            "SELECT content FROM desk_draft WHERE agent_id=? AND name=? AND revision=?",
            (claim.founder_id, selection.draft_name, selection.draft_revision),
        ).fetchone()
        if draft is None or selection.end > len(draft[0]):
            raise DutyRefused("Current private draft or selected range is unavailable")
        excerpt = draft[0][selection.start : selection.end]
        if not excerpt.strip():
            raise DutyRefused("Cannot publish an empty selection")
        asset = SharedAsset(
            uuid4().hex, selection.project_id, claim.founder_id, claim.duty_id, excerpt
        )
        database.execute(
            """INSERT INTO shared_publication (asset_id,project_id,author_id,duty_id,content,
                   request_id,request_digest,audience) VALUES (?,?,?,?,?,?,?,?)""",
            (
                *asdict(asset).values(),
                selection.request_id,
                digest,
                _json(sorted(selection.audience)),
            ),
        )
        writer.verify()
        return asset


def read_shared_asset(
    writer: WriterLease,
    claim: DutyClaim,
    purpose: str,
    asset_id: str,
) -> SharedAsset:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        require_desk(database, claim, purpose)
        row = database.execute(
            """SELECT asset_id,project_id,author_id,duty_id,content,audience
               FROM shared_publication WHERE asset_id=?""",
            (asset_id,),
        ).fetchone()
        if row is None:
            raise DutyRefused("Shared asset is unavailable")
        _, members, _ = _project(database, row[1])
        if claim.founder_id not in members or claim.founder_id not in json.loads(
            row[-1]
        ):
            raise DutyRefused("Shared asset is unavailable")
        return SharedAsset(*row[:-1])
