"""Retain and reopen source-bound paper statements without rewriting history."""

import json
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from sqlite3 import Connection
from time import time_ns

from .company import _connect, _identity
from .paper_window import WindowRefused, read_window
from .paper_window_statement import read_paper_statement, reassess_retained_costs
from .writer_lease import WriterLease


def _identifier(value: str) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128 and bool(value.strip())


@dataclass(frozen=True)
class StatementRequest:
    request_id: str
    window_id: str
    accounting_close_id: str | None = None
    supersedes: str | None = None

    def __post_init__(self) -> None:
        if (
            not _identifier(self.request_id)
            or not _identifier(self.window_id)
            or any(
                value is not None and not _identifier(value)
                for value in (self.accounting_close_id, self.supersedes)
            )
            or self.supersedes == self.request_id
        ):
            raise WindowRefused(
                "Statement needs an identified window and close request"
            )


@dataclass(frozen=True)
class RetainedStatement:
    request: StatementRequest
    revision: str
    payload: str

    def as_receipt(self) -> dict:
        return json.loads(self.payload) | {"revision": self.revision}


def _revision(payload: str) -> str:
    return sha256(payload.encode()).hexdigest()


def _read(
    database: Connection, company_id: str, request_id: str
) -> RetainedStatement | None:
    if not database.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='paper_statement'"
    ).fetchone():
        return None
    row = database.execute(
        """SELECT window_id,supersedes,payload,revision FROM paper_statement
           WHERE request_id=?""",
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    window_id, supersedes, payload, revision = row
    if _revision(payload) != revision:
        raise WindowRefused("Stored statement no longer matches its receipt")
    fields = json.loads(payload)
    request = StatementRequest(**fields["request"])
    if fields["company_id"] != company_id or (
        request.request_id,
        request.window_id,
        request.supersedes,
    ) != (request_id, window_id, supersedes):
        raise WindowRefused("Stored statement does not match its original scope")
    return RetainedStatement(request, revision, payload)


def read_retained_statement(
    store: Path, company_id: str, request_id: str
) -> RetainedStatement | None:
    if not _identifier(request_id):
        raise WindowRefused("Expected an exact statement request ID")
    with closing(_connect(store, "ro")) as database:
        database.execute("BEGIN")
        if _identity(database) != company_id:
            raise WindowRefused("Statement belongs to another company")
        return _read(database, company_id, request_id)


def _match(request: StatementRequest, previous: RetainedStatement) -> RetainedStatement:
    if request != previous.request:
        raise WindowRefused("Statement request conflicts with its original receipt")
    return previous


def retain_paper_statement(
    writer: WriterLease, directory: Path, request: StatementRequest
) -> RetainedStatement:
    writer.verify()
    store, company_id = writer.store, writer.company_id
    saved = read_retained_statement(store, company_id, request.request_id)
    if saved is not None:
        return _match(request, saved)
    window = read_window(store, company_id, request.window_id)
    assessed_ns = time_ns()
    if window is None or assessed_ns <= window.plan.closes_ns:
        raise WindowRefused("Wait for the registered window to close")
    report = None
    if request.supersedes is not None:
        predecessor = read_retained_statement(store, company_id, request.supersedes)
        if predecessor is None or predecessor.request.window_id != request.window_id:
            raise WindowRefused(
                "Statement must supersede the latest report for the window"
            )
        report = reassess_retained_costs(
            store,
            window,
            predecessor.as_receipt()["statement"],
            request.accounting_close_id,
        )
    if report is None:
        report = read_paper_statement(
            store,
            company_id,
            directory,
            request.window_id,
            close_id=request.accounting_close_id,
        ).as_report()
    if "window-still-open" in report["trading"]["missing"]:
        raise WindowRefused("Statement observation preceded the closing cutoff")
    payload = json.dumps(
        {
            "company_id": company_id,
            "request": asdict(request),
            "assessed_ns": assessed_ns,
            "statement": report,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    revision = _revision(payload)
    with closing(_connect(store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        writer.verify()
        if _identity(database) != company_id:
            raise WindowRefused("Statement belongs to another company")
        database.execute(
            """CREATE TABLE IF NOT EXISTS paper_statement (
                request_id TEXT PRIMARY KEY NOT NULL, window_id TEXT NOT NULL,
                supersedes TEXT UNIQUE REFERENCES paper_statement(request_id),
                payload TEXT NOT NULL, revision TEXT NOT NULL UNIQUE
            )"""
        )
        saved = _read(database, company_id, request.request_id)
        if saved is not None:
            return _match(request, saved)
        latest = database.execute(
            """SELECT prior.request_id FROM paper_statement prior
               WHERE prior.window_id=? AND NOT EXISTS (
                   SELECT 1 FROM paper_statement current WHERE current.supersedes=prior.request_id
               )""",
            (request.window_id,),
        ).fetchall()
        if (latest and latest != [(request.supersedes,)]) or (
            not latest and request.supersedes is not None
        ):
            raise WindowRefused(
                "Statement must supersede the latest report for the window"
            )
        database.execute(
            "INSERT INTO paper_statement VALUES (?,?,?,?,?)",
            (
                request.request_id,
                request.window_id,
                request.supersedes,
                payload,
                revision,
            ),
        )
        writer.verify()
    return RetainedStatement(request, revision, payload)
