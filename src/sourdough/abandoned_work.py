"""Fence a departed claimant without discarding committed decisions or effects."""

from contextlib import closing
from sqlite3 import Connection

from .company import _connect, _identity
from .duty_run import ASSESSMENT_REVIEW, DutyRefused
from .writer_lease import WriterLease


def _no_committed_result(tables: set[str]) -> str:
    committed = (
        "assessment_publication",
        "paper_research_decision",
        "paper_decision",
        "work_review_decision",
    )
    exclusions = " ".join(
        f"AND NOT EXISTS (SELECT 1 FROM {table} known WHERE known.duty_id = d.id)"
        for table in committed
        if table in tables
    )
    if "paper_order" in tables:
        exclusions += """
            AND NOT EXISTS (
                SELECT 1 FROM paper_order o JOIN duty_run original ON original.run_id = o.run_id
                WHERE original.duty_id = d.id
            )"""
    return exclusions


def _tables(connection: Connection) -> set[str]:
    return {
        table
        for (table,) in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        )
    }


def has_uncommitted_work(connection: Connection, duty_id: str) -> bool:
    return (
        connection.execute(
            f"""SELECT 1 FROM duty d WHERE d.id=? AND d.state='running'
            {_no_committed_result(_tables(connection))}""",
            (duty_id,),
        ).fetchone()
        is not None
    )


def requeue_uncommitted_work(
    writer: WriterLease, *, purpose: str | None = None, hired_only: bool = False
) -> tuple[str, ...]:
    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        writer.verify()
        if _identity(connection) != writer.company_id:
            raise DutyRefused("Company identity mismatch during work recovery")
        tables = _tables(connection)
        if hired_only and "hire_receipt" not in tables:
            return ()
        ownership = (
            "JOIN hire_receipt hire ON hire.agent_id = actor.id" if hired_only else ""
        )
        recovered = connection.execute(
            f"""SELECT d.id, run.run_id,
                       CASE WHEN founding.due_work_id IS NOT NULL THEN 'due' ELSE 'scheduled' END
                FROM duty d JOIN duty_run run ON run.duty_id = d.id AND run.is_current = 1
                JOIN agent actor ON actor.id = d.agent_id AND actor.company_id = ?
                {ownership}
                LEFT JOIN founding_receipt founding ON founding.due_work_id = d.id
                LEFT JOIN work_follow_up follow_up ON follow_up.duty_id = d.id
                WHERE (d.state = 'running' OR (? AND d.state = 'incomplete'))
                  AND (? IS NULL OR run.purpose = ?)
                  AND (founding.due_work_id IS NOT NULL OR follow_up.duty_id IS NOT NULL)
                {_no_committed_result(tables)}
                ORDER BY d.rowid""",
            (writer.company_id, purpose == ASSESSMENT_REVIEW, purpose, purpose),
        ).fetchall()
        from .runtime.work_kernel import sync_duty_terminal

        for duty_id, old_run, next_state in recovered:
            connection.execute(
                "UPDATE duty_run SET is_current = 0 WHERE run_id = ?", (old_run,)
            )
            connection.execute(
                "UPDATE duty SET state = ? WHERE id = ?", (next_state, duty_id)
            )
            sync_duty_terminal(
                connection, duty_id, item_state=next_state, run_status="fenced"
            )
    return tuple(old_run for _, old_run, _ in recovered)
