"""Reconcile and retain immutable closes for the company's operating accounts."""

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from time import time_ns

from .company import _identity
from .general_ledger import _label, _valid_scale
from .storage.db import open_reader, open_writer, read_tx, write_tx
from .storage.kernel import append_audit_event
from .writer_lease import WriterLease


class CloseRefused(Exception):
    """The close does not identify a valid period or evidence revision."""


@dataclass(frozen=True)
class ExpectedCost:
    source: str
    event_id: str
    expense_account: str
    owner: str
    evidence_revision: str | None = None
    amount_minor: int | None = None

    def __post_init__(self) -> None:
        if not all(_label(value) for value in (*self.key, self.owner)):
            raise CloseRefused("Expected cost must identify its source and owner")
        if (self.evidence_revision is not None or self.amount_minor is not None) and (
            not _label(self.evidence_revision) or type(self.amount_minor) is not int
        ):
            raise CloseRefused("A cost receipt requires a revision and exact amount")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.source, self.event_id, self.expense_account


@dataclass(frozen=True)
class CloseRequest:
    request_id: str
    company_id: str
    from_date: date
    through_date: date
    cash_account: str
    currency: str
    mapping_revision: str
    statement_revision: str
    opening_cash_minor: int
    statement_cash_minor: int
    expense_accounts: tuple[str, ...]
    expected_costs: tuple[ExpectedCost, ...]
    owner: str
    recheck_at_ns: int
    supersedes: str | None = None
    units_per_whole: int | None = None

    def __post_init__(self) -> None:
        if (
            not all(
                _label(value)
                for value in (
                    self.request_id,
                    self.company_id,
                    self.cash_account,
                    self.mapping_revision,
                    self.statement_revision,
                    self.owner,
                )
            )
            or type(self.from_date) is not date
            or type(self.through_date) is not date
            or self.from_date > self.through_date
            or not isinstance(self.currency, str)
            or re.fullmatch(r"[A-Z][A-Z0-9]{1,11}", self.currency) is None
            or not _valid_scale(self.units_per_whole)
            or type(self.opening_cash_minor) is not int
            or type(self.statement_cash_minor) is not int
            or type(self.recheck_at_ns) is not int
            or self.recheck_at_ns <= 0
            or (self.supersedes is not None and not _label(self.supersedes))
        ):
            raise CloseRefused(
                "Close requires an exact scope and independent statement"
            )
        if (
            not isinstance(self.expense_accounts, tuple)
            or not all(_label(account) for account in self.expense_accounts)
            or self.cash_account in self.expense_accounts
            or len(set(self.expense_accounts)) != len(self.expense_accounts)
            or not isinstance(self.expected_costs, tuple)
            or not all(
                isinstance(cost, ExpectedCost)
                and cost.expense_account in self.expense_accounts
                for cost in self.expected_costs
            )
            or len({cost.key for cost in self.expected_costs})
            != len(self.expected_costs)
        ):
            raise CloseRefused("Close requires a unique scoped cost inventory")


@dataclass(frozen=True)
class CloseIssue:
    reason: str
    owner: str
    reference: tuple[str, ...]


@dataclass(frozen=True)
class CloseReceipt:
    request_id: str
    supersedes: str | None
    statement_revision: str
    journal_ids: tuple[str, ...]
    book_cash_minor: int
    cash_difference_minor: int
    verified_cost_minor: int | None
    issues: tuple[CloseIssue, ...]
    recheck_at_ns: int | None

    @property
    def complete(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class ClosedPeriod:
    request: CloseRequest
    receipt: CloseReceipt
    assessed_ns: int | None


def _encoded(value) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=date.isoformat
    )


def _receipt(payload: str) -> CloseReceipt:
    values = json.loads(payload)
    values["journal_ids"] = tuple(values["journal_ids"])
    values["issues"] = tuple(
        CloseIssue(issue["reason"], issue["owner"], tuple(issue["reference"]))
        for issue in values["issues"]
    )
    return CloseReceipt(**values)


def read_period_close(
    store: Path, company_id: str, request_id: str
) -> ClosedPeriod | None:
    if not _label(request_id):
        raise CloseRefused("Expected an exact close request ID")
    with closing(open_reader(store)) as database, read_tx(database):
        if _identity(database) != company_id:
            raise CloseRefused("Period close belongs to another company")
        row = database.execute(
            "SELECT scope,supersedes,request,receipt,assessed_ns FROM accounting_close WHERE request_id=?",
            (request_id,),
        ).fetchone()
    if row is None:
        return None
    stored_scope, stored_supersedes, raw_request, raw_receipt, assessed_ns = row
    fields = json.loads(raw_request)
    fields["from_date"] = date.fromisoformat(fields["from_date"])
    fields["through_date"] = date.fromisoformat(fields["through_date"])
    fields["expense_accounts"] = tuple(fields["expense_accounts"])
    fields["expected_costs"] = tuple(
        ExpectedCost(**cost) for cost in fields["expected_costs"]
    )
    request, receipt = CloseRequest(**fields), _receipt(raw_receipt)
    expected_scope = _encoded(
        (
            request.from_date,
            request.through_date,
            request.cash_account,
            request.currency,
        )
    )
    if (
        (request.company_id, request.request_id) != (company_id, request_id)
        or stored_scope != expected_scope
        or stored_supersedes != request.supersedes
        or (
            request.request_id,
            request.statement_revision,
            request.supersedes,
        )
        != (receipt.request_id, receipt.statement_revision, receipt.supersedes)
    ):
        raise CloseRefused("Close receipt no longer matches its original request")
    return ClosedPeriod(request, receipt, assessed_ns)


def _assess(database: sqlite3.Connection, request: CloseRequest) -> CloseReceipt:
    cash = request.opening_cash_minor
    actual_costs: dict[tuple[str, str, str], tuple[str, int]] = {}
    journal_ids = set()
    mapping_conflicts = set()
    unit_conflicts = set()
    for (
        entry_id,
        source,
        event,
        evidence,
        mapping,
        account,
        amount,
        scale,
    ) in database.execute(
        """SELECT entry.id,entry.source,entry.source_event,entry.evidence_revision,
                  entry.mapping_revision,line.account,line.amount_minor,entry.units_per_whole
           FROM journal_entry entry JOIN journal_line line ON line.entry_id=entry.id
           WHERE entry.currency=? AND entry.effective_date BETWEEN ? AND ?""",
        (
            request.currency,
            request.from_date.isoformat(),
            request.through_date.isoformat(),
        ),
    ):
        if account != request.cash_account and account not in request.expense_accounts:
            continue
        journal_ids.add(entry_id)
        if mapping != request.mapping_revision:
            mapping_conflicts.add(entry_id)
        elif scale != request.units_per_whole:
            unit_conflicts.add(entry_id)
        elif account == request.cash_account:
            cash += amount
        else:
            key = source, event, account
            _, previous = actual_costs.get(key, (evidence, 0))
            actual_costs[key] = evidence, previous + amount

    difference = cash - request.statement_cash_minor
    issues = [
        CloseIssue("chart-mismatch", request.owner, (entry_id,))
        for entry_id in sorted(mapping_conflicts)
    ]
    issues.extend(
        CloseIssue("unit-mismatch", request.owner, (entry_id,))
        for entry_id in sorted(unit_conflicts)
    )
    if difference:
        issues.append(
            CloseIssue("cash-mismatch", request.owner, (request.statement_revision,))
        )
    costs = 0
    for expected in sorted(request.expected_costs, key=lambda cost: cost.key):
        observed = actual_costs.pop(expected.key, None)
        if expected.amount_minor is None:
            reason = "missing-cost-receipt"
        elif observed == (expected.evidence_revision, expected.amount_minor) or (
            observed is None and expected.amount_minor == 0
        ):
            costs += expected.amount_minor
            continue
        else:
            reason = "unmatched-cost"
        issues.append(CloseIssue(reason, expected.owner, expected.key))
    issues.extend(
        CloseIssue("undeclared-cost", request.owner, key)
        for key in sorted(actual_costs)
    )
    return CloseReceipt(
        request.request_id,
        request.supersedes,
        request.statement_revision,
        tuple(sorted(journal_ids)),
        cash,
        difference,
        None if issues else costs,
        tuple(issues),
        request.recheck_at_ns if issues else None,
    )


def _prior_cost_keys(payload: dict) -> set[tuple[str, str, str]]:
    return {
        (cost["source"], cost["event_id"], cost["expense_account"])
        for cost in payload["expected_costs"]
    }


def close_period(writer: WriterLease, request: CloseRequest) -> CloseReceipt:
    writer.verify_for(writer.store, request.company_id)
    scope = _encoded(
        (
            request.from_date,
            request.through_date,
            request.cash_account,
            request.currency,
        )
    )
    terms = asdict(request)
    if request.units_per_whole is None:
        del terms["units_per_whole"]
    payload = _encoded(terms)
    with (
        closing(open_writer(writer.store, lease=writer)) as database,
        write_tx(database),
    ):
        writer.verify()
        if _identity(database) != request.company_id:
            raise CloseRefused("Period close belongs to another company")
        replay = database.execute(
            "SELECT request,receipt FROM accounting_close WHERE request_id=?",
            (request.request_id,),
        ).fetchone()
        if replay is not None:
            if replay[0] != payload:
                raise CloseRefused("Close request conflicts with its original evidence")
            writer.verify()
            return _receipt(replay[1])
        latest = database.execute(
            """SELECT prior.request_id, prior.request
               FROM accounting_close prior
               WHERE prior.scope=?
                 AND NOT EXISTS (
                     SELECT 1 FROM accounting_close newer
                     WHERE newer.supersedes = prior.request_id
                 )
               LIMIT 1""",
            (scope,),
        ).fetchone()
        if request.supersedes != (latest[0] if latest else None):
            raise CloseRefused(
                "Close must supersede the latest receipt for this period"
            )
        if latest:
            prior = json.loads(latest[1])
            if (
                prior["mapping_revision"] != request.mapping_revision
                or prior.get("units_per_whole") != request.units_per_whole
                or set(prior["expense_accounts"]) != set(request.expense_accounts)
                or not _prior_cost_keys(prior)
                <= {cost.key for cost in request.expected_costs}
            ):
                raise CloseRefused(
                    "A correction cannot remove cost sources or change the chart or scale"
                )
        assessed_ns = time_ns()
        receipt = _assess(database, request)
        database.execute(
            "INSERT INTO accounting_close VALUES (?,?,?,?,?,?)",
            (
                request.request_id,
                scope,
                request.supersedes,
                payload,
                _encoded(asdict(receipt)),
                assessed_ns,
            ),
        )
        append_audit_event(
            database,
            company_id=request.company_id,
            domain="accounting",
            entity_type="accounting_close",
            entity_id=request.request_id,
            event_type="accounting.period_closed",
            actor_id=request.owner,
            payload={
                "request_id": request.request_id,
                "supersedes": request.supersedes,
                "complete": receipt.complete,
                "verified_cost_minor": receipt.verified_cost_minor,
                "issue_count": len(receipt.issues),
            },
            recorded_at_ns=assessed_ns,
        )
        writer.verify()
    return receipt
