"""Capture a revisioned valuation from the current paper venue evidence."""

import json
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path

from .company import _connect, _identity
from .nautilus_runtime import observe_bound_valuation
from .paper_window import read_window
from .spot_cash import MAX_ATOMS
from .writer_lease import WriterLease


BEST_BID_POLICY = "spot-best-bid-v1"


class ValuationRefused(Exception):
    """The requested cut is not supported by the original evidence."""


@dataclass(frozen=True)
class ValuationRequest:
    capture_id: str
    binding_revision: str
    policy: str

    def __post_init__(self) -> None:
        if self.policy != BEST_BID_POLICY or not all(
            isinstance(value, str) and 0 < len(value.strip()) <= 128
            for value in (self.capture_id, self.binding_revision)
        ):
            raise ValuationRefused(
                "Expected an identified capture and pinned mark policy"
            )


@dataclass(frozen=True)
class PaperValuation:
    request: ValuationRequest
    evidence: str
    trading_nav_atoms: int
    revision: str


@dataclass(frozen=True)
class WindowValuation:
    window_revision: str
    valuation: PaperValuation


def _terms(request: ValuationRequest) -> tuple[str, str]:
    return request.binding_revision, request.policy


def _read(connection, request: ValuationRequest) -> PaperValuation | None:
    if not connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='paper_valuation'"
    ).fetchone():
        return None
    row = connection.execute(
        """SELECT binding_revision,policy,evidence,trading_nav_atoms,revision
           FROM paper_valuation WHERE capture_id=?""",
        (request.capture_id,),
    ).fetchone()
    if row is None:
        return None
    if row[:2] != _terms(request):
        raise ValuationRefused("Valuation capture conflicts with its original request")
    evidence, nav, revision = row[2:]
    if _revision(request, evidence, nav) != revision:
        raise ValuationRefused(
            "Valuation evidence no longer matches its sealed revision"
        )
    return PaperValuation(request, evidence, nav, revision)


def _revision(request: ValuationRequest, evidence: str, nav: int) -> str:
    body = json.dumps(
        [asdict(request), evidence, nav], sort_keys=True, separators=(",", ":")
    )
    return sha256(body.encode()).hexdigest()


def read_paper_valuation(
    store: Path, company_id: str, request: ValuationRequest
) -> PaperValuation | None:
    with closing(_connect(store, "ro")) as database:
        database.execute("BEGIN")
        if _identity(database) != company_id:
            raise ValuationRefused("Valuation belongs to another company")
        return _read(database, request)


def read_window_valuation(
    store: Path, company_id: str, window_id: str, request: ValuationRequest
) -> WindowValuation:
    window = read_window(store, company_id, window_id)
    if window is None:
        raise ValuationRefused("Operating window has no registered receipt")
    valuation = read_paper_valuation(store, company_id, request)
    if valuation is None:
        raise ValuationRefused("Valuation cut has no captured receipt")
    cut = json.loads(valuation.evidence)
    observation = cut["observation"]
    if (
        request.binding_revision != window.plan.binding_revision
        or request.policy != window.plan.valuation_revision
        or observation["account"] != asdict(window.account)
        or cut.get("source_revision") != window.plan.source_revision
    ):
        raise ValuationRefused(
            "Valuation does not match the sealed account and policies"
        )
    if not all(
        window.admits_availability(observation[field])
        for field in (
            "quote_event_ns",
            "quote_received_ns",
            "supporting_trade_ns",
            "observed_ns",
        )
    ):
        raise ValuationRefused("Valuation evidence falls outside the sealed window")
    return WindowValuation(window.revision, valuation)


def capture_paper_valuation(
    writer: WriterLease, engine_directory: Path, request: ValuationRequest
) -> PaperValuation:
    writer.verify()
    saved = read_paper_valuation(writer.store, writer.company_id, request)
    if saved is not None:
        return saved
    cut = observe_bound_valuation(writer.store, writer.company_id, engine_directory)
    if cut.binding_revision != request.binding_revision:
        raise ValuationRefused("Valuation was not observed on the requested binding")
    observed = cut.observation
    cash, lots = observed.balance.cash_atoms, observed.balance.position_lots
    nav = cash + lots * observed.bid_atoms_per_lot
    if cash < 0 or lots < 0 or not 0 <= nav <= MAX_ATOMS:
        raise ValuationRefused(
            "Marked cash-spot exposure is outside the supported units"
        )
    evidence = json.dumps(asdict(cut), sort_keys=True, separators=(",", ":"))
    revision = _revision(request, evidence, nav)
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        writer.verify()
        if _identity(database) != writer.company_id:
            raise ValuationRefused("Valuation belongs to another company")
        database.execute(
            """CREATE TABLE IF NOT EXISTS paper_valuation (
                capture_id TEXT PRIMARY KEY NOT NULL,
                binding_revision TEXT NOT NULL, policy TEXT NOT NULL,
                evidence TEXT NOT NULL, trading_nav_atoms INTEGER NOT NULL,
                revision TEXT NOT NULL UNIQUE
            )"""
        )
        saved = _read(database, request)
        if saved is not None:
            return saved
        database.execute(
            "INSERT INTO paper_valuation VALUES (?,?,?,?,?,?)",
            (request.capture_id, *_terms(request), evidence, nav, revision),
        )
        writer.verify()
    return PaperValuation(request, evidence, nav, revision)
