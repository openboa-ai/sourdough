"""Resume managed paper admission only on corroborated current account evidence."""

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from time import time_ns

from .company import _connect
from .nautilus_paper import CONNECTOR
from .nautilus_runtime import paper_venue
from .paper_account import (
    PaperMandate,
    has_pending_binding_change,
    read_current_binding,
)
from .paper_books import pending_no_fill_observation_sql, resolved_order_sql
from .paper_risk import (
    AuthorizeBoard,
    PaperRiskReceipt,
    PaperRiskRefused,
    PaperSuspension,
    _resume_with_evidence,
)
from .writer_lease import WriterLease


@dataclass(frozen=True)
class _SettledCompany:
    binding: PaperMandate
    orders: tuple[str, ...]
    fills: tuple[tuple[str, str, int, int, int], ...]


def _has_table(connection: Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _settled_company(connection: Connection, company_id: str) -> _SettledCompany:
    binding = read_current_binding(connection, company_id)
    account = binding.account
    if account.connector_revision != CONNECTOR or has_pending_binding_change(
        connection
    ):
        raise PaperRiskRefused("Expected the current unchanged managed paper binding")
    if not _has_table(connection, "paper_order"):
        return _SettledCompany(binding, (), ())
    if connection.execute(
        f"""SELECT 1 FROM paper_order o WHERE NOT ({resolved_order_sql(connection)})
            OR ({pending_no_fill_observation_sql(connection)}) LIMIT 1"""
    ).fetchone():
        raise PaperRiskRefused("Company has unresolved original paper obligations")
    scope = (account.account_id, binding.revision)
    order_ids = tuple(
        row[0]
        for row in connection.execute(
            """SELECT order_id FROM paper_order
               WHERE account_id=? AND mandate_revision=? ORDER BY order_id""",
            scope,
        )
    )
    fills = ()
    if _has_table(connection, "paper_fill"):
        if not _has_table(connection, "paper_fill_fee"):
            raise PaperRiskRefused("Company fills lack original fee evidence")
        fills = tuple(
            connection.execute(
                """SELECT f.fill_id,f.order_id,f.quantity,f.price_minor_units,p.fee_minor_units
                   FROM paper_fill f JOIN paper_order o ON o.order_id=f.order_id
                   LEFT JOIN paper_fill_fee p ON p.fill_id=f.fill_id
                   WHERE o.account_id=? AND o.mandate_revision=? ORDER BY f.fill_id""",
                scope,
            )
        )
    return _SettledCompany(binding, order_ids, fills)


def resume_managed_paper_risk(
    writer: WriterLease,
    command: PaperSuspension,
    engine: Path,
    authorize: AuthorizeBoard | None,
) -> PaperRiskReceipt:
    def observe():
        with closing(_connect(writer.store, "ro")) as connection:
            connection.execute("BEGIN")
            expected = _settled_company(connection, writer.company_id)
        venue = paper_venue(writer.store, engine)
        account = expected.binding.account
        cut = venue.read_current_risk_cut(account.account_id)
        if (
            cut.account != account
            or cut.settled.order_ids != expected.orders
            or cut.settled.fills != expected.fills
            or cut.settled.balance.position_lots
            != sum(fill[2] for fill in expected.fills)
            or cut.settled.balance.cash_atoms
            != account.starting_minor_units
            - sum(
                quantity * price + fee for _, _, quantity, price, fee in expected.fills
            )
        ):
            raise PaperRiskRefused(
                "Company records do not match the original venue cut"
            )

        def verify_local(connection: Connection):
            if _settled_company(connection, writer.company_id) != expected:
                raise PaperRiskRefused(
                    "Paper obligations or binding changed during observation"
                )
            if not cut.observed_ns <= time_ns() <= cut.expires_ns:
                raise PaperRiskRefused("Current paper evidence expired before commit")

        return verify_local

    return _resume_with_evidence(writer, command, authorize, observe)
