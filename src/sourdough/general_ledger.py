"""Company accounting storage for already-authorized, evidenced postings."""

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from uuid import uuid4

from .company import _connect, _identity
from .writer_lease import WriterLease


class JournalRefused(Exception):
    """The accounting source, balance, or correction violates the posting contract."""


def _label(value: str) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256 and bool(value.strip())


@dataclass(frozen=True)
class JournalLine:
    account: str
    amount_minor: int

    def __post_init__(self) -> None:
        if (
            not _label(self.account)
            or type(self.amount_minor) is not int
            or not 0 < abs(self.amount_minor) < 2**63
        ):
            raise JournalRefused(
                "Journal line requires an account and signed integer units"
            )


@dataclass(frozen=True)
class JournalEntry:
    company_id: str
    source: str
    source_event: str
    evidence_revision: str
    effective_date: date
    currency: str
    mapping_revision: str
    lines: tuple[JournalLine, ...]
    reverses: str | None = None
    replaces: str | None = None

    def __post_init__(self) -> None:
        if (
            not all(
                _label(label)
                for label in (
                    self.company_id,
                    self.source,
                    self.source_event,
                    self.evidence_revision,
                    self.mapping_revision,
                )
            )
            or type(self.effective_date) is not date
            or not isinstance(self.currency, str)
            or re.fullmatch(r"[A-Z][A-Z0-9]{1,11}", self.currency) is None
            or any(
                reference is not None and not _label(reference)
                for reference in (self.reverses, self.replaces)
            )
            or (self.reverses is not None and self.replaces is not None)
        ):
            raise JournalRefused(
                "Journal entry requires an exact company and source scope"
            )
        if (
            not isinstance(self.lines, tuple)
            or not 2 <= len(self.lines) <= 100
            or not all(isinstance(line, JournalLine) for line in self.lines)
            or sum(line.amount_minor for line in self.lines) != 0
        ):
            raise JournalRefused("Journal entry must balance in the declared currency")


def _lines(database: sqlite3.Connection, entry_id: str) -> tuple[JournalLine, ...]:
    return tuple(
        JournalLine(*row)
        for row in database.execute(
            """SELECT account, amount_minor FROM journal_line
               WHERE entry_id=? ORDER BY ordinal""",
            (entry_id,),
        )
    )


def post_journal(writer: WriterLease, entry: JournalEntry) -> str:
    """Persist an internal accounting entry; a writer lease does not authorize a source."""
    writer.verify_for(writer.store, entry.company_id)
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        if _identity(database) != entry.company_id:
            raise JournalRefused("Journal entry belongs to another company")
        database.execute(
            """CREATE TABLE IF NOT EXISTS journal_entry (
                   id TEXT PRIMARY KEY NOT NULL,
                   source TEXT NOT NULL, source_event TEXT NOT NULL,
                   evidence_revision TEXT NOT NULL, effective_date TEXT NOT NULL,
                   currency TEXT NOT NULL, mapping_revision TEXT NOT NULL,
                   reverses TEXT UNIQUE REFERENCES journal_entry(id),
                   replaces TEXT UNIQUE REFERENCES journal_entry(id),
                   UNIQUE(source,source_event)
               )"""
        )
        database.execute(
            """CREATE TABLE IF NOT EXISTS journal_line (
                   entry_id TEXT NOT NULL REFERENCES journal_entry(id), ordinal INTEGER NOT NULL,
                   account TEXT NOT NULL, amount_minor INTEGER NOT NULL,
                   PRIMARY KEY(entry_id,ordinal)
               )"""
        )
        terms = (
            entry.evidence_revision,
            entry.effective_date.isoformat(),
            entry.currency,
            entry.mapping_revision,
            entry.reverses,
            entry.replaces,
        )
        existing = database.execute(
            """SELECT id,evidence_revision,effective_date,currency,mapping_revision,reverses,replaces
               FROM journal_entry WHERE source=? AND source_event=?""",
            (entry.source, entry.source_event),
        ).fetchone()
        if existing is not None:
            if existing[1:] != terms or _lines(database, existing[0]) != entry.lines:
                raise JournalRefused(
                    "Journal source event conflicts with its original posting"
                )
            writer.verify()
            return existing[0]

        if entry.reverses is not None:
            original = database.execute(
                """SELECT currency,mapping_revision,effective_date,reverses
                   FROM journal_entry WHERE id=?""",
                (entry.reverses,),
            ).fetchone()
            applied = database.execute(
                "SELECT 1 FROM journal_entry WHERE reverses=?", (entry.reverses,)
            ).fetchone()
            if (
                original is None
                or original[:2] != (entry.currency, entry.mapping_revision)
                or original[2] > terms[1]
                or original[3] is not None
                or applied is not None
                or entry.lines
                != tuple(
                    JournalLine(line.account, -line.amount_minor)
                    for line in _lines(database, entry.reverses)
                )
            ):
                raise JournalRefused(
                    "Reversal must exactly negate one available original entry"
                )
        elif entry.replaces is not None:
            reversal = database.execute(
                "SELECT currency,effective_date FROM journal_entry WHERE reverses=?",
                (entry.replaces,),
            ).fetchone()
            applied = database.execute(
                "SELECT 1 FROM journal_entry WHERE replaces=?", (entry.replaces,)
            ).fetchone()
            if (
                reversal is None
                or reversal[0] != entry.currency
                or reversal[1] > terms[1]
                or applied is not None
            ):
                raise JournalRefused(
                    "Replacement requires one effective original reversal"
                )

        identifier = uuid4().hex
        database.execute(
            "INSERT INTO journal_entry VALUES (?,?,?,?,?,?,?,?,?)",
            (identifier, entry.source, entry.source_event, *terms),
        )
        database.executemany(
            "INSERT INTO journal_line VALUES (?,?,?,?)",
            (
                (identifier, ordinal, line.account, line.amount_minor)
                for ordinal, line in enumerate(entry.lines)
            ),
        )
        writer.verify()
    return identifier
