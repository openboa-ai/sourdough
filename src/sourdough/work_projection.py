"""Build a read-only company timeline for the local board boundary."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Sequence
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from sqlite3 import Connection
from time import time_ns
from typing import Any

from .company import FoundingRefused, _identity
from .runtime.domains import DomainPlugin
from .storage.db import open_reader, read_tx
from .storage.kernel import max_event_seq


WORK_TIMELINE_REVISION = "work-timeline-v1"


def _rows(connection: Connection, sql: str) -> list[dict]:
    cursor = connection.execute(sql)
    labels = [column[0] for column in cursor.description]
    return [dict(zip(labels, row, strict=True)) for row in cursor]


def read_local_board_timeline_with_seq(
    store: Path,
    company_id: str,
    *,
    contract_revision: str = WORK_TIMELINE_REVISION,
    domain_plugins: Sequence[DomainPlugin] = (),
) -> tuple[dict, int]:
    """Read the local board timeline and matching committed event_seq in one snapshot."""
    if contract_revision != WORK_TIMELINE_REVISION:
        raise FoundingRefused("Unsupported work timeline contract")
    initial = store.stat(follow_symlinks=False)
    if not stat.S_ISREG(initial.st_mode) or initial.st_uid != os.geteuid():
        raise FoundingRefused(
            "Only the local company store owner can inspect this timeline"
        )

    with closing(open_reader(store)) as connection, read_tx(connection):
        if _identity(connection) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(connection, company_id)
        duties = _rows(
            connection,
            """
            SELECT d.id AS duty_id, d.agent_id AS owner_id, d.state,
                   wake.due_at AS due_at,
                   active.run_id AS run_id
            FROM duty d
            LEFT JOIN work_follow_up wake ON wake.duty_id = d.id
            LEFT JOIN duty_run active ON active.duty_id = d.id AND active.is_current = 1
            ORDER BY d.rowid
        """,
        )
        decisions = _decisions(connection)
        domains: dict[str, Any] = {}
        for plugin in domain_plugins:
            contrib = plugin.contribute_work_timeline(connection, company_id)
            if contrib is None:
                continue
            if contrib.decisions_by_duty_id:
                decisions.update(contrib.decisions_by_duty_id)
            if contrib.domain_payload is not None:
                domains[plugin.domain_name] = dict(contrib.domain_payload)
        for duty in duties:
            duty["decision"] = decisions.get(duty["duty_id"])
        body = {
            "company_id": company_id,
            "access": "local_store_owner",
            "duties": duties,
            "domains": domains,
        }
        current = store.stat(follow_symlinks=False)
        if (initial.st_dev, initial.st_ino, initial.st_uid) != (
            current.st_dev,
            current.st_ino,
            current.st_uid,
        ):
            raise FoundingRefused("Company store changed while inspecting its timeline")
    revision = sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return (
        body
        | {
            "contract_revision": WORK_TIMELINE_REVISION,
            "revision": revision,
            "as_of_ns": as_of_ns,
        },
        event_seq,
    )


def read_local_board_timeline(
    store: Path,
    company_id: str,
    *,
    contract_revision: str = WORK_TIMELINE_REVISION,
    domain_plugins: Sequence[DomainPlugin] = (),
) -> dict:
    timeline, _ = read_local_board_timeline_with_seq(
        store,
        company_id,
        contract_revision=contract_revision,
        domain_plugins=domain_plugins,
    )
    return timeline


def _decisions(connection: Connection) -> dict:
    decisions = {}
    for row in _rows(
        connection,
        """
        SELECT duty_id,call_id AS decision_id,result_revision,action,rationale,decided_at
        FROM work_review_decision
    """,
    ):
        decisions[row["duty_id"]] = row
    return decisions
