"""Prepare, start, and bind an independently supervised Nautilus paper venue."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .market_worker import SpotInstrumentTerms, read_spot_terms, reopen
from .nautilus_paper import NautilusPaperVenue, SandboxPlan, SandboxValuationCut
from .paper_account import (
    BindingChange,
    PaperAccount,
    PaperMandate,
    bind,
    change_paper_binding,
    reopen_binding,
)
from .paper_execution import VenueRefused
from .paper_sender_lease import hold_sender_lease, verify_sender_lease
from .replay_tape import ReplayTape
from .supervisor import SupervisionResult, supervise_worker
from .trading_engine import VERSION, _environment, prepare_engine
from .writer_lease import WriterLease, hold_company_writer


_WORKER = """import importlib, sys
sys.path.insert(0, sys.argv.pop(1))
importlib.import_module(sys.argv.pop(1)).main()
"""
VALUATION_SOURCE_REVISION = "binance-public-spot-quote-v1"
PAPER_READY_SECONDS = 32
PAPER_ACTIVATION_LIMIT_SECONDS = 600


@dataclass(frozen=True)
class BoundValuationCut:
    binding_revision: str
    observation: SandboxValuationCut
    source_revision: str


def paper_venue(
    store: Path, directory: Path, *, replay: bool = False
) -> NautilusPaperVenue:
    engine = directory.resolve() / f"nautilus-{VERSION}"
    venue = NautilusPaperVenue(
        engine / ("replay.sqlite" if replay else "sandbox.sqlite")
    )
    venue.require_independent_from(store)
    return venue


def observe_bound_valuation(
    store: Path, company_id: str, directory: Path
) -> BoundValuationCut:
    binding = reopen_binding(store, company_id)
    venue = paper_venue(store, directory)
    _, plan = venue.configuration()
    cut = venue.observe_valuation_cut(binding.account.account_id)
    if (
        plan.company_id != company_id
        or cut.account != binding.account
        or reopen_binding(store, company_id) != binding
    ):
        raise VenueRefused("Valuation cut does not match the current company binding")
    return BoundValuationCut(binding.revision, cut, VALUATION_SOURCE_REVISION)


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
    writer: WriterLease,
    directory: Path,
    account_id: str,
    starting_atoms: int,
    *,
    tape_path: Path | None = None,
) -> str:
    if tape_path is None:
        _, venue, terms = _prepared(writer, directory)
        revision = None
    else:
        tape = ReplayTape.load(tape_path)
        prepare_engine(writer, directory, allow_install=False)
        venue = paper_venue(writer.store, directory, replay=True)
        terms, revision = tape.terms, tape.revision
    writer.verify()
    return venue.provision(
        SandboxPlan(writer.company_id, account_id, terms, starting_atoms, 0, revision)
    )


def _binding_destination(
    writer: WriterLease, directory: Path, replay: bool
) -> tuple[NautilusPaperVenue, PaperAccount]:
    if replay:
        prepare_engine(writer, directory, allow_install=False)
        venue = paper_venue(writer.store, directory, replay=True)
        terms = None
    else:
        _, venue, terms = _prepared(writer, directory)
    _, plan = venue.configuration()
    if (
        plan.company_id != writer.company_id
        or replay != (plan.tape_revision is not None)
        or (terms is not None and plan.terms != terms)
    ):
        raise VenueRefused(
            "Virtual account does not match the prepared company and instrument"
        )
    return venue, venue.observe_account(plan.account_id)


def bind_paper(
    writer: WriterLease,
    directory: Path,
    *,
    revision: str,
    charter_revision: str,
    exposure_atoms: int,
    replay: bool = False,
) -> PaperMandate:
    venue, account = _binding_destination(writer, directory, replay)
    venue.observe_quote(account.account_id)
    writer.verify()
    return bind(
        writer.store,
        PaperMandate(
            revision, writer.company_id, charter_revision, account, exposure_atoms
        ),
        venue,
    )


def switch_paper(
    writer: WriterLease,
    previous_directory: Path,
    directory: Path,
    *,
    previous_revision: str,
    revision: str,
    charter_revision: str,
    exposure_atoms: int,
    from_replay: bool = False,
    replay: bool = False,
) -> BindingChange:
    current = paper_venue(writer.store, previous_directory, replay=from_replay)
    next_venue, next_account = _binding_destination(writer, directory, replay)
    return change_paper_binding(
        writer,
        previous_revision,
        PaperMandate(
            revision, writer.company_id, charter_revision, next_account, exposure_atoms
        ),
        current,
        next_venue,
    )


def supervise_paper(
    store: Path,
    company_id: str,
    directory: Path,
    *,
    seconds: int,
    emit: Callable[[dict], None],
    tape_path: Path | None = None,
    max_restarts: int = 1,
) -> SupervisionResult:
    if type(seconds) is not int or not 1 <= seconds <= PAPER_ACTIVATION_LIMIT_SECONDS:
        raise VenueRefused("Sandbox activation must be bounded to one to 600 seconds")
    journal = paper_venue(store, directory, replay=tape_path is not None).journal
    with hold_sender_lease(journal) as (descriptor, key):
        with hold_company_writer(store, company_id) as writer:
            if tape_path is None:
                engine, venue, terms = _prepared(writer, directory)
                tape_revision = None
                worker = "sourdough.nautilus_paper_worker"
                arguments = [str(seconds)]
            else:
                tape = ReplayTape.load(tape_path)
                installation = prepare_engine(writer, directory, allow_install=False)
                engine = (
                    directory.resolve()
                    / f"nautilus-{installation.distribution_version}"
                )
                venue = paper_venue(writer.store, directory, replay=True)
                terms, tape_revision = tape.terms, tape.revision
                worker = "sourdough.nautilus_replay_worker"
                arguments = [str(tape_path.resolve()), str(seconds)]
            _, plan = venue.configuration()
            if (
                venue.journal != journal
                or plan.company_id != writer.company_id
                or plan.terms != terms
                or plan.tape_revision != tape_revision
            ):
                raise VenueRefused("Prepared sandbox does not match the company")
            verify_sender_lease(journal, descriptor, key)
        return supervise_worker(
            [
                str(engine / "runtime" / "bin" / "python"),
                "-IB",
                "-c",
                _WORKER,
                str(Path(__file__).resolve().parents[1]),
                worker,
                str(journal),
                *arguments,
                "--sender-lease-fd",
                str(descriptor),
            ],
            journal,
            company_id,
            max_restarts=max_restarts,
            startup_seconds=PAPER_READY_SECONDS,
            max_seconds=seconds + 80,
            emit=emit,
            environment=_environment(engine / "home"),
            expected_store_key=key,
            inherited_fds=(descriptor,),
        )
