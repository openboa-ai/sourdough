"""Bind one verified paper mandate to an existing company."""

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .company import _connect, _identity


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


@dataclass(frozen=True)
class PaperMandate:
    revision: str
    company_id: str
    charter_revision: str
    account: PaperAccount
    exposure_limit_minor_units: int


class AccountObserver(Protocol):
    def observe_account(self, account_id: str) -> PaperAccount: ...


_CREATE_BINDING = """CREATE TABLE IF NOT EXISTS paper_binding (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision TEXT NOT NULL, company_id TEXT NOT NULL REFERENCES company(id),
    charter_revision TEXT NOT NULL, exposure_limit_minor_units INTEGER NOT NULL,
    account_id TEXT NOT NULL, connector_revision TEXT NOT NULL,
    instrument_rules_digest TEXT NOT NULL, currency TEXT NOT NULL,
    starting_minor_units INTEGER NOT NULL,
    environment TEXT NOT NULL CHECK (environment = 'paper')
)"""
_SELECT_BINDING = """SELECT revision, company_id, charter_revision,
    exposure_limit_minor_units, account_id, connector_revision,
    instrument_rules_digest, currency, starting_minor_units, environment
    FROM paper_binding WHERE singleton = 1"""


def bind(store: Path, mandate: PaperMandate, observer: AccountObserver) -> PaperMandate:
    account = mandate.account
    if (
        account.environment != "paper"
        or type(account.starting_minor_units) is not int
        or type(mandate.exposure_limit_minor_units) is not int
        or not 0 < mandate.exposure_limit_minor_units <= account.starting_minor_units
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
            )
        )
    ):
        raise BindingRefused("Invalid paper mandate")
    if observer.observe_account(account.account_id) != account:
        raise BindingRefused("Destination account does not match the mandate")
    with closing(_connect(store, "rw")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        charter = connection.execute(
            "SELECT charter_revision FROM company WHERE singleton = 1"
        ).fetchone()
        if _identity(connection) != mandate.company_id or charter != (
            mandate.charter_revision,
        ):
            raise BindingRefused("Company or charter mismatch")
        connection.execute(_CREATE_BINDING)
        existing = connection.execute(_SELECT_BINDING).fetchone()
        terms = (
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
        )
        if existing is None:
            connection.execute(
                "INSERT INTO paper_binding VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                terms,
            )
        elif existing != terms:
            raise BindingRefused("Existing paper binding cannot be replaced")
    return mandate


def reopen_binding(store: Path, expected_company_id: str) -> PaperMandate:
    with closing(_connect(store, "ro")) as connection, connection:
        connection.execute("BEGIN")
        if _identity(connection) != expected_company_id:
            raise BindingRefused("Company identity mismatch")
        if not connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'paper_binding'"
        ).fetchone():
            raise BindingRefused("Paper binding is absent")
        row = connection.execute(_SELECT_BINDING).fetchone()
        charter = connection.execute(
            "SELECT charter_revision FROM company WHERE singleton = 1"
        ).fetchone()
        if row is None or row[1] != expected_company_id or (row[2],) != charter:
            raise BindingRefused("No paper binding for the current company charter")
        return PaperMandate(*row[:3], PaperAccount(*row[4:]), row[3])
