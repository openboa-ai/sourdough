"""Host credential composition, default live venue resolution, and live testnet qualification."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from ..storage.db import open_reader, read_tx
from ..storage.schema import is_domain_installed
from ..writer_lease import WriterLease
from .accounts import record_venue_qualification_receipt
from .binance_venue import (
    BINANCE_ADAPTER_REVISION,
    MARKET_SPOT,
    MARKET_USDM_FUTURES,
    BinanceVenueAdapter,
    BinanceVenueConfig,
)
from .credentials import (
    ApiCredentialPair,
    CredentialProvider,
    CredentialRefused,
    EnvRefCredentialProvider,
    InMemoryTestCredentialProvider,
    MacOsKeychainCredentialProvider,
    parse_credential_handle,
)
from .domain import (
    ENV_EXTERNAL_TEST,
    ENV_LIVE,
    ENV_PAPER,
    TIER_COMPANY_LIVE,
    ExecutionTierRefused,
    compute_trading_core_digest,
)
from .external_venue import (
    ExternalEnvironmentMismatch,
    ExternalVenueAdapter,
)
from .venue_qualification import QualificationCaseResult, QualificationReport


class CompositeCredentialProvider:
    """Dispatch `envref:` and `keychain:` credential handles to their dedicated host providers."""

    def __init__(
        self,
        *,
        env_provider: CredentialProvider | None = None,
        keychain_provider: CredentialProvider | None = None,
    ) -> None:
        self._env = EnvRefCredentialProvider() if env_provider is None else env_provider
        self._keychain = (
            MacOsKeychainCredentialProvider()
            if keychain_provider is None
            else keychain_provider
        )

    def resolve(
        self, handle_ref: str, *, expected_environment: str
    ) -> ApiCredentialPair:
        parsed = parse_credential_handle(handle_ref)
        if parsed.scheme == "envref":
            return self._env.resolve(
                handle_ref, expected_environment=expected_environment
            )
        if parsed.scheme == "keychain":
            return self._keychain.resolve(
                handle_ref, expected_environment=expected_environment
            )
        raise CredentialRefused(f"Unsupported credential scheme: {parsed.scheme}")


_BINANCE_SPOT_CONNECTORS = frozenset({"binance-spot-v1", "binance-spot-rest-v1"})
_BINANCE_USDM_FUTURES_CONNECTORS = frozenset(
    {
        "binance-futures-v1",
        "binance-usdm-v1",
        "binance-usdm-futures-v1",
        "binance-fapi-v1",
    }
)
_BINANCE_GENERIC_REST_CONNECTORS = frozenset({BINANCE_ADAPTER_REVISION})
_UNSUPPORTED_MARKET_TOKENS = (
    "options",
    "option",
    "eapi",
    "coinm",
    "coin-m",
    "dapi",
    "margin",
    "portfolio",
)
_FUTURES_TOKENS = ("futures", "usdm", "usd-m", "fapi", "perp")
_SPOT_TOKENS = ("spot",)

_UNRUN_EXECUTION_QUALIFICATION_CASES: tuple[str, ...] = (
    "normal_submit_and_fill",
    "over_limit_rejected_zero_fill",
    "partial_fill_cancel_residual_position",
    "lost_reply_recovery_no_duplicate",
)


def infer_binance_market_kind(connector_revision: str, destination_id: str) -> str:
    """Validate Binance connector revision independently of destination and resolve Spot vs USD-M Futures."""
    conn_low = connector_revision.strip().lower()
    dest_low = destination_id.strip().lower()
    if (
        conn_low not in _BINANCE_SPOT_CONNECTORS
        and conn_low not in _BINANCE_USDM_FUTURES_CONNECTORS
        and conn_low not in _BINANCE_GENERIC_REST_CONNECTORS
    ):
        raise ExecutionTierRefused(
            f"Unsupported live venue connector '{connector_revision}' for destination '{destination_id}'"
        )
    if any(tok in dest_low for tok in _UNSUPPORTED_MARKET_TOKENS):
        raise ExecutionTierRefused(
            f"Unsupported Binance market destination '{destination_id}' for connector '{connector_revision}'"
        )
    dest_has_futures = any(tok in dest_low for tok in _FUTURES_TOKENS)
    dest_has_spot = any(tok in dest_low for tok in _SPOT_TOKENS)
    if dest_has_futures and dest_has_spot:
        raise ExecutionTierRefused(
            f"Contradictory Binance market destination '{destination_id}'"
        )
    if conn_low in _BINANCE_SPOT_CONNECTORS:
        if dest_has_futures:
            raise ExecutionTierRefused(
                f"Contradictory spot connector '{connector_revision}' with futures destination '{destination_id}'"
            )
        return MARKET_SPOT
    if conn_low in _BINANCE_USDM_FUTURES_CONNECTORS:
        if dest_has_spot:
            raise ExecutionTierRefused(
                f"Contradictory futures connector '{connector_revision}' with spot destination '{destination_id}'"
            )
        return MARKET_USDM_FUTURES
    # Generic Binance REST connector ('binance-rest-v1'): require explicit Spot or USD-M destination.
    if dest_has_futures:
        return MARKET_USDM_FUTURES
    if dest_has_spot:
        return MARKET_SPOT
    raise ExecutionTierRefused(
        f"Ambiguous destination '{destination_id}' for connector '{connector_revision}'; "
        "specify explicit spot or usdm/futures market identifier"
    )


def build_default_live_venue_resolver(
    store: Path,
    company_id: str,
    *,
    credential_provider: CredentialProvider | None = None,
    environment: str = ENV_LIVE,
    base_url_override: str | None = None,
    allow_loopback_override: bool = False,
    allow_recovery_statuses: bool = False,
) -> Callable[[str], ExternalVenueAdapter | None]:
    """Return a fail-closed binding_id -> ExternalVenueAdapter resolver backed by host credentials.

    When `allow_recovery_statuses` is False (default, used for new order submission),
    only `status = 'active'` bindings resolve. When True (used for reconciliation/recovery
    of existing unresolved obligations), `suspended` and `retired` live bindings also
    resolve so suspension never blocks external lookup and settlement.
    """
    if environment not in (ENV_LIVE, ENV_EXTERNAL_TEST):
        raise ExecutionTierRefused(
            f"Unsupported live venue resolver environment '{environment}'"
        )
    allowed_statuses = (
        frozenset({"active", "suspended", "retired"})
        if allow_recovery_statuses
        else frozenset({"active"})
    )
    creds = (
        CompositeCredentialProvider()
        if credential_provider is None
        else credential_provider
    )
    store_path = Path(store)

    def _resolve(binding_id: str) -> ExternalVenueAdapter | None:
        if not isinstance(binding_id, str) or not binding_id.strip():
            return None
        try:
            with closing(open_reader(store_path)) as db, read_tx(db):
                if not is_domain_installed(db, "trading"):
                    return None
                row = db.execute(
                    """SELECT tier, environment, status, account_id, destination_id,
                              connector_revision, credential_handle_ref
                       FROM execution_environment_binding
                       WHERE company_id = ? AND binding_id = ?""",
                    (company_id, binding_id),
                ).fetchone()
        except sqlite3.Error:
            return None

        if row is None:
            return None
        tier, row_env, status, account_id, destination_id, connector_rev, handle_ref = (
            row
        )
        if (
            str(tier) != TIER_COMPANY_LIVE
            or str(row_env) != ENV_LIVE
            or str(status) not in allowed_statuses
            or not handle_ref
        ):
            return None

        market_kind = infer_binance_market_kind(str(connector_rev), str(destination_id))
        cfg = BinanceVenueConfig(
            environment=environment,
            market_kind=market_kind,
            account_id=str(account_id),
            destination_id=str(destination_id),
            credential_handle_ref=str(handle_ref),
            base_url_override=base_url_override,
            allow_loopback_override=allow_loopback_override,
        )
        return BinanceVenueAdapter(cfg, creds)

    return _resolve


def run_live_binance_testnet_qualification(
    writer: WriterLease,
    *,
    adapter: BinanceVenueAdapter,
    instrument_rules_digest: str,
    symbol: str = "BTCUSDT",
    receipt_id: str | None = None,
) -> QualificationReport:
    """Qualify a live external Binance Testnet endpoint (read/sign/reconcile/isolation) and seal receipt."""
    if adapter.config.environment != ENV_EXTERNAL_TEST:
        raise ExternalEnvironmentMismatch(
            "Live testnet qualification must target environment='external_test' only"
        )

    results: list[QualificationCaseResult] = []

    # Case 1: Authenticated signed account observation against live Testnet.
    snap = adapter.observe_account()
    results.append(
        QualificationCaseResult(
            case_id="signed_account_observation",
            passed=(
                snap.environment == ENV_EXTERNAL_TEST
                and snap.account_id == adapter.config.account_id
                and snap.observed_at_ms > 0
            ),
            detail=f"balances={len(snap.balances)}, positions={len(snap.positions)}",
        )
    )

    # Case 2: Absent order probe must return absent_unsealed (never falsely sealed).
    probe_coid = adapter.client_order_id_for(f"qual-absent-{uuid4().hex[:10]}")
    absent = adapter.query_order(symbol, probe_coid)
    results.append(
        QualificationCaseResult(
            case_id="absent_order_remains_unsealed",
            passed=(absent.status == "absent_unsealed" and not absent.seals_absence),
            detail=f"status={absent.status}, seals_absence={absent.seals_absence}",
        )
    )

    # Case 3: Fill history & reconciliation query succeeds over signed Testnet API.
    recon = adapter.reconcile_external(symbol, frozenset())
    results.append(
        QualificationCaseResult(
            case_id="external_reconciliation_probe",
            passed=(recon.environment == ENV_EXTERNAL_TEST),
            detail=(
                f"clean={recon.clean}, unattributed_fills={len(recon.unattributed_fills)}, "
                f"unattributed_positions={len(recon.unattributed_positions)}"
            ),
        )
    )

    # Case 4: Paper environment & live credential handle isolation invariants.
    paper_blocked = False
    try:
        BinanceVenueConfig(
            environment=ENV_PAPER,
            market_kind=adapter.config.market_kind,
            account_id="bad-paper",
            destination_id="bad-dest",
            credential_handle_ref="envref:BINANCE_TESTNET_QUAL",
        )
    except ExternalEnvironmentMismatch:
        paper_blocked = True

    live_handle_blocked = False
    try:
        InMemoryTestCredentialProvider(
            {"envref:BINANCE_LIVE_PROD": ("k", "s")}
        ).resolve("envref:BINANCE_LIVE_PROD", expected_environment=ENV_EXTERNAL_TEST)
    except CredentialRefused:
        live_handle_blocked = True

    results.append(
        QualificationCaseResult(
            case_id="environment_and_credential_isolation",
            passed=(paper_blocked and live_handle_blocked),
            detail=f"paper_blocked={paper_blocked}, live_handle_blocked={live_handle_blocked}",
        )
    )

    passed_count = sum(1 for c in results if c.passed)
    failed_count = len(results) - passed_count
    unrun_count = len(_UNRUN_EXECUTION_QUALIFICATION_CASES)
    # Read-only Testnet probes never exercise submission, fill, cancellation, or
    # lost-reply recovery; fail closed with INCOMPLETE so they cannot satisfy live activation.
    verdict = "FAIL" if failed_count > 0 else "INCOMPLETE"
    evidence_sha = sha256(
        json.dumps(
            {
                "cases": [asdict(c) for c in results],
                "unrun_cases": list(_UNRUN_EXECUTION_QUALIFICATION_CASES),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    receipt = record_venue_qualification_receipt(
        writer,
        receipt_id=receipt_id
        or f"qual-testnet-{adapter.config.market_kind}-{uuid4().hex[:10]}",
        adapter_revision=adapter.capabilities.adapter_revision,
        environment=ENV_EXTERNAL_TEST,
        destination_id=adapter.config.destination_id,
        instrument_rules_digest=instrument_rules_digest,
        core_digest=compute_trading_core_digest(),
        verdict=verdict,
        cases_passed=passed_count,
        cases_failed=failed_count,
        cases_unrun=unrun_count,
        evidence_sha256=evidence_sha,
    )
    return QualificationReport(receipt=receipt, cases=tuple(results))


__all__ = [
    "CompositeCredentialProvider",
    "build_default_live_venue_resolver",
    "infer_binance_market_kind",
    "run_live_binance_testnet_qualification",
]
