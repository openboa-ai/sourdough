"""Recover original paper effects without authorizing a new send."""

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection

from .company import _connect, _identity
from .duty_run import DutyClaim
from .paper_account import reopen_binding
from .paper_books import (
    FILLED_QUANTITY,
    Posting,
    PostingRefused,
    post_confirmed_fill,
    read_booked_fill,
)
from .paper_execution import PaperExecution
from .writer_lease import WriterLease


MAX_LOOKUP_ATTEMPTS = 6


@dataclass(frozen=True)
class ReconciledOrder:
    original_claim: DutyClaim
    posting: Posting
    sources: tuple[str, ...]


@dataclass(frozen=True)
class PaperReconciliation:
    recovered: tuple[ReconciledOrder, ...]
    unresolved: int
    next_query_at: int | None


def _has_table(connection: Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def _open_recovery(
    store: Path, company_id: str, account_id: str, now: int, limit: int
) -> list[tuple]:
    with closing(_connect(store, "rw")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if _identity(db) != company_id:
            raise PostingRefused("Company identity mismatch")
        if not _has_table(db, "paper_order"):
            return []
        db.execute(
            """CREATE TABLE IF NOT EXISTS paper_reconciliation (
                order_id TEXT PRIMARY KEY NOT NULL REFERENCES paper_order(order_id),
                attempts INTEGER NOT NULL,
                next_query_at INTEGER,
                fill_id TEXT,
                sources TEXT NOT NULL
            )"""
        )
        return db.execute(
            f"""SELECT o.order_id, f.id, e.grant_id, d.id, e.run_id,
                       f.execution_profile, COALESCE(j.attempts, 0)
                FROM paper_order o
                JOIN duty_run e ON e.run_id = o.run_id
                JOIN duty d ON d.id = e.duty_id
                JOIN agent f ON f.id = d.agent_id
                LEFT JOIN paper_reconciliation j ON j.order_id = o.order_id
                WHERE o.account_id = ? AND f.company_id = ?
                  AND ({_missing_fill(db)} OR d.state = 'running')
                  AND (j.order_id IS NULL OR j.fill_id IS NOT NULL OR j.next_query_at <= ?
                       OR NOT ({_missing_fill(db)}))
                ORDER BY COALESCE(j.next_query_at, 0), o.order_id LIMIT ?""",
            (account_id, company_id, now, limit),
        ).fetchall()


def _missing_fill(connection: Connection) -> str:
    if _has_table(connection, "paper_fill"):
        return f"{FILLED_QUANTITY} != o.quantity"
    return "1"


def _record_lookup(
    store: Path,
    company_id: str,
    order_id: str,
    now: int,
    attempts: int,
    posting: Posting | None,
    sources: tuple[str, ...],
) -> None:
    next_query = None
    if posting is None and attempts < MAX_LOOKUP_ATTEMPTS:
        next_query = now + min(60, 2**attempts)
    with closing(_connect(store, "rw")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if _identity(db) != company_id:
            raise PostingRefused("Company identity mismatch")
        db.execute(
            """INSERT INTO paper_reconciliation VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(order_id) DO UPDATE SET
                   attempts = excluded.attempts, next_query_at = excluded.next_query_at,
                   fill_id = excluded.fill_id,
                   sources = CASE WHEN paper_reconciliation.fill_id IS NOT NULL
                                  AND excluded.sources = 'book'
                                  THEN paper_reconciliation.sources ELSE excluded.sources END
               WHERE paper_reconciliation.fill_id IS NULL OR excluded.fill_id IS NOT NULL""",
            (
                order_id,
                attempts,
                next_query,
                posting.fill_id if posting else None,
                ",".join(sources),
            ),
        )


def reconcile_paper_orders(
    store: Path,
    company_id: str,
    venue: PaperExecution,
    *,
    now: int,
    lookup_limit: int = 16,
    writer: WriterLease | None = None,
) -> PaperReconciliation:
    if (
        type(now) is not int
        or not 0 <= now <= 2**63 - 61
        or not 1 <= lookup_limit <= 64
    ):
        raise PostingRefused("Invalid paper reconciliation bounds")
    binding = reopen_binding(store, company_id)
    account = binding.account
    if writer is not None:
        writer.verify_for(store, company_id)
    candidates = _open_recovery(
        store, company_id, account.account_id, now, lookup_limit
    )
    recovered = []
    for (
        order_id,
        founder_id,
        grant_id,
        duty_id,
        run_id,
        profile,
        attempts,
    ) in candidates:
        posting = read_booked_fill(store, company_id, order_id)
        if posting is not None:
            sources = ("book",)
        else:
            if venue.observe_account(account.account_id) != account:
                raise PostingRefused("Recovery requires the original paper destination")
            observed = []
            for source, account_sync in (("history", False), ("account_sync", True)):
                try:
                    confirmed = post_confirmed_fill(
                        store,
                        company_id,
                        order_id,
                        venue,
                        account_sync=account_sync,
                        writer=writer,
                    )
                except TimeoutError:
                    continue
                if confirmed is not None:
                    observed.append((source, confirmed))
            posting = observed[0][1] if observed else None
            sources = tuple(source for source, _ in observed)
            attempts += 1
        if writer is not None:
            writer.verify_for(store, company_id)
        _record_lookup(store, company_id, order_id, now, attempts, posting, sources)
        if posting is not None:
            claim = DutyClaim(
                company_id, founder_id, grant_id, duty_id, run_id, profile
            )
            recovered.append(ReconciledOrder(claim, posting, sources))
    with closing(_connect(store, "ro")) as db:
        if not _has_table(db, "paper_order"):
            return PaperReconciliation((), 0, None)
        unresolved, next_query = db.execute(
            f"""SELECT count(*), min(CASE WHEN j.order_id IS NULL THEN ? ELSE j.next_query_at END)
                FROM paper_order o
                LEFT JOIN paper_reconciliation j ON j.order_id = o.order_id
                WHERE o.account_id = ? AND {_missing_fill(db)}""",
            (now, account.account_id),
        ).fetchone()
    return PaperReconciliation(tuple(recovered), unresolved, next_query)
