"""Prepare the hired Agent's desk and bind acceptance to its first duty."""

import json
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from sqlite3 import Connection
from tempfile import TemporaryDirectory
from uuid import uuid4

from .abandoned_work import has_uncommitted_work
from .company import _connect
from .duty_run import (
    PAPER_EXECUTION,
    DutyClaim,
    DutyRefused,
    has_turn_capacity,
    resolve_run,
    start_run,
)
from .writer_lease import WriterLease


MAX_PROVISIONING_ATTEMPTS = 3
PROVISIONING_RETRY_SECONDS = 60
_SCOPE = {"assessment": "assessment", PAPER_EXECUTION: "paper"}
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS hire_onboarding (
        agent_id TEXT PRIMARY KEY NOT NULL REFERENCES hire_receipt(agent_id),
        purpose TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('provisioning', 'blocked', 'ready', 'accepted')),
        attempts INTEGER NOT NULL,
        retry_at INTEGER, reason TEXT,
        first_duty_id TEXT UNIQUE REFERENCES duty(id)
    )""",
    """CREATE TABLE IF NOT EXISTS private_desk (
        agent_id TEXT PRIMARY KEY NOT NULL REFERENCES agent(id),
        manifest_revision TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS onboarding_read (
        run_id TEXT PRIMARY KEY NOT NULL REFERENCES duty_run(run_id),
        revision TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS onboarding_acceptance (
        duty_id TEXT PRIMARY KEY NOT NULL REFERENCES hire_onboarding(first_duty_id),
        run_id TEXT NOT NULL REFERENCES onboarding_read(run_id),
        revision TEXT NOT NULL, rationale TEXT NOT NULL
    )""",
)


@dataclass(frozen=True)
class OnboardingStatus:
    state: str
    first_duty_id: str | None
    retry_at: int | None
    reason: str | None


def _active_hire(
    connection: Connection, company_id: str, agent_id: str
) -> tuple[str, str, str]:
    row = connection.execute(
        """SELECT a.execution_profile, g.id, g.scope FROM hire_receipt h
           JOIN agent a ON a.id = h.agent_id AND a.company_id = ?
           JOIN standing_grant g ON g.id = h.grant_id AND g.agent_id = a.id
           WHERE a.id = ? AND g.scope = json_extract(h.terms, '$.scope')
             AND a.execution_profile = json_extract(h.terms, '$.execution_profile')""",
        (company_id, agent_id),
    ).fetchone()
    if row is None:
        raise DutyRefused("No current hire matches the committed authority")
    return row


def pending_onboarding(
    writer: WriterLease, execution_profile: str, purpose: str, *, resume_accepted: bool
) -> DutyClaim | None:
    if purpose not in _SCOPE:
        raise DutyRefused("Unsupported first duty purpose")
    writer.verify()
    with closing(_connect(writer.store, "ro")) as connection:
        connection.execute("BEGIN")
        tables = {
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_schema WHERE name IN ('hire_receipt','hire_onboarding')"
            )
        }
        if "hire_receipt" not in tables:
            return None
        join = ""
        active = ""
        first_duty_fields = "NULL, NULL"
        arguments = [writer.company_id, execution_profile, _SCOPE[purpose]]
        if "hire_onboarding" in tables:
            join = "LEFT JOIN hire_onboarding o ON o.agent_id=h.agent_id"
            first_duty_fields = "o.state, o.first_duty_id"
            active = """AND (o.agent_id IS NULL OR (
                o.purpose=? AND (o.state != 'accepted' OR ?)
                AND (o.state != 'blocked' OR o.retry_at IS NOT NULL)
            ))"""
            arguments.extend((purpose, resume_accepted))
        candidates = connection.execute(
            f"""SELECT a.id, {first_duty_fields}, manager.id, r.grant_id, r.duty_id, r.run_id,
                       manager.execution_profile
                FROM hire_receipt h
                JOIN agent a ON a.id=h.agent_id
                JOIN standing_grant g ON g.id=h.grant_id AND g.agent_id=a.id
                JOIN staffing_choice choice ON choice.choice_id=h.choice_id
                JOIN duty_run r ON r.run_id=choice.run_id
                JOIN duty d ON d.id=r.duty_id
                JOIN agent manager ON manager.id=d.agent_id AND manager.company_id=a.company_id
                {join}
                WHERE a.company_id=? AND a.execution_profile=? AND g.scope=?
                  AND a.execution_profile=json_extract(h.terms,'$.execution_profile')
                  AND g.scope=json_extract(h.terms,'$.scope')
                  {active}
                ORDER BY h.hired_at,h.request_id""",
            arguments,
        )
        return next(
            (
                DutyClaim(writer.company_id, *identity)
                for agent_id, state, first_duty_id, *identity in candidates
                if has_turn_capacity(connection, agent_id)
                and (
                    state != "accepted"
                    or has_uncommitted_work(connection, first_duty_id)
                )
            ),
            None,
        )


def _status(connection: Connection, agent_id: str) -> OnboardingStatus:
    return OnboardingStatus(
        *connection.execute(
            """SELECT state, first_duty_id, retry_at, reason
               FROM hire_onboarding WHERE agent_id = ?""",
            (agent_id,),
        ).fetchone()
    )


def probe_desk_scratch() -> bool:
    try:
        with TemporaryDirectory(prefix="sd-desk-"):
            return True
    except OSError:
        return False


def provision_hire(
    writer: WriterLease,
    agent_id: str,
    purpose: str,
    ready: Callable[[str], bool],
    *,
    now: int,
) -> OnboardingStatus:
    writer.verify()
    with closing(_connect(writer.store, "rw")) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            profile, _, scope = _active_hire(connection, writer.company_id, agent_id)
            if _SCOPE.get(purpose) != scope:
                raise DutyRefused("First duty purpose does not match the hire scope")
            for statement in SCHEMA:
                connection.execute(statement)
            connection.execute(
                """INSERT OR IGNORE INTO hire_onboarding (agent_id, purpose, state, attempts)
                   VALUES (?, ?, 'provisioning', 0)""",
                (agent_id, purpose),
            )
            current = _status(connection, agent_id)
            (original_purpose, attempts) = connection.execute(
                "SELECT purpose, attempts FROM hire_onboarding WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            if purpose != original_purpose:
                raise DutyRefused(
                    "First duty purpose conflicts with the admitted provision"
                )
            if current.first_duty_id or attempts >= MAX_PROVISIONING_ATTEMPTS:
                return current
            if current.retry_at is not None and current.retry_at > now:
                return current
        unavailable = None
        if not ready(profile):
            unavailable = "runtime_unavailable"
        elif not probe_desk_scratch():
            unavailable = "workspace_unavailable"
        writer.verify()
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            if _active_hire(connection, writer.company_id, agent_id)[0] != profile:
                raise DutyRefused(
                    "Hire lost its configured profile during provisioning"
                )
            current = _status(connection, agent_id)
            if (
                current.first_duty_id is not None
                or current.retry_at is not None
                and current.retry_at > now
            ):
                return current
            (attempts,) = connection.execute(
                "SELECT attempts FROM hire_onboarding WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            if attempts >= MAX_PROVISIONING_ATTEMPTS:
                return current
            if unavailable is not None:
                connection.execute(
                    """UPDATE hire_onboarding SET state = 'blocked', attempts = ?,
                           retry_at = ?, reason = ? WHERE agent_id = ?""",
                    (
                        attempts + 1,
                        now + PROVISIONING_RETRY_SECONDS
                        if attempts + 1 < MAX_PROVISIONING_ATTEMPTS
                        else None,
                        unavailable,
                        agent_id,
                    ),
                )
            else:
                revision = sha256(
                    f"{writer.company_id}:{agent_id}:{profile}".encode()
                ).hexdigest()
                connection.execute(
                    "INSERT INTO private_desk VALUES (?, ?)", (agent_id, revision)
                )
                duty_id = uuid4().hex
                connection.execute(
                    "INSERT INTO duty VALUES (?, ?, 'due')", (duty_id, agent_id)
                )
                connection.execute(
                    """UPDATE hire_onboarding
                       SET state = 'ready', retry_at = NULL, reason = NULL, first_duty_id = ?
                       WHERE agent_id = ?""",
                    (duty_id, agent_id),
                )
            return _status(connection, agent_id)


def claim_hired_first(
    writer: WriterLease, agent_id: str, execution_profile: str, *, resume: bool = False
) -> DutyClaim | None:
    writer.verify()
    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if not connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name = 'hire_onboarding' AND type = 'table'"
        ).fetchone():
            return None
        profile, grant_id, scope = _active_hire(connection, writer.company_id, agent_id)
        row = connection.execute(
            """SELECT d.id, o.purpose, d.state, o.state
               FROM hire_onboarding o
               JOIN duty d ON d.id = o.first_duty_id AND d.agent_id = o.agent_id
               JOIN private_desk desk ON desk.agent_id = o.agent_id
               WHERE o.agent_id = ? AND (o.state = 'ready' OR (? AND o.state = 'accepted'))""",
            (agent_id, resume),
        ).fetchone()
        if (
            row is None
            or execution_profile != profile
            or not has_turn_capacity(connection, agent_id)
        ):
            return None
        duty_id, purpose, state, onboarding_state = row
        if _SCOPE.get(purpose) != scope:
            raise DutyRefused("First duty purpose does not match the hire scope")
        if onboarding_state == "accepted" and not has_uncommitted_work(
            connection, duty_id
        ):
            return None
        if state == "running" and resume:
            connection.execute(
                "UPDATE duty_run SET is_current = 0 WHERE duty_id = ? AND is_current = 1",
                (duty_id,),
            )
            connection.execute("UPDATE duty SET state = 'due' WHERE id = ?", (duty_id,))
        elif state != "due":
            return None
        return start_run(
            connection,
            writer.company_id,
            agent_id,
            grant_id,
            duty_id,
            profile,
            purpose=purpose,
            grant_scope=scope,
        )


def _desk(connection: Connection, claim: DutyClaim) -> dict:
    authority = resolve_run(connection, claim)
    row = connection.execute(
        """SELECT o.purpose, desk.manifest_revision, c.charter_revision,
                  json_extract(assignment.evidence, '$.summary')
           FROM hire_onboarding o JOIN private_desk desk ON desk.agent_id = o.agent_id
           JOIN agent a ON a.id = o.agent_id JOIN company c ON c.id = a.company_id
           JOIN hire_receipt hire ON hire.agent_id = o.agent_id
           JOIN staffing_choice choice ON choice.choice_id = hire.choice_id
           JOIN staffing_assignment assignment ON assignment.duty_id = choice.duty_id
           WHERE o.first_duty_id = ? AND o.agent_id = ? AND o.state IN ('ready', 'accepted')""",
        (claim.duty_id, claim.founder_id),
    ).fetchone()
    if row is None or authority.duty_state != "running":
        raise DutyRefused("This claim has no active first-duty desk")
    purpose, manifest, charter, assignment = row
    authority.require_current(purpose)
    snapshot = {
        "duty_id": claim.duty_id,
        "agent_id": claim.founder_id,
        "purpose": purpose,
        "private_desk_revision": manifest,
        "charter_revision": charter,
        "assignment_summary": assignment,
        "granted_scope": authority.current_scope,
    }
    revision = sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return snapshot | {"revision": revision}


def read_first_duty(writer: WriterLease, claim: DutyClaim) -> dict:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        snapshot = _desk(connection, claim)
        connection.execute(
            "INSERT OR IGNORE INTO onboarding_read VALUES (?, ?)",
            (claim.run_id, snapshot["revision"]),
        )
    return snapshot


def accept_first_duty(
    writer: WriterLease, claim: DutyClaim, revision: str, rationale: str
) -> str:
    if not isinstance(rationale, str) or not 0 < len(rationale.strip()) <= 2000:
        raise DutyRefused("First duty acceptance requires a reason")
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        snapshot = _desk(connection, claim)
        read = connection.execute(
            "SELECT revision FROM onboarding_read WHERE run_id = ?", (claim.run_id,)
        ).fetchone()
        if read != (revision,) or snapshot["revision"] != revision:
            raise DutyRefused(
                "Acceptance requires the Agent's own current desk and duty read"
            )
        acceptance = (claim.run_id, revision, rationale)
        prior = connection.execute(
            """SELECT run_id, revision, rationale FROM onboarding_acceptance WHERE duty_id = ?""",
            (claim.duty_id,),
        ).fetchone()
        if prior is not None and (
            not has_hired_acceptance(connection, claim)
            or prior[1] != revision
            or (prior[0] == claim.run_id and prior != acceptance)
        ):
            raise DutyRefused("Acceptance conflicts with its original record")
        if prior is None:
            connection.execute(
                "INSERT INTO onboarding_acceptance VALUES (?, ?, ?, ?)",
                (claim.duty_id, *acceptance),
            )
            connection.execute(
                "UPDATE hire_onboarding SET state = 'accepted' WHERE first_duty_id = ?",
                (claim.duty_id,),
            )
    return claim.duty_id


def first_duty_accepted(writer: WriterLease, claim: DutyClaim) -> bool:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as connection:
        connection.execute("BEGIN")
        _desk(connection, claim)
        return has_hired_acceptance(connection, claim)


def is_hired_first_duty(connection: Connection, claim: DutyClaim) -> bool:
    if not connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE name = 'hire_onboarding' AND type = 'table'"
    ).fetchone():
        return False
    return (
        connection.execute(
            "SELECT 1 FROM hire_onboarding WHERE agent_id = ? AND first_duty_id = ?",
            (claim.founder_id, claim.duty_id),
        ).fetchone()
        is not None
    )


def has_hired_acceptance(connection: Connection, claim: DutyClaim) -> bool:
    return is_hired_first_duty(connection, claim) and (
        connection.execute(
            """SELECT 1 FROM onboarding_acceptance accepted
               JOIN duty_run original ON original.run_id=accepted.run_id
               JOIN duty_run claimed ON claimed.duty_id=accepted.duty_id
                 AND claimed.grant_id=original.grant_id
                 AND claimed.grant_scope=original.grant_scope
                 AND claimed.purpose=original.purpose
               WHERE accepted.duty_id=? AND claimed.run_id=? AND claimed.grant_id=?""",
            (claim.duty_id, claim.run_id, claim.grant_id),
        ).fetchone()
        is not None
    )


def invoke_onboarding(
    writer: WriterLease, claim: DutyClaim, name: str, arguments: dict
) -> dict:
    if name == "read_first_duty" and arguments == {}:
        return read_first_duty(writer, claim)
    if (
        name == "accept_first_duty"
        and isinstance(arguments, dict)
        and set(arguments) == {"revision", "rationale"}
    ):
        return {"duty_id": accept_first_duty(writer, claim, **arguments)}
    raise DutyRefused("Tool is unavailable for this onboarding duty")
