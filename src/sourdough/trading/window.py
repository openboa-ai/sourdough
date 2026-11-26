"""Seal the first paper operating plan before its observation window."""

import json
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from sqlite3 import Connection
from time import time_ns

from ..company import _identity
from ..duty_run import count_company_runs_in_tx
from .execution import PaperAccount, read_current_binding
from ..storage.db import open_reader, open_writer, read_tx, write_tx
from ..storage.kernel import append_audit_event
from ..storage.schema import is_domain_installed, require_domain
from ..writer_lease import WriterLease


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
    agent_turn_limit: int | None = None

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
            isinstance(pin, str) and 0 < len(pin) <= 128 and bool(pin.strip())
            for pin in pins
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
            or (
                self.agent_turn_limit is not None
                and (
                    type(self.agent_turn_limit) is not int
                    or not 0 <= self.agent_turn_limit < 2**31
                )
            )
        ):
            raise WindowRefused("Window has inconsistent timing, limits or coverage")


@dataclass(frozen=True)
class RegisteredWindow:
    company_id: str
    plan: WindowPlan
    account: PaperAccount
    registered_ns: int
    revision: str
    run_count_at_seal: int | None = None

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
    count = fields.get("run_count_at_seal")
    if (fields["plan"].agent_turn_limit is None and count is not None) or (
        fields["plan"].agent_turn_limit is not None
        and (type(count) is not int or count < 0)
    ):
        raise WindowRefused("Window has no valid sealed turn baseline")
    if not 0 < committed_ns < fields["plan"].opens_ns:
        raise WindowRefused("Window seal does not precede its opening")
    return RegisteredWindow(**fields, registered_ns=committed_ns, revision=revision)


def _read(database: Connection, window_id: str) -> RegisteredWindow | None:
    if not is_domain_installed(database, "trading"):
        return None
    row = database.execute(
        """SELECT contract,committed_ns,revision FROM trading_operating_window
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
    with closing(open_reader(store)) as database, read_tx(database):
        if _identity(database) != company_id:
            raise WindowRefused("Window belongs to another company")
        return _read(database, window_id)


def register_window(writer: WriterLease, plan: WindowPlan) -> RegisteredWindow:
    writer.verify()
    with closing(open_writer(writer.store, lease=writer)) as database:
        with write_tx(database):
            writer.verify()
            if _identity(database) != writer.company_id:
                raise WindowRefused("Window belongs to another company")
            require_domain(database, "trading")
            existing = _read(database, plan.window_id)
            if existing is not None:
                if existing.plan != plan:
                    raise WindowRefused(
                        "Window request conflicts with its original registration"
                    )
                return existing
            binding = read_current_binding(database, writer.company_id)
            if (
                binding.revision != plan.binding_revision
                or binding.account.environment != "paper"
                or plan.max_exposure_minor > binding.exposure_limit_minor_units
            ):
                raise WindowRefused("Window does not fit the existing paper mandate")
            if not 0 < time_ns() < plan.opens_ns:
                raise WindowRefused("Register the window before it opens")
            if database.execute(
                """SELECT 1 FROM trading_operating_window WHERE revision IS NOT NULL
                   AND json_extract(contract,'$.account.destination_id')=?
                   AND json_extract(contract,'$.plan.opens_ns')<=?
                   AND json_extract(contract,'$.plan.closes_ns')>=? LIMIT 1""",
                (binding.account.destination_id, plan.closes_ns, plan.opens_ns),
            ).fetchone():
                raise WindowRefused(
                    "The paper account already has an overlapping window"
                )
            plan_values = asdict(plan)
            fields = {
                "company_id": writer.company_id,
                "plan": plan_values,
                "account": asdict(binding.account),
            }
            if plan.agent_turn_limit is None:
                del plan_values["agent_turn_limit"]
            else:
                fields["run_count_at_seal"] = count_company_runs_in_tx(
                    database, writer.company_id
                )
            contract = json.dumps(fields, sort_keys=True, separators=(",", ":"))
            database.execute(
                """INSERT INTO trading_operating_window (window_id,contract) VALUES (?,?)
                   ON CONFLICT(window_id) DO NOTHING""",
                (plan.window_id, contract),
            )
            if database.execute(
                "SELECT contract FROM trading_operating_window WHERE window_id=?",
                (plan.window_id,),
            ).fetchone() != (contract,):
                raise WindowRefused(
                    "Window request conflicts with its original registration"
                )
            writer.verify()

        committed_ns = time_ns()
        if not 0 < committed_ns < plan.opens_ns:
            raise WindowRefused("Window opened before the registration committed")

        with write_tx(database):
            writer.verify()
            existing = _read(database, plan.window_id)
            if existing is not None:
                return existing
            if read_current_binding(database, writer.company_id) != binding:
                raise WindowRefused(
                    "Paper binding changed before the window was sealed"
                )
            if database.execute(
                """SELECT 1 FROM trading_operating_window WHERE revision IS NOT NULL
                   AND json_extract(contract,'$.account.destination_id')=?
                   AND json_extract(contract,'$.plan.opens_ns')<=?
                   AND json_extract(contract,'$.plan.closes_ns')>=? LIMIT 1""",
                (binding.account.destination_id, plan.closes_ns, plan.opens_ns),
            ).fetchone():
                raise WindowRefused(
                    "The paper account already has an overlapping window"
                )
            revision = _seal_revision(contract, committed_ns)
            updated = database.execute(
                """UPDATE trading_operating_window SET committed_ns=?,revision=?
                   WHERE window_id=? AND contract=? AND revision IS NULL""",
                (committed_ns, revision, plan.window_id, contract),
            )
            if updated.rowcount != 1:
                raise WindowRefused("Window registration state changed before sealing")
            append_audit_event(
                database,
                company_id=writer.company_id,
                domain="trading",
                entity_type="trading_operating_window",
                entity_id=plan.window_id,
                event_type="trading.window_sealed",
                actor_id="trading_window",
                payload={
                    "window_id": plan.window_id,
                    "binding_revision": plan.binding_revision,
                    "opens_ns": plan.opens_ns,
                    "closes_ns": plan.closes_ns,
                    "revision": revision,
                },
                recorded_at_ns=committed_ns,
            )
            writer.verify()
    return _decode(contract, committed_ns, revision)
