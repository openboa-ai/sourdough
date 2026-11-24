"""Publish an explicitly selected private draft excerpt into company custody."""

import json
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from sqlite3 import Connection
from uuid import uuid4

from .agent_desk import draft_at_revision, require_desk
from .company import _connect
from .duty_run import ASSESSMENT_REVIEW, DutyClaim, DutyRefused
from .runtime.work_kernel import sync_work_item, upsert_project_peer_responsibility
from .storage.kernel import append_audit_event
from .writer_lease import WriterLease


_MAX_PROJECT_MEMBERS = 32


def _label(value: str) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128 and bool(value.strip())


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _revision(value) -> str:
    return sha256(_json(value).encode()).hexdigest()


def project_membership(
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


def _current_project_manager(
    database: Connection, company_id: str, grant_id: str
) -> bool:
    return (
        database.execute(
            """SELECT 1 FROM founding_receipt f JOIN standing_grant g
           ON g.id=f.grant_id AND g.agent_id=f.founder_id
           WHERE f.company_id=? AND g.id=? AND g.scope='manage-company'""",
            (company_id, grant_id),
        ).fetchone()
        is not None
    )


def require_project_onboarding_authority(
    database: Connection, company_id: str, manager_grant: str, project_id: str
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    if not _current_project_manager(database, company_id, manager_grant):
        raise DutyRefused("Project onboarding requires current company management")
    revision, members, publishers = project_membership(database, project_id)
    (manager,) = database.execute(
        "SELECT agent_id FROM standing_grant WHERE id=?", (manager_grant,)
    ).fetchone()
    if manager not in members:
        raise DutyRefused("Hiring authority does not cover the designated project")
    return revision, members, publishers


def _sync_project_peer_routes(
    database: Connection,
    company_id: str,
    manager_grant: str,
    project_id: str,
    members: tuple[str, ...],
) -> None:
    row = database.execute(
        "SELECT agent_id FROM standing_grant WHERE id = ?", (manager_grant,)
    ).fetchone()
    if row is None:
        return
    manager_id = str(row[0])
    requesters = (
        [m for m in members if m != manager_id] if manager_id in members else []
    )
    upsert_project_peer_responsibility(
        database,
        company_id=company_id,
        owner_agent_id=manager_id,
        project_id=project_id,
        allowed_requesters=requesters,
    )


def enroll_project_participant(
    database: Connection,
    company_id: str,
    manager_grant: str,
    project_id: str,
    agent_id: str,
) -> None:
    if not database.in_transaction or not _active_member(
        database, company_id, agent_id
    ):
        raise DutyRefused("Project onboarding requires an active company Agent")
    revision, members, publishers = require_project_onboarding_authority(
        database, company_id, manager_grant, project_id
    )
    if agent_id in members:
        _sync_project_peer_routes(
            database, company_id, manager_grant, project_id, members
        )
        return
    if len(members) >= _MAX_PROJECT_MEMBERS:
        raise DutyRefused("Designated project has no room for another member")
    enrolled = tuple(sorted((*members, agent_id)))
    database.execute(
        "UPDATE shared_project SET revision=?, members=? WHERE project_id=?",
        (
            _revision((company_id, project_id, revision, enrolled, publishers)),
            _json(enrolled),
            project_id,
        ),
    )
    _sync_project_peer_routes(database, company_id, manager_grant, project_id, enrolled)


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
        or not 1 <= len(members) <= _MAX_PROJECT_MEMBERS
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
        if not _current_project_manager(
            database, writer.company_id, manager_grant
        ) or not all(
            _active_member(database, writer.company_id, member) for member in members
        ):
            raise DutyRefused("Project membership requires existing company authority")
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
            """INSERT INTO shared_project (project_id,company_id,revision,members,publishers)
               VALUES (?,?,?,?,?)
               ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision,
               members=excluded.members,publishers=excluded.publishers""",
            (
                project_id,
                writer.company_id,
                revision,
                _json(members),
                _json(publishers),
            ),
        )
        _sync_project_peer_routes(
            database, writer.company_id, manager_grant, project_id, members
        )
        writer.verify()
        return revision


def seal_project_review(
    writer: WriterLease,
    manager_grant: str,
    duty_id: str,
    project_id: str,
    deliverable: str,
) -> None:
    if (
        not _label(duty_id)
        or not _label(project_id)
        or not isinstance(deliverable, str)
        or not deliverable.strip()
        or len(deliverable.encode()) > 800
    ):
        raise DutyRefused("Project review requires a bounded deliverable")
    writer.verify()
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        if not _current_project_manager(database, writer.company_id, manager_grant):
            raise DutyRefused("Project review requires current company management")
        duty = database.execute(
            """SELECT d.agent_id,d.state FROM duty d JOIN work_follow_up w ON w.duty_id=d.id
               JOIN agent a ON a.id=d.agent_id WHERE d.id=? AND w.purpose=? AND a.company_id=?""",
            (duty_id, ASSESSMENT_REVIEW, writer.company_id),
        ).fetchone()
        _, members, _ = project_membership(database, project_id)
        if (
            duty is None
            or duty[0] not in members
            or not _active_member(database, writer.company_id, duty[0])
        ):
            raise DutyRefused("Project review is outside the assigned membership")
        terms = (duty[0], project_id, manager_grant, deliverable)
        prior = database.execute(
            """SELECT agent_id,project_id,manager_grant,deliverable
               FROM project_review_brief WHERE work_item_id=?""",
            (duty_id,),
        ).fetchone()
        if prior == terms:
            return
        if prior is not None or duty[1] != "scheduled":
            raise DutyRefused("Project review already has a brief or has been claimed")
        sync_work_item(
            database,
            company_id=writer.company_id,
            work_item_id=duty_id,
            owner_agent_id=duty[0],
            duty_kind="work.assessment_review",
            state="scheduled",
            source_kind="duty",
            source_id=duty_id,
        )
        database.execute(
            """INSERT INTO project_review_brief
               (work_item_id,company_id,agent_id,project_id,manager_grant,deliverable)
               VALUES (?,?,?,?,?,?)""",
            (duty_id, writer.company_id, *terms),
        )
        writer.verify()


def project_review_instruction(writer: WriterLease, claim: DutyClaim) -> str | None:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        require_desk(database, claim, ASSESSMENT_REVIEW)
        row = database.execute(
            """SELECT project_id,manager_grant,deliverable FROM project_review_brief
               WHERE work_item_id=? AND agent_id=?""",
            (claim.duty_id, claim.founder_id),
        ).fetchone()
        if row is None or not _current_project_manager(
            database, claim.company_id, row[1]
        ):
            return None
        project_id, _, deliverable = row
        _, members, _ = project_membership(database, project_id)
        if claim.founder_id not in members:
            return None
        return f"Assigned company deliverable for project {project_id}: {deliverable}"


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


@dataclass(frozen=True)
class PublicationReceipt:
    asset_id: str
    project_id: str
    duty_id: str
    request_id: str


def discover_project_references(
    writer: WriterLease,
    claim: DutyClaim,
    purpose: str,
    after: str,
) -> tuple[tuple[SharedAssetReference, ...], tuple[PublicationReceipt, ...], str]:
    if after and not _label(after):
        raise DutyRefused("Invalid shared asset cursor")
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        require_desk(database, claim, purpose)
        page = database.execute(
            """WITH scoped AS (
                       SELECT a.rowid AS position,a.asset_id,a.project_id,a.author_id,a.work_item_id,
                              EXISTS (SELECT 1 FROM json_each(a.audience) WHERE value=:actor) AS can_read,
                              CASE WHEN a.author_id=:actor AND a.work_item_id=:duty
                                AND EXISTS (SELECT 1 FROM json_each(p.publishers) WHERE value=:actor)
                                THEN a.request_id END AS receipt
                       FROM shared_publication a JOIN shared_project p ON p.project_id=a.project_id
                       WHERE EXISTS (SELECT 1 FROM json_each(p.members) WHERE value=:actor)
                   ), visible AS (
                       SELECT * FROM scoped WHERE can_read OR receipt IS NOT NULL
                   )
                   SELECT asset_id,project_id,author_id,work_item_id,can_read,receipt FROM visible
                   WHERE :after='' OR position<(SELECT position FROM visible WHERE asset_id=:after)
                   ORDER BY position DESC LIMIT 33""",
            {"actor": claim.founder_id, "duty": claim.duty_id, "after": after},
        ).fetchall()
        assets = tuple(
            SharedAssetReference(asset, project, author, duty)
            for asset, project, author, duty, can_read, _ in page[:32]
            if can_read
        )
        receipts = tuple(
            PublicationReceipt(asset, project, duty, receipt)
            for asset, project, _, duty, _, receipt in page[:32]
            if receipt is not None
        )
        return assets, receipts, page[31][0] if len(page) > 32 else ""


def project_access(
    writer: WriterLease,
    claim: DutyClaim,
    purpose: str,
) -> tuple[ProjectAccess, ...]:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        require_desk(database, claim, purpose)
        result = []
        for project_id, revision, members, publishers in database.execute(
            "SELECT project_id,revision,members,publishers FROM shared_project WHERE company_id=? ORDER BY project_id",
            (claim.company_id,),
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
        revision, members, publishers = project_membership(
            database, selection.project_id
        )
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
            """SELECT asset_id,project_id,author_id,work_item_id,content,request_digest
               FROM shared_publication WHERE author_id=? AND request_id=?""",
            (claim.founder_id, selection.request_id),
        ).fetchone()
        if prior is not None:
            if prior[-1] != digest:
                raise DutyRefused(
                    "Publication request conflicts with its original selection"
                )
            return SharedAsset(*prior[:-1])
        draft = draft_at_revision(
            writer.store,
            claim.founder_id,
            selection.draft_name,
            selection.draft_revision,
        )
        if draft is None or selection.end > len(draft.content):
            raise DutyRefused("Current private draft or selected range is unavailable")
        excerpt = draft.content[selection.start : selection.end]
        if not excerpt.strip():
            raise DutyRefused("Cannot publish an empty selection")
        asset = SharedAsset(
            uuid4().hex, selection.project_id, claim.founder_id, claim.duty_id, excerpt
        )
        database.execute(
            """INSERT INTO shared_publication (
                   asset_id,company_id,project_id,author_id,work_item_id,content,
                   request_id,request_digest,audience
               ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                asset.asset_id,
                claim.company_id,
                asset.project_id,
                asset.author_id,
                asset.duty_id,
                asset.content,
                selection.request_id,
                digest,
                _json(sorted(selection.audience)),
            ),
        )
        append_audit_event(
            database,
            company_id=claim.company_id,
            domain="assets",
            entity_type="shared_publication",
            entity_id=asset.asset_id,
            event_type="company.asset_published",
            actor_id=claim.founder_id,
            run_id=claim.run_id,
            payload={
                "asset_id": asset.asset_id,
                "project_id": asset.project_id,
                "work_item_id": asset.duty_id,
                "excerpt_sha256": sha256(excerpt.encode("utf-8")).hexdigest(),
            },
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
        return shared_asset_for(database, claim.founder_id, asset_id)


def shared_asset_for(database: Connection, agent_id: str, asset_id: str) -> SharedAsset:
    row = database.execute(
        """SELECT asset_id,project_id,author_id,work_item_id,content,audience
           FROM shared_publication WHERE asset_id=?""",
        (asset_id,),
    ).fetchone()
    if row is None:
        raise DutyRefused("Shared asset is unavailable")
    _, members, _ = project_membership(database, row[1])
    if agent_id not in members or agent_id not in json.loads(row[-1]):
        raise DutyRefused("Shared asset is unavailable")
    return SharedAsset(*row[:-1])
