"""Publish a company-only assessment from a hired Agent's scoped evidence."""

import json
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from sqlite3 import Connection
from time import time
from uuid import uuid4

from .agent_desk import save_draft
from .company import _connect
from .duty_run import ASSESSMENT_REVIEW, DutyClaim, DutyRefused, resolve_run
from .harness import ScopedWorkflow, tool
from .onboarding import has_hired_acceptance, invoke_onboarding
from .work import (
    FollowUp,
    FollowUpClaim,
    ResultReference,
    read_follow_up_result,
    schedule_follow_up,
)
from .writer_lease import WriterLease


ASSESSMENT = "assessment"
SAVE_PRIVATE_DRAFT = tool(
    "save_private_draft",
    "Save a private Markdown draft for your next duty. No sharing. Use an empty revision for a new draft.",
    {
        "name": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,47}$"},
        "content": {"type": "string"},
        "expected_revision": {"type": "string"},
    },
)
ASSESSMENT_WORKFLOW = ScopedWorkflow(
    ASSESSMENT,
    "Read your assignment and accept it if authorized. Compare the reported and observed "
    "values in one accessible company record. Publish an assessment with your rationale "
    "and a proposed next check. You cannot change the source records.",
    (
        SAVE_PRIVATE_DRAFT,
        tool("read_first_duty", "Read your assignment and private Desk reference.", {}),
        tool(
            "accept_first_duty",
            "Explicitly accept the assignment you read.",
            {
                "revision": {"type": "string"},
                "rationale": {"type": "string", "maxLength": 2000},
            },
        ),
        tool("read_assessment_inputs", "Read the authorized company records.", {}),
        tool(
            "publish_assessment",
            "Publish one assessment for the permitted audience.",
            {
                "source_revision": {"type": "string"},
                "record_id": {"type": "string"},
                "disposition": {
                    "enum": ["recommend_change", "no_change", "investigate"]
                },
                "rationale": {"type": "string", "maxLength": 2000},
                "next_check_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 86400,
                },
            },
        ),
    ),
    "publish_assessment",
)

ASSESSMENT_REVIEW_WORKFLOW = ScopedWorkflow(
    ASSESSMENT_REVIEW,
    "Inspect the preceding company result. Decide whether to hold, investigate, or recommend "
    "a change. Cite the observation, record the reason, and choose the next check. "
    "A review does not change the source records.",
    (
        SAVE_PRIVATE_DRAFT,
        tool("read_work_result", "Read the authorized result and current records.", {}),
        tool(
            "record_review_decision",
            "Record the reviewed decision and next responsibility.",
            {
                "observation_revision": {"type": "string"},
                "action": {"enum": ["hold", "investigate", "recommend_change"]},
                "rationale": {"type": "string", "maxLength": 2000},
                "next_check_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 86400,
                },
            },
        ),
    ),
    "record_review_decision",
)
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS assessment_source (
        company_id TEXT PRIMARY KEY NOT NULL REFERENCES company(id),
        revision TEXT NOT NULL, records TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS assessment_read (
        run_id TEXT PRIMARY KEY NOT NULL REFERENCES duty_run(run_id),
        revision TEXT NOT NULL, contents TEXT NOT NULL, read_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS assessment_publication (
        duty_id TEXT PRIMARY KEY NOT NULL REFERENCES duty(id),
        run_id TEXT NOT NULL REFERENCES assessment_read(run_id),
        action_id TEXT NOT NULL UNIQUE, revision TEXT NOT NULL, contents TEXT NOT NULL,
        author_id TEXT NOT NULL REFERENCES agent(id),
        audience_id TEXT NOT NULL REFERENCES agent(id),
        published_at INTEGER NOT NULL, review_at INTEGER NOT NULL
    )""",
)


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(contents: str) -> str:
    return sha256(contents.encode()).hexdigest()


def _text(value, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value.strip()) <= maximum


@dataclass(frozen=True)
class SourceRecord:
    record_id: str
    subject: str
    recorded: int
    observed: int
    available_at: int

    def __post_init__(self) -> None:
        if (
            not _text(self.record_id, 128)
            or not _text(self.subject, 256)
            or any(
                type(value) is not int or not 0 <= value < 2**63
                for value in (self.recorded, self.observed, self.available_at)
            )
        ):
            raise DutyRefused("Invalid assessment source record")


def seal_assessment_source(
    writer: WriterLease, records: tuple[SourceRecord, ...]
) -> str:
    writer.verify()
    if not 1 <= len(records) <= 64 or len(
        {record.record_id for record in records}
    ) != len(records):
        raise DutyRefused("Assessment requires a bounded source with unique records")
    body = _json(
        [asdict(record) for record in sorted(records, key=lambda row: row.record_id)]
    )
    revision = _digest(body)
    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        for statement in SCHEMA:
            connection.execute(statement)
        existing = connection.execute(
            "SELECT revision, records FROM assessment_source WHERE company_id = ?",
            (writer.company_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO assessment_source VALUES (?, ?, ?)",
                (writer.company_id, revision, body),
            )
        elif existing != (revision, body):
            raise DutyRefused("Assessment source conflicts with its sealed revision")
    return revision


def _audience(connection: Connection, claim: DutyClaim) -> str:
    authority = resolve_run(connection, claim)
    authority.require_current(ASSESSMENT)
    if (authority.current_scope, authority.duty_state) != (
        ASSESSMENT,
        "running",
    ) or not has_hired_acceptance(connection, claim):
        raise DutyRefused(
            "Only the hired Agent's accepted assessment duty may use this source"
        )
    row = connection.execute(
        """SELECT manager.agent_id FROM hire_receipt hire
           JOIN standing_grant manager ON manager.id = hire.manager_grant_id
           WHERE hire.agent_id = ?""",
        (claim.founder_id,),
    ).fetchone()
    if row is None:
        raise DutyRefused("Assessment has no original staffing owner")
    return row[0]


def read_assessment_inputs(writer: WriterLease, claim: DutyClaim, *, now: int) -> dict:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        _audience(connection, claim)
        prior = connection.execute(
            "SELECT revision, contents FROM assessment_read WHERE run_id = ?",
            (claim.run_id,),
        ).fetchone()
        if prior is not None:
            return json.loads(prior[1]) | {"revision": prior[0]}
        source = connection.execute(
            "SELECT revision, records FROM assessment_source WHERE company_id = ?",
            (claim.company_id,),
        ).fetchone()
        if source is None or source[0] != _digest(source[1]):
            raise DutyRefused("Sealed assessment source is unavailable")
        snapshot = {
            "source_revision": source[0],
            "records": [
                record
                for record in json.loads(source[1])
                if record["available_at"] <= now
            ],
            "cut_at": now,
        }
        body = _json(snapshot)
        revision = _digest(body)
        connection.execute(
            "INSERT INTO assessment_read VALUES (?, ?, ?, ?)",
            (claim.run_id, revision, body, now),
        )
    return snapshot | {"revision": revision}


@dataclass(frozen=True)
class AssessmentProposal:
    source_revision: str
    record_id: str
    disposition: str
    rationale: str
    next_check_seconds: int

    def __post_init__(self) -> None:
        if (
            not _text(self.source_revision, 128)
            or not _text(self.record_id, 128)
            or self.disposition not in ("recommend_change", "no_change", "investigate")
            or not _text(self.rationale, 2000)
            or type(self.next_check_seconds) is not int
            or not 60 <= self.next_check_seconds <= 86_400
        ):
            raise DutyRefused("Invalid assessment or next check")


@dataclass(frozen=True)
class AssessmentReceipt:
    action_id: str
    revision: str
    published_at: int
    review_at: int

    def reference(self) -> ResultReference:
        return ResultReference(
            "company_assessment", self.action_id, self.revision, self.published_at
        )


def publish_assessment(
    writer: WriterLease, claim: DutyClaim, proposal: AssessmentProposal, *, now: int
) -> AssessmentReceipt:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        recipient = _audience(connection, claim)
        observed = connection.execute(
            "SELECT revision, contents, read_at FROM assessment_read WHERE run_id = ?",
            (claim.run_id,),
        ).fetchone()
        if (
            observed is None
            or observed[0] != proposal.source_revision
            or observed[0] != _digest(observed[1])
            or observed[2] > now
            or not any(
                record["record_id"] == proposal.record_id
                for record in json.loads(observed[1])["records"]
            )
        ):
            raise DutyRefused(
                "Assessment must cite a record read by this duty at its cut"
            )
        body = _json(asdict(proposal))
        old = connection.execute(
            """SELECT action_id, revision, published_at, review_at, run_id, contents
               FROM assessment_publication WHERE duty_id = ?""",
            (claim.duty_id,),
        ).fetchone()
        if old is not None:
            if old[4:] != (claim.run_id, body):
                raise DutyRefused("Publication conflicts with the original assessment")
            return AssessmentReceipt(*old[:4])
        receipt = AssessmentReceipt(
            uuid4().hex, _digest(body), now, now + proposal.next_check_seconds
        )
        connection.execute(
            "INSERT INTO assessment_publication VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                claim.duty_id,
                claim.run_id,
                receipt.action_id,
                receipt.revision,
                body,
                claim.founder_id,
                recipient,
                receipt.published_at,
                receipt.review_at,
            ),
        )
    return receipt


def pending_publications(
    writer: WriterLease,
) -> tuple[tuple[DutyClaim, AssessmentReceipt], ...]:
    writer.verify()
    with closing(_connect(writer.store, "ro")) as connection:
        connection.execute("BEGIN")
        if not connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name = 'assessment_publication'"
        ).fetchone():
            return ()
        rows = connection.execute(
            """SELECT a.id, r.grant_id, d.id, r.run_id, a.execution_profile,
                      p.action_id, p.revision, p.published_at, p.review_at
               FROM assessment_publication p JOIN duty d ON d.id = p.duty_id
               JOIN duty_run r ON r.run_id = p.run_id AND r.is_current = 1
               JOIN agent a ON a.id = d.agent_id AND a.company_id = ?
               WHERE d.state = 'running' AND p.author_id = a.id
               ORDER BY p.published_at, p.action_id""",
            (writer.company_id,),
        ).fetchall()
    return tuple(
        (DutyClaim(writer.company_id, *row[:5]), AssessmentReceipt(*row[5:]))
        for row in rows
    )


def settle_assessment(
    writer: WriterLease, claim: DutyClaim, receipt: AssessmentReceipt
) -> FollowUp:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        authority = resolve_run(connection, claim)
        authority.require_current(ASSESSMENT)
        row = connection.execute(
            """SELECT action_id, revision, published_at, review_at, contents
               FROM assessment_publication WHERE duty_id = ? AND run_id = ? AND author_id = ?""",
            (claim.duty_id, claim.run_id, claim.founder_id),
        ).fetchone()
        if (
            authority.current_scope != ASSESSMENT
            or row is None
            or AssessmentReceipt(*row[:4]) != receipt
            or _digest(row[4]) != receipt.revision
        ):
            raise DutyRefused(
                "The original assessment publication could not be verified"
            )
        result = receipt.reference()
        if authority.duty_state == "settled":
            existing = connection.execute(
                """SELECT duty_id, due_at, purpose, result_kind, result_id, result_revision, available_at
                   FROM work_follow_up WHERE previous_duty_id = ?""",
                (claim.duty_id,),
            ).fetchone()
            if (
                existing is not None
                and (existing[1], existing[2]) == (receipt.review_at, ASSESSMENT_REVIEW)
                and ResultReference(*existing[3:]) == result
            ):
                return FollowUp(existing[0], existing[1], result, ASSESSMENT_REVIEW)
        if authority.duty_state != "running":
            raise DutyRefused("The assessment duty is not eligible for settlement")
        return schedule_follow_up(
            connection,
            claim,
            authority.current_scope,
            result,
            receipt.review_at,
            purpose=ASSESSMENT_REVIEW,
        )


def read_assessment_result(
    writer: WriterLease, work: FollowUpClaim, *, now: int
) -> dict:
    claim = work.claim
    writer.verify_for(writer.store, claim.company_id)

    def project(connection, reference):
        if reference.kind != "company_assessment":
            raise DutyRefused("This review does not reference a company assessment")
        row = connection.execute(
            """SELECT revision, contents FROM assessment_publication
               WHERE action_id = ? AND author_id = ?""",
            (reference.resource_id, claim.founder_id),
        ).fetchone()
        source = connection.execute(
            "SELECT revision, records FROM assessment_source WHERE company_id = ?",
            (claim.company_id,),
        ).fetchone()
        if (
            row is None
            or row[0] != _digest(row[1])
            or source is None
            or source[0] != _digest(source[1])
        ):
            raise DutyRefused("The committed assessment or its source is unavailable")
        return {
            "revision": row[0],
            "assessment": json.loads(row[1]),
            "current_records": [
                record
                for record in json.loads(source[1])
                if record["available_at"] <= now
            ],
        }

    return read_follow_up_result(
        writer.store, work, now=now, read=project, purpose=ASSESSMENT_REVIEW
    )


def invoke_assessment(
    writer: WriterLease, claim: DutyClaim, name: str, arguments: dict
) -> dict:
    if name == "save_private_draft":
        return persist_assessment_draft(writer, claim, ASSESSMENT, arguments)
    if name == "read_assessment_inputs" and arguments == {}:
        return read_assessment_inputs(writer, claim, now=int(time()))
    if name == "publish_assessment" and isinstance(arguments, dict):
        try:
            proposal = AssessmentProposal(**arguments)
        except TypeError as error:
            raise DutyRefused("Unexpected assessment publication fields") from error
        return asdict(publish_assessment(writer, claim, proposal, now=int(time())))
    return invoke_onboarding(writer, claim, name, arguments)


def persist_assessment_draft(
    writer: WriterLease, claim: DutyClaim, purpose: str, arguments: dict
) -> dict:
    if (
        not isinstance(arguments, dict)
        or arguments.keys() != {"name", "content", "expected_revision"}
        or not isinstance(arguments["expected_revision"], str)
    ):
        raise DutyRefused("Private draft requires an explicit base revision")
    return asdict(
        save_draft(
            writer,
            claim,
            purpose,
            name=arguments["name"],
            content=arguments["content"],
            expected_revision=arguments["expected_revision"] or None,
        )
    )
