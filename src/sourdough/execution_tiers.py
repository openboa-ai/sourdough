"""Three-tier execution accounts, isolated per-Agent paper sandboxes, and live promotion gate.

Owns:
- execution_environment_binding
- agent_sandbox_order
- agent_sandbox_fill
- agent_sandbox_position
- venue_qualification_receipt
- environment_promotion_record

Strict invariants:
1. Tier 1 (agent_sandbox) writes exclusively to agent_sandbox_* tables and never
   touches company investment books (paper_order/paper_fill), company risk limits,
   or general ledger trading entries.
2. Tier 2 (company_paper) is the default company-wide simulated execution tier.
3. Tier 3 (company_live) can activate ONLY via board_control promote_execution_environment
   after verified company_paper soak evidence, a matching PASS external_test
   venue_qualification_receipt, opaque live credential handle isolation, and exact
   expected_payload_hash verification. Activating company_live keeps Tier 1 and Tier 2
   active in parallel.
"""

from __future__ import annotations

import json
import re
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from sqlite3 import Connection
from time import time_ns
from typing import Any
from uuid import uuid4

from .board_control import (
    BOARD_CONTROL_ROLE,
    BoardCommandOutcome,
    BoardControlConflict,
    BoardControlForbidden,
    BoardControlRefused,
    record_decision_question,
    resolve_board_access,
)
from .company import _identity
from .duty_run import employment_cutoff
from .paper_risk import AuthorizeBoard
from .storage.db import open_reader, open_writer, read_tx, write_tx
from .storage.kernel import (
    CommandConflict,
    admit_command_receipt,
    append_audit_event,
)
from .storage.schema import enable_domain_v1, is_domain_installed
from .writer_lease import WriterLease

TIER_AGENT_SANDBOX = "agent_sandbox"
TIER_COMPANY_PAPER = "company_paper"
TIER_COMPANY_LIVE = "company_live"
VALID_TIERS = frozenset({TIER_AGENT_SANDBOX, TIER_COMPANY_PAPER, TIER_COMPANY_LIVE})

ENV_PAPER = "paper"
ENV_EXTERNAL_TEST = "external_test"
ENV_LIVE = "live"
VALID_ENVIRONMENTS = frozenset({ENV_PAPER, ENV_EXTERNAL_TEST, ENV_LIVE})

EXECUTION_TIER_COMMAND = "execution-tier-command-v1"
PROMOTE_EXECUTION_ENVIRONMENT = "promote_execution_environment"
DEMOTE_LIVE_ENVIRONMENT = "demote_live_environment"

_MAX_I64 = 2**63 - 1
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CREDENTIAL_HANDLE_RE = re.compile(
    r"^(?:keychain|envref):[A-Za-z0-9][A-Za-z0-9._:/-]{2,120}$"
)
_NON_LIVE_HANDLE_MARKERS = ("paper", "testnet", "sandbox", "mock", "demo")
_CORE_DIGEST_FILES = (
    "paper_account.py",
    "paper_order.py",
    "paper_books.py",
    "paper_reconcile.py",
    "paper_risk.py",
    "execution_tiers.py",
)


class ExecutionTierRefused(Exception):
    """A three-tier execution account, sandbox trade, or qualification call was refused."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_json(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_safe_id(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or ".." in value
        or _SAFE_ID_RE.fullmatch(value) is None
    ):
        raise ExecutionTierRefused(f"Invalid {label}")
    return value


def _require_text(value: Any, label: str, *, max_len: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_len
        or "\x00" in value
    ):
        raise ExecutionTierRefused(f"Invalid {label}")
    return value.strip()


def _require_sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise ExecutionTierRefused(f"{label} must be a 64-character lowercase SHA-256")
    return value


def _require_positive_int(value: Any, label: str) -> int:
    if type(value) is not int or not 0 < value <= _MAX_I64:
        raise ExecutionTierRefused(f"{label} must be a positive integer")
    return value


def _require_bps(value: Any, label: str, *, upper: int) -> int:
    if type(value) is not int or not 0 <= value <= upper:
        raise ExecutionTierRefused(f"{label} must be an integer between 0 and {upper}")
    return value


def _validate_credential_handle(
    handle: Any, *, environment: str, required: bool
) -> str | None:
    if handle is None:
        if required:
            raise ExecutionTierRefused(
                f"Opaque credential_handle_ref is required for {environment} bindings"
            )
        return None
    if not isinstance(handle, str) or _CREDENTIAL_HANDLE_RE.fullmatch(handle) is None:
        raise ExecutionTierRefused(
            "credential_handle_ref must be an opaque 'keychain:...' or 'envref:...' handle (never a raw secret)"
        )
    if environment == ENV_LIVE:
        lowered = handle.lower()
        if any(marker in lowered for marker in _NON_LIVE_HANDLE_MARKERS):
            raise ExecutionTierRefused(
                "Live binding cannot reference a paper, testnet, or sandbox credential handle"
            )
    return handle


def compute_trading_core_digest() -> str:
    """Compute a deterministic SHA-256 digest over the shared trading core source modules."""
    root = Path(__file__).resolve().parent
    digest = sha256()
    for filename in _CORE_DIGEST_FILES:
        path = root / filename
        digest.update(filename.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(path.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


@dataclass(frozen=True)
class ExecutionBinding:
    binding_id: str
    company_id: str
    tier: str
    environment: str
    owner_agent_id: str | None
    account_id: str
    destination_id: str
    connector_revision: str
    instrument_rules_digest: str
    currency: str
    starting_minor_units: int
    exposure_limit_minor_units: int
    fee_bps: int
    slippage_bps: int
    quote_source: str
    credential_handle_ref: str | None
    status: str
    created_at_ns: int
    updated_at_ns: int


@dataclass(frozen=True)
class SandboxOrderReceipt:
    order_id: str
    binding_id: str
    agent_id: str
    action_id: str
    instrument_id: str
    side: str
    quantity: int
    limit_minor_units: int
    market_price_minor_units: int
    status: str
    filled_quantity: int
    fill_price_minor_units: int | None
    fee_minor_units: int
    fill_id: str | None
    realized_pnl_minor_units: int
    replayed: bool = False


@dataclass(frozen=True)
class VenueQualificationReceipt:
    receipt_id: str
    company_id: str
    adapter_revision: str
    environment: str
    destination_id: str
    instrument_rules_digest: str
    core_digest: str
    verdict: str
    cases_passed: int
    cases_failed: int
    cases_unrun: int
    evidence_sha256: str
    recorded_at_ns: int


def _row_to_binding(row: tuple[Any, ...]) -> ExecutionBinding:
    return ExecutionBinding(
        binding_id=str(row[0]),
        company_id=str(row[1]),
        tier=str(row[2]),
        environment=str(row[3]),
        owner_agent_id=None if row[4] is None else str(row[4]),
        account_id=str(row[5]),
        destination_id=str(row[6]),
        connector_revision=str(row[7]),
        instrument_rules_digest=str(row[8]),
        currency=str(row[9]),
        starting_minor_units=int(row[10]),
        exposure_limit_minor_units=int(row[11]),
        fee_bps=int(row[12]),
        slippage_bps=int(row[13]),
        quote_source=str(row[14]),
        credential_handle_ref=None if row[15] is None else str(row[15]),
        status=str(row[16]),
        created_at_ns=int(row[17]),
        updated_at_ns=int(row[18]),
    )


_SELECT_BINDING_COLS = """SELECT binding_id, company_id, tier, environment, owner_agent_id,
       account_id, destination_id, connector_revision, instrument_rules_digest,
       currency, starting_minor_units, exposure_limit_minor_units, fee_bps,
       slippage_bps, quote_source, credential_handle_ref, status,
       created_at_ns, updated_at_ns
FROM execution_environment_binding"""


def _assert_active_agent(db: Connection, company_id: str, agent_id: str) -> None:
    row = db.execute(
        "SELECT 1 FROM agent WHERE id = ? AND company_id = ?",
        (agent_id, company_id),
    ).fetchone()
    if row is None:
        raise ExecutionTierRefused("Agent does not belong to this company")
    if employment_cutoff(db, agent_id) is not None:
        raise ExecutionTierRefused("Retired agent cannot operate a paper sandbox")


def _compute_sandbox_cash_and_exposure(
    db: Connection, binding: ExecutionBinding
) -> tuple[int, int]:
    """Return (available_cash_minor_units, gross_position_exposure_minor_units)."""
    cash = binding.starting_minor_units
    for side, qty, price, fee in db.execute(
        """SELECT side, quantity, price_minor_units, fee_minor_units
           FROM agent_sandbox_fill
           WHERE binding_id = ?""",
        (binding.binding_id,),
    ).fetchall():
        notional = int(qty) * int(price)
        if side == "buy":
            cash -= notional + int(fee)
        else:
            cash += notional - int(fee)

    gross_exposure = 0
    for qty, avg_entry in db.execute(
        """SELECT quantity, avg_entry_minor_units
           FROM agent_sandbox_position
           WHERE binding_id = ? AND quantity != 0""",
        (binding.binding_id,),
    ).fetchall():
        gross_exposure += abs(int(qty)) * int(avg_entry)
    return cash, gross_exposure


# ---------------------------------------------------------------------------
# Tier 1: Per-Agent Isolated Paper Sandbox
# ---------------------------------------------------------------------------


def open_agent_sandbox(
    writer: WriterLease,
    *,
    agent_id: str,
    account_id: str,
    currency: str = "USD",
    starting_minor_units: int = 10_000_000,
    exposure_limit_minor_units: int | None = None,
    fee_bps: int = 4,
    slippage_bps: int = 2,
    quote_source: str = "live_read_only",
    destination_id: str | None = None,
    connector_revision: str = "sourdough-agent-sandbox-v1",
    instrument_rules_digest: str = "spot-default-v1",
    binding_id: str | None = None,
) -> ExecutionBinding:
    """Open an isolated Tier 1 paper sandbox account for one active Agent."""
    writer.verify()
    _require_safe_id(agent_id, "agent_id")
    _require_safe_id(account_id, "account_id")
    _require_text(currency, "currency", max_len=32)
    _require_positive_int(starting_minor_units, "starting_minor_units")
    limit = (
        starting_minor_units
        if exposure_limit_minor_units is None
        else _require_positive_int(
            exposure_limit_minor_units, "exposure_limit_minor_units"
        )
    )
    if limit > starting_minor_units:
        raise ExecutionTierRefused(
            "exposure_limit_minor_units cannot exceed starting_minor_units"
        )
    _require_bps(fee_bps, "fee_bps", upper=1000)
    _require_bps(slippage_bps, "slippage_bps", upper=500)
    _require_text(quote_source, "quote_source", max_len=64)
    resolved_dest = (
        f"sandbox:{agent_id}:{account_id}"
        if destination_id is None
        else _require_safe_id(destination_id, "destination_id")
    )
    _require_text(connector_revision, "connector_revision", max_len=128)
    _require_text(instrument_rules_digest, "instrument_rules_digest", max_len=128)
    resolved_id = (
        f"sbx-{uuid4().hex[:16]}"
        if binding_id is None
        else _require_safe_id(binding_id, "binding_id")
    )

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        enable_domain_v1(db, "paper", company_id=writer.company_id, actor_id=agent_id)
        _assert_active_agent(db, writer.company_id, agent_id)

        existing = db.execute(
            f"""{_SELECT_BINDING_COLS}
                WHERE company_id = ? AND tier = ? AND owner_agent_id = ? AND status = 'active'""",
            (writer.company_id, TIER_AGENT_SANDBOX, agent_id),
        ).fetchone()
        if existing is not None:
            prior = _row_to_binding(existing)
            if (
                prior.account_id == account_id
                and prior.destination_id == resolved_dest
                and prior.currency == currency
                and prior.starting_minor_units == starting_minor_units
                and prior.exposure_limit_minor_units == limit
                and prior.fee_bps == fee_bps
                and prior.slippage_bps == slippage_bps
                and prior.quote_source == quote_source
            ):
                return prior
            raise ExecutionTierRefused(
                "Agent already has an active sandbox account; call reset_agent_sandbox first"
            )

        now = time_ns()
        db.execute(
            """INSERT INTO execution_environment_binding (
                   binding_id, company_id, tier, environment, owner_agent_id,
                   account_id, destination_id, connector_revision,
                   instrument_rules_digest, currency, starting_minor_units,
                   exposure_limit_minor_units, fee_bps, slippage_bps,
                   quote_source, credential_handle_ref, status,
                   created_at_ns, updated_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'active', ?, ?)""",
            (
                resolved_id,
                writer.company_id,
                TIER_AGENT_SANDBOX,
                ENV_PAPER,
                agent_id,
                account_id,
                resolved_dest,
                connector_revision,
                instrument_rules_digest,
                currency,
                starting_minor_units,
                limit,
                fee_bps,
                slippage_bps,
                quote_source,
                now,
                now,
            ),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="agent_sandbox",
            entity_type="execution_environment_binding",
            entity_id=resolved_id,
            event_type="agent_sandbox.opened",
            actor_id=agent_id,
            payload={
                "binding_id": resolved_id,
                "agent_id": agent_id,
                "account_id": account_id,
                "starting_minor_units": starting_minor_units,
                "fee_bps": fee_bps,
                "slippage_bps": slippage_bps,
                "quote_source": quote_source,
            },
            recorded_at_ns=now,
        )
        writer.verify()
        return ExecutionBinding(
            binding_id=resolved_id,
            company_id=writer.company_id,
            tier=TIER_AGENT_SANDBOX,
            environment=ENV_PAPER,
            owner_agent_id=agent_id,
            account_id=account_id,
            destination_id=resolved_dest,
            connector_revision=connector_revision,
            instrument_rules_digest=instrument_rules_digest,
            currency=currency,
            starting_minor_units=starting_minor_units,
            exposure_limit_minor_units=limit,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            quote_source=quote_source,
            credential_handle_ref=None,
            status="active",
            created_at_ns=now,
            updated_at_ns=now,
        )


def reset_agent_sandbox(
    writer: WriterLease,
    *,
    agent_id: str,
    new_account_id: str,
    currency: str = "USD",
    starting_minor_units: int = 10_000_000,
    exposure_limit_minor_units: int | None = None,
    fee_bps: int = 4,
    slippage_bps: int = 2,
    quote_source: str = "live_read_only",
) -> ExecutionBinding:
    """Retire the Agent's active sandbox binding (preserving immutable history) and open a fresh one."""
    writer.verify()
    _require_safe_id(agent_id, "agent_id")
    _require_safe_id(new_account_id, "new_account_id")
    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        if is_domain_installed(db, "paper"):
            _assert_active_agent(db, writer.company_id, agent_id)
            now = time_ns()
            active = db.execute(
                """SELECT binding_id, account_id FROM execution_environment_binding
                   WHERE company_id = ? AND tier = ? AND owner_agent_id = ? AND status = 'active'""",
                (writer.company_id, TIER_AGENT_SANDBOX, agent_id),
            ).fetchone()
            if active is not None:
                if str(active[1]) == new_account_id:
                    raise ExecutionTierRefused(
                        "Resetting an agent sandbox requires a new account_id"
                    )
                db.execute(
                    """UPDATE execution_environment_binding
                       SET status = 'retired', updated_at_ns = ?
                       WHERE binding_id = ?""",
                    (now, str(active[0])),
                )
                append_audit_event(
                    db,
                    company_id=writer.company_id,
                    domain="agent_sandbox",
                    entity_type="execution_environment_binding",
                    entity_id=str(active[0]),
                    event_type="agent_sandbox.retired",
                    actor_id=agent_id,
                    payload={"retired_binding_id": str(active[0])},
                    recorded_at_ns=now,
                )
    return open_agent_sandbox(
        writer,
        agent_id=agent_id,
        account_id=new_account_id,
        currency=currency,
        starting_minor_units=starting_minor_units,
        exposure_limit_minor_units=exposure_limit_minor_units,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        quote_source=quote_source,
    )


def submit_agent_sandbox_order(
    writer: WriterLease,
    *,
    agent_id: str,
    action_id: str,
    instrument_id: str,
    side: str,
    quantity: int,
    limit_minor_units: int,
    market_price_minor_units: int,
    quote_observed_at_ns: int | None = None,
    fill_mode: str = "immediate",
) -> SandboxOrderReceipt:
    """Submit a simulated order strictly inside an Agent's isolated Tier 1 sandbox."""
    writer.verify()
    _require_safe_id(agent_id, "agent_id")
    _require_safe_id(action_id, "action_id")
    _require_safe_id(instrument_id, "instrument_id")
    if side not in ("buy", "sell"):
        raise ExecutionTierRefused("side must be 'buy' or 'sell'")
    if fill_mode not in ("immediate", "resting"):
        raise ExecutionTierRefused("fill_mode must be 'immediate' or 'resting'")
    _require_positive_int(quantity, "quantity")
    _require_positive_int(limit_minor_units, "limit_minor_units")
    _require_positive_int(market_price_minor_units, "market_price_minor_units")
    observed_ns = time_ns() if quote_observed_at_ns is None else quote_observed_at_ns
    _require_positive_int(observed_ns, "quote_observed_at_ns")

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        if not is_domain_installed(db, "paper"):
            raise ExecutionTierRefused("Agent sandbox is not opened")
        _assert_active_agent(db, writer.company_id, agent_id)

        binding_row = db.execute(
            f"""{_SELECT_BINDING_COLS}
                WHERE company_id = ? AND tier = ? AND owner_agent_id = ? AND status = 'active'""",
            (writer.company_id, TIER_AGENT_SANDBOX, agent_id),
        ).fetchone()
        if binding_row is None:
            raise ExecutionTierRefused("Agent has no active sandbox account")
        binding = _row_to_binding(binding_row)

        existing = db.execute(
            """SELECT order_id, instrument_id, side, quantity, limit_minor_units,
                      market_price_minor_units, status, filled_quantity,
                      fill_price_minor_units, fee_minor_units
               FROM agent_sandbox_order
               WHERE binding_id = ? AND action_id = ?""",
            (binding.binding_id, action_id),
        ).fetchone()
        if existing is not None:
            if (
                str(existing[1]),
                str(existing[2]),
                int(existing[3]),
                int(existing[4]),
            ) != (instrument_id, side, quantity, limit_minor_units):
                raise ExecutionTierRefused(
                    "Sandbox action_id conflicts with previously submitted order terms"
                )
            fill_row = db.execute(
                """SELECT fill_id, realized_pnl_minor_units
                   FROM agent_sandbox_fill
                   WHERE order_id = ?""",
                (str(existing[0]),),
            ).fetchone()
            return SandboxOrderReceipt(
                order_id=str(existing[0]),
                binding_id=binding.binding_id,
                agent_id=agent_id,
                action_id=action_id,
                instrument_id=instrument_id,
                side=side,
                quantity=quantity,
                limit_minor_units=limit_minor_units,
                market_price_minor_units=int(existing[5]),
                status=str(existing[6]),
                filled_quantity=int(existing[7]),
                fill_price_minor_units=None
                if existing[8] is None
                else int(existing[8]),
                fee_minor_units=int(existing[9]),
                fill_id=None if fill_row is None else str(fill_row[0]),
                realized_pnl_minor_units=0 if fill_row is None else int(fill_row[1]),
                replayed=True,
            )

        # Adverse slippage calculation (realistic simulation).
        if side == "buy":
            slipped_price = (
                market_price_minor_units
                + (market_price_minor_units * binding.slippage_bps + 9_999) // 10_000
            )
            crosses_limit = slipped_price <= limit_minor_units
        else:
            slipped_price = max(
                1,
                market_price_minor_units
                - (market_price_minor_units * binding.slippage_bps) // 10_000,
            )
            crosses_limit = slipped_price >= limit_minor_units

        cash_available, gross_exposure = _compute_sandbox_cash_and_exposure(db, binding)
        pos_row = db.execute(
            """SELECT quantity, avg_entry_minor_units, realized_pnl_minor_units, total_fees_minor_units
               FROM agent_sandbox_position
               WHERE binding_id = ? AND instrument_id = ?""",
            (binding.binding_id, instrument_id),
        ).fetchone()
        cur_qty = int(pos_row[0]) if pos_row else 0
        cur_avg = int(pos_row[1]) if pos_row else 0
        cur_realized = int(pos_row[2]) if pos_row else 0
        cur_fees = int(pos_row[3]) if pos_row else 0

        now = time_ns()
        order_id = f"sbxo-{uuid4().hex[:16]}"
        should_fill = fill_mode == "immediate" and crosses_limit

        if should_fill:
            notional = quantity * slipped_price
            fee = (notional * binding.fee_bps + 5_000) // 10_000
            delta = quantity if side == "buy" else -quantity
            new_qty = cur_qty + delta
            other_exposure = gross_exposure - abs(cur_qty) * cur_avg

            realized_delta = 0
            if (
                cur_qty == 0
                or (cur_qty > 0 and delta > 0)
                or (cur_qty < 0 and delta < 0)
            ):
                # Opening or adding to position.
                total_cost = abs(cur_qty) * cur_avg + quantity * slipped_price
                new_avg = total_cost // abs(new_qty)
            else:
                # Reducing, closing, or flipping position.
                closing_qty = min(abs(cur_qty), quantity)
                if cur_qty > 0:
                    realized_delta = (slipped_price - cur_avg) * closing_qty
                else:
                    realized_delta = (cur_avg - slipped_price) * closing_qty
                if new_qty == 0:
                    new_avg = 0
                elif (cur_qty > 0 and new_qty > 0) or (cur_qty < 0 and new_qty < 0):
                    new_avg = cur_avg
                else:
                    new_avg = slipped_price

            projected_exposure = other_exposure + abs(new_qty) * new_avg
            if projected_exposure > binding.exposure_limit_minor_units:
                raise ExecutionTierRefused(
                    "Sandbox order exceeds agent sandbox exposure limit"
                )
            if side == "buy" and notional + fee > cash_available:
                raise ExecutionTierRefused(
                    "Sandbox order exceeds available virtual cash"
                )
            realized_net = realized_delta - fee
            fill_id = f"sbxf-{uuid4().hex[:16]}"
            db.execute(
                """INSERT INTO agent_sandbox_order (
                       order_id, binding_id, company_id, agent_id, action_id,
                       instrument_id, side, quantity, limit_minor_units,
                       market_price_minor_units, quote_observed_at_ns, quote_source,
                       fee_bps, slippage_bps, status, filled_quantity,
                       fill_price_minor_units, fee_minor_units, created_at_ns, updated_at_ns
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?, ?, ?, ?)""",
                (
                    order_id,
                    binding.binding_id,
                    writer.company_id,
                    agent_id,
                    action_id,
                    instrument_id,
                    side,
                    quantity,
                    limit_minor_units,
                    market_price_minor_units,
                    observed_ns,
                    binding.quote_source,
                    binding.fee_bps,
                    binding.slippage_bps,
                    quantity,
                    slipped_price,
                    fee,
                    now,
                    now,
                ),
            )
            db.execute(
                """INSERT INTO agent_sandbox_fill (
                       fill_id, order_id, binding_id, company_id, agent_id,
                       instrument_id, side, quantity, price_minor_units,
                       fee_minor_units, realized_pnl_minor_units, filled_at_ns
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fill_id,
                    order_id,
                    binding.binding_id,
                    writer.company_id,
                    agent_id,
                    instrument_id,
                    side,
                    quantity,
                    slipped_price,
                    fee,
                    realized_net,
                    now,
                ),
            )
            db.execute(
                """INSERT INTO agent_sandbox_position (
                       binding_id, company_id, agent_id, instrument_id,
                       quantity, avg_entry_minor_units, realized_pnl_minor_units,
                       total_fees_minor_units, updated_at_ns
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (binding_id, instrument_id) DO UPDATE SET
                       quantity = excluded.quantity,
                       avg_entry_minor_units = excluded.avg_entry_minor_units,
                       realized_pnl_minor_units = excluded.realized_pnl_minor_units,
                       total_fees_minor_units = excluded.total_fees_minor_units,
                       updated_at_ns = excluded.updated_at_ns""",
                (
                    binding.binding_id,
                    writer.company_id,
                    agent_id,
                    instrument_id,
                    new_qty,
                    new_avg,
                    cur_realized + realized_net,
                    cur_fees + fee,
                    now,
                ),
            )
            append_audit_event(
                db,
                company_id=writer.company_id,
                domain="agent_sandbox",
                entity_type="agent_sandbox_fill",
                entity_id=fill_id,
                event_type="agent_sandbox.order_filled",
                actor_id=agent_id,
                payload={
                    "order_id": order_id,
                    "fill_id": fill_id,
                    "binding_id": binding.binding_id,
                    "instrument_id": instrument_id,
                    "side": side,
                    "quantity": quantity,
                    "price_minor_units": slipped_price,
                    "fee_minor_units": fee,
                },
                recorded_at_ns=now,
            )
            writer.verify()
            return SandboxOrderReceipt(
                order_id=order_id,
                binding_id=binding.binding_id,
                agent_id=agent_id,
                action_id=action_id,
                instrument_id=instrument_id,
                side=side,
                quantity=quantity,
                limit_minor_units=limit_minor_units,
                market_price_minor_units=market_price_minor_units,
                status="filled",
                filled_quantity=quantity,
                fill_price_minor_units=slipped_price,
                fee_minor_units=fee,
                fill_id=fill_id,
                realized_pnl_minor_units=realized_net,
                replayed=False,
            )

        # Resting open limit order (did not cross slipped market price or fill_mode == 'resting').
        if side == "buy" and quantity * limit_minor_units > cash_available:
            raise ExecutionTierRefused(
                "Sandbox limit order exceeds available virtual cash"
            )
        db.execute(
            """INSERT INTO agent_sandbox_order (
                   order_id, binding_id, company_id, agent_id, action_id,
                   instrument_id, side, quantity, limit_minor_units,
                   market_price_minor_units, quote_observed_at_ns, quote_source,
                   fee_bps, slippage_bps, status, filled_quantity,
                   fill_price_minor_units, fee_minor_units, created_at_ns, updated_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 0, NULL, 0, ?, ?)""",
            (
                order_id,
                binding.binding_id,
                writer.company_id,
                agent_id,
                action_id,
                instrument_id,
                side,
                quantity,
                limit_minor_units,
                market_price_minor_units,
                observed_ns,
                binding.quote_source,
                binding.fee_bps,
                binding.slippage_bps,
                now,
                now,
            ),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="agent_sandbox",
            entity_type="agent_sandbox_order",
            entity_id=order_id,
            event_type="agent_sandbox.order_opened",
            actor_id=agent_id,
            payload={
                "order_id": order_id,
                "binding_id": binding.binding_id,
                "instrument_id": instrument_id,
                "side": side,
                "quantity": quantity,
                "limit_minor_units": limit_minor_units,
            },
            recorded_at_ns=now,
        )
        writer.verify()
        return SandboxOrderReceipt(
            order_id=order_id,
            binding_id=binding.binding_id,
            agent_id=agent_id,
            action_id=action_id,
            instrument_id=instrument_id,
            side=side,
            quantity=quantity,
            limit_minor_units=limit_minor_units,
            market_price_minor_units=market_price_minor_units,
            status="open",
            filled_quantity=0,
            fill_price_minor_units=None,
            fee_minor_units=0,
            fill_id=None,
            realized_pnl_minor_units=0,
            replayed=False,
        )


def cancel_agent_sandbox_order(
    writer: WriterLease,
    *,
    agent_id: str,
    order_id: str,
) -> dict[str, Any]:
    """Cancel an open order strictly within the owning Agent's Tier 1 sandbox."""
    writer.verify()
    _require_safe_id(agent_id, "agent_id")
    _require_safe_id(order_id, "order_id")
    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        if not is_domain_installed(db, "paper"):
            raise ExecutionTierRefused("Agent sandbox order not found")
        row = db.execute(
            """SELECT binding_id, agent_id, status
               FROM agent_sandbox_order
               WHERE company_id = ? AND order_id = ?""",
            (writer.company_id, order_id),
        ).fetchone()
        if row is None or str(row[1]) != agent_id:
            raise ExecutionTierRefused("Sandbox order does not belong to this agent")
        status = str(row[2])
        if status == "cancelled":
            return {"order_id": order_id, "status": "cancelled", "replayed": True}
        if status != "open":
            raise ExecutionTierRefused(
                f"Cannot cancel sandbox order in terminal state '{status}'"
            )
        now = time_ns()
        db.execute(
            """UPDATE agent_sandbox_order
               SET status = 'cancelled', updated_at_ns = ?
               WHERE order_id = ?""",
            (now, order_id),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="agent_sandbox",
            entity_type="agent_sandbox_order",
            entity_id=order_id,
            event_type="agent_sandbox.order_cancelled",
            actor_id=agent_id,
            payload={"order_id": order_id, "binding_id": str(row[0])},
            recorded_at_ns=now,
        )
        writer.verify()
        return {"order_id": order_id, "status": "cancelled", "replayed": False}


def read_agent_sandbox_portfolio_from_db(
    db: Connection, company_id: str, agent_id: str
) -> dict[str, Any] | None:
    """Read an Agent's active Tier 1 sandbox portfolio inside an existing read transaction."""
    if not is_domain_installed(db, "paper"):
        return None
    row = db.execute(
        f"""{_SELECT_BINDING_COLS}
            WHERE company_id = ? AND tier = ? AND owner_agent_id = ? AND status = 'active'""",
        (company_id, TIER_AGENT_SANDBOX, agent_id),
    ).fetchone()
    if row is None:
        return None
    binding = _row_to_binding(row)
    cash_available, gross_exposure = _compute_sandbox_cash_and_exposure(db, binding)
    positions = [
        {
            "instrument_id": str(r[0]),
            "quantity": int(r[1]),
            "avg_entry_minor_units": int(r[2]),
            "realized_pnl_minor_units": int(r[3]),
            "total_fees_minor_units": int(r[4]),
            "updated_at_ns": int(r[5]),
        }
        for r in db.execute(
            """SELECT instrument_id, quantity, avg_entry_minor_units,
                      realized_pnl_minor_units, total_fees_minor_units, updated_at_ns
               FROM agent_sandbox_position
               WHERE binding_id = ?
               ORDER BY instrument_id ASC""",
            (binding.binding_id,),
        ).fetchall()
    ]
    orders = [
        {
            "order_id": str(r[0]),
            "action_id": str(r[1]),
            "instrument_id": str(r[2]),
            "side": str(r[3]),
            "quantity": int(r[4]),
            "limit_minor_units": int(r[5]),
            "status": str(r[6]),
            "filled_quantity": int(r[7]),
            "fill_price_minor_units": None if r[8] is None else int(r[8]),
            "fee_minor_units": int(r[9]),
            "created_at_ns": int(r[10]),
        }
        for r in db.execute(
            """SELECT order_id, action_id, instrument_id, side, quantity,
                      limit_minor_units, status, filled_quantity,
                      fill_price_minor_units, fee_minor_units, created_at_ns
               FROM agent_sandbox_order
               WHERE binding_id = ?
               ORDER BY created_at_ns DESC LIMIT 50""",
            (binding.binding_id,),
        ).fetchall()
    ]
    fill_count = int(
        db.execute(
            "SELECT COUNT(*) FROM agent_sandbox_fill WHERE binding_id = ?",
            (binding.binding_id,),
        ).fetchone()[0]
    )
    total_realized = sum(p["realized_pnl_minor_units"] for p in positions)
    total_fees = sum(p["total_fees_minor_units"] for p in positions)
    return {
        "binding": asdict(binding),
        "cash_minor_units": cash_available,
        "gross_exposure_minor_units": gross_exposure,
        "exposure_limit_minor_units": binding.exposure_limit_minor_units,
        "realized_pnl_minor_units": total_realized,
        "total_fees_minor_units": total_fees,
        "fill_count": fill_count,
        "positions": positions,
        "orders": orders,
    }


def read_agent_sandbox_portfolio(
    store: Path, company_id: str, agent_id: str
) -> dict[str, Any] | None:
    """Read an Agent's active Tier 1 sandbox portfolio via open_reader."""
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        return read_agent_sandbox_portfolio_from_db(db, company_id, agent_id)


# ---------------------------------------------------------------------------
# Tier 2: Company Paper Registration & Legacy Binding Sync
# ---------------------------------------------------------------------------


def register_company_paper_tier(
    writer: WriterLease,
    *,
    account_id: str,
    destination_id: str,
    connector_revision: str,
    instrument_rules_digest: str,
    currency: str,
    starting_minor_units: int,
    exposure_limit_minor_units: int,
    fee_bps: int = 4,
    slippage_bps: int = 2,
    quote_source: str = "live_read_only",
    binding_id: str | None = None,
    book_name: str | None = None,
    strategy_release_id: str | None = None,
) -> ExecutionBinding:
    """Register or idempotently verify an active Tier 2 company_paper book binding."""
    writer.verify()
    _require_safe_id(account_id, "account_id")
    _require_safe_id(destination_id, "destination_id")
    _require_text(connector_revision, "connector_revision", max_len=128)
    _require_text(instrument_rules_digest, "instrument_rules_digest", max_len=128)
    _require_text(currency, "currency", max_len=32)
    _require_positive_int(starting_minor_units, "starting_minor_units")
    _require_positive_int(exposure_limit_minor_units, "exposure_limit_minor_units")
    if exposure_limit_minor_units > starting_minor_units:
        raise ExecutionTierRefused(
            "exposure_limit_minor_units cannot exceed starting_minor_units"
        )
    _require_bps(fee_bps, "fee_bps", upper=1000)
    _require_bps(slippage_bps, "slippage_bps", upper=500)
    _require_text(quote_source, "quote_source", max_len=64)
    resolved_book_name = _require_text(
        book_name if book_name is not None else account_id, "book_name", max_len=128
    )
    resolved_strategy = (
        None
        if strategy_release_id is None
        else _require_safe_id(strategy_release_id, "strategy_release_id")
    )
    resolved_id = (
        f"cpap-{uuid4().hex[:16]}"
        if binding_id is None
        else _require_safe_id(binding_id, "binding_id")
    )

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        enable_domain_v1(
            db, "paper", company_id=writer.company_id, actor_id="execution_tiers"
        )
        existing = db.execute(
            f"""{_SELECT_BINDING_COLS}
                WHERE company_id = ? AND tier = ? AND account_id = ? AND status = 'active'""",
            (writer.company_id, TIER_COMPANY_PAPER, account_id),
        ).fetchone()
        if existing is not None:
            prior = _row_to_binding(existing)
            if (
                prior.account_id == account_id
                and prior.destination_id == destination_id
                and prior.connector_revision == connector_revision
                and prior.instrument_rules_digest == instrument_rules_digest
                and prior.currency == currency
                and prior.starting_minor_units == starting_minor_units
                and prior.exposure_limit_minor_units == exposure_limit_minor_units
                and prior.fee_bps == fee_bps
                and prior.slippage_bps == slippage_bps
            ):
                now = time_ns()
                db.execute(
                    """INSERT OR IGNORE INTO company_paper_book (
                           binding_id, company_id, book_name, strategy_release_id,
                           role, shadow_of_live_binding_id, nominated_question_id,
                           created_at_ns, updated_at_ns
                       ) VALUES (?, ?, ?, ?, 'active', NULL, NULL, ?, ?)""",
                    (
                        prior.binding_id,
                        writer.company_id,
                        resolved_book_name,
                        resolved_strategy,
                        now,
                        now,
                    ),
                )
                return prior
            raise ExecutionTierRefused(
                "An active company_paper book binding already exists for this account_id with different terms"
            )
        now = time_ns()
        db.execute(
            """INSERT INTO execution_environment_binding (
                   binding_id, company_id, tier, environment, owner_agent_id,
                   account_id, destination_id, connector_revision,
                   instrument_rules_digest, currency, starting_minor_units,
                   exposure_limit_minor_units, fee_bps, slippage_bps,
                   quote_source, credential_handle_ref, status,
                   created_at_ns, updated_at_ns
               ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'active', ?, ?)""",
            (
                resolved_id,
                writer.company_id,
                TIER_COMPANY_PAPER,
                ENV_PAPER,
                account_id,
                destination_id,
                connector_revision,
                instrument_rules_digest,
                currency,
                starting_minor_units,
                exposure_limit_minor_units,
                fee_bps,
                slippage_bps,
                quote_source,
                now,
                now,
            ),
        )
        db.execute(
            """INSERT INTO company_paper_book (
                   binding_id, company_id, book_name, strategy_release_id,
                   role, shadow_of_live_binding_id, nominated_question_id,
                   created_at_ns, updated_at_ns
               ) VALUES (?, ?, ?, ?, 'active', NULL, NULL, ?, ?)""",
            (
                resolved_id,
                writer.company_id,
                resolved_book_name,
                resolved_strategy,
                now,
                now,
            ),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="paper",
            entity_type="execution_environment_binding",
            entity_id=resolved_id,
            event_type="execution_tier.company_paper_registered",
            actor_id="execution_tiers",
            payload={
                "binding_id": resolved_id,
                "account_id": account_id,
                "book_name": resolved_book_name,
                "strategy_release_id": resolved_strategy,
                "destination_id": destination_id,
                "fee_bps": fee_bps,
                "slippage_bps": slippage_bps,
            },
            recorded_at_ns=now,
        )
        writer.verify()
        return ExecutionBinding(
            binding_id=resolved_id,
            company_id=writer.company_id,
            tier=TIER_COMPANY_PAPER,
            environment=ENV_PAPER,
            owner_agent_id=None,
            account_id=account_id,
            destination_id=destination_id,
            connector_revision=connector_revision,
            instrument_rules_digest=instrument_rules_digest,
            currency=currency,
            starting_minor_units=starting_minor_units,
            exposure_limit_minor_units=exposure_limit_minor_units,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            quote_source=quote_source,
            credential_handle_ref=None,
            status="active",
            created_at_ns=now,
            updated_at_ns=now,
        )


def open_company_paper_book(
    writer: WriterLease,
    *,
    account_id: str,
    book_name: str,
    strategy_release_id: str | None = None,
    destination_id: str = "company-paper-sim",
    connector_revision: str = "company-paper-sim-v1",
    instrument_rules_digest: str = "btc-usdt-spot-v1",
    currency: str = "USD",
    starting_minor_units: int = 100_000_000,
    exposure_limit_minor_units: int | None = None,
    fee_bps: int = 4,
    slippage_bps: int = 2,
    quote_source: str = "live_read_only",
    binding_id: str | None = None,
) -> ExecutionBinding:
    """Open an isolated concurrent Company Paper Book with its own simulated ledger."""
    exp_limit = (
        starting_minor_units // 2
        if exposure_limit_minor_units is None
        else exposure_limit_minor_units
    )
    return register_company_paper_tier(
        writer,
        account_id=account_id,
        destination_id=destination_id,
        connector_revision=connector_revision,
        instrument_rules_digest=instrument_rules_digest,
        currency=currency,
        starting_minor_units=starting_minor_units,
        exposure_limit_minor_units=exp_limit,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        quote_source=quote_source,
        binding_id=binding_id,
        book_name=book_name,
        strategy_release_id=strategy_release_id,
    )


def _compute_company_paper_book_cash_and_exposure(
    db: Connection, binding: ExecutionBinding
) -> tuple[int, int]:
    """Return (available_cash_minor_units, gross_exposure_minor_units) for one Company Paper Book."""
    cash = binding.starting_minor_units
    for side, qty, price, fee in db.execute(
        """SELECT side, quantity, price_minor_units, fee_minor_units
           FROM company_paper_fill
           WHERE binding_id = ?""",
        (binding.binding_id,),
    ).fetchall():
        notional = int(qty) * int(price)
        if side == "buy":
            cash -= notional + int(fee)
        else:
            cash += notional - int(fee)

    gross_exposure = 0
    for qty, avg_entry in db.execute(
        """SELECT quantity, avg_entry_minor_units
           FROM company_paper_position
           WHERE binding_id = ? AND quantity != 0""",
        (binding.binding_id,),
    ).fetchall():
        gross_exposure += abs(int(qty)) * int(avg_entry)
    return cash, gross_exposure


def submit_company_paper_book_order(
    writer: WriterLease,
    *,
    binding_id: str,
    actor_id: str,
    action_id: str,
    instrument_id: str,
    side: str,
    quantity: int,
    limit_minor_units: int,
    market_price_minor_units: int,
    quote_observed_at_ns: int | None = None,
    fill_mode: str = "immediate",
) -> dict[str, Any]:
    """Submit a realistic simulated order strictly inside one Company Paper Book."""
    writer.verify()
    _require_safe_id(binding_id, "binding_id")
    _require_safe_id(actor_id, "actor_id")
    _require_safe_id(action_id, "action_id")
    _require_safe_id(instrument_id, "instrument_id")
    if side not in ("buy", "sell"):
        raise ExecutionTierRefused("side must be 'buy' or 'sell'")
    _require_positive_int(quantity, "quantity")
    _require_positive_int(limit_minor_units, "limit_minor_units")
    _require_positive_int(market_price_minor_units, "market_price_minor_units")
    if fill_mode not in ("immediate", "resting"):
        raise ExecutionTierRefused("fill_mode must be 'immediate' or 'resting'")
    observed_ns = time_ns() if quote_observed_at_ns is None else quote_observed_at_ns
    _require_positive_int(observed_ns, "quote_observed_at_ns")

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        if not is_domain_installed(db, "paper"):
            raise ExecutionTierRefused("Paper domain is not installed")
        row = db.execute(
            f"""{_SELECT_BINDING_COLS}
                WHERE company_id = ? AND binding_id = ? AND tier = ? AND status = 'active'""",
            (writer.company_id, binding_id, TIER_COMPANY_PAPER),
        ).fetchone()
        if row is None:
            raise ExecutionTierRefused(
                f"Active company_paper book binding '{binding_id}' not found"
            )
        binding = _row_to_binding(row)

        existing = db.execute(
            """SELECT order_id, instrument_id, side, quantity, limit_minor_units,
                      market_price_minor_units, status, filled_quantity,
                      fill_price_minor_units, fee_minor_units
               FROM company_paper_order
               WHERE binding_id = ? AND action_id = ?""",
            (binding.binding_id, action_id),
        ).fetchone()
        if existing is not None:
            if (
                str(existing[1]) != instrument_id
                or str(existing[2]) != side
                or int(existing[3]) != quantity
                or int(existing[4]) != limit_minor_units
            ):
                raise ExecutionTierRefused(
                    "action_id already used for a different company paper order"
                )
            fill_row = db.execute(
                """SELECT fill_id, realized_pnl_minor_units
                   FROM company_paper_fill WHERE order_id = ?""",
                (str(existing[0]),),
            ).fetchone()
            return {
                "order_id": str(existing[0]),
                "binding_id": binding.binding_id,
                "action_id": action_id,
                "instrument_id": instrument_id,
                "side": side,
                "quantity": quantity,
                "status": str(existing[6]),
                "filled_quantity": int(existing[7]),
                "fill_price_minor_units": None
                if existing[8] is None
                else int(existing[8]),
                "fee_minor_units": int(existing[9]),
                "fill_id": None if fill_row is None else str(fill_row[0]),
                "realized_pnl_minor_units": 0 if fill_row is None else int(fill_row[1]),
                "replayed": True,
            }

        if side == "buy":
            slipped_price = (
                market_price_minor_units
                + (market_price_minor_units * binding.slippage_bps + 9_999) // 10_000
            )
            crosses_limit = slipped_price <= limit_minor_units
        else:
            slipped_price = max(
                1,
                market_price_minor_units
                - (market_price_minor_units * binding.slippage_bps) // 10_000,
            )
            crosses_limit = slipped_price >= limit_minor_units

        cash_available, gross_exposure = _compute_company_paper_book_cash_and_exposure(
            db, binding
        )
        pos_row = db.execute(
            """SELECT quantity, avg_entry_minor_units, realized_pnl_minor_units, total_fees_minor_units
               FROM company_paper_position
               WHERE binding_id = ? AND instrument_id = ?""",
            (binding.binding_id, instrument_id),
        ).fetchone()
        cur_qty = int(pos_row[0]) if pos_row else 0
        cur_avg = int(pos_row[1]) if pos_row else 0
        cur_realized = int(pos_row[2]) if pos_row else 0
        cur_fees = int(pos_row[3]) if pos_row else 0

        now = time_ns()
        order_id = f"cpbo-{uuid4().hex[:16]}"
        should_fill = fill_mode == "immediate" and crosses_limit

        if should_fill:
            notional = quantity * slipped_price
            fee = (notional * binding.fee_bps + 5_000) // 10_000
            delta = quantity if side == "buy" else -quantity
            new_qty = cur_qty + delta
            other_exposure = gross_exposure - abs(cur_qty) * cur_avg

            realized_delta = 0
            if (
                cur_qty == 0
                or (cur_qty > 0 and delta > 0)
                or (cur_qty < 0 and delta < 0)
            ):
                total_cost = abs(cur_qty) * cur_avg + quantity * slipped_price
                new_avg = total_cost // abs(new_qty)
            else:
                closing_qty = min(abs(cur_qty), quantity)
                if cur_qty > 0:
                    realized_delta = (slipped_price - cur_avg) * closing_qty
                else:
                    realized_delta = (cur_avg - slipped_price) * closing_qty
                if new_qty == 0:
                    new_avg = 0
                elif (cur_qty > 0 and new_qty > 0) or (cur_qty < 0 and new_qty < 0):
                    new_avg = cur_avg
                else:
                    new_avg = slipped_price

            projected_exposure = other_exposure + abs(new_qty) * new_avg
            if projected_exposure > binding.exposure_limit_minor_units:
                raise ExecutionTierRefused(
                    "Company paper book order exceeds exposure limit"
                )
            if side == "buy" and notional + fee > cash_available:
                raise ExecutionTierRefused(
                    "Company paper book order exceeds available virtual cash"
                )
            realized_net = realized_delta - fee
            fill_id = f"cpbf-{uuid4().hex[:16]}"
            db.execute(
                """INSERT INTO company_paper_order (
                       order_id, binding_id, company_id, actor_id, action_id,
                       instrument_id, side, quantity, limit_minor_units,
                       market_price_minor_units, quote_observed_at_ns, quote_source,
                       fee_bps, slippage_bps, status, filled_quantity,
                       fill_price_minor_units, fee_minor_units, created_at_ns, updated_at_ns
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?, ?, ?, ?)""",
                (
                    order_id,
                    binding.binding_id,
                    writer.company_id,
                    actor_id,
                    action_id,
                    instrument_id,
                    side,
                    quantity,
                    limit_minor_units,
                    market_price_minor_units,
                    observed_ns,
                    binding.quote_source,
                    binding.fee_bps,
                    binding.slippage_bps,
                    quantity,
                    slipped_price,
                    fee,
                    now,
                    now,
                ),
            )
            db.execute(
                """INSERT INTO company_paper_fill (
                       fill_id, order_id, binding_id, company_id, actor_id,
                       instrument_id, side, quantity, price_minor_units,
                       fee_minor_units, realized_pnl_minor_units, filled_at_ns
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fill_id,
                    order_id,
                    binding.binding_id,
                    writer.company_id,
                    actor_id,
                    instrument_id,
                    side,
                    quantity,
                    slipped_price,
                    fee,
                    realized_net,
                    now,
                ),
            )
            db.execute(
                """INSERT INTO company_paper_position (
                       binding_id, company_id, instrument_id,
                       quantity, avg_entry_minor_units, realized_pnl_minor_units,
                       total_fees_minor_units, updated_at_ns
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (binding_id, instrument_id) DO UPDATE SET
                       quantity = excluded.quantity,
                       avg_entry_minor_units = excluded.avg_entry_minor_units,
                       realized_pnl_minor_units = excluded.realized_pnl_minor_units,
                       total_fees_minor_units = excluded.total_fees_minor_units,
                       updated_at_ns = excluded.updated_at_ns""",
                (
                    binding.binding_id,
                    writer.company_id,
                    instrument_id,
                    new_qty,
                    new_avg,
                    cur_realized + realized_net,
                    cur_fees + fee,
                    now,
                ),
            )
            append_audit_event(
                db,
                company_id=writer.company_id,
                domain="paper",
                entity_type="company_paper_fill",
                entity_id=fill_id,
                event_type="company_paper.order_filled",
                actor_id=actor_id,
                payload={
                    "order_id": order_id,
                    "fill_id": fill_id,
                    "binding_id": binding.binding_id,
                    "instrument_id": instrument_id,
                    "side": side,
                    "quantity": quantity,
                    "price_minor_units": slipped_price,
                    "fee_minor_units": fee,
                },
                recorded_at_ns=now,
            )
            writer.verify()
            return {
                "order_id": order_id,
                "binding_id": binding.binding_id,
                "action_id": action_id,
                "instrument_id": instrument_id,
                "side": side,
                "quantity": quantity,
                "status": "filled",
                "filled_quantity": quantity,
                "fill_price_minor_units": slipped_price,
                "fee_minor_units": fee,
                "fill_id": fill_id,
                "realized_pnl_minor_units": realized_net,
                "replayed": False,
            }

        if side == "buy" and quantity * limit_minor_units > cash_available:
            raise ExecutionTierRefused(
                "Company paper book limit order exceeds available virtual cash"
            )
        db.execute(
            """INSERT INTO company_paper_order (
                   order_id, binding_id, company_id, actor_id, action_id,
                   instrument_id, side, quantity, limit_minor_units,
                   market_price_minor_units, quote_observed_at_ns, quote_source,
                   fee_bps, slippage_bps, status, filled_quantity,
                   fill_price_minor_units, fee_minor_units, created_at_ns, updated_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 0, NULL, 0, ?, ?)""",
            (
                order_id,
                binding.binding_id,
                writer.company_id,
                actor_id,
                action_id,
                instrument_id,
                side,
                quantity,
                limit_minor_units,
                market_price_minor_units,
                observed_ns,
                binding.quote_source,
                binding.fee_bps,
                binding.slippage_bps,
                now,
                now,
            ),
        )
        writer.verify()
        return {
            "order_id": order_id,
            "binding_id": binding.binding_id,
            "action_id": action_id,
            "instrument_id": instrument_id,
            "side": side,
            "quantity": quantity,
            "status": "open",
            "filled_quantity": 0,
            "fill_price_minor_units": None,
            "fee_minor_units": 0,
            "fill_id": None,
            "realized_pnl_minor_units": 0,
            "replayed": False,
        }


def read_company_paper_book_portfolio_from_db(
    db: Connection, company_id: str, binding_id: str
) -> dict[str, Any] | None:
    """Read one Company Paper Book's independent virtual cash, exposure, fills, and soak state."""
    if not is_domain_installed(db, "paper"):
        return None
    row = db.execute(
        f"""{_SELECT_BINDING_COLS}
            WHERE company_id = ? AND binding_id = ? AND tier = ?""",
        (company_id, binding_id, TIER_COMPANY_PAPER),
    ).fetchone()
    if row is None:
        return None
    binding = _row_to_binding(row)
    meta_row = db.execute(
        """SELECT book_name, strategy_release_id, role,
                  shadow_of_live_binding_id, nominated_question_id,
                  created_at_ns, updated_at_ns
           FROM company_paper_book
           WHERE company_id = ? AND binding_id = ?""",
        (company_id, binding_id),
    ).fetchone()
    book_name = str(meta_row[0]) if meta_row is not None else binding.account_id
    strategy_release_id = (
        None if meta_row is None or meta_row[1] is None else str(meta_row[1])
    )
    role = (
        str(meta_row[2])
        if meta_row is not None
        else ("retired" if binding.status == "retired" else "active")
    )
    shadow_of_live_binding_id = (
        None if meta_row is None or meta_row[3] is None else str(meta_row[3])
    )
    nominated_question_id = (
        None if meta_row is None or meta_row[4] is None else str(meta_row[4])
    )
    if role == "nominated" and nominated_question_id is not None:
        q_row = db.execute(
            "SELECT state FROM decision_question WHERE company_id = ? AND question_id = ?",
            (company_id, nominated_question_id),
        ).fetchone()
        if q_row is not None and str(q_row[0]) != "open":
            role = "active"
            nominated_question_id = None

    cash, gross_exposure = _compute_company_paper_book_cash_and_exposure(db, binding)
    positions = [
        {
            "instrument_id": str(r[0]),
            "quantity": int(r[1]),
            "avg_entry_minor_units": int(r[2]),
            "realized_pnl_minor_units": int(r[3]),
            "total_fees_minor_units": int(r[4]),
            "updated_at_ns": int(r[5]),
        }
        for r in db.execute(
            """SELECT instrument_id, quantity, avg_entry_minor_units,
                      realized_pnl_minor_units, total_fees_minor_units, updated_at_ns
               FROM company_paper_position
               WHERE binding_id = ?
               ORDER BY instrument_id ASC""",
            (binding.binding_id,),
        ).fetchall()
    ]
    orders = [
        {
            "order_id": str(r[0]),
            "action_id": str(r[1]),
            "instrument_id": str(r[2]),
            "side": str(r[3]),
            "quantity": int(r[4]),
            "limit_minor_units": int(r[5]),
            "status": str(r[6]),
            "filled_quantity": int(r[7]),
            "fill_price_minor_units": None if r[8] is None else int(r[8]),
            "fee_minor_units": int(r[9]),
            "created_at_ns": int(r[10]),
        }
        for r in db.execute(
            """SELECT order_id, action_id, instrument_id, side, quantity,
                      limit_minor_units, status, filled_quantity,
                      fill_price_minor_units, fee_minor_units, created_at_ns
               FROM company_paper_order
               WHERE binding_id = ?
               ORDER BY created_at_ns DESC LIMIT 30""",
            (binding.binding_id,),
        ).fetchall()
    ]
    fill_count = int(
        db.execute(
            "SELECT COUNT(*) FROM company_paper_fill WHERE binding_id = ?",
            (binding.binding_id,),
        ).fetchone()[0]
    )
    open_orders_count = int(
        db.execute(
            "SELECT COUNT(*) FROM company_paper_order WHERE binding_id = ? AND status = 'open'",
            (binding.binding_id,),
        ).fetchone()[0]
    )
    realized_pnl = sum(p["realized_pnl_minor_units"] for p in positions)
    total_fees = sum(p["total_fees_minor_units"] for p in positions)
    open_positions_count = sum(1 for p in positions if p["quantity"] != 0)

    # Book-level soak readiness: recorded fills in this book OR legacy soak evidence if mirroring legacy paper_binding.
    soak_ready = fill_count > 0
    if not soak_ready:
        legacy_match = db.execute(
            "SELECT 1 FROM paper_binding WHERE singleton = 1 AND company_id = ? AND account_id = ?",
            (company_id, binding.account_id),
        ).fetchone()
        if legacy_match is not None:
            for sql in (
                "SELECT 1 FROM paper_fill LIMIT 1",
                "SELECT 1 FROM paper_valuation LIMIT 1",
                "SELECT 1 FROM paper_statement LIMIT 1",
            ):
                if db.execute(sql).fetchone() is not None:
                    soak_ready = True
                    break

    return {
        "binding_id": binding.binding_id,
        "account_id": binding.account_id,
        "book_name": book_name,
        "strategy_release_id": strategy_release_id,
        "role": role,
        "status": binding.status,
        "shadow_of_live_binding_id": shadow_of_live_binding_id,
        "nominated_question_id": nominated_question_id,
        "currency": binding.currency,
        "starting_minor_units": binding.starting_minor_units,
        "exposure_limit_minor_units": binding.exposure_limit_minor_units,
        "fee_bps": binding.fee_bps,
        "slippage_bps": binding.slippage_bps,
        "quote_source": binding.quote_source,
        "cash_minor_units": cash,
        "gross_exposure_minor_units": gross_exposure,
        "realized_pnl_minor_units": realized_pnl,
        "total_fees_minor_units": total_fees,
        "fill_count": fill_count,
        "open_positions_count": open_positions_count,
        "open_orders_count": open_orders_count,
        "soak_ready": soak_ready,
        "positions": positions,
        "orders": orders,
    }


def sync_company_paper_from_legacy_binding(
    writer: WriterLease, *, fee_bps: int = 4, slippage_bps: int = 2
) -> ExecutionBinding:
    """Mirror an existing singleton paper_binding into Tier 2 execution_environment_binding."""
    writer.verify()
    with closing(open_reader(writer.store)) as db, read_tx(db):
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        if not is_domain_installed(db, "paper"):
            raise ExecutionTierRefused("Legacy paper_binding is not installed")
        row = db.execute(
            """SELECT account_id, destination_id, connector_revision,
                      instrument_rules_digest, currency, starting_minor_units,
                      exposure_limit_minor_units
               FROM paper_binding WHERE singleton = 1"""
        ).fetchone()
    if row is None:
        raise ExecutionTierRefused("Legacy paper_binding row is absent")
    return register_company_paper_tier(
        writer,
        account_id=str(row[0]),
        destination_id=str(row[1]),
        connector_revision=str(row[2]),
        instrument_rules_digest=str(row[3]),
        currency=str(row[4]),
        starting_minor_units=int(row[5]),
        exposure_limit_minor_units=int(row[6]),
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )


# ---------------------------------------------------------------------------
# Tier 3: External Qualification Receipts, Live Candidate & Promotion Gate
# ---------------------------------------------------------------------------


def record_venue_qualification_receipt(
    writer: WriterLease,
    *,
    receipt_id: str,
    adapter_revision: str,
    environment: str,
    destination_id: str,
    instrument_rules_digest: str,
    core_digest: str,
    verdict: str,
    cases_passed: int,
    cases_failed: int,
    cases_unrun: int,
    evidence_sha256: str,
) -> VenueQualificationReceipt:
    """Seal an immutable venue qualification receipt (required for external_test -> live promotion)."""
    writer.verify()
    _require_safe_id(receipt_id, "receipt_id")
    _require_text(adapter_revision, "adapter_revision", max_len=128)
    if environment not in (ENV_PAPER, ENV_EXTERNAL_TEST):
        raise ExecutionTierRefused(
            "Qualification receipt environment must be 'paper' or 'external_test'"
        )
    _require_safe_id(destination_id, "destination_id")
    _require_text(instrument_rules_digest, "instrument_rules_digest", max_len=128)
    _require_sha256(core_digest, "core_digest")
    if verdict not in ("PASS", "FAIL", "INCOMPLETE", "BLOCKED"):
        raise ExecutionTierRefused("Invalid qualification verdict")
    for count, label in (
        (cases_passed, "cases_passed"),
        (cases_failed, "cases_failed"),
        (cases_unrun, "cases_unrun"),
    ):
        if type(count) is not int or count < 0:
            raise ExecutionTierRefused(f"{label} must be a non-negative integer")
    if verdict == "PASS" and (cases_passed <= 0 or cases_failed != 0):
        raise ExecutionTierRefused(
            "PASS qualification verdict requires cases_passed > 0 and cases_failed == 0"
        )
    _require_sha256(evidence_sha256, "evidence_sha256")

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        enable_domain_v1(
            db, "paper", company_id=writer.company_id, actor_id="venue_qualification"
        )
        existing = db.execute(
            """SELECT receipt_id, company_id, adapter_revision, environment,
                      destination_id, instrument_rules_digest, core_digest,
                      verdict, cases_passed, cases_failed, cases_unrun,
                      evidence_sha256, recorded_at_ns
               FROM venue_qualification_receipt
               WHERE receipt_id = ?""",
            (receipt_id,),
        ).fetchone()
        if existing is not None:
            prior = VenueQualificationReceipt(
                receipt_id=str(existing[0]),
                company_id=str(existing[1]),
                adapter_revision=str(existing[2]),
                environment=str(existing[3]),
                destination_id=str(existing[4]),
                instrument_rules_digest=str(existing[5]),
                core_digest=str(existing[6]),
                verdict=str(existing[7]),
                cases_passed=int(existing[8]),
                cases_failed=int(existing[9]),
                cases_unrun=int(existing[10]),
                evidence_sha256=str(existing[11]),
                recorded_at_ns=int(existing[12]),
            )
            if (
                prior.company_id != writer.company_id
                or prior.adapter_revision != adapter_revision
                or prior.environment != environment
                or prior.destination_id != destination_id
                or prior.instrument_rules_digest != instrument_rules_digest
                or prior.core_digest != core_digest
                or prior.verdict != verdict
                or prior.cases_passed != cases_passed
                or prior.cases_failed != cases_failed
                or prior.cases_unrun != cases_unrun
                or prior.evidence_sha256 != evidence_sha256
            ):
                raise ExecutionTierRefused(
                    "Qualification receipt ID conflicts with previously sealed record"
                )
            return prior

        now = time_ns()
        db.execute(
            """INSERT INTO venue_qualification_receipt (
                   receipt_id, company_id, adapter_revision, environment,
                   destination_id, instrument_rules_digest, core_digest,
                   verdict, cases_passed, cases_failed, cases_unrun,
                   evidence_sha256, recorded_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                receipt_id,
                writer.company_id,
                adapter_revision,
                environment,
                destination_id,
                instrument_rules_digest,
                core_digest,
                verdict,
                cases_passed,
                cases_failed,
                cases_unrun,
                evidence_sha256,
                now,
            ),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="paper",
            entity_type="venue_qualification_receipt",
            entity_id=receipt_id,
            event_type="venue_qualification.recorded",
            actor_id="venue_qualification",
            payload={
                "receipt_id": receipt_id,
                "adapter_revision": adapter_revision,
                "environment": environment,
                "verdict": verdict,
                "cases_passed": cases_passed,
                "cases_failed": cases_failed,
            },
            recorded_at_ns=now,
        )
        writer.verify()
        return VenueQualificationReceipt(
            receipt_id=receipt_id,
            company_id=writer.company_id,
            adapter_revision=adapter_revision,
            environment=environment,
            destination_id=destination_id,
            instrument_rules_digest=instrument_rules_digest,
            core_digest=core_digest,
            verdict=verdict,
            cases_passed=cases_passed,
            cases_failed=cases_failed,
            cases_unrun=cases_unrun,
            evidence_sha256=evidence_sha256,
            recorded_at_ns=now,
        )


def register_live_candidate_binding(
    writer: WriterLease,
    *,
    binding_id: str,
    account_id: str,
    destination_id: str,
    connector_revision: str,
    instrument_rules_digest: str,
    currency: str,
    starting_minor_units: int,
    exposure_limit_minor_units: int,
    credential_handle_ref: str,
    fee_bps: int = 4,
    slippage_bps: int = 0,
    quote_source: str = "live_exchange",
) -> ExecutionBinding:
    """Register a dormant (status='registered') Tier 3 company_live candidate binding.

    Never activates live trading directly; activation requires Board promotion.
    """
    writer.verify()
    _require_safe_id(binding_id, "binding_id")
    _require_safe_id(account_id, "account_id")
    _require_safe_id(destination_id, "destination_id")
    _require_text(connector_revision, "connector_revision", max_len=128)
    _require_text(instrument_rules_digest, "instrument_rules_digest", max_len=128)
    _require_text(currency, "currency", max_len=32)
    _require_positive_int(starting_minor_units, "starting_minor_units")
    _require_positive_int(exposure_limit_minor_units, "exposure_limit_minor_units")
    if exposure_limit_minor_units > starting_minor_units:
        raise ExecutionTierRefused(
            "exposure_limit_minor_units cannot exceed starting_minor_units"
        )
    _require_bps(fee_bps, "fee_bps", upper=1000)
    _require_bps(slippage_bps, "slippage_bps", upper=500)
    _require_text(quote_source, "quote_source", max_len=64)
    handle = _validate_credential_handle(
        credential_handle_ref, environment=ENV_LIVE, required=True
    )

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        enable_domain_v1(
            db, "paper", company_id=writer.company_id, actor_id="execution_tiers"
        )

        # Ensure zero account/destination/credential collision with paper bindings.
        legacy = db.execute(
            "SELECT account_id, destination_id FROM paper_binding WHERE singleton = 1"
        ).fetchone()
        if legacy is not None and (
            str(legacy[0]) == account_id or str(legacy[1]) == destination_id
        ):
            raise ExecutionTierRefused(
                "Live candidate binding cannot reuse a paper account_id or destination_id"
            )
        collision = db.execute(
            """SELECT 1 FROM execution_environment_binding
               WHERE company_id = ? AND environment != 'live'
                 AND (account_id = ? OR destination_id = ? OR credential_handle_ref = ?)
               LIMIT 1""",
            (writer.company_id, account_id, destination_id, handle),
        ).fetchone()
        if collision is not None:
            raise ExecutionTierRefused(
                "Live candidate binding cannot reuse a paper/testnet account, destination, or credential handle"
            )

        existing = db.execute(
            f"{_SELECT_BINDING_COLS} WHERE binding_id = ?",
            (binding_id,),
        ).fetchone()
        if existing is not None:
            prior = _row_to_binding(existing)
            if (
                prior.company_id == writer.company_id
                and prior.tier == TIER_COMPANY_LIVE
                and prior.account_id == account_id
                and prior.destination_id == destination_id
                and prior.connector_revision == connector_revision
                and prior.instrument_rules_digest == instrument_rules_digest
                and prior.credential_handle_ref == handle
                and prior.exposure_limit_minor_units == exposure_limit_minor_units
            ):
                return prior
            raise ExecutionTierRefused(
                "Live candidate binding_id conflicts with existing binding"
            )

        now = time_ns()
        db.execute(
            """INSERT INTO execution_environment_binding (
                   binding_id, company_id, tier, environment, owner_agent_id,
                   account_id, destination_id, connector_revision,
                   instrument_rules_digest, currency, starting_minor_units,
                   exposure_limit_minor_units, fee_bps, slippage_bps,
                   quote_source, credential_handle_ref, status,
                   created_at_ns, updated_at_ns
               ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'registered', ?, ?)""",
            (
                binding_id,
                writer.company_id,
                TIER_COMPANY_LIVE,
                ENV_LIVE,
                account_id,
                destination_id,
                connector_revision,
                instrument_rules_digest,
                currency,
                starting_minor_units,
                exposure_limit_minor_units,
                fee_bps,
                slippage_bps,
                quote_source,
                handle,
                now,
                now,
            ),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="paper",
            entity_type="execution_environment_binding",
            entity_id=binding_id,
            event_type="execution_tier.live_candidate_registered",
            actor_id="execution_tiers",
            payload={
                "binding_id": binding_id,
                "account_id": account_id,
                "destination_id": destination_id,
                "connector_revision": connector_revision,
                "credential_handle_ref": handle,
                "exposure_limit_minor_units": exposure_limit_minor_units,
            },
            recorded_at_ns=now,
        )
        writer.verify()
        return ExecutionBinding(
            binding_id=binding_id,
            company_id=writer.company_id,
            tier=TIER_COMPANY_LIVE,
            environment=ENV_LIVE,
            owner_agent_id=None,
            account_id=account_id,
            destination_id=destination_id,
            connector_revision=connector_revision,
            instrument_rules_digest=instrument_rules_digest,
            currency=currency,
            starting_minor_units=starting_minor_units,
            exposure_limit_minor_units=exposure_limit_minor_units,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            quote_source=quote_source,
            credential_handle_ref=handle,
            status="registered",
            created_at_ns=now,
            updated_at_ns=now,
        )


def build_promotion_payload_hash(
    *,
    company_id: str,
    charter_revision: str,
    target_binding: ExecutionBinding,
    qualification_receipt_id: str,
) -> str:
    """Compute the deterministic payload hash that Board promotion must explicitly sign/confirm."""
    return _sha256_json(
        {
            "company_id": company_id,
            "charter_revision": charter_revision,
            "target_binding_id": target_binding.binding_id,
            "qualification_receipt_id": qualification_receipt_id,
            "account_id": target_binding.account_id,
            "destination_id": target_binding.destination_id,
            "connector_revision": target_binding.connector_revision,
            "instrument_rules_digest": target_binding.instrument_rules_digest,
            "credential_handle_ref": target_binding.credential_handle_ref,
            "exposure_limit_minor_units": target_binding.exposure_limit_minor_units,
        }
    )


def _has_company_paper_soak_evidence(db: Connection, company_id: str) -> bool:
    """Return True iff company_paper is active AND has observed fills, valuations, or statements."""
    if not is_domain_installed(db, "paper"):
        return False
    has_tier_paper = (
        db.execute(
            """SELECT 1 FROM execution_environment_binding
               WHERE company_id = ? AND tier = ? AND status = 'active' LIMIT 1""",
            (company_id, TIER_COMPANY_PAPER),
        ).fetchone()
        is not None
    )
    has_legacy_paper = (
        db.execute(
            "SELECT 1 FROM paper_binding WHERE singleton = 1 AND company_id = ?",
            (company_id,),
        ).fetchone()
        is not None
    )
    if not (has_tier_paper or has_legacy_paper):
        return False
    for sql in (
        "SELECT 1 FROM company_paper_fill LIMIT 1",
        "SELECT 1 FROM paper_fill LIMIT 1",
        "SELECT 1 FROM paper_valuation LIMIT 1",
        "SELECT 1 FROM paper_statement LIMIT 1",
    ):
        if db.execute(sql).fetchone() is not None:
            return True
    return False


def _require_board_control(
    db: Connection,
    company_id: str,
    *,
    operation: str,
    authorize: AuthorizeBoard | None,
    principal_hint: str | None,
) -> str:
    access = resolve_board_access(
        db,
        company_id,
        authorize_board=authorize,
        operation=operation,
        principal_hint=principal_hint,
    )
    if access.bootstrap_locked or access.principal_id is None:
        raise BoardControlForbidden("Board control authority is not bound")
    if access.role != BOARD_CONTROL_ROLE:
        raise BoardControlForbidden(
            "board_control role is required for execution environment promotion/demotion"
        )
    return access.principal_id


def promote_execution_environment(
    writer: WriterLease,
    *,
    command_id: str,
    charter_revision: Any,
    target_binding_id: Any,
    qualification_receipt_id: Any,
    expected_payload_hash: Any,
    reason: Any,
    authorize: AuthorizeBoard | None = None,
    principal_hint: str | None = None,
    peer_pid: int | None = None,
    expected_core_digest: str | None = None,
) -> BoardCommandOutcome:
    """Server-side fail-closed Board promotion gate activating Tier 3 company_live.

    Keeps Tier 1 (agent_sandbox) and Tier 2 (company_paper) active in parallel.
    """
    writer.verify()
    try:
        cmd_id = _require_safe_id(command_id, "command_id")
        charter = _require_text(charter_revision, "charter_revision", max_len=128)
        target_id = _require_safe_id(target_binding_id, "target_binding_id")
        qual_id = _require_safe_id(qualification_receipt_id, "qualification_receipt_id")
        exp_hash = _require_sha256(expected_payload_hash, "expected_payload_hash")
        clean_reason = _require_text(reason, "reason", max_len=1000)
    except ExecutionTierRefused as error:
        raise BoardControlRefused(str(error)) from error

    core_digest = (
        compute_trading_core_digest()
        if expected_core_digest is None
        else expected_core_digest
    )
    preconditions = {
        "charter_revision": charter,
        "target_binding_id": target_id,
        "qualification_receipt_id": qual_id,
        "expected_payload_hash": exp_hash,
    }
    payload_sha = _sha256_json(
        {
            "command_type": PROMOTE_EXECUTION_ENVIRONMENT,
            "preconditions": preconditions,
            "reason": clean_reason,
        }
    )

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise BoardControlForbidden("Company identity mismatch")
        actor_id = _require_board_control(
            db,
            writer.company_id,
            operation=PROMOTE_EXECUTION_ENVIRONMENT,
            authorize=authorize,
            principal_hint=principal_hint,
        )

        # Idempotent replay check before mutating anything.
        existing_receipt = db.execute(
            """SELECT company_id, command_id, actor_id, peer_pid, command_type,
                      contract_version, payload_sha256, preconditions_json, state,
                      denial_code, result_json, applied_seq, created_at_ns, finalized_at_ns
               FROM command_receipt
               WHERE company_id = ? AND command_id = ?""",
            (writer.company_id, cmd_id),
        ).fetchone()
        if existing_receipt is not None:
            try:
                replayed_receipt, _ = admit_command_receipt(
                    db,
                    company_id=writer.company_id,
                    command_id=cmd_id,
                    actor_id=actor_id,
                    peer_pid=peer_pid,
                    command_type=PROMOTE_EXECUTION_ENVIRONMENT,
                    contract_version=EXECUTION_TIER_COMMAND,
                    payload_sha256=payload_sha,
                    preconditions=preconditions,
                    state=str(existing_receipt[8]),
                )
            except CommandConflict as error:
                raise BoardControlConflict(str(error)) from error
            saved_result = (
                json.loads(replayed_receipt.result_json)
                if replayed_receipt.result_json
                else {}
            )
            return BoardCommandOutcome(
                command_id=replayed_receipt.command_id,
                command_type=replayed_receipt.command_type,
                state=replayed_receipt.state,
                disposition="applied"
                if replayed_receipt.state == "applied"
                else "denied",
                denial_code=replayed_receipt.denial_code,
                effective_revision=int(replayed_receipt.applied_seq or 0),
                recorded_at_ns=replayed_receipt.created_at_ns,
                peer_pid=replayed_receipt.peer_pid,
                result=saved_result,
            )

        if not is_domain_installed(db, "paper"):
            raise BoardControlRefused(
                "Paper domain must be active with soak evidence before live promotion"
            )

        current_charter_row = db.execute(
            "SELECT charter_revision FROM company WHERE id = ?", (writer.company_id,)
        ).fetchone()
        current_charter = str(current_charter_row[0]) if current_charter_row else ""

        target_row = db.execute(
            f"{_SELECT_BINDING_COLS} WHERE company_id = ? AND binding_id = ?",
            (writer.company_id, target_id),
        ).fetchone()
        if target_row is None:
            raise BoardControlRefused("Target live candidate binding was not found")
        target = _row_to_binding(target_row)
        if target.tier != TIER_COMPANY_LIVE or target.environment != ENV_LIVE:
            raise BoardControlRefused(
                "Promotion target must be a company_live binding with environment='live'"
            )
        if target.status not in ("registered", "suspended"):
            raise BoardControlRefused(
                f"Cannot promote target live binding in status '{target.status}'"
            )

        # Gate 1: Charter revision and signed payload hash match (conflict -> persisted denied receipt).
        actual_hash = build_promotion_payload_hash(
            company_id=writer.company_id,
            charter_revision=current_charter,
            target_binding=target,
            qualification_receipt_id=qual_id,
        )
        now = time_ns()
        if charter != current_charter or exp_hash != actual_hash:
            denied_result = {
                "target_binding_id": target_id,
                "expected_charter_revision": current_charter,
                "actual_payload_hash": actual_hash,
            }
            seq = append_audit_event(
                db,
                company_id=writer.company_id,
                domain="paper",
                entity_type="execution_environment_binding",
                entity_id=target_id,
                event_type="execution_tier.promote_denied_conflict",
                actor_id=actor_id,
                command_id=cmd_id,
                payload=denied_result,
                recorded_at_ns=now,
            )
            admit_command_receipt(
                db,
                company_id=writer.company_id,
                command_id=cmd_id,
                actor_id=actor_id,
                peer_pid=peer_pid,
                command_type=PROMOTE_EXECUTION_ENVIRONMENT,
                contract_version=EXECUTION_TIER_COMMAND,
                payload_sha256=payload_sha,
                preconditions=preconditions,
                state="denied",
                denial_code="conflicting_revision",
                result=denied_result,
                applied_seq=seq,
                now_ns=now,
            )
            writer.verify()
            return BoardCommandOutcome(
                command_id=cmd_id,
                command_type=PROMOTE_EXECUTION_ENVIRONMENT,
                state="denied",
                disposition="denied",
                denial_code="conflicting_revision",
                effective_revision=seq,
                recorded_at_ns=now,
                peer_pid=peer_pid,
                result=denied_result,
            )

        # Gate 2: Company paper soak evidence must exist.
        if not _has_company_paper_soak_evidence(db, writer.company_id):
            raise BoardControlRefused(
                "Company paper soak evidence (active paper binding plus observed fill, valuation, or statement) is required before live promotion"
            )

        # Gate 3: Matching PASS external_test venue qualification receipt.
        qual_row = db.execute(
            """SELECT adapter_revision, environment, instrument_rules_digest,
                      core_digest, verdict, cases_passed, cases_failed
               FROM venue_qualification_receipt
               WHERE company_id = ? AND receipt_id = ?""",
            (writer.company_id, qual_id),
        ).fetchone()
        if qual_row is None:
            raise BoardControlRefused(
                "External testnet venue qualification receipt is missing"
            )
        if (
            str(qual_row[1]) != ENV_EXTERNAL_TEST
            or str(qual_row[4]) != "PASS"
            or int(qual_row[5]) <= 0
            or int(qual_row[6]) != 0
        ):
            raise BoardControlRefused(
                "Live promotion requires a PASS external_test qualification receipt with zero failures"
            )
        if str(qual_row[2]) != target.instrument_rules_digest:
            raise BoardControlRefused(
                "Qualification receipt instrument_rules_digest does not match target live binding"
            )
        if str(qual_row[3]) != core_digest:
            raise BoardControlRefused(
                "Qualification receipt core_digest does not match current trading core"
            )

        # Gate 4: Opaque live credential handle isolation re-check.
        try:
            _validate_credential_handle(
                target.credential_handle_ref, environment=ENV_LIVE, required=True
            )
        except ExecutionTierRefused as error:
            raise BoardControlRefused(str(error)) from error

        # Ensure no other company_live binding is already active.
        already_live = db.execute(
            """SELECT binding_id FROM execution_environment_binding
               WHERE company_id = ? AND tier = ? AND status = 'active'""",
            (writer.company_id, TIER_COMPANY_LIVE),
        ).fetchone()
        if already_live is not None:
            raise BoardControlRefused(
                f"Another company_live binding ({already_live[0]}) is already active; demote it first"
            )

        paper_row = db.execute(
            """SELECT binding_id FROM execution_environment_binding
               WHERE company_id = ? AND tier = ? AND status = 'active'""",
            (writer.company_id, TIER_COMPANY_PAPER),
        ).fetchone()
        from_binding_id = None if paper_row is None else str(paper_row[0])

        # Activate company_live while preserving company_paper and agent_sandbox in parallel!
        db.execute(
            """UPDATE execution_environment_binding
               SET status = 'active', updated_at_ns = ?
               WHERE binding_id = ?""",
            (now, target_id),
        )
        promotion_id = f"promo-{uuid4().hex[:16]}"
        result_body = {
            "promotion_id": promotion_id,
            "action": "promote_live",
            "from_binding_id": from_binding_id,
            "to_binding_id": target_id,
            "qualification_receipt_id": qual_id,
            "company_paper_preserved": True,
            "agent_sandboxes_preserved": True,
        }
        seq = append_audit_event(
            db,
            company_id=writer.company_id,
            domain="paper",
            entity_type="execution_environment_binding",
            entity_id=target_id,
            event_type="execution_tier.promoted_live",
            actor_id=actor_id,
            command_id=cmd_id,
            payload=result_body,
            recorded_at_ns=now,
        )
        admit_command_receipt(
            db,
            company_id=writer.company_id,
            command_id=cmd_id,
            actor_id=actor_id,
            peer_pid=peer_pid,
            command_type=PROMOTE_EXECUTION_ENVIRONMENT,
            contract_version=EXECUTION_TIER_COMMAND,
            payload_sha256=payload_sha,
            preconditions=preconditions,
            state="applied",
            result=result_body,
            applied_seq=seq,
            now_ns=now,
        )
        db.execute(
            """INSERT INTO environment_promotion_record (
                   promotion_id, company_id, command_id, action,
                   from_binding_id, to_binding_id, qualification_receipt_id,
                   payload_hash, reason, recorded_at_ns
               ) VALUES (?, ?, ?, 'promote_live', ?, ?, ?, ?, ?, ?)""",
            (
                promotion_id,
                writer.company_id,
                cmd_id,
                from_binding_id,
                target_id,
                qual_id,
                actual_hash,
                clean_reason,
                now,
            ),
        )
        writer.verify()
        return BoardCommandOutcome(
            command_id=cmd_id,
            command_type=PROMOTE_EXECUTION_ENVIRONMENT,
            state="applied",
            disposition="applied",
            denial_code=None,
            effective_revision=seq,
            recorded_at_ns=now,
            peer_pid=peer_pid,
            result=result_body,
        )


def demote_live_environment(
    writer: WriterLease,
    *,
    command_id: str,
    charter_revision: Any,
    target_binding_id: Any,
    action: Any,
    reason: Any,
    authorize: AuthorizeBoard | None = None,
    principal_hint: str | None = None,
    peer_pid: int | None = None,
) -> BoardCommandOutcome:
    """Immediately suspend or retire an active Tier 3 company_live binding without stopping paper."""
    writer.verify()
    try:
        cmd_id = _require_safe_id(command_id, "command_id")
        charter = _require_text(charter_revision, "charter_revision", max_len=128)
        target_id = _require_safe_id(target_binding_id, "target_binding_id")
        clean_reason = _require_text(reason, "reason", max_len=1000)
    except ExecutionTierRefused as error:
        raise BoardControlRefused(str(error)) from error
    if action not in ("suspend_live", "demote_to_paper"):
        raise BoardControlRefused("action must be 'suspend_live' or 'demote_to_paper'")

    preconditions = {
        "charter_revision": charter,
        "target_binding_id": target_id,
    }
    payload_sha = _sha256_json(
        {
            "command_type": DEMOTE_LIVE_ENVIRONMENT,
            "preconditions": preconditions,
            "action": action,
            "reason": clean_reason,
        }
    )

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise BoardControlForbidden("Company identity mismatch")
        actor_id = _require_board_control(
            db,
            writer.company_id,
            operation=DEMOTE_LIVE_ENVIRONMENT,
            authorize=authorize,
            principal_hint=principal_hint,
        )

        existing_receipt = db.execute(
            """SELECT state, denial_code, result_json, applied_seq, created_at_ns, peer_pid
               FROM command_receipt
               WHERE company_id = ? AND command_id = ?""",
            (writer.company_id, cmd_id),
        ).fetchone()
        if existing_receipt is not None:
            try:
                replayed, _ = admit_command_receipt(
                    db,
                    company_id=writer.company_id,
                    command_id=cmd_id,
                    actor_id=actor_id,
                    peer_pid=peer_pid,
                    command_type=DEMOTE_LIVE_ENVIRONMENT,
                    contract_version=EXECUTION_TIER_COMMAND,
                    payload_sha256=payload_sha,
                    preconditions=preconditions,
                    state=str(existing_receipt[0]),
                )
            except CommandConflict as error:
                raise BoardControlConflict(str(error)) from error
            return BoardCommandOutcome(
                command_id=replayed.command_id,
                command_type=replayed.command_type,
                state=replayed.state,
                disposition="applied" if replayed.state == "applied" else "denied",
                denial_code=replayed.denial_code,
                effective_revision=int(replayed.applied_seq or 0),
                recorded_at_ns=replayed.created_at_ns,
                peer_pid=replayed.peer_pid,
                result=json.loads(replayed.result_json) if replayed.result_json else {},
            )

        if not is_domain_installed(db, "paper"):
            raise BoardControlRefused("Paper domain is not installed")

        current_charter = str(
            db.execute(
                "SELECT charter_revision FROM company WHERE id = ?",
                (writer.company_id,),
            ).fetchone()[0]
        )
        now = time_ns()
        if charter != current_charter:
            denied = {"expected_charter_revision": current_charter}
            seq = append_audit_event(
                db,
                company_id=writer.company_id,
                domain="paper",
                entity_type="execution_environment_binding",
                entity_id=target_id,
                event_type="execution_tier.demote_denied_conflict",
                actor_id=actor_id,
                command_id=cmd_id,
                payload=denied,
                recorded_at_ns=now,
            )
            admit_command_receipt(
                db,
                company_id=writer.company_id,
                command_id=cmd_id,
                actor_id=actor_id,
                peer_pid=peer_pid,
                command_type=DEMOTE_LIVE_ENVIRONMENT,
                contract_version=EXECUTION_TIER_COMMAND,
                payload_sha256=payload_sha,
                preconditions=preconditions,
                state="denied",
                denial_code="conflicting_revision",
                result=denied,
                applied_seq=seq,
                now_ns=now,
            )
            writer.verify()
            return BoardCommandOutcome(
                command_id=cmd_id,
                command_type=DEMOTE_LIVE_ENVIRONMENT,
                state="denied",
                disposition="denied",
                denial_code="conflicting_revision",
                effective_revision=seq,
                recorded_at_ns=now,
                peer_pid=peer_pid,
                result=denied,
            )

        target_row = db.execute(
            f"{_SELECT_BINDING_COLS} WHERE company_id = ? AND binding_id = ?",
            (writer.company_id, target_id),
        ).fetchone()
        if target_row is None:
            raise BoardControlRefused("Target live binding not found")
        target = _row_to_binding(target_row)
        if target.tier != TIER_COMPANY_LIVE:
            raise BoardControlRefused("Only company_live bindings can be demoted")
        if target.status == "retired":
            raise BoardControlRefused("Target live binding is already retired")

        next_status = "suspended" if action == "suspend_live" else "retired"
        db.execute(
            """UPDATE execution_environment_binding
               SET status = ?, updated_at_ns = ?
               WHERE binding_id = ?""",
            (next_status, now, target_id),
        )
        if next_status == "retired":
            db.execute(
                """UPDATE company_paper_book
                   SET role = 'active', shadow_of_live_binding_id = NULL, updated_at_ns = ?
                   WHERE company_id = ? AND shadow_of_live_binding_id = ? AND role = 'shadow'""",
                (now, writer.company_id, target_id),
            )
        promotion_id = f"demote-{uuid4().hex[:16]}"
        result_body = {
            "promotion_id": promotion_id,
            "action": action,
            "target_binding_id": target_id,
            "new_status": next_status,
            "reconciliation_preserved": True,
            "company_paper_preserved": True,
        }
        seq = append_audit_event(
            db,
            company_id=writer.company_id,
            domain="paper",
            entity_type="execution_environment_binding",
            entity_id=target_id,
            event_type="execution_tier.live_demoted",
            actor_id=actor_id,
            command_id=cmd_id,
            payload=result_body,
            recorded_at_ns=now,
        )
        admit_command_receipt(
            db,
            company_id=writer.company_id,
            command_id=cmd_id,
            actor_id=actor_id,
            peer_pid=peer_pid,
            command_type=DEMOTE_LIVE_ENVIRONMENT,
            contract_version=EXECUTION_TIER_COMMAND,
            payload_sha256=payload_sha,
            preconditions=preconditions,
            state="applied",
            result=result_body,
            applied_seq=seq,
            now_ns=now,
        )
        db.execute(
            """INSERT INTO environment_promotion_record (
                   promotion_id, company_id, command_id, action,
                   from_binding_id, to_binding_id, qualification_receipt_id,
                   payload_hash, reason, recorded_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
            (
                promotion_id,
                writer.company_id,
                cmd_id,
                action,
                target_id,
                target_id,
                payload_sha,
                clean_reason,
                now,
            ),
        )
        writer.verify()
        return BoardCommandOutcome(
            command_id=cmd_id,
            command_type=DEMOTE_LIVE_ENVIRONMENT,
            state="applied",
            disposition="applied",
            denial_code=None,
            effective_revision=seq,
            recorded_at_ns=now,
            peer_pid=peer_pid,
            result=result_body,
        )


# ---------------------------------------------------------------------------
# Safe Paper -> Live Migration Engine
# ---------------------------------------------------------------------------

VALID_MIGRATION_MODES = frozenset({"clean_start", "drain_incumbent"})
VALID_SOURCE_PAPER_DISPOSITIONS = frozenset({"keep_shadow", "retire_source"})


def propose_paper_to_live_migration(
    writer: WriterLease,
    *,
    question_id: str,
    source_paper_binding_id: str,
    target_live_binding_id: str,
    qualification_receipt_id: str,
    created_by_agent_id: str,
    migration_mode: str = "clean_start",
    source_paper_disposition: str = "keep_shadow",
    title: str | None = None,
    summary: str | None = None,
    respond_by_ns: int | None = None,
    expected_core_digest: str | None = None,
) -> dict[str, Any]:
    """Nominate a soaked Company Paper Book for Live Trading via a Board decision_question.

    The resulting decision_question surfaces identically in Paper Trading, Live Trading,
    and the unified Approvals inbox. Approving from any surface triggers board.apply_decision
    -> execute_paper_to_live_migration.
    """
    writer.verify()
    _require_safe_id(question_id, "question_id")
    _require_safe_id(source_paper_binding_id, "source_paper_binding_id")
    _require_safe_id(target_live_binding_id, "target_live_binding_id")
    _require_safe_id(qualification_receipt_id, "qualification_receipt_id")
    _require_safe_id(created_by_agent_id, "created_by_agent_id")
    if migration_mode not in VALID_MIGRATION_MODES:
        raise ExecutionTierRefused(
            "migration_mode must be 'clean_start' or 'drain_incumbent'"
        )
    if source_paper_disposition not in VALID_SOURCE_PAPER_DISPOSITIONS:
        raise ExecutionTierRefused(
            "source_paper_disposition must be 'keep_shadow' or 'retire_source'"
        )

    core_digest = (
        compute_trading_core_digest()
        if expected_core_digest is None
        else expected_core_digest
    )

    with closing(open_reader(writer.store)) as db, read_tx(db):
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        if not is_domain_installed(db, "paper"):
            raise ExecutionTierRefused("Paper domain is not installed")
        source_book = read_company_paper_book_portfolio_from_db(
            db, writer.company_id, source_paper_binding_id
        )
        if source_book is None or source_book["status"] != "active":
            raise ExecutionTierRefused(
                "Source paper book must be an active company_paper binding"
            )
        if not source_book["soak_ready"]:
            raise ExecutionTierRefused(
                "Source paper book lacks soak evidence (recorded fills or legacy soak) required before nominating for live"
            )
        target_row = db.execute(
            f"{_SELECT_BINDING_COLS} WHERE company_id = ? AND binding_id = ?",
            (writer.company_id, target_live_binding_id),
        ).fetchone()
        if target_row is None:
            raise ExecutionTierRefused("Target live candidate binding was not found")
        target = _row_to_binding(target_row)
        if target.tier != TIER_COMPANY_LIVE or target.environment != ENV_LIVE:
            raise ExecutionTierRefused(
                "Migration target must be a company_live binding"
            )
        if target.status not in ("registered", "suspended"):
            raise ExecutionTierRefused(
                f"Target live binding in status '{target.status}' cannot be nominated"
            )
        qual_row = db.execute(
            """SELECT environment, instrument_rules_digest, core_digest,
                      verdict, cases_passed, cases_failed, cases_unrun
               FROM venue_qualification_receipt
               WHERE company_id = ? AND receipt_id = ?""",
            (writer.company_id, qualification_receipt_id),
        ).fetchone()
        if qual_row is None:
            raise ExecutionTierRefused(
                "Venue qualification receipt was not found for migration nomination"
            )
        if (
            str(qual_row[0]) != ENV_EXTERNAL_TEST
            or str(qual_row[3]) != "PASS"
            or int(qual_row[4]) <= 0
            or int(qual_row[5]) != 0
            or int(qual_row[6]) != 0
        ):
            raise ExecutionTierRefused(
                "Migration nomination requires a PASS external_test qualification receipt with zero failed/unrun cases"
            )
        if str(qual_row[1]) != target.instrument_rules_digest:
            raise ExecutionTierRefused(
                "Qualification receipt instrument_rules_digest does not match target live binding"
            )
        if str(qual_row[2]) != core_digest:
            raise ExecutionTierRefused(
                "Qualification receipt core_digest does not match current trading core"
            )

    book_name = str(source_book["book_name"])
    resolved_title = (
        title.strip()
        if isinstance(title, str) and title.strip()
        else f"Migrate Paper Book '{book_name}' to Live ({target.account_id})"
    )
    resolved_summary = (
        summary.strip()
        if isinstance(summary, str) and summary.strip()
        else (
            f"Company recommends promoting '{book_name}' ({source_book['fill_count']} paper fills, "
            f"strategy {source_book['strategy_release_id'] or 'baseline'}) to Live venue "
            f"{target.account_id} ({target.destination_id}) via {migration_mode} with zero virtual "
            f"position carryover."
        )
    )
    impact = {
        "action": "migrate_paper_to_live",
        "source_paper_binding_id": source_paper_binding_id,
        "source_book_name": book_name,
        "strategy_release_id": source_book["strategy_release_id"],
        "target_live_binding_id": target_live_binding_id,
        "target_account_id": target.account_id,
        "target_destination_id": target.destination_id,
        "qualification_receipt_id": qualification_receipt_id,
        "migration_mode": migration_mode,
        "source_paper_disposition": source_paper_disposition,
        "confirm_verb": "Approve Live Migration",
        "consequence_note": (
            f"Activates Live Trading on {target.account_id} starting flat (0 virtual positions copied) "
            f"under cap {target.exposure_limit_minor_units} {target.currency} minor units, and "
            + (
                "retains the source Paper Book in parallel as a Shadow Benchmark."
                if source_paper_disposition == "keep_shadow"
                else "retires the source Paper Book."
            )
        ),
        "what_changes": [
            {
                "label": "Live Venue",
                "value": f"{target.account_id} ({target.destination_id}) activated ({migration_mode})",
            },
            {
                "label": "Source Paper Book",
                "value": f"{book_name} transitions to "
                + (
                    "Shadow Benchmark"
                    if source_paper_disposition == "keep_shadow"
                    else "Retired"
                ),
            },
            {
                "label": "Position Carryover",
                "value": "0 virtual positions copied (real capital starts flat)",
            },
        ],
        "what_stays_untouched": [
            {
                "label": "Other Paper Books",
                "value": "Peer Company Paper Books continue running undisturbed",
            },
            {
                "label": "Agent Sandboxes",
                "value": "Personal per-Agent sandboxes stay strictly isolated",
            },
            {
                "label": "Host API Secrets",
                "value": f"Resolved only in memory via {target.credential_handle_ref}",
            },
        ],
    }

    payload_hash = record_decision_question(
        writer,
        question_id=question_id,
        kind="live_promotion",
        title=resolved_title,
        summary=resolved_summary,
        impact=impact,
        options=("approve", "decline"),
        created_by_agent_id=created_by_agent_id,
        respond_by_ns=respond_by_ns,
    )

    now = time_ns()
    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        db.execute(
            """UPDATE company_paper_book
               SET role = 'nominated', nominated_question_id = ?, updated_at_ns = ?
               WHERE company_id = ? AND binding_id = ?""",
            (question_id, now, writer.company_id, source_paper_binding_id),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="paper",
            entity_type="company_paper_book",
            entity_id=source_paper_binding_id,
            event_type="execution_tier.paper_migration_nominated",
            actor_id=created_by_agent_id,
            payload={
                "question_id": question_id,
                "source_paper_binding_id": source_paper_binding_id,
                "target_live_binding_id": target_live_binding_id,
                "qualification_receipt_id": qualification_receipt_id,
                "migration_mode": migration_mode,
                "source_paper_disposition": source_paper_disposition,
            },
            recorded_at_ns=now,
        )
        writer.verify()

    return {
        "question_id": question_id,
        "payload_hash": payload_hash,
        "source_paper_binding_id": source_paper_binding_id,
        "target_live_binding_id": target_live_binding_id,
        "qualification_receipt_id": qualification_receipt_id,
        "migration_mode": migration_mode,
        "source_paper_disposition": source_paper_disposition,
    }


def execute_paper_to_live_migration(
    writer: WriterLease,
    *,
    source_paper_binding_id: str,
    target_live_binding_id: str,
    qualification_receipt_id: str,
    migration_mode: str = "clean_start",
    source_paper_disposition: str = "keep_shadow",
    reason: str = "Board approved Paper-to-Live migration",
    question_id: str | None = None,
    actor_id: str = "board_apply_decision",
    expected_core_digest: str | None = None,
    now_ns: int | None = None,
) -> dict[str, Any]:
    """Fail-closed execution engine migrating a soaked Paper Book into Live Trading.

    Enforces:
    1. Zero active company/execution/live risk restrictions.
    2. Zero uncertain orders (and zero working/pending_cancel orders during drain_incumbent).
    3. Source Paper Book soak readiness.
    4. Passing external_test venue qualification receipt matching target rules + current core digest.
    5. Valid opaque live credential handle isolation.
    6. Explicit incumbent handling (clean_start refuses active incumbent; drain_incumbent safely retires it).
    7. Strictly zero virtual paper positions copied (positions_copied = 0, real capital starts flat).
    """
    writer.verify()
    _require_safe_id(source_paper_binding_id, "source_paper_binding_id")
    _require_safe_id(target_live_binding_id, "target_live_binding_id")
    _require_safe_id(qualification_receipt_id, "qualification_receipt_id")
    clean_reason = _require_text(reason, "reason", max_len=1000)
    if migration_mode not in VALID_MIGRATION_MODES:
        raise ExecutionTierRefused(
            "migration_mode must be 'clean_start' or 'drain_incumbent'"
        )
    if source_paper_disposition not in VALID_SOURCE_PAPER_DISPOSITIONS:
        raise ExecutionTierRefused(
            "source_paper_disposition must be 'keep_shadow' or 'retire_source'"
        )
    if question_id is not None:
        _require_safe_id(question_id, "question_id")

    core_digest = (
        compute_trading_core_digest()
        if expected_core_digest is None
        else expected_core_digest
    )
    now = time_ns() if now_ns is None else now_ns

    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise ExecutionTierRefused("Company identity mismatch")
        if not is_domain_installed(db, "paper"):
            raise ExecutionTierRefused("Paper domain is not installed")

        # Gate 1: Zero active company/execution/live restrictions.
        restr_row = db.execute(
            """SELECT restriction_id, scope_ref, reason
               FROM restriction_cause
               WHERE company_id = ? AND active = 1
                 AND scope_ref IN ('company:all', 'execution:all', 'execution:live')
               LIMIT 1""",
            (writer.company_id,),
        ).fetchone()
        if restr_row is not None:
            raise ExecutionTierRefused(
                f"Migration blocked by active restriction {restr_row[0]} ({restr_row[1]}): {restr_row[2]}"
            )

        # Gate 2: Zero unresolved paper orders or pending binding changes.
        unresolved_count = int(
            db.execute(
                """SELECT COUNT(*) FROM paper_order o
                   LEFT JOIN paper_cancellation c ON c.order_id = o.order_id
                   WHERE c.order_id IS NULL
                     AND (SELECT COALESCE(SUM(f.quantity), 0) FROM paper_fill f WHERE f.order_id = o.order_id) < o.quantity"""
            ).fetchone()[0]
        )
        if unresolved_count > 0:
            raise ExecutionTierRefused(
                f"Migration blocked: {unresolved_count} unresolved order(s) must settle or cancel before live activation"
            )
        pending_binding_changes = int(
            db.execute(
                "SELECT COUNT(*) FROM paper_binding_change WHERE effective_at_ns IS NULL"
            ).fetchone()[0]
        )
        if pending_binding_changes > 0:
            raise ExecutionTierRefused(
                "Migration blocked: pending paper_binding_change must finalize before live activation"
            )

        # Gate 3: Source Paper Book active & soak ready.
        source_book = read_company_paper_book_portfolio_from_db(
            db, writer.company_id, source_paper_binding_id
        )
        if source_book is None or source_book["status"] != "active":
            raise ExecutionTierRefused(
                "Source paper book must be an active company_paper binding"
            )
        if not source_book["soak_ready"]:
            raise ExecutionTierRefused(
                "Source paper book lacks soak evidence required for live migration"
            )
        if (
            source_paper_disposition == "retire_source"
            and source_book["open_orders_count"] > 0
        ):
            raise ExecutionTierRefused(
                f"Cannot retire source paper book while {source_book['open_orders_count']} resting order(s) remain open"
            )

        # Gate 4: Target Live binding valid & opaque credential handle verified.
        target_row = db.execute(
            f"{_SELECT_BINDING_COLS} WHERE company_id = ? AND binding_id = ?",
            (writer.company_id, target_live_binding_id),
        ).fetchone()
        if target_row is None:
            raise ExecutionTierRefused("Target live candidate binding was not found")
        target = _row_to_binding(target_row)
        if target.tier != TIER_COMPANY_LIVE or target.environment != ENV_LIVE:
            raise ExecutionTierRefused(
                "Migration target must be a company_live binding"
            )
        if target.status not in ("registered", "suspended"):
            raise ExecutionTierRefused(
                f"Cannot migrate into target live binding in status '{target.status}'"
            )
        _validate_credential_handle(
            target.credential_handle_ref, environment=ENV_LIVE, required=True
        )

        # Gate 5: Matching PASS external_test venue qualification receipt & current core digest.
        qual_row = db.execute(
            """SELECT environment, instrument_rules_digest, core_digest,
                      verdict, cases_passed, cases_failed, cases_unrun
               FROM venue_qualification_receipt
               WHERE company_id = ? AND receipt_id = ?""",
            (writer.company_id, qualification_receipt_id),
        ).fetchone()
        if qual_row is None:
            raise ExecutionTierRefused(
                "External testnet venue qualification receipt is missing"
            )
        if (
            str(qual_row[0]) != ENV_EXTERNAL_TEST
            or str(qual_row[3]) != "PASS"
            or int(qual_row[4]) <= 0
            or int(qual_row[5]) != 0
            or int(qual_row[6]) != 0
        ):
            raise ExecutionTierRefused(
                "Migration requires a PASS external_test qualification receipt with zero failures or unrun cases"
            )
        if str(qual_row[1]) != target.instrument_rules_digest:
            raise ExecutionTierRefused(
                "Qualification receipt instrument_rules_digest does not match target live binding"
            )
        if str(qual_row[2]) != core_digest:
            raise ExecutionTierRefused(
                "Qualification receipt core_digest does not match current trading core"
            )

        # Gate 6: Incumbent live binding handling.
        incumbent_row = db.execute(
            """SELECT binding_id FROM execution_environment_binding
               WHERE company_id = ? AND tier = ? AND status = 'active'""",
            (writer.company_id, TIER_COMPANY_LIVE),
        ).fetchone()
        replaced_live_binding_id: str | None = None
        if incumbent_row is not None:
            incumbent_id = str(incumbent_row[0])
            if migration_mode == "clean_start":
                raise ExecutionTierRefused(
                    f"Active incumbent live binding ({incumbent_id}) exists; use migration_mode='drain_incumbent' or demote incumbent first"
                )
            replaced_live_binding_id = incumbent_id
            db.execute(
                """UPDATE execution_environment_binding
                   SET status = 'retired', updated_at_ns = ?
                   WHERE binding_id = ?""",
                (now, incumbent_id),
            )
            db.execute(
                """UPDATE company_paper_book
                   SET role = 'active', shadow_of_live_binding_id = NULL, updated_at_ns = ?
                   WHERE company_id = ? AND shadow_of_live_binding_id = ? AND role = 'shadow'""",
                (now, writer.company_id, incumbent_id),
            )

        # Activate target Live binding (real capital starts flat: positions_copied = 0).
        db.execute(
            """UPDATE execution_environment_binding
               SET status = 'active', updated_at_ns = ?
               WHERE binding_id = ?""",
            (now, target_live_binding_id),
        )

        # Transition source Paper Book to shadow benchmark OR retired.
        if source_paper_disposition == "keep_shadow":
            db.execute(
                """UPDATE company_paper_book
                   SET role = 'shadow',
                       shadow_of_live_binding_id = ?,
                       nominated_question_id = COALESCE(?, nominated_question_id),
                       updated_at_ns = ?
                   WHERE company_id = ? AND binding_id = ?""",
                (
                    target_live_binding_id,
                    question_id,
                    now,
                    writer.company_id,
                    source_paper_binding_id,
                ),
            )
            source_book_final_role = "shadow"
        else:
            db.execute(
                """UPDATE company_paper_book
                   SET role = 'retired',
                       shadow_of_live_binding_id = NULL,
                       nominated_question_id = COALESCE(?, nominated_question_id),
                       updated_at_ns = ?
                   WHERE company_id = ? AND binding_id = ?""",
                (question_id, now, writer.company_id, source_paper_binding_id),
            )
            db.execute(
                """UPDATE execution_environment_binding
                   SET status = 'retired', updated_at_ns = ?
                   WHERE binding_id = ?""",
                (now, source_paper_binding_id),
            )
            source_book_final_role = "retired"

        migration_id = f"mig-{uuid4().hex[:16]}"
        pre_check_summary = {
            "active_restrictions": 0,
            "unresolved_orders": unresolved_count,
            "pending_binding_changes": pending_binding_changes,
            "source_book_fills": source_book["fill_count"],
            "source_book_soak_ready": True,
            "qualification_cases_passed": int(qual_row[4]),
            "core_digest_verified": True,
            "positions_copied": 0,
        }
        db.execute(
            """INSERT INTO paper_live_migration_record (
                   migration_id, company_id, question_id,
                   source_paper_binding_id, target_live_binding_id,
                   replaced_live_binding_id, qualification_receipt_id,
                   migration_mode, source_paper_disposition, positions_copied,
                   pre_check_summary_json, reason, recorded_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
            (
                migration_id,
                writer.company_id,
                question_id,
                source_paper_binding_id,
                target_live_binding_id,
                replaced_live_binding_id,
                qualification_receipt_id,
                migration_mode,
                source_paper_disposition,
                _canonical_json(pre_check_summary),
                clean_reason,
                now,
            ),
        )
        result = {
            "migration_id": migration_id,
            "question_id": question_id,
            "source_paper_binding_id": source_paper_binding_id,
            "source_book_name": source_book["book_name"],
            "source_book_final_role": source_book_final_role,
            "target_live_binding_id": target_live_binding_id,
            "target_account_id": target.account_id,
            "replaced_live_binding_id": replaced_live_binding_id,
            "qualification_receipt_id": qualification_receipt_id,
            "migration_mode": migration_mode,
            "source_paper_disposition": source_paper_disposition,
            "positions_copied": 0,
            "pre_check_summary": pre_check_summary,
            "recorded_at_ns": now,
        }
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="paper",
            entity_type="paper_live_migration_record",
            entity_id=migration_id,
            event_type="execution_tier.paper_migrated_to_live",
            actor_id=actor_id,
            payload=result,
            recorded_at_ns=now,
        )
        writer.verify()
        return result


def read_execution_tiers_snapshot(db: Connection, company_id: str) -> dict[str, Any]:
    """Read a redacted snapshot of all three execution tiers inside an open read_tx."""
    if not is_domain_installed(db, "paper"):
        return {
            "paper_domain_installed": False,
            "company_paper": None,
            "company_paper_books": [],
            "company_paper_soak_ready": False,
            "company_live": None,
            "live_candidates": [],
            "agent_sandboxes": [],
            "qualification_receipts": [],
            "promotion_history": [],
            "migration_history": [],
            "current_core_digest": compute_trading_core_digest(),
        }

    bindings = [
        _row_to_binding(r)
        for r in db.execute(
            f"""{_SELECT_BINDING_COLS}
                WHERE company_id = ?
                ORDER BY created_at_ns ASC, binding_id ASC""",
            (company_id,),
        ).fetchall()
    ]
    company_paper = next(
        (
            asdict(b)
            for b in bindings
            if b.tier == TIER_COMPANY_PAPER and b.status == "active"
        ),
        None,
    )
    if company_paper is None:
        legacy = db.execute(
            """SELECT revision, account_id, destination_id, connector_revision,
                      instrument_rules_digest, currency, starting_minor_units,
                      exposure_limit_minor_units
               FROM paper_binding WHERE singleton = 1 AND company_id = ?""",
            (company_id,),
        ).fetchone()
        if legacy is not None:
            company_paper = {
                "binding_id": f"legacy:{legacy[0]}",
                "company_id": company_id,
                "tier": TIER_COMPANY_PAPER,
                "environment": ENV_PAPER,
                "owner_agent_id": None,
                "account_id": str(legacy[1]),
                "destination_id": str(legacy[2]),
                "connector_revision": str(legacy[3]),
                "instrument_rules_digest": str(legacy[4]),
                "currency": str(legacy[5]),
                "starting_minor_units": int(legacy[6]),
                "exposure_limit_minor_units": int(legacy[7]),
                "fee_bps": 0,
                "slippage_bps": 0,
                "quote_source": "legacy_paper_binding",
                "credential_handle_ref": None,
                "status": "active",
            }

    company_live = next(
        (
            asdict(b)
            for b in bindings
            if b.tier == TIER_COMPANY_LIVE and b.status == "active"
        ),
        None,
    )
    charter_row = db.execute(
        "SELECT charter_revision FROM company WHERE id = ?",
        (company_id,),
    ).fetchone()
    charter_revision = str(charter_row[0]) if charter_row is not None else ""
    current_core = compute_trading_core_digest()

    qual_receipts = [
        {
            "receipt_id": str(r[0]),
            "adapter_revision": str(r[1]),
            "environment": str(r[2]),
            "destination_id": str(r[3]),
            "instrument_rules_digest": str(r[4]),
            "core_digest": str(r[5]),
            "verdict": str(r[6]),
            "cases_passed": int(r[7]),
            "cases_failed": int(r[8]),
            "cases_unrun": int(r[9]),
            "evidence_sha256": str(r[10]),
            "recorded_at_ns": int(r[11]),
        }
        for r in db.execute(
            """SELECT receipt_id, adapter_revision, environment, destination_id,
                      instrument_rules_digest, core_digest, verdict,
                      cases_passed, cases_failed, cases_unrun,
                      evidence_sha256, recorded_at_ns
               FROM venue_qualification_receipt
               WHERE company_id = ?
               ORDER BY recorded_at_ns DESC LIMIT 20""",
            (company_id,),
        ).fetchall()
    ]

    live_candidates = []
    for b in bindings:
        if b.tier == TIER_COMPANY_LIVE and b.status != "active":
            matching = next(
                (
                    q
                    for q in qual_receipts
                    if q["verdict"] == "PASS"
                    and q["cases_failed"] == 0
                    and q["cases_unrun"] == 0
                    and q["adapter_revision"] == b.connector_revision
                    and q["instrument_rules_digest"] == b.instrument_rules_digest
                    and q["core_digest"] == current_core
                ),
                None,
            )
            matching_id = str(matching["receipt_id"]) if matching is not None else None
            promo_hash = (
                build_promotion_payload_hash(
                    company_id=company_id,
                    charter_revision=charter_revision,
                    target_binding=b,
                    qualification_receipt_id=matching_id,
                )
                if matching_id is not None and charter_revision
                else None
            )
            live_candidates.append(
                asdict(b)
                | {
                    "matching_qualification_receipt_id": matching_id,
                    "promotion_payload_hash": promo_hash,
                }
            )
    agent_sandboxes = []
    for b in bindings:
        if b.tier == TIER_AGENT_SANDBOX and b.status == "active" and b.owner_agent_id:
            portfolio = read_agent_sandbox_portfolio_from_db(
                db, company_id, b.owner_agent_id
            )
            if portfolio is not None:
                agent_sandboxes.append(
                    {
                        "agent_id": b.owner_agent_id,
                        "binding_id": b.binding_id,
                        "account_id": b.account_id,
                        "currency": b.currency,
                        "fee_bps": b.fee_bps,
                        "slippage_bps": b.slippage_bps,
                        "quote_source": b.quote_source,
                        "cash_minor_units": portfolio["cash_minor_units"],
                        "gross_exposure_minor_units": portfolio[
                            "gross_exposure_minor_units"
                        ],
                        "realized_pnl_minor_units": portfolio[
                            "realized_pnl_minor_units"
                        ],
                        "total_fees_minor_units": portfolio["total_fees_minor_units"],
                        "open_positions_count": sum(
                            1 for p in portfolio["positions"] if p["quantity"] != 0
                        ),
                        "fill_count": portfolio["fill_count"],
                    }
                )

    promotions = [
        {
            "promotion_id": str(r[0]),
            "command_id": str(r[1]),
            "action": str(r[2]),
            "from_binding_id": None if r[3] is None else str(r[3]),
            "to_binding_id": str(r[4]),
            "qualification_receipt_id": None if r[5] is None else str(r[5]),
            "payload_hash": str(r[6]),
            "reason": str(r[7]),
            "recorded_at_ns": int(r[8]),
        }
        for r in db.execute(
            """SELECT promotion_id, command_id, action, from_binding_id,
                      to_binding_id, qualification_receipt_id, payload_hash,
                      reason, recorded_at_ns
               FROM environment_promotion_record
               WHERE company_id = ?
               ORDER BY recorded_at_ns DESC LIMIT 20""",
            (company_id,),
        ).fetchall()
    ]
    company_paper_books = []
    binding_account_by_id = {b.binding_id: b.account_id for b in bindings}
    book_name_by_id: dict[str, str] = {}
    for b in bindings:
        if b.tier == TIER_COMPANY_PAPER:
            book = read_company_paper_book_portfolio_from_db(
                db, company_id, b.binding_id
            )
            if book is not None:
                company_paper_books.append(book)
                book_name_by_id[b.binding_id] = str(book["book_name"])

    migrations = [
        {
            "migration_id": str(r[0]),
            "question_id": None if r[1] is None else str(r[1]),
            "source_paper_binding_id": str(r[2]),
            "source_book_name": book_name_by_id.get(
                str(r[2]), binding_account_by_id.get(str(r[2]), str(r[2]))
            ),
            "target_live_binding_id": str(r[3]),
            "target_account_id": binding_account_by_id.get(str(r[3]), str(r[3])),
            "replaced_live_binding_id": None if r[4] is None else str(r[4]),
            "qualification_receipt_id": str(r[5]),
            "migration_mode": str(r[6]),
            "source_paper_disposition": str(r[7]),
            "positions_copied": int(r[8]),
            "pre_check_summary": json.loads(str(r[9])),
            "reason": str(r[10]),
            "recorded_at_ns": int(r[11]),
        }
        for r in db.execute(
            """SELECT migration_id, question_id, source_paper_binding_id,
                      target_live_binding_id, replaced_live_binding_id,
                      qualification_receipt_id, migration_mode,
                      source_paper_disposition, positions_copied,
                      pre_check_summary_json, reason, recorded_at_ns
               FROM paper_live_migration_record
               WHERE company_id = ?
               ORDER BY recorded_at_ns DESC LIMIT 20""",
            (company_id,),
        ).fetchall()
    ]
    return {
        "paper_domain_installed": True,
        "charter_revision": charter_revision,
        "company_paper": company_paper,
        "company_paper_books": company_paper_books,
        "company_paper_soak_ready": _has_company_paper_soak_evidence(db, company_id),
        "company_live": company_live,
        "live_candidates": live_candidates,
        "agent_sandboxes": agent_sandboxes,
        "qualification_receipts": qual_receipts,
        "promotion_history": promotions,
        "migration_history": migrations,
        "current_core_digest": current_core,
    }


# ---------------------------------------------------------------------------
# Outer Capability Broker Tools for Per-Agent Sandbox (Server-Bound Identity)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSandboxTools:
    """Server-bound Outer Capability Broker handler for one Agent's private paper sandbox.

    Identity is strictly fixed to `agent_id` by the host; tool arguments can never
    supply or override `agent_id`, `company_id`, or target company_paper / company_live.
    """

    writer: WriterLease
    agent_id: str

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ExecutionTierRefused("Expected JSON object tool arguments")
        if "agent_id" in arguments or "company_id" in arguments or "tier" in arguments:
            raise ExecutionTierRefused(
                "Server-bound identity fields cannot be supplied in sandbox tool arguments"
            )
        if name == "open_agent_sandbox":
            allowed = {
                "account_id",
                "currency",
                "starting_minor_units",
                "exposure_limit_minor_units",
                "fee_bps",
                "slippage_bps",
                "quote_source",
            }
            if not set(arguments).issubset(allowed) or "account_id" not in arguments:
                raise ExecutionTierRefused("Invalid open_agent_sandbox arguments")
            binding = open_agent_sandbox(
                self.writer, agent_id=self.agent_id, **arguments
            )
            return asdict(binding)
        if name == "submit_agent_sandbox_order":
            allowed = {
                "action_id",
                "instrument_id",
                "side",
                "quantity",
                "limit_minor_units",
                "market_price_minor_units",
                "quote_observed_at_ns",
                "fill_mode",
            }
            required = {
                "action_id",
                "instrument_id",
                "side",
                "quantity",
                "limit_minor_units",
                "market_price_minor_units",
            }
            if not set(arguments).issubset(allowed) or not required.issubset(arguments):
                raise ExecutionTierRefused(
                    "Invalid submit_agent_sandbox_order arguments"
                )
            receipt = submit_agent_sandbox_order(
                self.writer, agent_id=self.agent_id, **arguments
            )
            return asdict(receipt)
        if name == "cancel_agent_sandbox_order":
            if set(arguments) != {"order_id"}:
                raise ExecutionTierRefused(
                    "Invalid cancel_agent_sandbox_order arguments"
                )
            return cancel_agent_sandbox_order(
                self.writer, agent_id=self.agent_id, order_id=arguments["order_id"]
            )
        if name == "read_agent_sandbox_portfolio":
            if arguments:
                raise ExecutionTierRefused(
                    "read_agent_sandbox_portfolio accepts no arguments"
                )
            portfolio = read_agent_sandbox_portfolio(
                self.writer.store, self.writer.company_id, self.agent_id
            )
            return {"portfolio": portfolio}
        raise ExecutionTierRefused(f"Unsupported agent sandbox tool: {name}")
