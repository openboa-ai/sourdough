"""Record a blocking company Work request over the canonical Work kernel."""

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
from .runtime.work_kernel import (
    PeerRequest,
    WorkKernelRefused,
    mark_peer_request_resumed,
    open_peer_request,
    read_peer_request_outcome,
    record_peer_request_read,
    resolve_peer_request,
    sync_duty_terminal,
    sync_work_item,
    sync_work_run,
    upsert_project_peer_responsibility,
)
from .writer_lease import WriterLease


PEER_RESPONSE = "peer_response"
_ASSESSMENT = "assessment"
_NS_PER_SECOND = 1_000_000_000


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
    members: tuple[str, ...]


def _evidence_refs(question: PeerQuestion) -> list[str]:
    if not question.asset_id:
        return []
    return [f"shared_publication:{question.asset_id}@{question.asset_revision}"]


def _parse_asset_ref(evidence_refs_json: str) -> tuple[str, str]:
    refs = json.loads(evidence_refs_json)
    if not refs:
        return "", ""
    first = str(refs[0])
    if first.startswith("shared_publication:") and "@" in first:
        spec = first.removeprefix("shared_publication:")
        asset_id, asset_revision = spec.split("@", 1)
        return asset_id, asset_revision
    return "", ""


def _question_from_row(peer: PeerRequest) -> PeerQuestion:
    asset_id, asset_revision = _parse_asset_ref(peer.evidence_refs_json)
    return PeerQuestion(
        request_key=peer.request_key or peer.request_id,
        recipient_id=peer.recipient_agent_id,
        question=peer.question,
        expected_response=peer.expected_decision,
        expires_at=peer.deadline_ns // _NS_PER_SECOND,
        asset_id=asset_id,
        asset_revision=asset_revision,
    )


def _fetch_peer_by_response(
    database: Connection, company_id: str, response_work_item_id: str
) -> PeerRequest | None:
    row = database.execute(
        """SELECT request_id, company_id, requester_work_item_id, requester_run_id,
                  requester_agent_id, recipient_agent_id, response_work_item_id,
                  question, expected_decision, evidence_refs_json, deadline_ns,
                  state, response_json, resolved_at_ns, created_at_ns,
                  request_key, project_id, read_run_id, answered_run_id, resumed_run_id
           FROM work_peer_requests
           WHERE company_id = ? AND response_work_item_id = ?""",
        (company_id, response_work_item_id),
    ).fetchone()
    return PeerRequest(*row) if row is not None else None


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
    return _ProjectPeer(project_id, recipient, grant, profile, members)


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
        if authority.purpose != _ASSESSMENT:
            return None
        row = database.execute(
            """SELECT requester_work_item_id, response_work_item_id, deadline_ns
               FROM work_peer_requests
               WHERE company_id = ? AND requester_work_item_id = ? AND requester_run_id = ?""",
            (claim.company_id, claim.duty_id, claim.run_id),
        ).fetchone()
        if row is None:
            return None
        return PeerWait(str(row[0]), str(row[1]), int(row[2]) // _NS_PER_SECOND)


def request_peer_response(
    writer: WriterLease, claim: DutyClaim, question: PeerQuestion, *, now: int
) -> PeerWait:
    writer.verify_for(writer.store, claim.company_id)
    if not _timestamp(now):
        raise DutyRefused("Invalid request time")
    refs = _evidence_refs(question)
    evidence_json = json.dumps(sorted(refs), sort_keys=True, separators=(",", ":"))
    deadline_ns = question.expires_at * _NS_PER_SECOND
    now_ns = now * _NS_PER_SECOND
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        authority = resolve_run(database, claim)
        if (
            authority.purpose != _ASSESSMENT
            or authority.current_scope != authority.issued_scope
            or employment_cutoff(database, claim.founder_id) is not None
        ):
            raise DutyRefused("Peer request requires a current assessment grant")
        prior_row = database.execute(
            """SELECT response_work_item_id, requester_run_id, recipient_agent_id,
                      question, expected_decision, evidence_refs_json, deadline_ns
               FROM work_peer_requests
               WHERE company_id = ? AND requester_work_item_id = ? AND request_key = ?""",
            (claim.company_id, claim.duty_id, question.request_key),
        ).fetchone()
        if prior_row is not None:
            if prior_row[1:] != (
                claim.run_id,
                question.recipient_id,
                question.question,
                question.expected_response,
                evidence_json,
                deadline_ns,
            ):
                raise DutyRefused("Peer request conflicts with the original receipt")
            return PeerWait(claim.duty_id, str(prior_row[0]), question.expires_at)
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
        upsert_project_peer_responsibility(
            database,
            company_id=claim.company_id,
            owner_agent_id=route.recipient_id,
            project_id=route.project_id,
            allowed_requesters=[m for m in route.members if m != route.recipient_id],
            now_ns=now_ns,
        )
        sync_work_item(
            database,
            company_id=claim.company_id,
            work_item_id=claim.duty_id,
            owner_agent_id=claim.founder_id,
            duty_kind=f"work.{_ASSESSMENT}",
            state="running",
            source_kind="duty",
            source_id=claim.duty_id,
            now_ns=now_ns,
        )
        fencing_token = sync_work_run(
            database,
            company_id=claim.company_id,
            work_item_id=claim.duty_id,
            run_id=claim.run_id,
            agent_id=claim.founder_id,
            duty_kind=f"work.{_ASSESSMENT}",
            status="running",
            now_ns=now_ns,
        )
        try:
            peer_req, _ = open_peer_request(
                database,
                company_id=claim.company_id,
                requester_work_item_id=claim.duty_id,
                requester_run_id=claim.run_id,
                fencing_token=fencing_token,
                recipient_agent_id=question.recipient_id,
                question=question.question,
                expected_decision=question.expected_response,
                evidence_refs=refs,
                deadline_ns=deadline_ns,
                request_id=uuid4().hex,
                request_key=question.request_key,
                project_id=route.project_id,
                now_ns=now_ns,
            )
        except WorkKernelRefused as error:
            raise DutyRefused(str(error)) from error
        database.execute(
            "INSERT OR IGNORE INTO duty VALUES (?,?,'scheduled')",
            (peer_req.response_work_item_id, question.recipient_id),
        )
        database.execute("UPDATE duty SET state='waiting' WHERE id=?", (claim.duty_id,))
        database.execute(
            "UPDATE duty_run SET is_current=0 WHERE run_id=?", (claim.run_id,)
        )
        return PeerWait(
            claim.duty_id, peer_req.response_work_item_id, question.expires_at
        )


def claim_peer_response(
    writer: WriterLease, profile: str, *, now: int
) -> DutyClaim | None:
    writer.verify()
    if not _timestamp(now):
        raise DutyRefused("Invalid response time")
    now_ns = now * _NS_PER_SECOND
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        rows = database.execute(
            """SELECT p.request_id, p.company_id, p.requester_work_item_id, p.requester_run_id,
                      p.requester_agent_id, p.recipient_agent_id, p.response_work_item_id,
                      p.question, p.expected_decision, p.evidence_refs_json, p.deadline_ns,
                      p.state, p.response_json, p.resolved_at_ns, p.created_at_ns,
                      p.request_key, p.project_id, p.read_run_id, p.answered_run_id, p.resumed_run_id
               FROM work_peer_requests p
               JOIN work_items response ON response.work_item_id = p.response_work_item_id
               JOIN agent a ON a.id = p.recipient_agent_id AND a.company_id = p.company_id
               JOIN work_items source ON source.work_item_id = p.requester_work_item_id
                    AND source.state = 'waiting'
               WHERE p.company_id = ? AND p.state = 'open'
                 AND response.state IN ('due', 'scheduled')
                 AND a.execution_profile = ? AND p.deadline_ns > ?
               ORDER BY p.deadline_ns, p.rowid""",
            (writer.company_id, profile, now_ns),
        ).fetchall()
        for row in rows:
            peer = PeerRequest(*row)
            question = _question_from_row(peer)
            try:
                route = _project_route(
                    database, writer.company_id, peer.requester_work_item_id, question
                )
            except DutyRefused:
                continue
            if (
                (route.project_id, route.profile) == (peer.project_id, profile)
                and has_turn_capacity(database, question.recipient_id)
                and not database.execute(
                    "SELECT 1 FROM duty WHERE agent_id=? AND state='running'",
                    (question.recipient_id,),
                ).fetchone()
                and not database.execute(
                    "SELECT 1 FROM work_runs WHERE agent_id=? AND status='running'",
                    (question.recipient_id,),
                ).fetchone()
            ):
                database.execute(
                    "INSERT OR IGNORE INTO duty VALUES (?,?,'scheduled')",
                    (peer.response_work_item_id, question.recipient_id),
                )
                return start_run(
                    database,
                    writer.company_id,
                    question.recipient_id,
                    route.grant_id,
                    peer.response_work_item_id,
                    profile,
                    purpose=PEER_RESPONSE,
                    grant_scope="manage-company",
                )
        return None


def _response(
    database: Connection, claim: DutyClaim
) -> tuple[str, PeerRequest, PeerQuestion]:
    authority = resolve_run(database, claim)
    authority.require_current(PEER_RESPONSE)
    peer = _fetch_peer_by_response(database, claim.company_id, claim.duty_id)
    if peer is None:
        raise DutyRefused("Response duty is not linked to a peer question")
    question = _question_from_row(peer)
    route = _project_route(
        database, claim.company_id, peer.requester_work_item_id, question
    )
    if (route.project_id, route.recipient_id, route.grant_id, route.profile) != (
        peer.project_id,
        claim.founder_id,
        claim.grant_id,
        claim.execution_profile,
    ):
        raise DutyRefused("Current peer response route does not match its claim")
    return authority.duty_state, peer, question


def _run_fencing_token(database: Connection, run_id: str) -> int:
    row = database.execute(
        "SELECT fencing_token FROM work_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise DutyRefused("Active run is missing its Work kernel fencing token")
    return int(row[0])


def read_peer_question(writer: WriterLease, claim: DutyClaim, *, now: int) -> dict:
    writer.verify_for(writer.store, claim.company_id)
    if not _timestamp(now):
        raise DutyRefused("Peer question is not open for this run")
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        state, peer, question = _response(database, claim)
        if state != "running" or peer.state != "open" or now >= question.expires_at:
            raise DutyRefused("Peer question is not open for this run")
        require_desk(database, claim, PEER_RESPONSE)
        token = _run_fencing_token(database, claim.run_id)
        try:
            record_peer_request_read(
                database,
                company_id=claim.company_id,
                request_id=peer.request_id,
                actor_id=claim.founder_id,
                response_run_id=claim.run_id,
                response_fencing_token=token,
                now_ns=now * _NS_PER_SECOND,
            )
        except WorkKernelRefused as error:
            raise DutyRefused(str(error)) from error
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
        state, peer, question = _response(database, claim)
        if peer.state != "open":
            saved_payload = (
                json.loads(peer.response_json) if peer.response_json is not None else {}
            )
            saved_answer = (
                saved_payload.get("answer") if isinstance(saved_payload, dict) else None
            )
            if (peer.state, saved_answer, peer.answered_run_id) != (
                outcome,
                answer,
                claim.run_id,
            ):
                raise DutyRefused("Peer outcome conflicts with the original result")
            return {"response_duty": claim.duty_id, "outcome": outcome}
        if (
            state != "running"
            or peer.read_run_id != claim.run_id
            or now >= question.expires_at
        ):
            raise DutyRefused(
                "Peer outcome requires a current read before the deadline"
            )
        token = _run_fencing_token(database, claim.run_id)
        now_ns = now * _NS_PER_SECOND
        try:
            resolve_peer_request(
                database,
                company_id=claim.company_id,
                request_id=peer.request_id,
                actor_id=claim.founder_id,
                resolution_state=outcome,
                response_payload={"answer": answer},
                response_run_id=claim.run_id,
                response_fencing_token=token,
                now_ns=now_ns,
            )
        except WorkKernelRefused as error:
            raise DutyRefused(str(error)) from error
        database.execute("UPDATE duty SET state='settled' WHERE id=?", (claim.duty_id,))
        sync_duty_terminal(
            database,
            claim.duty_id,
            item_state="settled",
            run_status="completed",
            now_ns=now_ns,
        )
        return {"response_duty": claim.duty_id, "outcome": outcome}


def claim_resumed_peer_work(
    writer: WriterLease, profile: str, *, now: int
) -> DutyClaim | None:
    writer.verify()
    if not _timestamp(now):
        raise DutyRefused("Invalid resume time")
    now_ns = now * _NS_PER_SECOND
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        expired = database.execute(
            """SELECT request_id, response_work_item_id
               FROM work_peer_requests
               WHERE company_id = ? AND state = 'open' AND deadline_ns <= ?""",
            (writer.company_id, now_ns),
        ).fetchall()
        for req_id, response_duty in expired:
            resolve_peer_request(
                database,
                company_id=writer.company_id,
                request_id=str(req_id),
                actor_id="work_scheduler",
                resolution_state="expired",
                response_payload={"reason": "deadline_elapsed"},
                now_ns=now_ns,
            )
            database.execute(
                "UPDATE duty SET state='settled' WHERE id=? AND state IN ('scheduled','running')",
                (str(response_duty),),
            )
            database.execute(
                "UPDATE duty_run SET is_current=0 WHERE duty_id=?",
                (str(response_duty),),
            )
            sync_duty_terminal(
                database,
                str(response_duty),
                item_state="cancelled",
                run_status="fenced",
                now_ns=now_ns,
            )
        candidates = database.execute(
            """SELECT p.request_id, p.requester_work_item_id, d.agent_id, g.id
               FROM work_peer_requests p
               JOIN duty d ON d.id = p.requester_work_item_id AND d.state = 'waiting'
               JOIN agent a ON a.id = d.agent_id AND a.company_id = p.company_id
               JOIN duty_run original ON original.run_id = p.requester_run_id AND original.duty_id = d.id
               JOIN standing_grant g ON g.id = original.grant_id AND g.agent_id = a.id
               WHERE p.company_id = ? AND p.state != 'open' AND p.resumed_run_id IS NULL
                 AND g.scope = original.grant_scope AND g.scope = 'assessment'
                 AND a.execution_profile = ?
               ORDER BY p.rowid""",
            (writer.company_id, profile),
        ).fetchall()
        for req_id, source_duty, agent_id, grant in candidates:
            if (
                employment_cutoff(database, str(agent_id)) is not None
                or not has_turn_capacity(database, str(agent_id))
                or database.execute(
                    "SELECT 1 FROM duty WHERE agent_id=? AND state='running'",
                    (str(agent_id),),
                ).fetchone()
                or database.execute(
                    "SELECT 1 FROM work_runs WHERE agent_id=? AND status='running'",
                    (str(agent_id),),
                ).fetchone()
            ):
                continue
            database.execute(
                "UPDATE duty SET state='due' WHERE id=?", (str(source_duty),)
            )
            resumed = start_run(
                database,
                writer.company_id,
                str(agent_id),
                str(grant),
                str(source_duty),
                profile,
                purpose=_ASSESSMENT,
                grant_scope=_ASSESSMENT,
            )
            mark_peer_request_resumed(
                database,
                company_id=writer.company_id,
                request_id=str(req_id),
                resumed_run_id=resumed.run_id,
            )
            return resumed
        return None


def has_waiting_peer_work(writer: WriterLease) -> bool:
    writer.verify()
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        return (
            database.execute(
                """SELECT 1 FROM work_peer_requests p
                   JOIN duty d ON d.id = p.requester_work_item_id AND d.state = 'waiting'
                   JOIN agent a ON a.id = d.agent_id AND a.company_id = p.company_id
                   JOIN duty_run original ON original.run_id = p.requester_run_id
                   JOIN standing_grant g ON g.id = original.grant_id AND g.scope = 'assessment'
                   WHERE p.company_id = ? AND p.resumed_run_id IS NULL
                   LIMIT 1""",
                (writer.company_id,),
            ).fetchone()
            is not None
        )


def recover_unanswered_peer_runs(writer: WriterLease) -> None:
    writer.verify()
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        recovered = database.execute(
            """SELECT d.id FROM work_peer_requests p
               JOIN duty d ON d.id = p.response_work_item_id AND d.state = 'running'
               JOIN agent a ON a.id = d.agent_id AND a.company_id = p.company_id
               WHERE p.company_id = ? AND p.state = 'open'""",
            (writer.company_id,),
        ).fetchall()
        for (duty_id,) in recovered:
            database.execute(
                "UPDATE duty_run SET is_current=0 WHERE duty_id=? AND is_current=1",
                (str(duty_id),),
            )
            database.execute(
                "UPDATE duty SET state='scheduled' WHERE id=?", (str(duty_id),)
            )
            sync_duty_terminal(
                database,
                str(duty_id),
                item_state="due",
                run_status="fenced",
            )


def peer_turn_has_disposition(
    writer: WriterLease, claim: DutyClaim, *, now: int
) -> bool:
    writer.verify_for(writer.store, claim.company_id)
    now_ns = now * _NS_PER_SECOND
    with closing(_connect(writer.store, "ro")) as database:
        resolved = resolve_run(database, claim)
        return (
            resolved.purpose == PEER_RESPONSE
            and database.execute(
                """SELECT 1 FROM work_peer_requests
                   WHERE company_id = ? AND response_work_item_id = ?
                     AND ((answered_run_id = ? AND state IN ('answered', 'declined'))
                       OR (state = 'open' AND deadline_ns <= ?))""",
                (claim.company_id, claim.duty_id, claim.run_id, now_ns),
            ).fetchone()
            is not None
        )


def read_peer_outcome(writer: WriterLease, claim: DutyClaim) -> dict | None:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        require_desk(database, claim, _ASSESSMENT)
        return read_peer_request_outcome(
            database,
            company_id=claim.company_id,
            requester_work_item_id=claim.duty_id,
            actor_id=claim.founder_id,
        )
