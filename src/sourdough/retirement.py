"""Permanently archive an Agent without changing admitted effects."""

import json
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from sqlite3 import Connection

from .company import _connect
from .duty_run import DutyRefused, employment_cutoff
from .runtime.work_kernel import retire_agent_work
from .writer_lease import WriterLease


TERMINATION_SCOPE = "retire-agent"


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
        prior = connection.execute(
            """SELECT cutoff,agent_id,grant_id,employment_revision,reason
               FROM agent_archive WHERE manager_id=? AND request_id=?""",
            (manager_id, request.request_id),
        ).fetchone()
        if prior is not None:
            if prior[1:] != (
                request.agent_id,
                grant_id,
                request.employment_revision,
                request.reason,
            ):
                raise DutyRefused("Retirement conflicts with the original request")
            return Cutoff(*prior[:2])
        founder = connection.execute(
            "SELECT founder_id FROM founding_receipt WHERE company_id=?", (company_id,)
        ).fetchone()
        if founder == (request.agent_id,) or request.agent_id == manager_id:
            raise DutyRefused("Cannot archive the founding owner or the acting manager")
        if (
            _active_revision(connection, company_id, request.agent_id)
            != request.employment_revision
        ):
            raise DutyRefused("Employment revision changed before retirement")
        previous = connection.execute(
            "SELECT scope FROM standing_grant WHERE agent_id=?", (request.agent_id,)
        ).fetchone()
        if previous is None:
            raise DutyRefused("Agent has no employment grant")
        (sequence,) = connection.execute(
            """INSERT INTO agent_archive (
                   company_id,agent_id,manager_id,grant_id,request_id,
                   employment_revision,reason,previous_scope
               ) VALUES (?,?,?,?,?,?,?,?) RETURNING cutoff""",
            (
                company_id,
                request.agent_id,
                manager_id,
                grant_id,
                request.request_id,
                request.employment_revision,
                request.reason,
                previous[0],
            ),
        ).fetchone()
        connection.execute(
            """UPDATE duty_run SET is_current=0 WHERE is_current=1
               AND duty_id IN (SELECT id FROM duty WHERE agent_id=?)""",
            (request.agent_id,),
        )
        connection.execute(
            "UPDATE duty SET state='archived' WHERE agent_id=? AND state IN ('due','scheduled','running')",
            (request.agent_id,),
        )
        connection.execute(
            "UPDATE standing_grant SET scope='revoked' WHERE agent_id=?",
            (request.agent_id,),
        )
        retire_agent_work(
            connection,
            company_id=company_id,
            agent_id=request.agent_id,
            actor_id=manager_id,
        )
        writer.verify()
        return Cutoff(sequence, request.agent_id)
