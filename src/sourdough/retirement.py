"""Commit an Agent-wide cutoff without settling held obligations."""

import json
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from sqlite3 import Connection

from .company import _connect
from .duty_run import DutyRefused, employment_cutoff
from .writer_lease import WriterLease


TERMINATION_SCOPE = "retire-agent"
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS agent_retirement (
        cutoff INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL UNIQUE REFERENCES agent(id),
        manager_id TEXT NOT NULL REFERENCES agent(id), grant_id TEXT NOT NULL REFERENCES standing_grant(id),
        request_id TEXT NOT NULL, employment_revision TEXT NOT NULL, reason TEXT NOT NULL,
        continuity_id TEXT NOT NULL REFERENCES agent(id), state TEXT NOT NULL,
        UNIQUE(manager_id,request_id)
    )""",
    """CREATE TABLE IF NOT EXISTS retirement_grant (
        cutoff INTEGER NOT NULL REFERENCES agent_retirement(cutoff),
        grant_id TEXT NOT NULL REFERENCES standing_grant(id), previous_scope TEXT NOT NULL,
        PRIMARY KEY(cutoff,grant_id)
    )""",
    """CREATE TABLE IF NOT EXISTS retirement_duty (
        cutoff INTEGER NOT NULL REFERENCES agent_retirement(cutoff),
        duty_id TEXT NOT NULL REFERENCES duty(id), previous_state TEXT NOT NULL,
        last_run TEXT REFERENCES duty_run(run_id),
        PRIMARY KEY(cutoff,duty_id)
    )""",
)


def _reference(value: str, maximum: int = 128) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum and bool(value.strip())


@dataclass(frozen=True)
class Retirement:
    request_id: str
    agent_id: str
    employment_revision: str
    reason: str

    def __post_init__(self) -> None:
        if not all(
            _reference(value)
            for value in (self.request_id, self.agent_id, self.employment_revision)
        ) or not _reference(self.reason, 1000):
            raise DutyRefused("Invalid retirement request")


@dataclass(frozen=True)
class Cutoff:
    sequence: int
    agent_id: str
    continuity_id: str


def _admit_manager(
    connection: Connection, company_id: str, manager_id: str, grant_id: str
) -> None:
    row = connection.execute(
        """SELECT g.scope FROM standing_grant g JOIN agent a ON a.id=g.agent_id
           WHERE a.company_id=? AND a.id=? AND g.id=?""",
        (company_id, manager_id, grant_id),
    ).fetchone()
    if row != (TERMINATION_SCOPE,) or employment_cutoff(connection, manager_id):
        raise DutyRefused("Current termination grant is required")


def _active_revision(connection: Connection, company_id: str, agent_id: str) -> str:
    if connection.execute(
        "SELECT execution_profile FROM agent WHERE company_id=? AND id=?",
        (company_id, agent_id),
    ).fetchone() is None or employment_cutoff(connection, agent_id):
        raise DutyRefused("Agent employment is not active")
    grants = connection.execute(
        "SELECT id,scope FROM standing_grant WHERE agent_id=? ORDER BY id", (agent_id,)
    ).fetchall()
    return sha256(json.dumps([company_id, agent_id, grants]).encode()).hexdigest()


def employment_revision(
    writer: WriterLease, manager_id: str, grant_id: str, agent_id: str
) -> str:
    writer.verify_for(writer.store, writer.company_id)
    with closing(_connect(writer.store, "ro")) as connection:
        connection.execute("BEGIN")
        _admit_manager(connection, writer.company_id, manager_id, grant_id)
        return _active_revision(connection, writer.company_id, agent_id)


def retire_agent(
    writer: WriterLease, manager_id: str, grant_id: str, request: Retirement
) -> Cutoff:
    writer.verify_for(writer.store, writer.company_id)
    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        company_id = writer.company_id
        _admit_manager(connection, company_id, manager_id, grant_id)
        for schema in _SCHEMA:
            connection.execute(schema)
        prior = connection.execute(
            """SELECT cutoff,agent_id,continuity_id,grant_id,employment_revision,reason
               FROM agent_retirement WHERE manager_id=? AND request_id=?""",
            (manager_id, request.request_id),
        ).fetchone()
        if prior is not None:
            if (prior[1], *prior[3:]) != (
                request.agent_id,
                grant_id,
                request.employment_revision,
                request.reason,
            ):
                raise DutyRefused("Retirement conflicts with the original request")
            return Cutoff(*prior[:3])
        continuity = connection.execute(
            """SELECT a.id FROM founding_receipt f JOIN standing_grant g ON g.id=f.grant_id
               JOIN agent a ON a.id=g.agent_id WHERE f.company_id=? AND a.company_id=?
                 AND g.scope='manage-company'""",
            (company_id, company_id),
        ).fetchone()
        if (
            continuity is None
            or request.agent_id in (continuity[0], manager_id)
            or employment_cutoff(connection, continuity[0])
        ):
            raise DutyRefused(
                "Retirement requires independent company continuity coverage"
            )
        if (
            _active_revision(connection, company_id, request.agent_id)
            != request.employment_revision
        ):
            raise DutyRefused("Employment revision changed before retirement")
        row = connection.execute(
            """INSERT INTO agent_retirement
               (agent_id,manager_id,grant_id,request_id,employment_revision,reason,continuity_id,state)
               VALUES (?,?,?,?,?,?,?,'retiring') RETURNING cutoff""",
            (
                request.agent_id,
                manager_id,
                grant_id,
                request.request_id,
                request.employment_revision,
                request.reason,
                continuity[0],
            ),
        ).fetchone()
        sequence = row[0]
        connection.execute(
            """INSERT INTO retirement_grant
               SELECT ?,id,scope FROM standing_grant WHERE agent_id=?""",
            (sequence, request.agent_id),
        )
        connection.execute(
            """INSERT INTO retirement_duty
               SELECT ?,d.id,d.state,r.run_id FROM duty d
               LEFT JOIN duty_run r ON r.duty_id=d.id AND r.is_current=1
               WHERE d.agent_id=? AND d.state IN ('due','scheduled','running')""",
            (sequence, request.agent_id),
        )
        connection.execute(
            """UPDATE duty_run SET is_current=0 WHERE is_current=1
               AND duty_id IN (SELECT id FROM duty WHERE agent_id=?)""",
            (request.agent_id,),
        )
        connection.execute(
            "UPDATE duty SET state='held_for_retirement' WHERE id IN (SELECT duty_id FROM retirement_duty WHERE cutoff=?)",
            (sequence,),
        )
        connection.execute(
            "UPDATE standing_grant SET scope='revoked' WHERE agent_id=?",
            (request.agent_id,),
        )
        writer.verify()
        return Cutoff(sequence, request.agent_id, continuity[0])
