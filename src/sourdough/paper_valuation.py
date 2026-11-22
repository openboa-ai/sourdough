"""Capture a revisioned valuation from the current paper venue evidence."""

import json
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path

from .company import _connect, _identity
from .nautilus_paper import QUOTE_MAX_AGE_NS, current_quote_times
from .nautilus_runtime import observe_bound_valuation
from .paper_window import RegisteredWindow, read_window
from .spot_cash import MAX_ATOMS
from .writer_lease import WriterLease


BEST_BID_POLICY = "spot-best-bid-v1"
_SCHEDULED_PREFIX = "window-cut-v1:"


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


@dataclass(frozen=True)
class WindowSamples:
    window_revision: str
    captures: tuple[tuple[int, PaperValuation], ...]
    missing_ranges: tuple[tuple[int, int], ...]


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
    _require_window_cut(window, request, json.loads(valuation.evidence))
    return WindowValuation(window.revision, valuation)


def _require_window_cut(
    window: RegisteredWindow, request: ValuationRequest, cut: dict
) -> None:
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


def _last_slot(window: RegisteredWindow) -> int:
    plan = window.plan
    return (plan.closes_ns - plan.opens_ns) // plan.cadence_ns


def _slot_request(window: RegisteredWindow, slot: int) -> ValuationRequest:
    if type(slot) is not int or not 0 <= slot <= _last_slot(window):
        raise ValuationRefused("Valuation slot is not part of this sealed window")
    return ValuationRequest(
        f"{_SCHEDULED_PREFIX}{window.revision}:{slot}",
        window.plan.binding_revision,
        window.plan.valuation_revision,
    )


def earliest_sample_ns(window: RegisteredWindow, slot: int) -> int:
    plan = window.plan
    start = plan.opens_ns + slot * plan.cadence_ns
    if slot == _last_slot(window):
        return max(start, plan.closes_ns - QUOTE_MAX_AGE_NS)
    return start


def _require_slot_cut(
    window: RegisteredWindow, slot: int, request: ValuationRequest, cut: dict
) -> None:
    _require_window_cut(window, request, cut)
    plan = window.plan
    starts = plan.opens_ns + slot * plan.cadence_ns
    ends = min(plan.closes_ns, starts + plan.cadence_ns - 1)
    if not all(
        starts <= cut["observation"][field] <= ends
        for field in ("quote_event_ns", "quote_received_ns", "observed_ns")
    ):
        raise ValuationRefused("Valuation evidence falls outside its scheduled slot")


def capture_window_sample(
    writer: WriterLease, engine_directory: Path, window_id: str, slot: int
) -> WindowValuation:
    writer.verify()
    window = read_window(writer.store, writer.company_id, window_id)
    if window is None:
        raise ValuationRefused("Operating window has no registered receipt")
    request = _slot_request(window, slot)

    def admit(cut):
        quote = cut["observation"]
        if slot == _last_slot(window) and not current_quote_times(
            quote["quote_event_ns"], quote["quote_received_ns"], window.plan.closes_ns
        ):
            raise ValuationRefused(
                "Quote cannot support the registered closing instant"
            )

    saved = _capture(
        writer,
        engine_directory,
        request,
        lambda cut: _require_slot_cut(window, slot, request, cut),
        admit=admit,
    )
    return WindowValuation(window.revision, saved)


def read_window_samples(store: Path, company_id: str, window_id: str) -> WindowSamples:
    window = read_window(store, company_id, window_id)
    if window is None:
        raise ValuationRefused("Operating window has no registered receipt")
    prefix = f"{_SCHEDULED_PREFIX}{window.revision}:"
    captures = []
    with closing(_connect(store, "ro")) as database:
        database.execute("BEGIN")
        if _identity(database) != company_id:
            raise ValuationRefused("Valuation belongs to another company")
        if database.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='paper_valuation'"
        ).fetchone():
            for (capture_id,) in database.execute(
                "SELECT capture_id FROM paper_valuation WHERE capture_id GLOB ?",
                (prefix + "*",),
            ):
                suffix = capture_id.removeprefix(prefix)
                if (
                    not 0 < len(suffix) <= 19
                    or not suffix.isascii()
                    or not suffix.isdecimal()
                ):
                    raise ValuationRefused("Valuation slot receipt has a changed ID")
                slot = int(suffix)
                request = _slot_request(window, slot)
                if request.capture_id != capture_id:
                    raise ValuationRefused("Valuation slot receipt has a changed ID")
                saved = _read(database, request)
                if saved is None:
                    raise ValuationRefused("Valuation slot receipt is unavailable")
                _require_slot_cut(window, slot, request, json.loads(saved.evidence))
                captures.append((slot, saved))
    captures.sort()
    missing = []
    upcoming = 0
    for slot, _ in captures:
        if slot > upcoming:
            missing.append((upcoming, slot - 1))
        upcoming = slot + 1
    if upcoming <= _last_slot(window):
        missing.append((upcoming, _last_slot(window)))
    return WindowSamples(window.revision, tuple(captures), tuple(missing))


def capture_paper_valuation(
    writer: WriterLease, engine_directory: Path, request: ValuationRequest
) -> PaperValuation:
    if request.capture_id.startswith(_SCHEDULED_PREFIX):
        raise ValuationRefused("Scheduled valuation IDs cannot be assigned explicitly")
    return _capture(writer, engine_directory, request)


def _capture(
    writer: WriterLease,
    engine_directory: Path,
    request: ValuationRequest,
    validate: Callable[[dict], None] | None = None,
    *,
    admit: Callable[[dict], None] | None = None,
) -> PaperValuation:
    writer.verify()
    saved = read_paper_valuation(writer.store, writer.company_id, request)
    if saved is not None:
        if validate is not None:
            validate(json.loads(saved.evidence))
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
    values = asdict(cut)
    if validate is not None:
        validate(values)
    if admit is not None:
        admit(values)
    evidence = json.dumps(values, sort_keys=True, separators=(",", ":"))
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
            if validate is not None:
                validate(json.loads(saved.evidence))
            return saved
        database.execute(
            "INSERT INTO paper_valuation VALUES (?,?,?,?,?,?)",
            (request.capture_id, *_terms(request), evidence, nav, revision),
        )
        writer.verify()
    return PaperValuation(request, evidence, nav, revision)
