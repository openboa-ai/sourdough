"""Own the verified paper mandate and explicit destination changes."""

import json
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from time import time_ns
from typing import TYPE_CHECKING, Protocol

from .company import _connect, _identity
from .duty_run import PAPER_EXECUTION
from .storage.schema import enable_domain_v1, is_domain_installed, require_domain

if TYPE_CHECKING:
    from .writer_lease import WriterLease


class BindingRefused(Exception):
    """The paper mandate could not be admitted or resolved."""


@dataclass(frozen=True)
class PaperAccount:
    account_id: str
    connector_revision: str
    instrument_rules_digest: str
    currency: str
    starting_minor_units: int
    environment: str
    destination_id: str = ""


@dataclass(frozen=True)
class PaperMandate:
    revision: str
    company_id: str
    charter_revision: str
    account: PaperAccount
    exposure_limit_minor_units: int


class AccountObserver(Protocol):
    def observe_account(self, account_id: str) -> PaperAccount: ...


class PaperChangeoverObserver(AccountObserver, Protocol):
    def observe_drained_account(self, account_id: str) -> PaperAccount | None: ...


@dataclass(frozen=True)
class BindingChange:
    previous_revision: str
    revision: str
    requested_at_ns: int
    effective_at_ns: int | None
    blocked_by: str | None = None


_SELECT_BINDING = """SELECT revision, company_id, charter_revision,
    exposure_limit_minor_units, account_id, connector_revision,
    instrument_rules_digest, currency, starting_minor_units, environment, destination_id
    FROM paper_binding WHERE singleton = 1"""


def _validate(mandate: PaperMandate, observer: AccountObserver) -> None:
    account = mandate.account
    if (
        account.environment != "paper"
        or type(account.starting_minor_units) is not int
        or type(mandate.exposure_limit_minor_units) is not int
        or not 0
        < mandate.exposure_limit_minor_units
        <= account.starting_minor_units
        <= 2**63 - 1
        or not all(
            isinstance(value, str) and value.strip()
            for value in (
                mandate.revision,
                mandate.company_id,
                mandate.charter_revision,
                account.account_id,
                account.connector_revision,
                account.instrument_rules_digest,
                account.currency,
                account.destination_id,
            )
        )
    ):
        raise BindingRefused("Invalid paper mandate")
    if observer.observe_account(account.account_id) != account:
        raise BindingRefused("Destination account does not match the mandate")


def _terms(mandate: PaperMandate) -> tuple:
    account = mandate.account
    return (
        mandate.revision,
        mandate.company_id,
        mandate.charter_revision,
        mandate.exposure_limit_minor_units,
        account.account_id,
        account.connector_revision,
        account.instrument_rules_digest,
        account.currency,
        account.starting_minor_units,
        account.environment,
        account.destination_id,
    )


_INSERT_BINDING = (
    "INSERT INTO paper_binding VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def bind(store: Path, mandate: PaperMandate, observer: AccountObserver) -> PaperMandate:
    _validate(mandate, observer)
    with closing(_connect(store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        charter = connection.execute(
            "SELECT charter_revision FROM company WHERE singleton = 1"
        ).fetchone()
        if _identity(connection) != mandate.company_id or charter != (
            mandate.charter_revision,
        ):
            raise BindingRefused("Company or charter mismatch")
        enable_domain_v1(
            connection,
            "paper",
            company_id=mandate.company_id,
            actor_id="paper.bind",
        )
        existing = connection.execute(_SELECT_BINDING).fetchone()
        terms = _terms(mandate)
        if existing is None:
            connection.execute(_INSERT_BINDING, terms)
        elif existing != terms:
            raise BindingRefused("Existing paper binding cannot be replaced")
    return mandate


def has_pending_binding_change(connection) -> bool:
    return (
        is_domain_installed(connection, "paper")
        and connection.execute(
            "SELECT 1 FROM paper_binding_change WHERE effective_at_ns IS NULL"
        ).fetchone()
        is not None
    )


def _has_unsubmitted_buy(connection, mandate: PaperMandate) -> bool:
    if not is_domain_installed(connection, "paper"):
        return False
    sources = (
        (
            "paper_research_decision",
            "JOIN paper_research_snapshot s ON s.run_id = decision.run_id",
            """json_extract(decision.content, '$.action') = 'buy'
               AND json_extract(s.inputs, '$.account_id') = ?
               AND json_extract(s.inputs, '$.instrument_rules_digest') = ?""",
        ),
        (
            "paper_decision",
            "",
            """decision.action = 'buy' AND decision.account_id = ?
               AND decision.instrument_rules_digest = ?""",
        ),
    )
    order_check = """AND NOT EXISTS (
        SELECT 1 FROM paper_order o WHERE o.action_id = decision.decision_id
            AND o.account_id = ? AND o.mandate_revision = ?
    )"""
    account = mandate.account
    terms = (
        PAPER_EXECUTION,
        account.account_id,
        account.instrument_rules_digest,
        account.account_id,
        mandate.revision,
    )
    return any(
        connection.execute(
            f"""SELECT 1 FROM {table} decision {snapshot}
                JOIN duty d ON d.id = decision.duty_id
                JOIN duty_run r ON r.duty_id = d.id AND r.is_current = 1
                JOIN standing_grant g ON g.id = r.grant_id AND g.agent_id = d.agent_id
                WHERE d.state = 'running' AND r.purpose = ?
                  AND r.grant_scope = 'paper' AND g.scope = 'paper'
                  AND {original_account} {order_check} LIMIT 1""",
            terms,
        ).fetchone()
        is not None
        for table, snapshot, original_account in sources
    )


def _open_effect(connection, mandate: PaperMandate) -> bool:
    if not is_domain_installed(connection, "paper"):
        return False
    return (
        connection.execute(
            """SELECT 1 FROM paper_order orders
           JOIN duty_run run ON run.run_id=orders.run_id
           JOIN duty ON duty.id=run.duty_id
           WHERE orders.mandate_revision=? AND orders.account_id=?
             AND (orders.reserved_minor_units!=0 OR duty.state='running') LIMIT 1""",
            (mandate.revision, mandate.account.account_id),
        ).fetchone()
        is not None
    )


def _previous_change(connection, revision: str):
    if not is_domain_installed(connection, "paper"):
        return None
    return connection.execute(
        """SELECT previous_revision,old_terms,new_terms,requested_at_ns,effective_at_ns
           FROM paper_binding_change WHERE revision=?""",
        (revision,),
    ).fetchone()


def was_applied_binding_change(connection, revision: str) -> bool:
    saved = _previous_change(connection, revision)
    return saved is not None and saved[4] is not None


def read_applied_binding(
    connection, company_id: str, revision: str
) -> tuple[PaperAccount, int] | None:
    saved = _previous_change(connection, revision)
    if saved is None or saved[4] is None:
        return None
    terms = json.loads(saved[2])
    if terms[1] != company_id:
        raise BindingRefused("Binding receipt belongs to another company")
    return PaperAccount(*terms[4:]), saved[4]


def is_retired_account_scope(
    connection, company_id: str, account_id: str, instrument_revision: str
) -> bool:
    if not is_domain_installed(connection, "paper"):
        return False
    return any(
        (terms[1], terms[4], terms[6]) == (company_id, account_id, instrument_revision)
        for (stored,) in connection.execute(
            "SELECT old_terms FROM paper_binding_change WHERE effective_at_ns IS NOT NULL"
        )
        for terms in (json.loads(stored),)
    )


def _require_new_identity(connection, mandate: PaperMandate) -> None:
    for revision, old_terms in connection.execute(
        "SELECT previous_revision,old_terms FROM paper_binding_change"
    ):
        _, _, _, _, account_id, _, _, _, _, _, destination_id = json.loads(old_terms)
        if (
            revision == mandate.revision
            or account_id == mandate.account.account_id
            or destination_id == mandate.account.destination_id
        ):
            raise BindingRefused(
                "A retired binding, account or destination cannot be reselected"
            )


def change_paper_binding(
    writer: WriterLease,
    previous_revision: str,
    next_mandate: PaperMandate,
    current_destination: PaperChangeoverObserver,
    next_destination: PaperChangeoverObserver,
) -> BindingChange:
    writer.verify_for(writer.store, next_mandate.company_id)
    next_terms = json.dumps(_terms(next_mandate))
    with closing(_connect(writer.store, "ro")) as connection:
        saved = _previous_change(connection, next_mandate.revision)
    if saved is not None and (saved[0], saved[2]) != (previous_revision, next_terms):
        raise BindingRefused("Binding revision conflicts with its original request")
    if saved is not None and saved[4] is not None:
        return BindingChange(
            previous_revision, next_mandate.revision, saved[3], saved[4]
        )

    current = reopen_binding(writer.store, writer.company_id)
    if current.revision != previous_revision:
        raise BindingRefused("Change must name the effective paper binding revision")
    if (
        next_mandate.revision == current.revision
        or next_mandate.charter_revision != current.charter_revision
        or next_mandate.account.account_id == current.account.account_id
        or next_mandate.account.destination_id == current.account.destination_id
    ):
        raise BindingRefused(
            "Change requires a distinct paper account under the same charter"
        )
    _validate(next_mandate, next_destination)
    if (
        current_destination.observe_account(current.account.account_id)
        != current.account
    ):
        raise BindingRefused("Change requires the original paper destination")
    old_drained = current_destination.observe_drained_account(
        current.account.account_id
    )
    new_drained = next_destination.observe_drained_account(
        next_mandate.account.account_id
    )
    if old_drained not in (None, current.account) or new_drained not in (
        None,
        next_mandate.account,
    ):
        raise BindingRefused(
            "Drain evidence does not belong to the verified destination"
        )

    with closing(_connect(writer.store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        writer.verify()
        require_domain(connection, "paper")
        if connection.execute(_SELECT_BINDING).fetchone() != _terms(current):
            raise BindingRefused("Paper binding changed before transition")
        if _has_unsubmitted_buy(connection, current):
            raise BindingRefused("Finish the original buy decision before changeover")
        if saved is None:
            if has_pending_binding_change(connection):
                raise BindingRefused("Another paper binding change is pending")
            _require_new_identity(connection, next_mandate)
            requested = time_ns()
            connection.execute(
                "INSERT INTO paper_binding_change VALUES (?,?,?,?,?,NULL)",
                (
                    next_mandate.revision,
                    previous_revision,
                    json.dumps(_terms(current)),
                    next_terms,
                    requested,
                ),
            )
        else:
            requested = saved[3]
        if _open_effect(connection, current):
            return BindingChange(
                previous_revision,
                next_mandate.revision,
                requested,
                None,
                "outstanding-company-effect",
            )
        if old_drained is None or new_drained is None:
            return BindingChange(
                previous_revision,
                next_mandate.revision,
                requested,
                None,
                "unverified-flat-destination",
            )
        effective = time_ns()
        connection.execute("DELETE FROM paper_binding WHERE singleton=1")
        connection.execute(_INSERT_BINDING, _terms(next_mandate))
        connection.execute(
            "UPDATE paper_binding_change SET effective_at_ns=? WHERE revision=?",
            (effective, next_mandate.revision),
        )
        writer.verify()
    return BindingChange(previous_revision, next_mandate.revision, requested, effective)


def read_current_binding(connection, expected_company_id: str) -> PaperMandate:
    if _identity(connection) != expected_company_id:
        raise BindingRefused("Company identity mismatch")
    if not is_domain_installed(connection, "paper"):
        raise BindingRefused("Paper binding is absent")
    row = connection.execute(_SELECT_BINDING).fetchone()
    charter = connection.execute(
        "SELECT charter_revision FROM company WHERE singleton = 1"
    ).fetchone()
    if row is None or row[1] != expected_company_id or (row[2],) != charter:
        raise BindingRefused("No paper binding for the current company charter")
    return PaperMandate(*row[:3], PaperAccount(*row[4:]), row[3])


def reopen_binding(store: Path, expected_company_id: str) -> PaperMandate:
    with closing(_connect(store, "ro")) as connection, connection:
        connection.execute("BEGIN")
        return read_current_binding(connection, expected_company_id)
