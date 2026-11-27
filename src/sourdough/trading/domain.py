"""Trading domain shared constants, errors, dataclasses, and core digest."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

TIER_AGENT_SANDBOX = "agent_sandbox"
TIER_COMPANY_PAPER = "company_paper"
TIER_COMPANY_LIVE = "company_live"
VALID_TIERS = frozenset({TIER_AGENT_SANDBOX, TIER_COMPANY_PAPER, TIER_COMPANY_LIVE})

ENV_AGENT_SANDBOX = "agent_sandbox"
ENV_PAPER = "paper"
ENV_EXTERNAL_TEST = "external_test"
ENV_LIVE = "live"
VALID_ENVIRONMENTS = frozenset(
    {ENV_AGENT_SANDBOX, ENV_PAPER, ENV_EXTERNAL_TEST, ENV_LIVE}
)

EXECUTION_TIER_COMMAND = "execution-tier-command-v1"
PROMOTE_EXECUTION_ENVIRONMENT = "promote_execution_environment"
DEMOTE_LIVE_ENVIRONMENT = "demote_live_environment"

VALID_MIGRATION_MODES = frozenset({"clean_start", "drain_incumbent"})
VALID_SOURCE_PAPER_DISPOSITIONS = frozenset({"keep_shadow", "retire_source"})

_MAX_I64 = 2**63 - 1
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CREDENTIAL_HANDLE_RE = re.compile(
    r"^(?:keychain|envref):[A-Za-z0-9][A-Za-z0-9._:/-]{2,120}$"
)
_NON_LIVE_HANDLE_MARKERS = ("paper", "testnet", "sandbox", "mock", "demo")
_CORE_DIGEST_FILES = (
    "domain.py",
    "accounts.py",
    "ledger.py",
    "migration.py",
    "sandbox_tools.py",
    "risk.py",
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
    binding_revision: str = "1"
    book_name: str | None = None
    strategy_release_id: str | None = None
    book_role: str | None = None
    shadow_of_live_binding_id: str | None = None
    nominated_question_id: str | None = None


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
        binding_revision=str(row[19]) if len(row) > 19 and row[19] is not None else "1",
        book_name=None if len(row) <= 20 or row[20] is None else str(row[20]),
        strategy_release_id=None if len(row) <= 21 or row[21] is None else str(row[21]),
        book_role=None if len(row) <= 22 or row[22] is None else str(row[22]),
        shadow_of_live_binding_id=(
            None if len(row) <= 23 or row[23] is None else str(row[23])
        ),
        nominated_question_id=(
            None if len(row) <= 24 or row[24] is None else str(row[24])
        ),
    )


_SELECT_BINDING_COLS = """SELECT binding_id, company_id, tier, environment, owner_agent_id,
       account_id, destination_id, connector_revision, instrument_rules_digest,
       currency, starting_minor_units, exposure_limit_minor_units, fee_bps,
       slippage_bps, quote_source, credential_handle_ref, status,
       created_at_ns, updated_at_ns, binding_revision, book_name,
       strategy_release_id, book_role, shadow_of_live_binding_id,
       nominated_question_id
FROM execution_environment_binding"""


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
