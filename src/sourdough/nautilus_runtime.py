"""Prepare, start, and bind an independently supervised Nautilus paper venue."""

from collections.abc import Callable
from pathlib import Path

from .market_worker import SpotInstrumentTerms, journal_key, read_spot_terms, reopen
from .nautilus_paper import NautilusPaperVenue, SandboxPlan
from .paper_account import PaperMandate, bind
from .paper_execution import VenueRefused
from .supervisor import SupervisionResult, supervise_worker
from .trading_engine import VERSION, _environment, prepare_engine
from .writer_lease import WriterLease, hold_company_writer


_WORKER = """import sys
sys.path.insert(0, sys.argv.pop(1))
from sourdough.nautilus_paper_worker import main
main()
"""


def paper_venue(store: Path, directory: Path) -> NautilusPaperVenue:
    engine = directory.resolve() / f"nautilus-{VERSION}"
    venue = NautilusPaperVenue(engine / "sandbox.sqlite")
    venue.require_independent_from(store)
    return venue


def _prepared(
    writer: WriterLease, directory: Path
) -> tuple[Path, NautilusPaperVenue, SpotInstrumentTerms]:
    installation = prepare_engine(writer, directory, allow_install=False)
    engine = directory.resolve() / f"nautilus-{installation.distribution_version}"
    journal = engine / "market.sqlite"
    scope = reopen(journal, writer.company_id)
    terms = read_spot_terms(journal)
    if (
        scope.engine_lock_digest != installation.lock_digest
        or terms is None
        or scope.symbol != terms.symbol
    ):
        raise VenueRefused(
            "Prepare and observe the original spot market before sandbox use"
        )
    return engine, paper_venue(writer.store, directory), terms


def prepare_paper(
    writer: WriterLease, directory: Path, account_id: str, starting_atoms: int
) -> str:
    _, venue, terms = _prepared(writer, directory)
    writer.verify()
    return venue.provision(
        SandboxPlan(writer.company_id, account_id, terms, starting_atoms, 0)
    )


def bind_paper(
    writer: WriterLease,
    directory: Path,
    *,
    revision: str,
    charter_revision: str,
    exposure_atoms: int,
) -> PaperMandate:
    _, venue, terms = _prepared(writer, directory)
    _, plan = venue.configuration()
    if plan.company_id != writer.company_id or plan.terms != terms:
        raise VenueRefused(
            "Virtual account does not match the prepared company and instrument"
        )
    account = venue.observe_account(plan.account_id)
    venue.observe_quote(plan.account_id)
    writer.verify()
    return bind(
        writer.store,
        PaperMandate(
            revision, writer.company_id, charter_revision, account, exposure_atoms
        ),
        venue,
    )


def supervise_paper(
    store: Path,
    company_id: str,
    directory: Path,
    *,
    seconds: int,
    emit: Callable[[dict], None],
) -> SupervisionResult:
    if type(seconds) is not int or not 1 <= seconds <= 600:
        raise VenueRefused("Sandbox activation must be bounded to one to 600 seconds")
    with hold_company_writer(store, company_id) as writer:
        engine, venue, terms = _prepared(writer, directory)
        _, plan = venue.configuration()
        if plan.company_id != writer.company_id or plan.terms != terms:
            raise VenueRefused("Prepared sandbox does not match the company")
        key = journal_key(venue.journal)
    return supervise_worker(
        [
            str(engine / "runtime" / "bin" / "python"),
            "-IB",
            "-c",
            _WORKER,
            str(Path(__file__).resolve().parents[1]),
            str(venue.journal),
            str(seconds),
        ],
        venue.journal,
        company_id,
        max_restarts=1,
        startup_seconds=32,
        max_seconds=seconds + 80,
        emit=emit,
        environment=_environment(engine / "home"),
        expected_store_key=key,
    )
