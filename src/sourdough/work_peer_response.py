"""Record a blocking company Work request over the canonical Work kernel."""

import json
from contextlib import closing
from dataclasses import asdict, dataclass, replace
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
    WorkRunClaim,
    employment_cutoff,
    has_turn_capacity,
    resolve_run,
    start_run,
)
from .onboarding import has_hired_acceptance
from .runtime.work_kernel import (
    _PEER_REQUEST_COLUMNS,
    MAX_PEER_CHAIN_DEPTH as MAX_PEER_CHAIN_DEPTH,
    PAIR_WINDOW_NS as PAIR_WINDOW_NS,
    PAIRWISE_RATE_LIMIT_COUNT as PAIRWISE_RATE_LIMIT_COUNT,
    PeerRequest,
    RunFenced,
    WorkKernelRefused,
    advance_peer_read_on_resume,
    claim_work_run,
    has_active_grant,
    has_authorized_peer_response_item,
    mark_peer_request_resumed,
    open_peer_request,
    read_peer_request_outcome,
    record_peer_request_read,
    resolve_peer_request,
    sync_duty_terminal,
    sync_work_item,
    sync_work_run,
    upsert_project_peer_responsibility,
    wake_waiting_runs,
)
from .writer_lease import WriterLease


PEER_RESPONSE = "peer_response"
_ASSESSMENT = "assessment"
_NS_PER_SECOND = 1_000_000_000
_PREFIXED_PEER_REQUEST_COLUMNS = ", ".join(
    f"p.{col.strip()}" for col in _PEER_REQUEST_COLUMNS.split(",")
)


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
        f"""SELECT {_PEER_REQUEST_COLUMNS}
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


def peer_wait_for_run(
    writer: WriterLease, claim: DutyClaim | WorkRunClaim
) -> PeerWait | None:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        if isinstance(claim, WorkRunClaim):
            run_row = database.execute(
                """SELECT status, fencing_token, work_item_id, agent_id
                   FROM work_runs WHERE company_id = ? AND run_id = ?""",
                (claim.company_id, claim.run_id),
            ).fetchone()
            if run_row is None or (
                int(run_row[1]),
                str(run_row[2]),
                str(run_row[3]),
            ) != (claim.fencing_token, claim.duty_id, claim.founder_id):
                raise DutyRefused("Work run claim identity mismatch")
            if str(run_row[0]) not in ("running", "waiting"):
                resolve_run(database, claim)
        else:
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
    writer: WriterLease,
    claim: DutyClaim | WorkRunClaim,
    question: PeerQuestion,
    *,
    now: int,
) -> PeerWait:
    writer.verify_for(writer.store, claim.company_id)
    if not _timestamp(now):
        raise DutyRefused("Invalid request time")
    if question.expires_at > (2**63 - 1) // _NS_PER_SECOND:
        raise DutyRefused(
            "expires_at must be in Unix epoch seconds (within 64-bit nanosecond range)"
        )
    refs = _evidence_refs(question)
    evidence_json = json.dumps(sorted(refs), sort_keys=True, separators=(",", ":"))
    deadline_ns = question.expires_at * _NS_PER_SECOND
    now_ns = now * _NS_PER_SECOND
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        prior_row = database.execute(
            """SELECT response_work_item_id, requester_run_id, recipient_agent_id,
                      question, expected_decision, evidence_refs_json, deadline_ns
               FROM work_peer_requests
               WHERE company_id = ? AND requester_work_item_id = ? AND request_key = ?""",
            (claim.company_id, claim.duty_id, question.request_key),
        ).fetchone()

        if isinstance(claim, WorkRunClaim):
            run_row = database.execute(
                """SELECT status, fencing_token, work_item_id, agent_id
                   FROM work_runs
                   WHERE company_id = ? AND run_id = ?""",
                (claim.company_id, claim.run_id),
            ).fetchone()
            if (
                run_row is not None
                and str(run_row[0]) == "waiting"
                and int(run_row[1]) == claim.fencing_token
                and str(run_row[2]) == claim.duty_id
                and str(run_row[3]) == claim.founder_id
            ):
                if prior_row is not None:
                    if prior_row[1:] != (
                        claim.run_id,
                        question.recipient_id,
                        question.question,
                        question.expected_response,
                        evidence_json,
                        deadline_ns,
                    ):
                        raise DutyRefused(
                            "Peer request conflicts with the original receipt"
                        )
                    if employment_cutoff(
                        database, claim.founder_id
                    ) is not None or not has_active_grant(
                        database, claim.company_id, claim.founder_id
                    ):
                        raise DutyRefused("Peer request requires an active grant")
                    return PeerWait(
                        claim.duty_id, str(prior_row[0]), question.expires_at
                    )
                open_existing = database.execute(
                    """SELECT 1 FROM work_peer_requests
                       WHERE company_id = ? AND requester_work_item_id = ? AND state = 'open'""",
                    (claim.company_id, claim.duty_id),
                ).fetchone()
                if open_existing is not None:
                    raise DutyRefused(
                        "Requester work item already has an open blocking peer request"
                    )
            authority = resolve_run(database, claim, now_ns=now_ns)
            if (
                authority.current_scope != authority.issued_scope
                or employment_cutoff(database, claim.founder_id) is not None
            ):
                raise DutyRefused("Peer request requires an active grant")
            if prior_row is not None:
                if prior_row[1:] != (
                    claim.run_id,
                    question.recipient_id,
                    question.question,
                    question.expected_response,
                    evidence_json,
                    deadline_ns,
                ):
                    raise DutyRefused(
                        "Peer request conflicts with the original receipt"
                    )
                return PeerWait(claim.duty_id, str(prior_row[0]), question.expires_at)
            desk_claim = (
                replace(claim, _clock=now_ns)
                if claim.current_now_ns() is None
                else claim
            )
            require_desk(database, desk_claim, claim.purpose)
            if now >= question.expires_at:
                raise DutyRefused("Peer request deadline must be in the future")
            project_id: str | None = None
            if question.asset_id:
                asset = shared_asset_for(database, claim.founder_id, question.asset_id)
                if (
                    sha256(asset.content.encode()).hexdigest()
                    != question.asset_revision
                ):
                    raise DutyRefused("Peer evidence is outside the admitted revision")
                project_id = asset.project_id
            try:
                peer_req, _ = open_peer_request(
                    database,
                    company_id=claim.company_id,
                    requester_work_item_id=claim.duty_id,
                    requester_run_id=claim.run_id,
                    fencing_token=claim.fencing_token,
                    recipient_agent_id=question.recipient_id,
                    question=question.question,
                    expected_decision=question.expected_response,
                    evidence_refs=refs,
                    deadline_ns=deadline_ns,
                    request_id=uuid4().hex,
                    request_key=question.request_key,
                    project_id=project_id,
                    now_ns=now_ns,
                )
            except RunFenced:
                raise
            except WorkKernelRefused as error:
                raise DutyRefused(str(error)) from error
            return PeerWait(
                claim.duty_id, peer_req.response_work_item_id, question.expires_at
            )

        authority = resolve_run(database, claim)
        if (
            authority.purpose != _ASSESSMENT
            or authority.current_scope != authority.issued_scope
            or employment_cutoff(database, claim.founder_id) is not None
        ):
            raise DutyRefused("Peer request requires a current assessment grant")
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
        except RunFenced:
            raise
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
) -> DutyClaim | WorkRunClaim | None:
    writer.verify()
    if not _timestamp(now):
        raise DutyRefused("Invalid response time")
    now_ns = now * _NS_PER_SECOND
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        rows = database.execute(
            f"""SELECT {_PREFIXED_PEER_REQUEST_COLUMNS},
                       response.state,
                       EXISTS (
                           SELECT 1 FROM hire_onboarding o
                           WHERE o.first_duty_id = p.requester_work_item_id
                       ) AS is_legacy
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
            peer = PeerRequest(*row[:24])
            response_state = str(row[24])
            is_legacy = bool(row[25])
            question = _question_from_row(peer)
            if is_legacy:
                try:
                    route = _project_route(
                        database,
                        writer.company_id,
                        peer.requester_work_item_id,
                        question,
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
                continue

            if response_state != "due":
                continue
            if (
                employment_cutoff(database, question.recipient_id) is not None
                or not has_turn_capacity(database, question.recipient_id)
                or not has_authorized_peer_response_item(
                    database, writer.company_id, peer.response_work_item_id
                )
                or database.execute(
                    "SELECT 1 FROM duty WHERE agent_id=? AND state='running'",
                    (question.recipient_id,),
                ).fetchone()
                or database.execute(
                    "SELECT 1 FROM work_runs WHERE agent_id=? AND status='running'",
                    (question.recipient_id,),
                ).fetchone()
            ):
                continue
            grant_row = database.execute(
                """SELECT g.id, g.scope
                   FROM standing_grant g
                   JOIN agent a ON a.id = g.agent_id
                   WHERE a.company_id = ? AND a.id = ?
                     AND trim(g.scope) != '' AND lower(trim(g.scope)) != 'revoked'
                   ORDER BY g.id LIMIT 1""",
                (writer.company_id, question.recipient_id),
            ).fetchone()
            if grant_row is None:
                continue
            ensure_private_desk(
                database, writer.company_id, question.recipient_id, profile
            )
            try:
                resp_run = claim_work_run(
                    database,
                    company_id=writer.company_id,
                    work_item_id=peer.response_work_item_id,
                    lease_owner="peer_response",
                    lease_ttl_ns=60_000_000_000,
                    now_ns=now_ns,
                )
            except WorkKernelRefused:
                continue
            return WorkRunClaim(
                company_id=writer.company_id,
                founder_id=question.recipient_id,
                duty_id=peer.response_work_item_id,
                run_id=resp_run.run_id,
                fencing_token=resp_run.fencing_token,
                grant_id=str(grant_row[0]),
                grant_scope=str(grant_row[1]),
                purpose=PEER_RESPONSE,
                _clock=now_ns,
            )
        return None


def _response(
    database: Connection,
    claim: DutyClaim | WorkRunClaim,
    *,
    now_ns: int | None = None,
) -> tuple[str, PeerRequest, PeerQuestion]:
    authority = resolve_run(database, claim, now_ns=now_ns)
    if isinstance(claim, WorkRunClaim):
        if authority.purpose not in (PEER_RESPONSE, f"work.{PEER_RESPONSE}"):
            raise DutyRefused("Duty claim or grant is no longer current")
        authority.require_current(authority.purpose)
    else:
        authority.require_current(PEER_RESPONSE)
    peer = _fetch_peer_by_response(database, claim.company_id, claim.duty_id)
    if peer is None:
        raise DutyRefused("Response duty is not linked to a peer question")
    question = _question_from_row(peer)
    if isinstance(claim, WorkRunClaim):
        if peer.recipient_agent_id != claim.founder_id:
            raise DutyRefused("Current peer response route does not match its claim")
        return authority.duty_state, peer, question
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


def read_peer_question(
    writer: WriterLease, claim: DutyClaim | WorkRunClaim, *, now: int
) -> dict:
    writer.verify_for(writer.store, claim.company_id)
    if not _timestamp(now):
        raise DutyRefused("Peer question is not open for this run")
    now_ns = now * _NS_PER_SECOND
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        eff_claim = (
            replace(claim, _clock=now_ns)
            if isinstance(claim, WorkRunClaim) and claim.current_now_ns() is None
            else claim
        )
        state, peer, question = _response(database, eff_claim, now_ns=now_ns)
        if state != "running" or peer.state != "open" or now >= question.expires_at:
            raise DutyRefused("Peer question is not open for this run")
        require_desk(
            database,
            eff_claim,
            eff_claim.purpose if isinstance(eff_claim, WorkRunClaim) else PEER_RESPONSE,
        )
        token = (
            claim.fencing_token
            if isinstance(claim, WorkRunClaim)
            else _run_fencing_token(database, claim.run_id)
        )
        try:
            record_peer_request_read(
                database,
                company_id=claim.company_id,
                request_id=peer.request_id,
                actor_id=claim.founder_id,
                response_run_id=claim.run_id,
                response_fencing_token=token,
                now_ns=now_ns,
            )
        except RunFenced:
            raise
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
    writer: WriterLease,
    claim: DutyClaim | WorkRunClaim,
    outcome: str,
    answer: str,
    *,
    now: int,
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
        if isinstance(claim, WorkRunClaim):
            existing_peer = _fetch_peer_by_response(
                database, claim.company_id, claim.duty_id
            )
            if existing_peer is not None and existing_peer.state != "open":
                run_row = database.execute(
                    """SELECT work_item_id, agent_id, fencing_token, status
                       FROM work_runs
                       WHERE company_id = ? AND run_id = ?""",
                    (claim.company_id, claim.run_id),
                ).fetchone()
                if (
                    run_row is None
                    or (str(run_row[0]), str(run_row[1]), int(run_row[2]))
                    != (claim.duty_id, claim.founder_id, claim.fencing_token)
                    or str(run_row[3]) not in ("running", "completed")
                ):
                    raise DutyRefused(
                        "Response duty claim does not match peer question"
                    )
                if employment_cutoff(database, claim.founder_id) is not None:
                    raise DutyRefused("Agent employment is fenced")
                if not has_active_grant(database, claim.company_id, claim.founder_id):
                    raise DutyRefused("Agent has no active standing grant")
                grant_row = database.execute(
                    """SELECT g.scope FROM standing_grant g
                       JOIN agent a ON a.id = g.agent_id
                       WHERE g.id = ? AND g.agent_id = ? AND a.company_id = ?""",
                    (claim.grant_id, claim.founder_id, claim.company_id),
                ).fetchone()
                if (
                    grant_row is None
                    or not str(grant_row[0]).strip()
                    or str(grant_row[0]).strip().lower() == "revoked"
                ):
                    raise DutyRefused("Work run claim grant is revoked")
                desk_row = database.execute(
                    "SELECT manifest_revision FROM private_desk WHERE agent_id = ?",
                    (claim.founder_id,),
                ).fetchone()
                if desk_row is None:
                    raise DutyRefused("The current Agent has no private Desk")
                saved_payload = (
                    json.loads(existing_peer.response_json)
                    if existing_peer.response_json is not None
                    else {}
                )
                saved_answer = (
                    saved_payload.get("answer")
                    if isinstance(saved_payload, dict)
                    else None
                )
                if (
                    existing_peer.state,
                    saved_answer,
                    existing_peer.answered_run_id,
                ) != (
                    outcome,
                    answer,
                    claim.run_id,
                ):
                    raise DutyRefused("Peer outcome conflicts with the original result")
                return {"response_duty": claim.duty_id, "outcome": outcome}
        now_ns = now * _NS_PER_SECOND
        state, peer, question = _response(database, claim, now_ns=now_ns)
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
        token = (
            claim.fencing_token
            if isinstance(claim, WorkRunClaim)
            else _run_fencing_token(database, claim.run_id)
        )
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
        except RunFenced:
            raise
        except WorkKernelRefused as error:
            raise DutyRefused(str(error)) from error
        if isinstance(claim, DutyClaim):
            database.execute(
                "UPDATE duty SET state='settled' WHERE id=?", (claim.duty_id,)
            )
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
) -> DutyClaim | WorkRunClaim | None:
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

        canonical_candidates = database.execute(
            """SELECT p.request_id, p.requester_work_item_id, p.requester_run_id,
                      wi.owner_agent_id, wi.duty_kind, wi.state
               FROM work_peer_requests p
               JOIN work_items wi ON wi.work_item_id = p.requester_work_item_id
               JOIN agent a ON a.id = wi.owner_agent_id AND a.company_id = p.company_id
               WHERE p.company_id = ? AND p.state != 'open' AND p.resumed_run_id IS NULL
                 AND wi.state IN ('waiting', 'due')
                 AND a.execution_profile = ?
                 AND NOT EXISTS (SELECT 1 FROM duty_run dr WHERE dr.run_id = p.requester_run_id)
               ORDER BY p.rowid""",
            (writer.company_id, profile),
        ).fetchall()
        for (
            req_id,
            source_item_id,
            orig_run_id,
            agent_id,
            item_duty_kind,
            item_state,
        ) in canonical_candidates:
            agent_str = str(agent_id)
            item_str = str(source_item_id)
            if (
                employment_cutoff(database, agent_str) is not None
                or not has_turn_capacity(database, agent_str)
                or database.execute(
                    "SELECT 1 FROM duty WHERE agent_id=? AND state='running'",
                    (agent_str,),
                ).fetchone()
                or database.execute(
                    "SELECT 1 FROM work_runs WHERE agent_id=? AND status='running'",
                    (agent_str,),
                ).fetchone()
            ):
                continue
            grant_row = database.execute(
                """SELECT g.id, g.scope
                   FROM standing_grant g
                   JOIN agent a ON a.id = g.agent_id
                   WHERE a.company_id = ? AND a.id = ?
                     AND trim(g.scope) != '' AND lower(trim(g.scope)) != 'revoked'
                   ORDER BY g.id LIMIT 1""",
                (writer.company_id, agent_str),
            ).fetchone()
            if grant_row is None:
                continue
            if str(item_state) == "waiting":
                wake_waiting_runs(
                    database,
                    writer.company_id,
                    now_ns=now_ns,
                    event_key=f"peer_response:{req_id}",
                    triggering_actor_id="work_scheduler",
                )
            try:
                resumed_run = claim_work_run(
                    database,
                    company_id=writer.company_id,
                    work_item_id=item_str,
                    lease_owner="peer_resume",
                    lease_ttl_ns=60_000_000_000,
                    now_ns=now_ns,
                )
            except WorkKernelRefused:
                continue
            mark_peer_request_resumed(
                database,
                company_id=writer.company_id,
                request_id=str(req_id),
                resumed_run_id=resumed_run.run_id,
            )
            duty_kind_str = str(item_duty_kind)
            if duty_kind_str == f"work.{PEER_RESPONSE}":
                advance_peer_read_on_resume(
                    database,
                    company_id=writer.company_id,
                    response_work_item_id=item_str,
                    old_run_id=str(orig_run_id),
                    new_run_id=resumed_run.run_id,
                )
            return WorkRunClaim(
                company_id=writer.company_id,
                founder_id=agent_str,
                duty_id=item_str,
                run_id=resumed_run.run_id,
                fencing_token=resumed_run.fencing_token,
                grant_id=str(grant_row[0]),
                grant_scope=str(grant_row[1]),
                purpose=(
                    PEER_RESPONSE
                    if duty_kind_str == f"work.{PEER_RESPONSE}"
                    else duty_kind_str.removeprefix("work.")
                ),
                _clock=now_ns,
            )
        return None


def has_waiting_peer_work(writer: WriterLease) -> bool:
    writer.verify()
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        legacy_waiting = (
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
        if legacy_waiting:
            return True
        return (
            database.execute(
                """SELECT 1 FROM work_peer_requests p
                   JOIN work_items wi ON wi.work_item_id = p.requester_work_item_id
                   WHERE p.company_id = ? AND p.resumed_run_id IS NULL
                     AND wi.state = 'waiting'
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
            """SELECT p.response_work_item_id, p.state
               FROM work_peer_requests p
               JOIN agent a ON a.id = p.recipient_agent_id AND a.company_id = p.company_id
               LEFT JOIN duty d ON d.id = p.response_work_item_id
               WHERE p.company_id = ?
                 AND (
                     d.state = 'running'
                     OR EXISTS (
                         SELECT 1 FROM work_runs wr
                         WHERE wr.work_item_id = p.response_work_item_id
                           AND wr.status = 'running'
                     )
                 )""",
            (writer.company_id,),
        ).fetchall()
        for duty_id, peer_state in recovered:
            item_id = str(duty_id)
            database.execute(
                "UPDATE duty_run SET is_current=0 WHERE duty_id=? AND is_current=1",
                (item_id,),
            )
            if str(peer_state) == "open":
                database.execute(
                    "UPDATE duty SET state='scheduled' WHERE id=?", (item_id,)
                )
                sync_duty_terminal(
                    database,
                    item_id,
                    item_state="due",
                    run_status="fenced",
                )
            else:
                database.execute(
                    "UPDATE duty SET state='settled' WHERE id=?", (item_id,)
                )
                sync_duty_terminal(
                    database,
                    item_id,
                    item_state="settled"
                    if str(peer_state) == "answered"
                    else "cancelled",
                    run_status="fenced",
                )


def peer_turn_has_disposition(
    writer: WriterLease, claim: DutyClaim | WorkRunClaim, *, now: int
) -> bool:
    writer.verify_for(writer.store, claim.company_id)
    now_ns = now * _NS_PER_SECOND
    with closing(_connect(writer.store, "ro")) as database:
        if isinstance(claim, WorkRunClaim):
            run_row = database.execute(
                """SELECT status, fencing_token, work_item_id, agent_id
                   FROM work_runs WHERE company_id = ? AND run_id = ?""",
                (claim.company_id, claim.run_id),
            ).fetchone()
            if (
                run_row is None
                or str(run_row[0]) not in ("running", "completed")
                or (int(run_row[1]), str(run_row[2]), str(run_row[3]))
                != (claim.fencing_token, claim.duty_id, claim.founder_id)
                or claim.purpose not in (PEER_RESPONSE, f"work.{PEER_RESPONSE}")
            ):
                return False
            return (
                database.execute(
                    """SELECT 1 FROM work_peer_requests
                       WHERE company_id = ? AND response_work_item_id = ?
                         AND ((answered_run_id = ? AND state IN ('answered', 'declined'))
                           OR (state = 'open' AND deadline_ns <= ?))""",
                    (claim.company_id, claim.duty_id, claim.run_id, now_ns),
                ).fetchone()
                is not None
            )
        resolved = resolve_run(database, claim, now_ns=now_ns)
        return (
            resolved.purpose in (PEER_RESPONSE, f"work.{PEER_RESPONSE}")
            and database.execute(
                """SELECT 1 FROM work_peer_requests
                   WHERE company_id = ? AND response_work_item_id = ?
                     AND ((answered_run_id = ? AND state IN ('answered', 'declined'))
                       OR (state = 'open' AND deadline_ns <= ?))""",
                (claim.company_id, claim.duty_id, claim.run_id, now_ns),
            ).fetchone()
            is not None
        )


def read_peer_outcome(
    writer: WriterLease, claim: DutyClaim | WorkRunClaim
) -> dict | None:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        require_desk(
            database,
            claim,
            claim.purpose if isinstance(claim, WorkRunClaim) else _ASSESSMENT,
        )
        return read_peer_request_outcome(
            database,
            company_id=claim.company_id,
            requester_work_item_id=claim.duty_id,
            actor_id=claim.founder_id,
        )
