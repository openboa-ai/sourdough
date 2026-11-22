"""Seal the first paper operating plan before its observation window."""

import json
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from sqlite3 import Connection
from time import time_ns

from .company import _connect, _identity
from .paper_account import PaperAccount, reopen_binding
from .writer_lease import WriterLease


class WindowRefused(Exception):
    """The proposed window cannot represent one prospective operating trial."""


@dataclass(frozen=True)
class WindowPlan:
    window_id: str
    binding_revision: str
    operating_revision: str
    evaluation_mandate_revision: str
    source_revision: str
    valuation_revision: str
    cost_inventory_revision: str
    execution_assumptions_revision: str
    cash_reference_revision: str
    contingency_revision: str
    opens_ns: int
    closes_ns: int
    cadence_ns: int
    minimum_observations: int
    max_exposure_minor: int
    max_spend_minor: int
    max_drawdown_bps: int
    hurdle_bps: int

    def __post_init__(self) -> None:
        pins = (
            self.window_id,
            self.binding_revision,
            self.operating_revision,
            self.evaluation_mandate_revision,
            self.source_revision,
            self.valuation_revision,
            self.cost_inventory_revision,
            self.execution_assumptions_revision,
            self.cash_reference_revision,
            self.contingency_revision,
        )
        if not all(
            isinstance(pin, str) and 0 < len(pin.strip()) <= 128 for pin in pins
        ):
            raise WindowRefused(
                "Window requires each original policy and source revision"
            )
        quantities = (
            self.opens_ns,
            self.closes_ns,
            self.cadence_ns,
            self.minimum_observations,
            self.max_exposure_minor,
            self.max_spend_minor,
            self.max_drawdown_bps,
            self.hurdle_bps,
        )
        if (
            not all(type(value) is int and 0 <= value < 2**63 for value in quantities)
            or not 0 < self.opens_ns < self.closes_ns
            or not self.cadence_ns
            or not 2
            <= self.minimum_observations
            <= 1 + (self.closes_ns - self.opens_ns) // self.cadence_ns
            or not 0 < self.max_drawdown_bps <= 10_000
            or not self.max_exposure_minor
        ):
            raise WindowRefused("Window has inconsistent timing, limits or coverage")


@dataclass(frozen=True)
class RegisteredWindow:
    company_id: str
    plan: WindowPlan
    account: PaperAccount
    registered_ns: int
    revision: str

    def admits_availability(self, available_ns: int) -> bool:
        return (
            type(available_ns) is int
            and self.registered_ns < available_ns
            and self.plan.opens_ns <= available_ns <= self.plan.closes_ns
        )


def _seal_revision(contract: str, committed_ns: int) -> str:
    return sha256(f"{committed_ns}:{contract}".encode()).hexdigest()


def _decode(contract: str, committed_ns: int, revision: str) -> RegisteredWindow:
    if _seal_revision(contract, committed_ns) != revision:
        raise WindowRefused("Window no longer matches its original seal")
    fields = json.loads(contract)
    fields["plan"] = WindowPlan(**fields["plan"])
    fields["account"] = PaperAccount(**fields["account"])
    if not 0 < committed_ns < fields["plan"].opens_ns:
        raise WindowRefused("Window seal does not precede its opening")
    return RegisteredWindow(**fields, registered_ns=committed_ns, revision=revision)


def _read(database: Connection, window_id: str) -> RegisteredWindow | None:
    if not database.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='paper_operating_window'"
    ).fetchone():
        return None
    row = database.execute(
        """SELECT contract,committed_ns,revision FROM paper_operating_window
           WHERE window_id=? AND revision IS NOT NULL""",
        (window_id,),
    ).fetchone()
    if row is None:
        return None
    receipt = _decode(*row)
    if (receipt.company_id, receipt.plan.window_id) != (_identity(database), window_id):
        raise WindowRefused("Window evidence belongs to another company or request")
    return receipt


def read_window(
    store: Path, company_id: str, window_id: str
) -> RegisteredWindow | None:
    with closing(_connect(store, "ro")) as database:
        database.execute("BEGIN")
        if _identity(database) != company_id:
            raise WindowRefused("Window belongs to another company")
        return _read(database, window_id)


def register_window(writer: WriterLease, plan: WindowPlan) -> RegisteredWindow:
    writer.verify()
    with closing(_connect(writer.store, "rw")) as database, database:
        database.execute("BEGIN IMMEDIATE")
        writer.verify()
        if _identity(database) != writer.company_id:
            raise WindowRefused("Window belongs to another company")
        existing = _read(database, plan.window_id)
        if existing is not None:
            if existing.plan != plan:
                raise WindowRefused(
                    "Window request conflicts with its original registration"
                )
            return existing
        binding = reopen_binding(writer.store, writer.company_id)
        if (
            binding.revision != plan.binding_revision
            or binding.account.environment != "paper"
            or plan.max_exposure_minor > binding.exposure_limit_minor_units
        ):
            raise WindowRefused("Window does not fit the existing paper mandate")
        if not 0 < time_ns() < plan.opens_ns:
            raise WindowRefused("Register the window before it opens")
        database.execute(
            """CREATE TABLE IF NOT EXISTS paper_operating_window (
                window_id TEXT PRIMARY KEY NOT NULL, contract TEXT NOT NULL,
                committed_ns INTEGER, revision TEXT UNIQUE,
                CHECK ((committed_ns IS NULL) = (revision IS NULL))
            )"""
        )
        contract = json.dumps(
            {
                "company_id": writer.company_id,
                "plan": asdict(plan),
                "account": asdict(binding.account),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        database.execute(
            """INSERT INTO paper_operating_window (window_id,contract) VALUES (?,?)
               ON CONFLICT(window_id) DO NOTHING""",
            (plan.window_id, contract),
        )
        if database.execute(
            "SELECT contract FROM paper_operating_window WHERE window_id=?",
            (plan.window_id,),
        ).fetchone() != (contract,):
            raise WindowRefused(
                "Window request conflicts with its original registration"
            )
        writer.verify()
        database.commit()
        committed_ns = time_ns()
        if not 0 < committed_ns < plan.opens_ns:
            raise WindowRefused("Window opened before the registration committed")

        database.execute("BEGIN IMMEDIATE")
        writer.verify()
        existing = _read(database, plan.window_id)
        if existing is not None:
            return existing
        if reopen_binding(writer.store, writer.company_id) != binding:
            raise WindowRefused("Paper binding changed before the window was sealed")
        if database.execute(
            """SELECT 1 FROM paper_operating_window WHERE revision IS NOT NULL
               AND json_extract(contract,'$.account.destination_id')=?
               AND json_extract(contract,'$.plan.opens_ns')<=?
               AND json_extract(contract,'$.plan.closes_ns')>=? LIMIT 1""",
            (binding.account.destination_id, plan.closes_ns, plan.opens_ns),
        ).fetchone():
            raise WindowRefused("The paper account already has an overlapping window")
        revision = _seal_revision(contract, committed_ns)
        database.execute(
            """UPDATE paper_operating_window SET committed_ns=?,revision=?
               WHERE window_id=? AND revision IS NULL""",
            (committed_ns, revision, plan.window_id),
        )
        writer.verify()
    return RegisteredWindow(
        writer.company_id, plan, binding.account, committed_ns, revision
    )
