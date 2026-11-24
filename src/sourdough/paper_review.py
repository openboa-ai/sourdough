"""Bind a current paper quote to the Agent's authorized position read."""

from dataclasses import asdict, astuple, dataclass
from pathlib import Path
from sqlite3 import Connection, OperationalError
from time import time

from .paper_account import BindingRefused, reopen_binding
from .paper_execution import PaperExecution, VenueRefused
from .storage.db import StorageRefused
from .storage.schema import require_domain


@dataclass(frozen=True)
class ReviewQuote:
    account_id: str | None
    connector_revision: str | None
    quote_minor_units: int | None
    observed_at: int


def observe_review_quote(
    store: Path, company_id: str, venue: PaperExecution
) -> ReviewQuote:
    try:
        account = reopen_binding(store, company_id).account
        if venue.observe_account(account.account_id) == account:
            price = venue.observe_quote(account.account_id)
            return ReviewQuote(
                account.account_id, account.connector_revision, price, int(time())
            )
    except (BindingRefused, StorageRefused, VenueRefused, OperationalError):
        pass
    return ReviewQuote(None, None, None, int(time()))


def bind_review_quote(
    connection: Connection, run_id: str, position: dict, quote: ReviewQuote
) -> dict:
    require_domain(connection, "paper")
    if quote.account_id != position["account_id"]:
        quote = ReviewQuote(None, None, None, quote.observed_at)
    connection.execute(
        "INSERT OR IGNORE INTO paper_review_quote VALUES (?, ?, ?, ?, ?)",
        (run_id, *astuple(quote)),
    )
    saved = connection.execute(
        """SELECT account_id, connector_revision, quote_minor_units, observed_at
           FROM paper_review_quote WHERE run_id = ?""",
        (run_id,),
    ).fetchone()
    return position | {"current_observation": asdict(ReviewQuote(*saved))}
