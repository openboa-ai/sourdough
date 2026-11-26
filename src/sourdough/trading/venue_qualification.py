"""Automated external-testnet venue qualification runner issuing sealed receipts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from .accounts import record_venue_qualification_receipt
from .binance_emulator import BinanceWireEmulator
from .binance_venue import (
    BinanceVenueAdapter,
    BinanceVenueConfig,
)
from .credentials import InMemoryTestCredentialProvider
from .domain import (
    ENV_EXTERNAL_TEST,
    ENV_PAPER,
    VenueQualificationReceipt,
    compute_trading_core_digest,
)
from .external_venue import (
    ExternalEnvironmentMismatch,
    ExternalOrderReplyUnknown,
    ExternalOrderRequest,
    ExternalVenueRefused,
)
from ..writer_lease import WriterLease


@dataclass(frozen=True)
class QualificationCaseResult:
    case_id: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class QualificationReport:
    receipt: VenueQualificationReceipt
    cases: tuple[QualificationCaseResult, ...]


def run_binance_testnet_qualification(
    writer: WriterLease,
    *,
    adapter: BinanceVenueAdapter,
    emulator: BinanceWireEmulator,
    instrument_rules_digest: str,
    symbol: str = "BTCUSDT",
    receipt_id: str | None = None,
) -> QualificationReport:
    """Execute the 6 canonical external_test qualification cases and seal a receipt."""
    if adapter.config.environment != ENV_EXTERNAL_TEST:
        raise ExternalEnvironmentMismatch(
            "VenueQualificationRunner must target environment='external_test' only"
        )

    known_client_ids: set[str] = set()
    results: list[QualificationCaseResult] = []

    # Case 1: Normal submit, fill, and independent fill observation.
    act1 = f"qual-normal-{uuid4().hex[:8]}"
    cid1 = adapter.client_order_id_for(act1)
    known_client_ids.add(cid1)
    st1 = adapter.submit_order(
        ExternalOrderRequest(
            action_id=act1,
            symbol=symbol,
            side="BUY",
            quantity=Decimal("0.1"),
            limit_price=Decimal("50000"),
            time_in_force="IOC",
        )
    )
    fills1 = adapter.query_fills(symbol, client_order_id=cid1)
    results.append(
        QualificationCaseResult(
            case_id="normal_submit_and_fill",
            passed=(
                st1.status == "filled"
                and st1.filled_quantity == Decimal("0.1")
                and len(fills1) == 1
                and fills1[0].commission > 0
            ),
            detail=f"status={st1.status}, fills={len(fills1)}",
        )
    )

    # Case 2: Over-limit order rejected with zero venue fill.
    act2 = f"qual-reject-{uuid4().hex[:8]}"
    cid2 = adapter.client_order_id_for(act2)
    rejected = False
    try:
        adapter.submit_order(
            ExternalOrderRequest(
                action_id=act2,
                symbol=symbol,
                side="BUY",
                quantity=Decimal("10"),
                limit_price=Decimal("50000"),
                time_in_force="IOC",
            )
        )
    except ExternalVenueRefused:
        rejected = True
    fills2 = adapter.query_fills(symbol, client_order_id=cid2)
    results.append(
        QualificationCaseResult(
            case_id="over_limit_rejected_zero_fill",
            passed=rejected and len(fills2) == 0,
            detail=f"rejected={rejected}, fills={len(fills2)}",
        )
    )

    # Case 3: Partial fill + terminal cancellation preserves residual exposure (cancel != flat).
    act3 = f"qual-partial-{uuid4().hex[:8]}"
    cid3 = adapter.client_order_id_for(act3)
    known_client_ids.add(cid3)
    emulator.set_fault(cid3, "partial_then_cancel")
    st3 = adapter.submit_order(
        ExternalOrderRequest(
            action_id=act3,
            symbol=symbol,
            side="BUY",
            quantity=Decimal("0.2"),
            limit_price=Decimal("50000"),
            time_in_force="IOC",
        )
    )
    results.append(
        QualificationCaseResult(
            case_id="partial_fill_cancel_residual_position",
            passed=(
                st3.status == "cancelled"
                and st3.filled_quantity == Decimal("0.0800")
                and st3.remaining_quantity == Decimal("0.1200")
            ),
            detail=f"status={st3.status}, filled={st3.filled_quantity}, rem={st3.remaining_quantity}",
        )
    )

    # Case 4: Lost reply after venue accept recovers via original clientOrderId with ZERO duplicate submit.
    act4 = f"qual-lost-reply-{uuid4().hex[:8]}"
    cid4 = adapter.client_order_id_for(act4)
    known_client_ids.add(cid4)
    emulator.set_fault(cid4, "lost_reply_after_accept")
    submits_before = emulator.submit_count
    unknown_raised = False
    try:
        adapter.submit_order(
            ExternalOrderRequest(
                action_id=act4,
                symbol=symbol,
                side="BUY",
                quantity=Decimal("0.05"),
                limit_price=Decimal("50000"),
                time_in_force="IOC",
            )
        )
    except ExternalOrderReplyUnknown:
        unknown_raised = True
    recovered = adapter.query_order(symbol, cid4)
    submits_after = emulator.submit_count
    results.append(
        QualificationCaseResult(
            case_id="lost_reply_recovery_by_original_id",
            passed=(
                unknown_raised
                and recovered.status == "filled"
                and recovered.filled_quantity == Decimal("0.05")
                and submits_after == submits_before + 1
            ),
            detail=f"unknown={unknown_raised}, recovered={recovered.status}, submits_delta={submits_after - submits_before}",
        )
    )

    # Case 5: Absent order returns absent_unsealed (never falsely sealed) and later reveals delayed fill.
    act5 = f"qual-delayed-{uuid4().hex[:8]}"
    cid5 = adapter.client_order_id_for(act5)
    known_client_ids.add(cid5)
    emulator.set_fault(cid5, "delayed_unsealed")
    delayed_unknown = False
    try:
        adapter.submit_order(
            ExternalOrderRequest(
                action_id=act5,
                symbol=symbol,
                side="BUY",
                quantity=Decimal("0.02"),
                limit_price=Decimal("50000"),
                time_in_force="IOC",
            )
        )
    except ExternalOrderReplyUnknown:
        delayed_unknown = True
    first_lookup = adapter.query_order(symbol, cid5)
    second_lookup = adapter.query_order(symbol, cid5)
    results.append(
        QualificationCaseResult(
            case_id="absent_order_remains_unsealed",
            passed=(
                delayed_unknown
                and first_lookup.status == "absent_unsealed"
                and first_lookup.seals_absence is False
                and second_lookup.status == "filled"
            ),
            detail=f"first={first_lookup.status}, second={second_lookup.status}",
        )
    )

    # Case 6: Environment & credential isolation + external venue-originated fill detection.
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
    except Exception:
        live_handle_blocked = True

    emulator.inject_external_fill(
        symbol=symbol,
        side="BUY",
        quantity=Decimal("0.01"),
        price=Decimal("50000"),
        external_client_id=f"ext_rogue_{uuid4().hex[:6]}",
    )
    recon = adapter.reconcile_external(symbol, frozenset(known_client_ids))
    results.append(
        QualificationCaseResult(
            case_id="environment_isolation_and_external_reconcile",
            passed=(
                paper_blocked
                and live_handle_blocked
                and not recon.clean
                and len(recon.unattributed_fills) == 1
            ),
            detail=f"paper_blocked={paper_blocked}, live_handle_blocked={live_handle_blocked}, unattributed={len(recon.unattributed_fills)}",
        )
    )

    passed_count = sum(1 for c in results if c.passed)
    failed_count = len(results) - passed_count
    verdict = "PASS" if failed_count == 0 else "FAIL"
    evidence_sha = sha256(
        json.dumps(
            [asdict(c) for c in results], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()

    receipt = record_venue_qualification_receipt(
        writer,
        receipt_id=receipt_id
        or f"qual-{adapter.config.market_kind}-{uuid4().hex[:12]}",
        adapter_revision=adapter.capabilities.adapter_revision,
        environment=ENV_EXTERNAL_TEST,
        destination_id=adapter.config.destination_id,
        instrument_rules_digest=instrument_rules_digest,
        core_digest=compute_trading_core_digest(),
        verdict=verdict,
        cases_passed=passed_count,
        cases_failed=failed_count,
        cases_unrun=0,
        evidence_sha256=evidence_sha,
    )
    return QualificationReport(receipt=receipt, cases=tuple(results))
