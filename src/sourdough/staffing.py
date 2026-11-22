"""Record staffing judgment separately from the allocation and hire."""

import json
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from sqlite3 import Connection
from time import time
from uuid import uuid4

from .company import _connect, upgrade_work_schema
from .duty_run import DutyClaim, DutyRefused, RunAuthority, resolve_run, start_run
from .writer_lease import WriterLease


STAFFING_REVIEW = "staffing_review"
CHOICES = frozenset(("hire", "reassign", "defer"))
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS staffing_authority (
        grant_id TEXT PRIMARY KEY NOT NULL REFERENCES standing_grant(id),
        execution_profile TEXT NOT NULL, allowed_scope TEXT NOT NULL,
        slots INTEGER NOT NULL CHECK (slots > 0),
        turns INTEGER NOT NULL CHECK (turns > 0),
        manager_scope TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS staffing_assignment (
        duty_id TEXT PRIMARY KEY NOT NULL REFERENCES duty(id),
        manager_grant_id TEXT NOT NULL REFERENCES staffing_authority(grant_id),
        evidence TEXT NOT NULL, evidence_revision TEXT NOT NULL,
        available_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS staffing_read (
        run_id TEXT PRIMARY KEY NOT NULL REFERENCES duty_run(run_id),
        snapshot TEXT NOT NULL, revision TEXT NOT NULL, read_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS staffing_choice (
        duty_id TEXT PRIMARY KEY NOT NULL REFERENCES staffing_assignment(duty_id),
        run_id TEXT NOT NULL REFERENCES staffing_read(run_id),
        choice_id TEXT NOT NULL UNIQUE,
        terms TEXT NOT NULL, decided_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS hire_receipt (
        request_id TEXT PRIMARY KEY NOT NULL,
        choice_id TEXT NOT NULL UNIQUE REFERENCES staffing_choice(choice_id),
        manager_grant_id TEXT NOT NULL REFERENCES staffing_authority(grant_id),
        agent_id TEXT NOT NULL UNIQUE REFERENCES agent(id),
        grant_id TEXT NOT NULL UNIQUE REFERENCES standing_grant(id),
        allocated_turns INTEGER NOT NULL CHECK (allocated_turns > 0),
        terms TEXT NOT NULL, hired_at INTEGER NOT NULL
    )""",
)


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _label(value: str, limit: int = 2000) -> bool:
    return isinstance(value, str) and 0 < len(value.strip()) <= limit


@dataclass(frozen=True)
class StaffingAuthority:
    manager_grant_id: str
    execution_profile: str
    allowed_scope: str
    slots: int
    turns: int

    def __post_init__(self) -> None:
        if (
            not all(
                _label(value, 128)
                for value in (
                    self.manager_grant_id,
                    self.execution_profile,
                    self.allowed_scope,
                )
            )
            or type(self.slots) is not int
            or not 1 <= self.slots <= 8
            or type(self.turns) is not int
            or not self.slots <= self.turns <= 64
        ):
            raise DutyRefused("Invalid staffing delegation")


@dataclass(frozen=True)
class StaffingNeed:
    duty_id: str
    summary: str
    backlog: int
    available_capacity: int
    assigned_capacity: int
    available_at: int

    def __post_init__(self) -> None:
        if (
            not _label(self.duty_id, 128)
            or not _label(self.summary)
            or any(
                type(value) is not int or not 0 <= value < 2**63
                for value in (
                    self.backlog,
                    self.available_capacity,
                    self.assigned_capacity,
                    self.available_at,
                )
            )
        ):
            raise DutyRefused("Invalid staffing need")


def stage_staffing(
    writer: WriterLease, authority: StaffingAuthority, need: StaffingNeed
) -> str:
    writer.verify()
    upgrade_work_schema(writer.store, writer.company_id)
    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        for statement in SCHEMA:
            connection.execute(statement)
        row = connection.execute(
            """SELECT g.agent_id, g.scope FROM standing_grant g JOIN agent a ON a.id = g.agent_id
               WHERE g.id = ? AND a.company_id = ? AND g.scope != 'revoked'""",
            (authority.manager_grant_id, writer.company_id),
        ).fetchone()
        if row is None:
            raise DutyRefused(
                "Staffing authority has no active manager in this company"
            )
        manager_id, manager_scope = row
        terms = (*asdict(authority).values(), manager_scope)
        connection.execute(
            "INSERT OR IGNORE INTO staffing_authority VALUES (?, ?, ?, ?, ?, ?)", terms
        )
        existing = connection.execute(
            "SELECT * FROM staffing_authority WHERE grant_id = ?", (terms[0],)
        ).fetchone()
        if existing != terms:
            raise DutyRefused("Staffing authority conflicts with the sealed envelope")
        body = _canonical(asdict(need))
        revision = sha256(body.encode()).hexdigest()
        assignment = (
            need.duty_id,
            authority.manager_grant_id,
            body,
            revision,
            need.available_at,
        )
        prior = connection.execute(
            "SELECT * FROM staffing_assignment WHERE duty_id = ?", (need.duty_id,)
        ).fetchone()
        if prior is not None:
            if prior != assignment:
                raise DutyRefused("Staffing need conflicts with the sealed assignment")
        else:
            connection.execute(
                "INSERT INTO duty VALUES (?, ?, 'due')", (need.duty_id, manager_id)
            )
            connection.execute(
                "INSERT INTO staffing_assignment VALUES (?, ?, ?, ?, ?)", assignment
            )
    return revision


def claim_staffing(
    writer: WriterLease, execution_profile: str, *, now: int
) -> DutyClaim | None:
    writer.verify()
    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if not connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name = 'staffing_assignment'"
        ).fetchone():
            return None
        row = connection.execute(
            """SELECT d.agent_id, g.id, d.id, a.execution_profile, g.scope
               FROM staffing_assignment s
               JOIN duty d ON d.id = s.duty_id AND d.state = 'due'
               JOIN agent a ON a.id = d.agent_id
               JOIN standing_grant g ON g.id = s.manager_grant_id AND g.agent_id = a.id
               JOIN staffing_authority delegation ON delegation.grant_id = g.id
                    AND delegation.manager_scope = g.scope
               WHERE a.company_id = ? AND a.execution_profile = ? AND s.available_at <= ?
               ORDER BY s.available_at, d.id LIMIT 1""",
            (writer.company_id, execution_profile, now),
        ).fetchone()
        if row is None:
            return None
        return start_run(
            connection,
            writer.company_id,
            *row[:4],
            purpose=STAFFING_REVIEW,
            grant_scope=row[4],
        )


def _staffing_run(
    connection: Connection, claim: DutyClaim
) -> tuple[str, str, RunAuthority]:
    grant = resolve_run(connection, claim)
    row = connection.execute(
        """SELECT s.evidence, s.evidence_revision, a.manager_scope
           FROM staffing_assignment s JOIN staffing_authority a ON a.grant_id = s.manager_grant_id
           WHERE s.duty_id = ? AND s.manager_grant_id = ?""",
        (claim.duty_id, claim.grant_id),
    ).fetchone()
    if row is None or grant.purpose != STAFFING_REVIEW or row[2] != grant.issued_scope:
        raise DutyRefused(
            "Staffing claim does not match the original delegated assignment"
        )
    body, revision, _ = row
    if sha256(body.encode()).hexdigest() != revision:
        raise DutyRefused("Staffing evidence does not match its sealed revision")
    return body, revision, grant


def _assignment(connection: Connection, claim: DutyClaim) -> tuple[str, str]:
    body, revision, grant = _staffing_run(connection, claim)
    grant.require_current(STAFFING_REVIEW)
    if grant.duty_state != "running":
        raise DutyRefused("Staffing decision requires an active assigned manager")
    return body, revision


def read_need(writer: WriterLease, claim: DutyClaim) -> dict:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        body, revision = _assignment(connection, claim)
        prior = connection.execute(
            "SELECT snapshot, revision FROM staffing_read WHERE run_id = ?",
            (claim.run_id,),
        ).fetchone()
        if prior is not None:
            return json.loads(prior[0]) | {"revision": prior[1]}
        slots, turns = _remaining(connection, claim.grant_id)
        permitted_profile, permitted_scope = connection.execute(
            "SELECT execution_profile, allowed_scope FROM staffing_authority WHERE grant_id = ?",
            (claim.grant_id,),
        ).fetchone()
        snapshot = json.loads(body) | {
            "need_revision": revision,
            "available_slots": slots,
            "available_turns": turns,
            "profile": permitted_profile,
            "scope": permitted_scope,
        }
        saved = _canonical(snapshot)
        observed = sha256(saved.encode()).hexdigest()
        connection.execute(
            "INSERT INTO staffing_read VALUES (?, ?, ?, ?)",
            (claim.run_id, saved, observed, int(time())),
        )
    return snapshot | {"revision": observed}


@dataclass(frozen=True)
class StaffingChoice:
    evidence_revision: str
    action: str
    rationale: str
    alternatives: tuple[str, ...]
    next_check_seconds: int

    def __post_init__(self) -> None:
        if (
            not _label(self.evidence_revision, 128)
            or not isinstance(self.action, str)
            or self.action not in CHOICES
            or not _label(self.rationale)
            or not isinstance(self.alternatives, tuple)
            or not all(isinstance(option, str) for option in self.alternatives)
            or set(self.alternatives) != CHOICES - {self.action}
            or len(self.alternatives) != len(CHOICES) - 1
            or type(self.next_check_seconds) is not int
            or not 60 <= self.next_check_seconds <= 86_400
        ):
            raise DutyRefused("Invalid staffing choice or review condition")


def record_choice(writer: WriterLease, claim: DutyClaim, choice: StaffingChoice) -> str:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        _assignment(connection, claim)
        observed = connection.execute(
            "SELECT revision FROM staffing_read WHERE run_id = ?",
            (claim.run_id,),
        ).fetchone()
        if (choice.evidence_revision,) != observed:
            raise DutyRefused("Staffing choice requires an authorized evidence read")
        body = _canonical(asdict(choice))
        prior = connection.execute(
            "SELECT run_id, choice_id, terms FROM staffing_choice WHERE duty_id = ?",
            (claim.duty_id,),
        ).fetchone()
        if prior is not None:
            if prior[0] != claim.run_id or prior[2] != body:
                raise DutyRefused(
                    "Staffing choice conflicts with the original judgment"
                )
            return prior[1]
        choice_id = uuid4().hex
        connection.execute(
            "INSERT INTO staffing_choice VALUES (?, ?, ?, ?, ?)",
            (claim.duty_id, claim.run_id, choice_id, body, int(time())),
        )
        return choice_id


def _remaining(connection: Connection, manager_grant_id: str) -> tuple[int, int]:
    return connection.execute(
        """SELECT a.slots - count(h.request_id), a.turns - coalesce(sum(h.allocated_turns), 0)
           FROM staffing_authority a LEFT JOIN hire_receipt h ON h.manager_grant_id = a.grant_id
           WHERE a.grant_id = ? GROUP BY a.grant_id""",
        (manager_grant_id,),
    ).fetchone()


@dataclass(frozen=True)
class HireRequest:
    request_id: str
    choice_id: str
    role: str
    execution_profile: str
    scope: str
    turns: int

    def __post_init__(self) -> None:
        if (
            not all(
                _label(value, 128)
                for value in (
                    self.request_id,
                    self.choice_id,
                    self.role,
                    self.execution_profile,
                    self.scope,
                )
            )
            or type(self.turns) is not int
            or not 1 <= self.turns <= 64
        ):
            raise DutyRefused("Invalid hire request")


@dataclass(frozen=True)
class HireReceipt:
    agent_id: str
    grant_id: str
    allocated_turns: int


def admit_hire(
    writer: WriterLease, claim: DutyClaim, request: HireRequest
) -> HireReceipt:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        _staffing_run(connection, claim)
        choice = connection.execute(
            """SELECT terms FROM staffing_choice
               WHERE choice_id = ? AND duty_id = ? AND run_id = ?""",
            (request.choice_id, claim.duty_id, claim.run_id),
        ).fetchone()
        if choice is None or json.loads(choice[0])["action"] != "hire":
            raise DutyRefused("An admitted staffing choice to hire is required")
        terms = _canonical(asdict(request))
        old = connection.execute(
            """SELECT request_id, agent_id, grant_id, allocated_turns, terms
               FROM hire_receipt WHERE request_id = ? OR choice_id = ?""",
            (request.request_id, request.choice_id),
        ).fetchone()
        if old is not None:
            if old[0] != request.request_id or old[-1] != terms:
                raise DutyRefused("Hire replay conflicts with the original request")
            return HireReceipt(*old[1:-1])
        _assignment(connection, claim)
        scope = connection.execute(
            "SELECT execution_profile, allowed_scope FROM staffing_authority WHERE grant_id = ?",
            (claim.grant_id,),
        ).fetchone()
        slots, turns = _remaining(connection, claim.grant_id)
        if (
            scope != (request.execution_profile, request.scope)
            or slots < 1
            or request.turns > turns
        ):
            raise DutyRefused("Hire exceeds delegated scope or allocation")
        agent_id, grant_id = uuid4().hex, uuid4().hex
        connection.execute(
            "INSERT INTO agent VALUES (?, ?, ?)",
            (agent_id, claim.company_id, request.execution_profile),
        )
        connection.execute(
            "INSERT INTO standing_grant VALUES (?, ?, ?)",
            (grant_id, agent_id, request.scope),
        )
        connection.execute(
            "INSERT INTO hire_receipt VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request.request_id,
                request.choice_id,
                claim.grant_id,
                agent_id,
                grant_id,
                request.turns,
                terms,
                int(time()),
            ),
        )
        return HireReceipt(agent_id, grant_id, request.turns)
