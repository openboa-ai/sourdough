"""Connected Flow E2E test for Core Company OS / Trading Domain separation and unified desk."""

from __future__ import annotations

import importlib
import json
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from sourdough.agent_runtime.protocol import (
    PROVIDER_CODEX_CLI,
    AdapterManifest,
    AgentProfileBinding,
    ShiftResult,
    TokenUsage,
)
from sourdough.agent_runtime.registry import AgentAdapterRegistry
from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    DECISION_COMMAND,
    RESTRICTION_COMMAND,
    SUBMIT_DECISION_ANSWER,
    bind_board_principal,
)
from sourdough.company import FoundingTerms, found
from sourdough.ipc import ClientWebBridge
from sourdough.runtime.handlers import build_production_duty_registry
from sourdough.runtime.resident import ResidentCompanyService
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.storage.schema import enable_domain_v1, installed_domains
from sourdough.trading import TradingDomainPlugin
from sourdough.writer_lease import hold_company_writer


def _resolve_attr(module_candidates: tuple[str, ...], attr: str) -> Any:
    for mod_name in module_candidates:
        try:
            mod = importlib.import_module(mod_name)
        except ModuleNotFoundError:
            continue
        if hasattr(mod, attr):
            return getattr(mod, attr)
    raise AttributeError(f"Could not resolve '{attr}' from {module_candidates}")


def _trading_attr(attr: str) -> Any:
    return _resolve_attr(
        (
            "sourdough.trading",
            "sourdough.trading.accounts",
            "sourdough.trading.ledger",
            "sourdough.trading.migration",
            "sourdough.trading.sandbox_tools",
            "sourdough.trading.domain",
            "sourdough.execution_tiers",
        ),
        attr,
    )


def _bridge_json(
    url: str,
    token: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    body: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8")
        return err.code, json.loads(raw) if raw else {}


def _stream_events(
    base: str, token: str, *, after_seq: int = 0
) -> list[dict[str, Any]]:
    req = urllib.request.Request(
        f"{base}/api/v1/events?after_seq={after_seq}&limit=100&wait_ms=200",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/x-ndjson"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        text = resp.read().decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _wait_decision_terminal(
    base: str, token: str, question_id: str, *, after_seq: int, timeout: float = 10.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    cursor = after_seq
    matched_event = False
    while time.monotonic() < deadline:
        events = _stream_events(base, token, after_seq=cursor)
        for ev in events:
            seq = int(ev.get("event_seq", cursor))
            if seq > cursor:
                cursor = seq
            if (
                ev.get("event_type") == "governance.decision_effect_applied"
                and ev.get("entity_id") == question_id
            ):
                matched_event = True
        if matched_event:
            st, data = _bridge_json(f"{base}/api/v1/projections/approvals", token)
            if st == 200:
                for bucket in ("history", "pending"):
                    for item in data.get(bucket, []) or []:
                        if item.get("question_id") == question_id:
                            return item
    raise AssertionError(
        f"Decision '{question_id}' did not reach terminal application_status within {timeout}s"
    )


class _NoopAdapter:
    manifest = AdapterManifest(
        provider=PROVIDER_CODEX_CLI,
        adapter_revision="test-noop-v1",
        transport="local_cli_uds",
        supports_tool_mediation=True,
        supports_usage_accounting=True,
        supports_sandbox_desk=True,
    )

    def run_shift(self, profile: Any, request: Any, broker: Any) -> ShiftResult:
        del broker
        return ShiftResult(
            provider=self.manifest.provider,
            model=profile.model,
            run_id=request.run_id,
            turn_id=request.turn_id,
            output_text="ok",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            tool_calls_executed=0,
            events=(),
        )


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self) -> None:
        open_agent_sandbox = _trading_attr("open_agent_sandbox")
        submit_agent_sandbox_order = _trading_attr("submit_agent_sandbox_order")
        open_company_paper_book = _trading_attr("open_company_paper_book")
        submit_company_paper_book_order = _trading_attr(
            "submit_company_paper_book_order"
        )
        register_live_candidate_binding = _trading_attr(
            "register_live_candidate_binding"
        )
        record_venue_qualification_receipt = _trading_attr(
            "record_venue_qualification_receipt"
        )
        compute_trading_core_digest = _trading_attr("compute_trading_core_digest")
        build_promotion_payload_hash = _trading_attr("build_promotion_payload_hash")
        propose_paper_to_live_migration = _trading_attr(
            "propose_paper_to_live_migration"
        )
        record_live_book_order_fill = _trading_attr("record_live_book_order_fill")
        record_trading_book_valuation = _trading_attr("record_trading_book_valuation")

        SpotInstrumentTerms = _resolve_attr(
            ("sourdough.trading.market_runtime", "sourdough.market_runtime"),
            "SpotInstrumentTerms",
        )
        SandboxPlan = _resolve_attr(
            ("sourdough.trading.nautilus_paper", "sourdough.nautilus_paper"),
            "SandboxPlan",
        )
        VERSION = _resolve_attr(
            ("sourdough.trading.nautilus_runtime", "sourdough.nautilus_runtime"),
            "VERSION",
        )
        paper_venue = _resolve_attr(
            ("sourdough.trading.nautilus_runtime", "sourdough.nautilus_runtime"),
            "paper_venue",
        )

        with (
            TemporaryDirectory(prefix="sd-cleanup-flow-") as tmp,
            TemporaryDirectory(prefix="sd-", dir="/tmp") as sock_tmp,
        ):
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            pending_dir = root / "pending"
            engine_dir = root / "engine"
            sock_dir = Path(sock_tmp)

            adapters = AgentAdapterRegistry()
            adapters.register(_NoopAdapter())
            profile = AgentProfileBinding(
                provider=PROVIDER_CODEX_CLI,
                model="gpt-5-codex",
                revision="v1",
            )

            # Phase 0: Core-only founding installs 'core' only and gates Trading commands.
            founding = found(
                store,
                FoundingTerms(
                    "sourdough-cleanup-flow",
                    "charter-v1",
                    "company:all",
                    profile.profile_id(),
                ),
            )
            company_id = founding.company_id
            ceo_id = founding.founder_id

            with closing(open_reader(store)) as db, read_tx(db):
                self.assertEqual(set(installed_domains(db)), {"core"})

            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                bind_board_principal(
                    writer, principal_id="board-chair", role_name=BOARD_CONTROL_ROLE
                )

            core_registry = build_production_duty_registry(
                adapter_registry=adapters,
                profile_bindings=lambda _aid: profile,
                domain_plugins=(),
            )
            with (
                ResidentCompanyService(
                    store=store,
                    company_id=company_id,
                    duty_registry=core_registry,
                    socket_dir=sock_dir,
                    lease_directory=leases,
                    tick_interval_seconds=0.05,
                    domain_plugins=(),
                ) as resident,
                ClientWebBridge(
                    resident.socket_path,
                    port=0,
                    pending_dir=pending_dir,
                    principal="board-chair",
                ) as bridge,
            ):
                base = bridge.info.url.rstrip("/")
                tok = bridge.info.session_token
                st_meta, meta = _bridge_json(f"{base}/api/v1/meta", tok)
                self.assertEqual(st_meta, 200)
                self.assertEqual(meta["active_domains"], ["core"])
                st_gate, _ = _bridge_json(
                    f"{base}/api/v1/commands",
                    tok,
                    method="POST",
                    idempotency_key="flow-core-gate-1",
                    payload={
                        "company_id": company_id,
                        "command_id": "flow-core-gate-1",
                        "command_type": "promote_execution_environment",
                        "contract_version": "execution-tier-command-v1",
                        "preconditions": {
                            "charter_revision": "charter-v1",
                            "target_binding_id": "none",
                            "qualification_receipt_id": "none",
                            "expected_payload_hash": "0" * 64,
                        },
                        "payload": {"reason": "Must be gated on core-only"},
                    },
                )
                self.assertEqual(st_gate, 422)

                # Core-only pause_company must never leak "domains": {"trading": ...} into work-timeline.
                st_pause, pause_body = _bridge_json(
                    f"{base}/api/v1/commands",
                    tok,
                    method="POST",
                    idempotency_key="flow-core-pause-1",
                    payload={
                        "company_id": company_id,
                        "command_id": "flow-core-pause-1",
                        "command_type": "pause_company",
                        "contract_version": RESTRICTION_COMMAND,
                        "preconditions": {
                            "charter_revision": "charter-v1",
                            "restriction_id": "rest-core-flow",
                            "expected_restriction_revision": 0,
                        },
                        "payload": {"reason": "Core-only pause check"},
                    },
                )
                self.assertEqual(st_pause, 200, pause_body)
                st_tl, tl_body = _bridge_json(
                    f"{base}/api/v1/projections/work-timeline", tok
                )
                self.assertEqual(st_tl, 200)
                self.assertNotIn("trading", tl_body.get("domains", {}))
                st_res, _ = _bridge_json(
                    f"{base}/api/v1/commands",
                    tok,
                    method="POST",
                    idempotency_key="flow-core-resume-1",
                    payload={
                        "company_id": company_id,
                        "command_id": "flow-core-resume-1",
                        "command_type": "resume_company",
                        "contract_version": RESTRICTION_COMMAND,
                        "preconditions": {
                            "charter_revision": "charter-v1",
                            "restriction_id": "rest-core-flow",
                            "expected_restriction_revision": 1,
                        },
                        "payload": {"reason": "Core-only resume check"},
                    },
                )
                self.assertEqual(st_res, 200)

            # Phase 1: Opt-in Trading domain, Tier 1 agent_sandbox, and Tier 2 multi-book company_paper.
            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    enable_domain_v1(
                        db, "trading", company_id=company_id, actor_id="board-chair"
                    )
                    now_ns = time.time_ns()
                    db.execute(
                        "INSERT INTO agent (id, company_id, execution_profile) VALUES (?, ?, ?)",
                        ("agent-quant-1", company_id, profile.profile_id()),
                    )
                    db.execute(
                        "INSERT INTO standing_grant (id, agent_id, scope) VALUES (?, ?, ?)",
                        ("grant-quant-1", "agent-quant-1", "trading"),
                    )
                    db.execute(
                        """INSERT INTO agent_profile (
                               agent_id, company_id, display_name, role_label, avatar_variant,
                               monthly_budget_minor, updated_at_ns
                           ) VALUES (?, ?, 'Ada Quant', 'Portfolio Researcher', 'cobalt', 100000, ?)""",
                        ("agent-quant-1", company_id, now_ns),
                    )

            t0 = 1_730_000_000_000_000_000
            hour_ns = 3_600 * 1_000_000_000

            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                open_agent_sandbox(
                    writer,
                    agent_id="agent-quant-1",
                    account_id="sbx-quant-1",
                    starting_minor_units=10_000_000,
                    exposure_limit_minor_units=8_000_000,
                )
                submit_agent_sandbox_order(
                    writer,
                    agent_id="agent-quant-1",
                    action_id="flow-sbx-1",
                    instrument_id="BTC-USDT",
                    side="buy",
                    quantity=1,
                    limit_minor_units=1_010_000,
                    market_price_minor_units=1_000_000,
                )

                book1 = open_company_paper_book(
                    writer,
                    account_id="paper-btc-mom",
                    book_name="BTC Spot Momentum",
                    starting_minor_units=50_000_000,
                    exposure_limit_minor_units=25_000_000,
                )
                book2 = open_company_paper_book(
                    writer,
                    account_id="paper-eth-basis",
                    book_name="ETH Basis Neutral",
                    starting_minor_units=40_000_000,
                    exposure_limit_minor_units=20_000_000,
                )
                paper_ord = submit_company_paper_book_order(
                    writer,
                    binding_id=book1.binding_id,
                    actor_id=ceo_id,
                    action_id="flow-p1",
                    instrument_id="BTC-USDT",
                    side="buy",
                    quantity=2,
                    limit_minor_units=6_600_000,
                    market_price_minor_units=6_500_000,
                    now_ns=t0 + hour_ns,
                )
                record_trading_book_valuation(
                    writer,
                    binding_id=book1.binding_id,
                    label="Paper Mark",
                    mark_prices={"BTC-USDT": 6_800_000},
                    now_ns=t0 + 2 * hour_ns,
                )
                submit_company_paper_book_order(
                    writer,
                    binding_id=book2.binding_id,
                    actor_id=ceo_id,
                    action_id="flow-p2",
                    instrument_id="ETH-USDT",
                    side="buy",
                    quantity=4,
                    limit_minor_units=360_000,
                    market_price_minor_units=350_000,
                    now_ns=t0 + hour_ns,
                )

                live_cand = register_live_candidate_binding(
                    writer,
                    binding_id="live-flow-1",
                    account_id="binance-prod-01",
                    destination_id="binance-spot-prod",
                    connector_revision="binance-spot-v1",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    currency="USD",
                    starting_minor_units=25_000_000,
                    exposure_limit_minor_units=15_000_000,
                    credential_handle_ref="keychain:openboa/binance/prod-01",
                )
                record_venue_qualification_receipt(
                    writer,
                    receipt_id="qual-flow-1",
                    adapter_revision="binance-spot-v1",
                    environment="external_test",
                    destination_id="binance-spot-testnet",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    core_digest=compute_trading_core_digest(),
                    verdict="PASS",
                    cases_passed=6,
                    cases_failed=0,
                    cases_unrun=0,
                    evidence_sha256="a" * 64,
                )
                nom_fail = propose_paper_to_live_migration(
                    writer,
                    question_id="q-flow-fail",
                    source_paper_binding_id=book1.binding_id,
                    target_live_binding_id=live_cand.binding_id,
                    qualification_receipt_id="qual-flow-1",
                    created_by_agent_id=ceo_id,
                    migration_mode="clean_start",
                    source_paper_disposition="keep_shadow",
                )

            (engine_dir / f"nautilus-{VERSION}").mkdir(parents=True, exist_ok=True)
            terms = SpotInstrumentTerms(
                "BTCUSDT", "BTC", "USDT", "0.01", "0.00001", "0.00001", "5"
            )
            plan = SandboxPlan(company_id, "paper", terms, 100_000_000_000, 0)
            venue = paper_venue(store, engine_dir)
            venue.provision(plan)
            bal = venue.start_generation()
            venue.prove_account("current", "BINANCE-001", "USDT", bal)
            stamp = time.time_ns()
            venue.publish_quote("current", 59_000_000, stamp, stamp, event_ns=stamp)

            plugins = (TradingDomainPlugin(),)
            duty_registry = build_production_duty_registry(
                adapter_registry=adapters,
                profile_bindings=lambda _aid: profile,
                domain_plugins=plugins,
            )

            # Phase 2: Failed live_promotion under active restriction -> clear -> retry approved live_promotion.
            with (
                ResidentCompanyService(
                    store=store,
                    company_id=company_id,
                    duty_registry=duty_registry,
                    socket_dir=sock_dir,
                    lease_directory=leases,
                    engine_directory=engine_dir,
                    tick_interval_seconds=0.05,
                    domain_plugins=plugins,
                ) as resident,
                ClientWebBridge(
                    resident.socket_path,
                    port=0,
                    pending_dir=pending_dir,
                    principal="board-chair",
                ) as bridge,
            ):
                base = bridge.info.url.rstrip("/")
                tok = bridge.info.session_token

                st_pause, _ = _bridge_json(
                    f"{base}/api/v1/commands",
                    tok,
                    method="POST",
                    idempotency_key="flow-pause-live-1",
                    payload={
                        "company_id": company_id,
                        "command_id": "flow-pause-live-1",
                        "command_type": "pause_company",
                        "contract_version": RESTRICTION_COMMAND,
                        "preconditions": {
                            "charter_revision": "charter-v1",
                            "restriction_id": "rest-flow-live",
                            "expected_restriction_revision": 0,
                        },
                        "payload": {
                            "cause_category": "trading_risk_pause",
                            "scope_ref": "execution:live",
                            "reason": "Hold live promotion",
                            "operator_releasable": True,
                        },
                    },
                )
                self.assertEqual(st_pause, 200)

                _, app0 = _bridge_json(f"{base}/api/v1/projections/approvals", tok)
                seq_fail = int(app0["event_seq"])
                st_f, _ = _bridge_json(
                    f"{base}/api/v1/commands",
                    tok,
                    method="POST",
                    idempotency_key="flow-ans-fail",
                    payload={
                        "company_id": company_id,
                        "command_id": "flow-ans-fail",
                        "command_type": SUBMIT_DECISION_ANSWER,
                        "contract_version": DECISION_COMMAND,
                        "preconditions": {
                            "question_id": "q-flow-fail",
                            "expected_revision": 1,
                            "expected_payload_hash": nom_fail["payload_hash"],
                        },
                        "payload": {
                            "choice": "approve",
                            "reason": "Approve while live restricted",
                        },
                    },
                )
                self.assertEqual(st_f, 200)
                failed_item = _wait_decision_terminal(
                    base, tok, "q-flow-fail", after_seq=seq_fail
                )
                self.assertEqual(failed_item["application_status"], "failed")

                fresh_ns = time.time_ns()
                venue.publish_quote(
                    "current", 59_000_000, fresh_ns, fresh_ns, event_ns=fresh_ns
                )
                st_res, _ = _bridge_json(
                    f"{base}/api/v1/commands",
                    tok,
                    method="POST",
                    idempotency_key="flow-resume-live-1",
                    payload={
                        "company_id": company_id,
                        "command_id": "flow-resume-live-1",
                        "command_type": "resume_company",
                        "contract_version": RESTRICTION_COMMAND,
                        "preconditions": {
                            "charter_revision": "charter-v1",
                            "restriction_id": "rest-flow-live",
                            "expected_restriction_revision": 1,
                        },
                        "payload": {"reason": "Clear live hold"},
                    },
                )
                self.assertEqual(st_res, 200)

            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                nom_ok = propose_paper_to_live_migration(
                    writer,
                    question_id="q-flow-ok",
                    source_paper_binding_id=book1.binding_id,
                    target_live_binding_id=live_cand.binding_id,
                    qualification_receipt_id="qual-flow-1",
                    created_by_agent_id=ceo_id,
                    migration_mode="clean_start",
                    source_paper_disposition="keep_shadow",
                )
                expected_promo_hash = build_promotion_payload_hash(
                    company_id=company_id,
                    charter_revision="charter-v1",
                    target_binding=live_cand,
                    qualification_receipt_id="qual-flow-1",
                )

            with (
                ResidentCompanyService(
                    store=store,
                    company_id=company_id,
                    duty_registry=duty_registry,
                    socket_dir=sock_dir,
                    lease_directory=leases,
                    engine_directory=engine_dir,
                    tick_interval_seconds=0.05,
                    domain_plugins=plugins,
                ) as resident,
                ClientWebBridge(
                    resident.socket_path,
                    port=0,
                    pending_dir=pending_dir,
                    principal="board-chair",
                ) as bridge,
            ):
                base = bridge.info.url.rstrip("/")
                tok = bridge.info.session_token

                _, app1 = _bridge_json(f"{base}/api/v1/projections/approvals", tok)
                seq_ok = int(app1["event_seq"])
                st_ok, _ = _bridge_json(
                    f"{base}/api/v1/commands",
                    tok,
                    method="POST",
                    idempotency_key="flow-ans-ok",
                    payload={
                        "company_id": company_id,
                        "command_id": "flow-ans-ok",
                        "command_type": SUBMIT_DECISION_ANSWER,
                        "contract_version": DECISION_COMMAND,
                        "preconditions": {
                            "question_id": "q-flow-ok",
                            "expected_revision": 1,
                            "expected_payload_hash": nom_ok["payload_hash"],
                        },
                        "payload": {
                            "choice": "approve",
                            "reason": "Approved live launch",
                        },
                    },
                )
                self.assertEqual(st_ok, 200)
                ok_item = _wait_decision_terminal(
                    base, tok, "q-flow-ok", after_seq=seq_ok
                )
                self.assertEqual(ok_item["application_status"], "applied")

                st_susp, susp_body = _bridge_json(
                    f"{base}/api/v1/commands",
                    tok,
                    method="POST",
                    idempotency_key="flow-suspend-live-1",
                    payload={
                        "company_id": company_id,
                        "command_id": "flow-suspend-live-1",
                        "command_type": "demote_live_environment",
                        "contract_version": "execution-tier-command-v1",
                        "preconditions": {
                            "charter_revision": "charter-v1",
                            "target_binding_id": live_cand.binding_id,
                        },
                        "payload": {
                            "action": "suspend_live",
                            "reason": "Exercise Board live suspend gate",
                        },
                    },
                )
                self.assertEqual(st_susp, 200)
                self.assertEqual(susp_body.get("state"), "applied")

                st_prom, prom_body = _bridge_json(
                    f"{base}/api/v1/commands",
                    tok,
                    method="POST",
                    idempotency_key="flow-promote-live-1",
                    payload={
                        "company_id": company_id,
                        "command_id": "flow-promote-live-1",
                        "command_type": "promote_execution_environment",
                        "contract_version": "execution-tier-command-v1",
                        "preconditions": {
                            "charter_revision": "charter-v1",
                            "target_binding_id": live_cand.binding_id,
                            "qualification_receipt_id": "qual-flow-1",
                            "expected_payload_hash": expected_promo_hash,
                        },
                        "payload": {"reason": "Promote live trading after suspend"},
                    },
                )
                self.assertEqual(st_prom, 200)
                self.assertEqual(prom_body.get("state"), "applied")

                st_live0, live_before = _bridge_json(
                    f"{base}/api/v1/projections/trading-desk?environment=live", tok
                )
                self.assertEqual(st_live0, 200)
                self.assertEqual(live_before["selected_book_id"], live_cand.binding_id)
                self.assertEqual(live_before["open_positions"], [])
                self.assertEqual(
                    live_before["cumulative_return_series"][0]["return_pct"], 0.0
                )
                self.assertEqual(
                    live_before["governance"]["migration_history"][0][
                        "positions_copied"
                    ],
                    0,
                )
                self.assertEqual(
                    live_before["governance"]["promotion_history"][0]["action"],
                    "promote_live",
                )
                launch_ns = live_before["cumulative_return_series"][0]["observed_at_ns"]

            # Phase 3: Record live fill + shadow valuation, then verify trading-desk-v1 & order-trace-v1.
            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                live_fill = record_live_book_order_fill(
                    writer,
                    binding_id=live_cand.binding_id,
                    actor_id=ceo_id,
                    action_id="flow-live-fill-1",
                    external_order_id="bn-flow-ord-1",
                    external_fill_id="bn-flow-fill-1",
                    instrument_id="BTC-USDT",
                    side="buy",
                    quantity=2,
                    limit_minor_units=6_900_000,
                    fill_price_minor_units=6_830_000,
                    fee_minor_units=6_830,
                    now_ns=launch_ns + hour_ns,
                )
                record_trading_book_valuation(
                    writer,
                    binding_id=live_cand.binding_id,
                    label="Live T+4h",
                    mark_prices={"BTC-USDT": 7_010_000},
                    now_ns=launch_ns + 4 * hour_ns,
                )
                record_trading_book_valuation(
                    writer,
                    binding_id=book1.binding_id,
                    label="Shadow T+4h",
                    mark_prices={"BTC-USDT": 7_040_000},
                    now_ns=launch_ns + 4 * hour_ns,
                )

            with closing(open_reader(store)) as db, read_tx(db):
                audit_dom = db.execute(
                    "SELECT domain FROM audit_event WHERE event_type = 'company_live.order_filled' ORDER BY event_seq DESC LIMIT 1"
                ).fetchone()
                self.assertIsNotNone(audit_dom)
                self.assertEqual(audit_dom[0], "trading")

            with (
                ResidentCompanyService(
                    store=store,
                    company_id=company_id,
                    duty_registry=duty_registry,
                    socket_dir=sock_dir,
                    lease_directory=leases,
                    engine_directory=engine_dir,
                    tick_interval_seconds=0.05,
                    domain_plugins=plugins,
                ) as resident,
                ClientWebBridge(
                    resident.socket_path,
                    port=0,
                    pending_dir=pending_dir,
                    principal="board-chair",
                ) as bridge,
            ):
                base = bridge.info.url.rstrip("/")
                tok = bridge.info.session_token

                st_live1, live_after = _bridge_json(
                    f"{base}/api/v1/projections/trading-desk?environment=live", tok
                )
                self.assertEqual(st_live1, 200)
                self.assertEqual(
                    live_after["execution_tape"][0]["external_order_id"],
                    "bn-flow-ord-1",
                )
                self.assertIsNotNone(live_after["shadow_benchmark"])
                self.assertEqual(
                    live_after["shadow_benchmark"]["binding_id"], book1.binding_id
                )

                st_lt, live_trace = _bridge_json(
                    f"{base}/api/v1/projections/orders/{live_fill['order_id']}", tok
                )
                self.assertEqual(st_lt, 200)
                self.assertEqual(live_trace["projection"], "order-trace-v1")
                self.assertEqual(live_trace["order"]["environment"], "live")

                st_pt, paper_trace = _bridge_json(
                    f"{base}/api/v1/projections/orders/{paper_ord['order_id']}", tok
                )
                self.assertEqual(st_pt, 200)
                self.assertEqual(paper_trace["projection"], "order-trace-v1")
                self.assertEqual(paper_trace["order"]["environment"], "paper")

                # Sanitized avatar_variant for legacy 'cobalt' profile & fail-closed unknown book_id.
                from sourdough.board_control import ALLOWED_AVATAR_VARIANTS

                st_ag, ag_body = _bridge_json(
                    f"{base}/api/v1/projections/agents-graph", tok
                )
                self.assertEqual(st_ag, 200)
                for node in ag_body["nodes"]:
                    self.assertIn(node["avatar_variant"], ALLOWED_AVATAR_VARIANTS)

                st_missing, missing_desk = _bridge_json(
                    f"{base}/api/v1/projections/trading-desk?environment=paper&book_id=missing-book",
                    tok,
                )
                self.assertEqual(st_missing, 200)
                self.assertIsNone(missing_desk["selected_book"])
                self.assertIsNone(missing_desk["selected_book_id"])


if __name__ == "__main__":
    unittest.main()
