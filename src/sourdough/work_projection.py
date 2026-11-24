"""Build a read-only company timeline for the local board boundary."""

import json
import os
import stat
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from sqlite3 import Connection
from time import time_ns

from .company import FoundingRefused, _identity
from .paper_risk import PaperRiskRefused, effective_paper_risk
from .storage.db import open_reader, read_tx
from .storage.schema import is_domain_installed


WORK_TIMELINE_REVISION = "work-timeline-v1"


def _rows(connection: Connection, sql: str) -> list[dict]:
    cursor = connection.execute(sql)
    labels = [column[0] for column in cursor.description]
    return [dict(zip(labels, row, strict=True)) for row in cursor]


def read_local_board_timeline(
    store: Path,
    company_id: str,
    *,
    contract_revision: str = WORK_TIMELINE_REVISION,
) -> dict:
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
        has_paper = is_domain_installed(connection, "paper")
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
        decisions = _decisions(connection, has_paper)
        for duty in duties:
            duty["decision"] = decisions.get(duty["duty_id"])
        body = {
            "company_id": company_id,
            "access": "local_store_owner",
            "duties": duties,
            "paper": _paper_projection(connection, has_paper),
            "risk_control": _risk_control(connection, company_id, has_paper),
        }
        current = store.stat(follow_symlinks=False)
        if (initial.st_dev, initial.st_ino, initial.st_uid) != (
            current.st_dev,
            current.st_ino,
            current.st_uid,
        ):
            raise FoundingRefused("Company store changed while inspecting its timeline")
    revision = sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return body | {
        "contract_revision": WORK_TIMELINE_REVISION,
        "revision": revision,
        "as_of_ns": as_of_ns,
    }


def _risk_control(
    connection: Connection, company_id: str, has_paper: bool
) -> dict | None:
    has_risk_receipt = (
        connection.execute(
            """SELECT 1 FROM command_receipt
               WHERE company_id = ?
                 AND command_type IN ('suspend_new_risk', 'resume_new_risk')
               LIMIT 1""",
            (company_id,),
        ).fetchone()
        is not None
    )
    if not has_paper and not has_risk_receipt:
        return None
    has_paper_binding = (
        has_paper
        and connection.execute("SELECT 1 FROM paper_binding LIMIT 1").fetchone()
        is not None
    )
    has_risk_state = (
        has_paper
        and connection.execute(
            "SELECT 1 FROM paper_risk_state WHERE company_id = ? LIMIT 1",
            (company_id,),
        ).fetchone()
        is not None
    )
    if not (has_paper_binding or has_risk_state or has_risk_receipt):
        return None
    try:
        current = effective_paper_risk(connection)
    except PaperRiskRefused:
        return {
            "state": "unavailable",
            "effective_revision": None,
            "last_applied": None,
        }
    applied = current.applied
    return {
        "state": "suspended" if current.suspended else "not_suspended",
        "effective_revision": current.revision,
        "last_applied": (
            {
                "request_id": applied.request_id,
                "operation": applied.operation,
                "recorded_at_ns": applied.recorded_at_ns,
            }
            if applied is not None
            else None
        ),
    }


def _decisions(connection: Connection, has_paper: bool) -> dict:
    decisions = {}
    if has_paper:
        rows = _rows(
            connection,
            """
            SELECT d.duty_id,d.decision_id,d.finding_id,d.decided_at,
                   f.revision AS finding_revision, d.content, s.inputs
            FROM paper_research_decision d
            JOIN paper_research_finding f ON f.finding_id=d.finding_id
            JOIN paper_research_snapshot s ON s.run_id=d.run_id
        """,
        )
        for row in rows:
            row["known_at"] = json.loads(row.pop("inputs"))["cut_at"]
            proposal = json.loads(row.pop("content"))
            row.update(action=proposal["action"], rationale=proposal["rationale"])
            decisions[row["duty_id"]] = row
    for row in _rows(
        connection,
        """
        SELECT duty_id,call_id AS decision_id,result_revision,action,rationale,decided_at
        FROM work_review_decision
    """,
    ):
        decisions[row["duty_id"]] = row
    if has_paper:
        for row in _rows(
            connection,
            """
            SELECT duty_id,decision_id,result_revision,action,rationale,decided_at
            FROM paper_decision
        """,
        ):
            decisions[row["duty_id"]] = row
    return decisions


def _paper_projection(connection: Connection, has_paper: bool) -> dict | None:
    if not has_paper:
        return None
    trades = _rows(
        connection,
        """
        SELECT o.order_id,o.run_id,o.action_id,o.account_id,o.instrument_rules_digest,
               o.quantity AS ordered_quantity,o.reserved_minor_units,o.limit_minor_units,
               (SELECT latest.fill_id FROM paper_fill latest WHERE order_id=o.order_id
                ORDER BY latest.rowid DESC LIMIT 1) AS fill_id,
               SUM(p.quantity) AS quantity,
               CASE WHEN COUNT(p.fill_id)=1 THEN MAX(p.price_minor_units) END AS price_minor_units,
               SUM(f.fee_minor_units) AS fee_minor_units,
               CASE WHEN c.released=1 THEN c.outcome END AS terminal
        FROM paper_order o
        LEFT JOIN paper_fill p ON o.order_id=p.order_id
        LEFT JOIN paper_fill_fee f ON f.fill_id=p.fill_id
        LEFT JOIN paper_cancellation c ON c.order_id=o.order_id
        GROUP BY o.order_id ORDER BY o.rowid
    """,
    )
    if not trades:
        return None
    for order in trades:
        order["remaining_quantity"] = order["ordered_quantity"] - (
            order["quantity"] or 0
        )
        limit = order.pop("limit_minor_units")
        order["status"] = order.pop("terminal") or (
            "filled" if order["remaining_quantity"] == 0 else "unresolved"
        )
        order["open_reservation_minor_units"] = (
            order["remaining_quantity"] * limit
            if order["status"] == "unresolved"
            else 0
        )
    return {
        "orders": trades,
        "unresolved_orders": sum(record["status"] == "unresolved" for record in trades),
        "booked_position": sum(
            record["quantity"] for record in trades if record["quantity"] is not None
        ),
    }
