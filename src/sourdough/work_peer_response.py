"""Record a blocking company Work request without transferring the source duty."""

import json
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from sqlite3 import Connection
from uuid import uuid4

from .abandoned_work import has_uncommitted_work
from .agent_desk import ensure_private_desk, require_desk
from .company import _connect
from .company_assets import (
    project_membership,
    require_project_onboarding_authority,
    shared_asset_for,
)
from .duty_run import (
    DutyClaim,
    DutyRefused,
    employment_cutoff,
    has_turn_capacity,
    resolve_run,
    start_run,
)
from .onboarding import has_hired_acceptance
from .writer_lease import WriterLease


PEER_RESPONSE = "peer_response"
_ASSESSMENT = "assessment"
_SCHEMA = """CREATE TABLE IF NOT EXISTS work_peer_request (
    response_duty TEXT PRIMARY KEY REFERENCES duty(id),
    source_duty TEXT NOT NULL REFERENCES duty(id),
    source_run TEXT NOT NULL UNIQUE REFERENCES duty_run(run_id),
    request_key TEXT NOT NULL, project_id TEXT NOT NULL,
    expires_at INTEGER NOT NULL, terms TEXT NOT NULL,
    outcome TEXT CHECK(outcome IN ('answered', 'declined', 'expired')),
    answer TEXT, read_run TEXT REFERENCES duty_run(run_id),
    answered_run TEXT REFERENCES duty_run(run_id),
    resumed_run TEXT UNIQUE REFERENCES duty_run(run_id),
    UNIQUE(source_duty, request_key)
)"""


def _text(value: object, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value.strip()) and len(value) <= limit


def _timestamp(value: object) -> bool:
    return type(value) is int and 0 <= value < 2**63


@dataclass(frozen=True)
class PeerQuestion:
    request_key: str
    recipient_id: str
    question: str
    expected_response: str
    expires_at: int
    asset_id: str = ""
    asset_revision: str = ""

    def __post_init__(self) -> None:
        if (
            not _text(self.request_key, 128)
            or not _text(self.recipient_id, 128)
            or not _text(self.question, 1600)
            or not _text(self.expected_response, 500)
            or not _timestamp(self.expires_at)
            or not (
                self.asset_id == self.asset_revision == ""
                or _text(self.asset_id, 128)
                and _text(self.asset_revision, 128)
            )
        ):
            raise DutyRefused("Invalid peer question")


@dataclass(frozen=True)
class PeerWait:
    source_duty: str
    response_duty: str
    expires_at: int


@dataclass(frozen=True)
class _ProjectPeer:
    project_id: str
    recipient_id: str
    grant_id: str
    profile: str


def _exists(database: Connection) -> bool:
    return (
        database.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='work_peer_request'"
        ).fetchone()
        is not None
    )


def _project_route(
    database: Connection,
    company_id: str,
    source_duty: str,
    question: PeerQuestion | None = None,
) -> _ProjectPeer:
    route = database.execute(
        """SELECT json_extract(s.evidence,'$.project_id'),manager.id,manager.execution_profile,g.id,
                  source.agent_id
           FROM hire_onboarding o JOIN duty source ON source.id=o.first_duty_id
           JOIN agent requester ON requester.id=source.agent_id AND requester.company_id=?
           JOIN hire_receipt h ON h.agent_id=o.agent_id AND h.agent_id=requester.id
           JOIN standing_grant delegated ON delegated.id=h.grant_id
             AND delegated.agent_id=requester.id AND delegated.scope='assessment'
           JOIN staffing_choice c ON c.choice_id=h.choice_id
           JOIN staffing_assignment s ON s.duty_id=c.duty_id
           JOIN standing_grant g ON g.id=h.manager_grant_id
           JOIN agent manager ON manager.id=g.agent_id AND manager.company_id=requester.company_id
           WHERE source.id=? AND o.purpose='assessment' AND o.state IN ('ready','accepted')""",
        (company_id, source_duty),
    ).fetchone()
    if (
        route is None
        or route[0] is None
        or (question is not None and route[1] != question.recipient_id)
    ):
        raise DutyRefused("Peer response is outside the assigned project route")
    project_id, recipient, profile, grant, requester = route
    if recipient == requester or any(
        employment_cutoff(database, actor) is not None
        for actor in (requester, recipient)
    ):
        raise DutyRefused("Peer response requires distinct current participants")
    _, members, _ = require_project_onboarding_authority(
        database, company_id, grant, project_id
    )
    if requester not in members:
        raise DutyRefused("Requester no longer belongs to the assigned project")
    if question is not None and question.asset_id:
        for actor in (requester, recipient):
            asset = shared_asset_for(database, actor, question.asset_id)
            if (
                asset.project_id != project_id
                or sha256(asset.content.encode()).hexdigest() != question.asset_revision
            ):
                raise DutyRefused("Peer evidence is outside the admitted revision")
    return _ProjectPeer(project_id, recipient, grant, profile)


def assigned_peer(writer: WriterLease, claim: DutyClaim) -> dict | None:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        require_desk(database, claim, _ASSESSMENT)
        try:
            route = _project_route(database, claim.company_id, claim.duty_id)
        except DutyRefused:
            return None
        return {"project_id": route.project_id, "recipient_id": route.recipient_id}


def peer_wait_for_run(writer: WriterLease, claim: DutyClaim) -> PeerWait | None:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        authority = resolve_run(database, claim)
        if authority.purpose != _ASSESSMENT or not _exists(database):
            return None
        row = database.execute(
            """SELECT source_duty,response_duty,expires_at FROM work_peer_request
               WHERE source_duty=? AND source_run=?""",
            (claim.duty_id, claim.run_id),
        ).fetchone()
        return PeerWait(*row) if row else None


def request_peer_response(
    writer: WriterLease, claim: DutyClaim, question: PeerQuestion, *, now: int
) -> PeerWait:
    writer.verify_for(writer.store, claim.company_id)
    if not _timestamp(now):
        raise DutyRefused("Invalid request time")
    terms = json.dumps(asdict(question), sort_keys=True, separators=(",", ":"))
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        authority = resolve_run(database, claim)
        if (
            authority.purpose != _ASSESSMENT
            or authority.current_scope != authority.issued_scope
            or employment_cutoff(database, claim.founder_id) is not None
        ):
            raise DutyRefused("Peer request requires a current assessment grant")
        database.execute(_SCHEMA)
        prior = database.execute(
            """SELECT response_duty,source_run,terms FROM work_peer_request
               WHERE source_duty=? AND request_key=?""",
            (claim.duty_id, question.request_key),
        ).fetchone()
        if prior is not None:
            if prior[1:] != (claim.run_id, terms):
                raise DutyRefused("Peer request conflicts with the original receipt")
            return PeerWait(claim.duty_id, prior[0], question.expires_at)
        require_desk(database, claim, _ASSESSMENT)
        if not has_hired_acceptance(database, claim):
            raise DutyRefused("Peer request requires an accepted assignment")
        if not has_uncommitted_work(database, claim.duty_id):
            raise DutyRefused("Peer request cannot suspend a committed result")
        if not now < question.expires_at <= now + 86400:
            raise DutyRefused("Peer request deadline is outside its bounded window")
        if not has_turn_capacity(database, claim.founder_id):
            raise DutyRefused(
                "No turn allocation remains to resume the requesting work"
            )
        route = _project_route(database, claim.company_id, claim.duty_id, question)
        if not has_turn_capacity(database, question.recipient_id):
            raise DutyRefused("Recipient has no available turn allocation")
        ensure_private_desk(
            database, claim.company_id, question.recipient_id, route.profile
        )
        response_duty = uuid4().hex
        database.execute(
            "INSERT INTO duty VALUES (?,?,'scheduled')",
            (response_duty, question.recipient_id),
        )
        database.execute(
            """INSERT INTO work_peer_request(
                   response_duty,source_duty,source_run,request_key,project_id,expires_at,terms
               ) VALUES (?,?,?,?,?,?,?)""",
            (
                response_duty,
                claim.duty_id,
                claim.run_id,
                question.request_key,
                route.project_id,
                question.expires_at,
                terms,
            ),
        )
        database.execute("UPDATE duty SET state='waiting' WHERE id=?", (claim.duty_id,))
        database.execute(
            "UPDATE duty_run SET is_current=0 WHERE run_id=?", (claim.run_id,)
        )
        return PeerWait(claim.duty_id, response_duty, question.expires_at)


def claim_peer_response(
    writer: WriterLease, profile: str, *, now: int
) -> DutyClaim | None:
    writer.verify()
    if not _timestamp(now):
        raise DutyRefused("Invalid response time")
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        if not _exists(database):
            return None
        candidates = database.execute(
            """SELECT p.response_duty,p.source_duty,p.project_id,p.terms
               FROM work_peer_request p JOIN duty response ON response.id=p.response_duty
               JOIN agent a ON a.id=response.agent_id AND a.company_id=?
               JOIN duty source ON source.id=p.source_duty AND source.state='waiting'
               WHERE p.outcome IS NULL AND response.state='scheduled'
                 AND a.execution_profile=? AND p.expires_at>?
               ORDER BY p.expires_at,p.rowid""",
            (writer.company_id, profile, now),
        ).fetchall()
        for response_duty, source_duty, project, terms in candidates:
            question = PeerQuestion(**json.loads(terms))
            try:
                route = _project_route(
                    database, writer.company_id, source_duty, question
                )
            except DutyRefused:
                continue
            if (
                (route.project_id, route.profile) == (project, profile)
                and has_turn_capacity(database, question.recipient_id)
                and not database.execute(
                    "SELECT 1 FROM duty WHERE agent_id=? AND state='running'",
                    (question.recipient_id,),
                ).fetchone()
            ):
                return start_run(
                    database,
                    writer.company_id,
                    question.recipient_id,
                    route.grant_id,
                    response_duty,
                    profile,
                    purpose=PEER_RESPONSE,
                    grant_scope="manage-company",
                )
        return None


def _response(database: Connection, claim: DutyClaim) -> tuple:
    authority = resolve_run(database, claim)
    authority.require_current(PEER_RESPONSE)
    row = database.execute(
        """SELECT source_duty,project_id,terms,read_run,outcome,answer,answered_run
           FROM work_peer_request WHERE response_duty=?""",
        (claim.duty_id,),
    ).fetchone()
    if row is None:
        raise DutyRefused("Response duty is not linked to a peer question")
    source_duty, project, terms, *_ = row
    question = PeerQuestion(**json.loads(terms))
    route = _project_route(database, claim.company_id, source_duty, question)
    if route != _ProjectPeer(
        project, claim.founder_id, claim.grant_id, claim.execution_profile
    ):
        raise DutyRefused("Current peer response route does not match its claim")
    return (authority.duty_state, question, *row[3:])


def read_peer_question(writer: WriterLease, claim: DutyClaim, *, now: int) -> dict:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        state, question, _, outcome, _, _ = _response(database, claim)
        if (
            state != "running"
            or outcome is not None
            or not _timestamp(now)
            or now >= question.expires_at
        ):
            raise DutyRefused("Peer question is not open for this run")
        require_desk(database, claim, PEER_RESPONSE)
        database.execute(
            "UPDATE work_peer_request SET read_run=? WHERE response_duty=?",
            (claim.run_id, claim.duty_id),
        )
        evidence = (
            asdict(shared_asset_for(database, claim.founder_id, question.asset_id))
            if question.asset_id
            else None
        )
        return {
            "question": question.question,
            "expected_response": question.expected_response,
            "expires_at": question.expires_at,
            "evidence": evidence,
        }


def answer_peer_question(
    writer: WriterLease, claim: DutyClaim, outcome: str, answer: str, *, now: int
) -> dict:
    writer.verify_for(writer.store, claim.company_id)
    if (
        outcome not in ("answered", "declined")
        or not _text(answer, 1600)
        or not _timestamp(now)
    ):
        raise DutyRefused("Invalid peer outcome")
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        state, question, read_run, prior, saved_answer, completed_run = _response(
            database, claim
        )
        if prior is not None:
            if (prior, saved_answer, completed_run) != (outcome, answer, claim.run_id):
                raise DutyRefused("Peer outcome conflicts with the original result")
        else:
            if (
                state != "running"
                or read_run != claim.run_id
                or now >= question.expires_at
            ):
                raise DutyRefused(
                    "Peer outcome requires a current read before the deadline"
                )
            database.execute(
                "UPDATE work_peer_request SET outcome=?,answer=?,answered_run=? WHERE response_duty=?",
                (outcome, answer, claim.run_id, claim.duty_id),
            )
            database.execute(
                "UPDATE duty SET state='settled' WHERE id=?", (claim.duty_id,)
            )
        return {"response_duty": claim.duty_id, "outcome": outcome}


def claim_resumed_peer_work(
    writer: WriterLease, profile: str, *, now: int
) -> DutyClaim | None:
    writer.verify()
    if not _timestamp(now):
        raise DutyRefused("Invalid resume time")
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        if not _exists(database):
            return None
        expired = database.execute(
            """UPDATE work_peer_request SET outcome='expired'
               WHERE outcome IS NULL AND expires_at <= ? RETURNING response_duty""",
            (now,),
        ).fetchall()
        for (response_duty,) in expired:
            database.execute(
                "UPDATE duty SET state='settled' WHERE id=? AND state IN ('scheduled','running')",
                (response_duty,),
            )
            database.execute(
                "UPDATE duty_run SET is_current=0 WHERE duty_id=?", (response_duty,)
            )
        candidates = database.execute(
            """SELECT p.response_duty,p.source_duty,d.agent_id,g.id
               FROM work_peer_request p JOIN duty d ON d.id=p.source_duty AND d.state='waiting'
               JOIN agent a ON a.id=d.agent_id AND a.company_id=?
               JOIN duty_run original ON original.run_id=p.source_run AND original.duty_id=d.id
               JOIN standing_grant g ON g.id=original.grant_id AND g.agent_id=a.id
               WHERE p.outcome IS NOT NULL AND p.resumed_run IS NULL
                 AND g.scope=original.grant_scope AND g.scope='assessment' AND a.execution_profile=?
               ORDER BY p.rowid""",
            (writer.company_id, profile),
        ).fetchall()
        for response_duty, source_duty, agent_id, grant in candidates:
            if (
                not has_turn_capacity(database, agent_id)
                or database.execute(
                    "SELECT 1 FROM duty WHERE agent_id=? AND state='running'",
                    (agent_id,),
                ).fetchone()
            ):
                continue
            database.execute("UPDATE duty SET state='due' WHERE id=?", (source_duty,))
            resumed = start_run(
                database,
                writer.company_id,
                agent_id,
                grant,
                source_duty,
                profile,
                purpose=_ASSESSMENT,
                grant_scope=_ASSESSMENT,
            )
            database.execute(
                "UPDATE work_peer_request SET resumed_run=? WHERE response_duty=?",
                (resumed.run_id, response_duty),
            )
            return resumed
        return None


def has_waiting_peer_work(writer: WriterLease) -> bool:
    writer.verify()
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        return (
            _exists(database)
            and database.execute(
                """SELECT 1 FROM work_peer_request p
               JOIN duty d ON d.id=p.source_duty AND d.state='waiting'
               JOIN agent a ON a.id=d.agent_id AND a.company_id=?
               JOIN duty_run original ON original.run_id=p.source_run
               JOIN standing_grant g ON g.id=original.grant_id AND g.scope='assessment'
               WHERE p.resumed_run IS NULL LIMIT 1""",
                (writer.company_id,),
            ).fetchone()
            is not None
        )


def recover_unanswered_peer_runs(writer: WriterLease) -> None:
    writer.verify()
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        if not _exists(database):
            return
        recovered = database.execute(
            """SELECT d.id FROM work_peer_request p
               JOIN duty d ON d.id=p.response_duty AND d.state='running'
               JOIN agent a ON a.id=d.agent_id AND a.company_id=?
               WHERE p.outcome IS NULL""",
            (writer.company_id,),
        ).fetchall()
        for (duty_id,) in recovered:
            database.execute(
                "UPDATE duty_run SET is_current=0 WHERE duty_id=? AND is_current=1",
                (duty_id,),
            )
            database.execute("UPDATE duty SET state='scheduled' WHERE id=?", (duty_id,))


def peer_turn_has_disposition(
    writer: WriterLease, claim: DutyClaim, *, now: int
) -> bool:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        resolved = resolve_run(database, claim)
        return (
            resolved.purpose == PEER_RESPONSE
            and database.execute(
                """SELECT 1 FROM work_peer_request WHERE response_duty=?
                   AND ((answered_run=? AND outcome IN ('answered','declined'))
                     OR (outcome IS NULL AND expires_at <= ?))""",
                (claim.duty_id, claim.run_id, now),
            ).fetchone()
            is not None
        )


def read_peer_outcome(writer: WriterLease, claim: DutyClaim) -> dict | None:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        require_desk(database, claim, _ASSESSMENT)
        row = (
            database.execute(
                """SELECT request_key,outcome,answer,project_id FROM work_peer_request
               WHERE source_duty=? AND resumed_run IS NOT NULL
               ORDER BY rowid DESC LIMIT 1""",
                (claim.duty_id,),
            ).fetchone()
            if _exists(database)
            else None
        )
        if row is None:
            return None
        request_key, outcome, answer, project = row
        try:
            _, members, _ = project_membership(database, project)
        except DutyRefused:
            members = ()
        if claim.founder_id not in members:
            return {
                "request_key": request_key,
                "outcome": "unavailable",
                "answer": None,
            }
        return {"request_key": request_key, "outcome": outcome, "answer": answer}
