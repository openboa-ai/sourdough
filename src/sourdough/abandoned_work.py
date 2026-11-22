"""Fence a departed claimant without discarding committed decisions or effects."""

from contextlib import closing

from .company import _connect, _identity
from .duty_run import DutyRefused
from .writer_lease import WriterLease


def requeue_uncommitted_work(writer: WriterLease) -> tuple[str, ...]:
    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        writer.verify()
        if _identity(connection) != writer.company_id:
            raise DutyRefused("Company identity mismatch during work recovery")
        tables = {
            table
            for (table,) in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        committed = (
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
        recovered = connection.execute(
            f"""SELECT d.id, run.run_id,
                       CASE WHEN founding.due_work_id IS NOT NULL THEN 'due' ELSE 'scheduled' END
                FROM duty d JOIN duty_run run ON run.duty_id = d.id AND run.is_current = 1
                JOIN founder actor ON actor.id = d.founder_id AND actor.company_id = ?
                LEFT JOIN founding_receipt founding ON founding.due_work_id = d.id
                LEFT JOIN work_follow_up follow_up ON follow_up.duty_id = d.id
                WHERE d.state = 'running'
                  AND (founding.due_work_id IS NOT NULL OR follow_up.duty_id IS NOT NULL)
                {exclusions}
                ORDER BY d.rowid""",
            (writer.company_id,),
        ).fetchall()
        for duty_id, old_run, next_state in recovered:
            connection.execute(
                "UPDATE duty_run SET is_current = 0 WHERE run_id = ?", (old_run,)
            )
            connection.execute(
                "UPDATE duty SET state = ? WHERE id = ?", (next_state, duty_id)
            )
    return tuple(old_run for _, old_run, _ in recovered)
