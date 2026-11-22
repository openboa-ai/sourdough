"""Prepare the hired Agent's desk and bind acceptance to its first duty."""

import json
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from sqlite3 import Connection
from uuid import uuid4

from .company import _connect
from .duty_run import DutyClaim, DutyRefused, resolve_run, start_run
from .writer_lease import WriterLease


MAX_PROVISIONING_ATTEMPTS = 3
PROVISIONING_RETRY_SECONDS = 60
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
) -> tuple[str, str]:
    row = connection.execute(
        """SELECT a.execution_profile, g.id FROM hire_receipt h
           JOIN agent a ON a.id = h.agent_id AND a.company_id = ?
           JOIN standing_grant g ON g.id = h.grant_id AND g.agent_id = a.id
           WHERE a.id = ? AND g.scope = json_extract(h.terms, '$.scope')
             AND a.execution_profile = json_extract(h.terms, '$.execution_profile')""",
        (company_id, agent_id),
    ).fetchone()
    if row is None:
        raise DutyRefused("No current hire matches the committed authority")
    return row


def _status(connection: Connection, agent_id: str) -> OnboardingStatus:
    return OnboardingStatus(
        *connection.execute(
            """SELECT state, first_duty_id, retry_at, reason
               FROM hire_onboarding WHERE agent_id = ?""",
            (agent_id,),
        ).fetchone()
    )


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
            profile, _ = _active_hire(connection, writer.company_id, agent_id)
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
        available = ready(profile)
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
            if not available:
                connection.execute(
                    """UPDATE hire_onboarding SET state = 'blocked', attempts = ?,
                           retry_at = ?, reason = 'runtime_unavailable' WHERE agent_id = ?""",
                    (
                        attempts + 1,
                        now + PROVISIONING_RETRY_SECONDS
                        if attempts + 1 < MAX_PROVISIONING_ATTEMPTS
                        else None,
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
    writer: WriterLease, agent_id: str, execution_profile: str
) -> DutyClaim | None:
    writer.verify()
    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if not connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name = 'hire_onboarding' AND type = 'table'"
        ).fetchone():
            return None
        profile, grant_id = _active_hire(connection, writer.company_id, agent_id)
        row = connection.execute(
            """SELECT d.id, o.purpose, g.scope
               FROM hire_onboarding o
               JOIN duty d ON d.id = o.first_duty_id AND d.agent_id = o.agent_id
               JOIN private_desk desk ON desk.agent_id = o.agent_id
               JOIN standing_grant g ON g.id = ? AND g.agent_id = d.agent_id
               WHERE o.agent_id = ? AND o.state = 'ready' AND d.state = 'due'""",
            (grant_id, agent_id),
        ).fetchone()
        if row is None or execution_profile != profile:
            return None
        duty_id, purpose, scope = row
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
        """SELECT o.purpose, desk.manifest_revision, c.charter_revision
           FROM hire_onboarding o JOIN private_desk desk ON desk.agent_id = o.agent_id
           JOIN agent a ON a.id = o.agent_id JOIN company c ON c.id = a.company_id
           WHERE o.first_duty_id = ? AND o.agent_id = ? AND o.state IN ('ready', 'accepted')""",
        (claim.duty_id, claim.founder_id),
    ).fetchone()
    if row is None or authority.duty_state != "running":
        raise DutyRefused("This claim has no active first-duty desk")
    purpose, manifest, charter = row
    authority.require_current(purpose)
    snapshot = {
        "duty_id": claim.duty_id,
        "agent_id": claim.founder_id,
        "purpose": purpose,
        "private_desk_revision": manifest,
        "charter_revision": charter,
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
        if prior is not None and prior != acceptance:
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
        return (
            connection.execute(
                "SELECT 1 FROM onboarding_acceptance WHERE duty_id = ? AND run_id = ?",
                (claim.duty_id, claim.run_id),
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
