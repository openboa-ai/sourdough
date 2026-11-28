"""Unit tests for operator onboarding CLI, default live venue resolver, and Day-2 Board commands."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from typing import Any

from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    BOARD_VIEWER_ROLE,
    BoardControlConflict,
    BoardControlForbidden,
    bind_board_principal,
)
from sourdough.company import FoundingTerms, found
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.storage.schema import enable_domain_v1
from sourdough.trading.accounts import (
    open_company_paper_book,
    record_venue_qualification_receipt,
    register_live_candidate_binding,
)
from sourdough.trading.binance_emulator import BinanceWireEmulator
from sourdough.trading.binance_venue import (
    MARKET_SPOT,
    MARKET_USDM_FUTURES,
    BinanceVenueAdapter,
    BinanceVenueConfig,
)
from sourdough.trading.commands import (
    OPEN_COMPANY_PAPER_BOOK,
    RECONCILE_TRADING_BOOKS,
    execute_execution_tier_command,
)
from sourdough.trading.credentials import (
    CredentialRefused,
    EnvRefCredentialProvider,
    InMemoryTestCredentialProvider,
)
from sourdough.trading.domain import (
    ENV_EXTERNAL_TEST,
    ENV_LIVE,
    ExecutionTierRefused,
    compute_trading_core_digest,
)
from sourdough.trading.ledger import submit_company_paper_book_order
from sourdough.trading.migration import execute_paper_to_live_migration
from sourdough.trading.plugin import TradingDomainPlugin
from sourdough.trading.quotes import MarketQuote, StaticQuoteProvider
from sourdough.trading.venue_resolver import (
    CompositeCredentialProvider,
    build_default_live_venue_resolver,
    infer_binance_market_kind,
    run_live_binance_testnet_qualification,
)
from sourdough.writer_lease import hold_company_writer


def _run_cli(*args: str) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-m", "sourdough.cli", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


class TestOperatorTradingOnboarding(unittest.TestCase):
    def test_composite_credentials_and_default_live_venue_resolver(self) -> None:
        env_creds = EnvRefCredentialProvider(
            {
                "BINANCE_PROD_API_KEY": "live-key-01",
                "BINANCE_PROD_API_SECRET": "live-sec-01",
                "BINANCE_TESTNET_API_KEY": "test-key-01",
                "BINANCE_TESTNET_API_SECRET": "test-sec-01",
            }
        )
        kc_stub = InMemoryTestCredentialProvider(
            {"keychain:openboa/binance-prod": ("kc-key", "kc-sec")}
        )
        comp = CompositeCredentialProvider(
            env_provider=env_creds, keychain_provider=kc_stub
        )
        pair_env = comp.resolve("envref:BINANCE_PROD", expected_environment=ENV_LIVE)
        self.assertEqual(pair_env.api_key, "live-key-01")
        pair_kc = comp.resolve(
            "keychain:openboa/binance-prod", expected_environment=ENV_LIVE
        )
        self.assertEqual(pair_kc.api_key, "kc-key")

        # Environment isolation still enforced fail-closed!
        with self.assertRaises(CredentialRefused):
            comp.resolve("envref:BINANCE_TESTNET", expected_environment=ENV_LIVE)

        self.assertEqual(
            infer_binance_market_kind("binance-rest-v1", "binance-spot-prod"),
            MARKET_SPOT,
        )
        self.assertEqual(
            infer_binance_market_kind("binance-futures-v1", "binance-usdm-prod"),
            MARKET_USDM_FUTURES,
        )
        # Unsupported, contradictory, or ambiguous connectors/destinations MUST fail closed!
        for bad_conn, bad_dest in (
            ("unknown-exchange-v1", "mystery-dest"),
            ("unknown-exchange-v1", "binance-spot-prod"),
            ("binance-options-v1", "binance-options-prod"),
            ("binance-options-v1", "binance-spot-prod"),
            ("binance-spot-v1", "binance-usdm-prod"),
            ("binance-futures-v1", "binance-spot-prod"),
            ("binance-rest-v1", "binance-options-prod"),
            ("binance-rest-v1", "binance-spot-futures-prod"),
            ("binance-rest-v1", "binance-generic-prod"),
        ):
            with self.assertRaises(ExecutionTierRefused, msg=(bad_conn, bad_dest)):
                infer_binance_market_kind(bad_conn, bad_dest)

        with TemporaryDirectory(prefix="sd-resolver-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            founding = found(
                store,
                FoundingTerms("sd-res", "charter-v1", "paper", "profile-v1"),
            )
            cid = founding.company_id
            now = time_ns()
            emulator = BinanceWireEmulator(
                api_key="live-key-01",
                api_secret="live-sec-01",
                starting_usdt=Decimal("250000"),
                fee_bps=4,
            )
            emulator.start()
            try:
                with hold_company_writer(store, cid, lease_directory=leases) as writer:
                    with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                        enable_domain_v1(db, "trading")
                    pbook = open_company_paper_book(
                        writer,
                        account_id="paper-src-1",
                        book_name="Seed Paper",
                        starting_minor_units=20_000_000,
                        exposure_limit_minor_units=10_000_000,
                    )
                    submit_company_paper_book_order(
                        writer,
                        binding_id=pbook.binding_id,
                        actor_id=founding.founder_id,
                        action_id="seed-fill-1",
                        instrument_id="BTC-USDT",
                        side="buy",
                        quantity=1,
                        limit_minor_units=6_500_000,
                        market_price_minor_units=6_400_000,
                        quote_observed_at_ns=now,
                        now_ns=now,
                    )
                    cand = register_live_candidate_binding(
                        writer,
                        binding_id="live-res-1",
                        account_id="binance-prod-us01",
                        destination_id="binance-spot-prod",
                        connector_revision="binance-rest-v1",
                        instrument_rules_digest="btc-usdt-spot-v1",
                        currency="USD",
                        starting_minor_units=20_000_000,
                        exposure_limit_minor_units=10_000_000,
                        credential_handle_ref="envref:BINANCE_PROD",
                    )
                    record_venue_qualification_receipt(
                        writer,
                        receipt_id="qual-res-1",
                        adapter_revision="binance-rest-v1",
                        environment=ENV_EXTERNAL_TEST,
                        destination_id="binance-spot-testnet",
                        instrument_rules_digest="btc-usdt-spot-v1",
                        core_digest=compute_trading_core_digest(),
                        verdict="PASS",
                        cases_passed=6,
                        cases_failed=0,
                        cases_unrun=0,
                        evidence_sha256="a" * 64,
                    )
                    resolver = build_default_live_venue_resolver(
                        store,
                        cid,
                        credential_provider=comp,
                        environment=ENV_LIVE,
                    )
                    # Dormant 'registered' candidate MUST NOT resolve!
                    self.assertIsNone(resolver(cand.binding_id))
                    # Paper binding MUST NOT resolve!
                    self.assertIsNone(resolver(pbook.binding_id))

                    # Activate live binding via migration -> now resolves cleanly pinned to Binance prod!
                    execute_paper_to_live_migration(
                        writer,
                        source_paper_binding_id=pbook.binding_id,
                        target_live_binding_id=cand.binding_id,
                        qualification_receipt_id="qual-res-1",
                        reason="Activate live for resolver test",
                    )
                    adapter = resolver(cand.binding_id)
                    self.assertIsNotNone(adapter)
                    assert adapter is not None
                    self.assertEqual(adapter.config.environment, ENV_LIVE)
                    self.assertEqual(adapter.config.base_url, "https://api.binance.com")
                    self.assertEqual(adapter.config.account_id, "binance-prod-us01")

                    # Exercise read-only live-testnet probe over loopback HMAC emulator:
                    # MUST record INCOMPLETE with 4 unrun execution cases so it cannot satisfy live promotion!
                    tn_adapter = BinanceVenueAdapter(
                        BinanceVenueConfig(
                            environment=ENV_EXTERNAL_TEST,
                            market_kind=MARKET_SPOT,
                            account_id="binance-tn-qual",
                            destination_id="binance-spot-testnet",
                            credential_handle_ref="envref:BINANCE_TESTNET",
                            base_url_override=emulator.base_url,
                            allow_loopback_override=True,
                        ),
                        InMemoryTestCredentialProvider(
                            {"envref:BINANCE_TESTNET": ("live-key-01", "live-sec-01")}
                        ),
                    )
                    tn_report = run_live_binance_testnet_qualification(
                        writer,
                        adapter=tn_adapter,
                        instrument_rules_digest="btc-usdt-spot-v1",
                        receipt_id="qual-live-tn-1",
                    )
                    self.assertEqual(tn_report.receipt.verdict, "INCOMPLETE")
                    self.assertEqual(tn_report.receipt.cases_passed, 4)
                    self.assertEqual(tn_report.receipt.cases_failed, 0)
                    self.assertEqual(tn_report.receipt.cases_unrun, 4)
                    cand_blocked = register_live_candidate_binding(
                        writer,
                        binding_id="live-res-2",
                        account_id="binance-prod-us02",
                        destination_id="binance-spot-prod",
                        connector_revision="binance-rest-v1",
                        instrument_rules_digest="btc-usdt-spot-v1",
                        currency="USD",
                        starting_minor_units=20_000_000,
                        exposure_limit_minor_units=10_000_000,
                        credential_handle_ref="envref:BINANCE_PROD",
                    )
                    with self.assertRaises(ExecutionTierRefused):
                        execute_paper_to_live_migration(
                            writer,
                            source_paper_binding_id=pbook.binding_id,
                            target_live_binding_id=cand_blocked.binding_id,
                            qualification_receipt_id="qual-live-tn-1",
                            reason="Read-only INCOMPLETE receipt must not promote live",
                        )

                    # Suspending the active live binding disables new-order resolution
                    # while preserving recovery/reconciliation resolution!
                    with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                        db.execute(
                            "UPDATE execution_environment_binding SET status = 'suspended' WHERE binding_id = ?",
                            (cand.binding_id,),
                        )
                    self.assertIsNone(resolver(cand.binding_id))
                    recovery_resolver = build_default_live_venue_resolver(
                        store,
                        cid,
                        credential_provider=comp,
                        environment=ENV_LIVE,
                        allow_recovery_statuses=True,
                    )
                    rec_adapter = recovery_resolver(cand.binding_id)
                    self.assertIsNotNone(rec_adapter)
                    assert rec_adapter is not None
                    self.assertEqual(rec_adapter.config.account_id, "binance-prod-us01")
                    # Dormant 'registered' candidate still MUST NOT resolve even for recovery!
                    self.assertIsNone(recovery_resolver(cand_blocked.binding_id))
            finally:
                emulator.stop()

    def test_operator_trading_cli_subcommands_end_to_end(self) -> None:
        with TemporaryDirectory(prefix="sd-cli-onboard-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"

            found_out = _run_cli(
                "found",
                str(store),
                "--setup-identity",
                "sourdough-cli-co",
                "--charter-revision",
                "charter-cli-v1",
                "--founder-scope",
                "paper",
                "--execution-profile",
                "profile-v1",
            )
            cid = found_out["company_id"]
            ceo_id = found_out["founder_id"]

            en_out = _run_cli(
                "enable-trading-domain",
                str(store),
                "--company-id",
                cid,
                "--lease-dir",
                str(leases),
            )
            self.assertTrue(en_out["installed_now"])
            # Idempotent second call
            en_out_2 = _run_cli(
                "enable-trading-domain",
                str(store),
                "--company-id",
                cid,
                "--lease-dir",
                str(leases),
            )
            self.assertFalse(en_out_2["installed_now"])

            paper_out = _run_cli(
                "open-paper-book",
                str(store),
                "--company-id",
                cid,
                "--lease-dir",
                str(leases),
                "--account-id",
                "paper-cli-btc",
                "--book-name",
                "BTC Momentum CLI",
                "--starting-minor",
                "50000000",
                "--exposure-limit-minor",
                "25000000",
                "--strategy-release",
                "rel-cli-v1",
            )
            self.assertEqual(paper_out["account_id"], "paper-cli-btc")
            self.assertEqual(paper_out["tier"], "company_paper")

            live_out = _run_cli(
                "register-live-candidate",
                str(store),
                "--company-id",
                cid,
                "--lease-dir",
                str(leases),
                "--binding-id",
                "live-cli-cand-1",
                "--account-id",
                "binance-cli-prod-01",
                "--destination-id",
                "binance-spot-prod",
                "--connector-revision",
                "binance-rest-v1",
                "--instrument-rules-digest",
                "btc-usdt-spot-v1",
                "--starting-minor",
                "25000000",
                "--exposure-limit-minor",
                "15000000",
                "--credential-handle",
                "envref:BINANCE_CLI_PROD",
            )
            self.assertEqual(live_out["binding_id"], "live-cli-cand-1")
            self.assertEqual(live_out["status"], "registered")

            qual_out = _run_cli(
                "qualify-live-venue",
                str(store),
                "--company-id",
                cid,
                "--lease-dir",
                str(leases),
                "--instrument-rules-digest",
                "btc-usdt-spot-v1",
                "--mode",
                "emulator",
                "--receipt-id",
                "qual-cli-emu-1",
            )
            self.assertEqual(qual_out["receipt"]["verdict"], "PASS")
            self.assertEqual(qual_out["receipt"]["cases_passed"], 6)
            self.assertEqual(len(qual_out["cases"]), 6)

            arm_out = _run_cli(
                "arm-trading-schedule",
                str(store),
                "--company-id",
                cid,
                "--lease-dir",
                str(leases),
                "--agent-id",
                ceo_id,
                "--decision-delay",
                "120",
                "--reconcile-delay",
                "30",
                "--enqueue-now",
            )
            self.assertEqual(len(arm_out["armed_conditions"]), 2)
            self.assertIsNotNone(arm_out["immediate_work_item_id"])

            with closing(open_reader(store)) as db, read_tx(db):
                resp_cnt = int(
                    db.execute(
                        "SELECT COUNT(*) FROM work_responsibilities WHERE company_id = ? AND status = 'active'",
                        (cid,),
                    ).fetchone()[0]
                )
                cond_cnt = int(
                    db.execute(
                        "SELECT COUNT(*) FROM work_conditions WHERE company_id = ? AND status = 'armed'",
                        (cid,),
                    ).fetchone()[0]
                )
                item_cnt = int(
                    db.execute(
                        "SELECT COUNT(*) FROM work_items WHERE company_id = ? AND duty_kind = 'quant.paper_decision'",
                        (cid,),
                    ).fetchone()[0]
                )
            self.assertEqual(resp_cnt, 2)
            self.assertEqual(cond_cnt, 2)
            self.assertEqual(item_cnt, 1)

    def test_board_commands_open_paper_book_and_reconcile_rbac_idempotency_conflict(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-board-cmd-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            now = time_ns()
            founding = found(
                store,
                FoundingTerms("sd-board-cmd", "charter-v7", "paper", "profile-v1"),
            )
            cid = founding.company_id
            ceo_id = founding.founder_id

            with hold_company_writer(store, cid, lease_directory=leases) as writer:
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    enable_domain_v1(db, "trading")
                bind_board_principal(
                    writer, principal_id="board-chair", role_name=BOARD_CONTROL_ROLE
                )
                bind_board_principal(
                    writer, principal_id="board-obs", role_name=BOARD_VIEWER_ROLE
                )

                # Viewer is forbidden (403)
                with self.assertRaises(BoardControlForbidden):
                    execute_execution_tier_command(
                        writer,
                        command_id="cmd-viewer-blocked",
                        command_type=OPEN_COMPANY_PAPER_BOOK,
                        preconditions={"charter_revision": "charter-v7"},
                        payload={
                            "account_id": "paper-nope",
                            "book_name": "Viewer Book",
                            "starting_minor_units": 10_000_000,
                            "exposure_limit_minor_units": 5_000_000,
                        },
                        principal_hint="board-obs",
                    )

                # Stale charter returns deterministic denied receipt
                stale_out = execute_execution_tier_command(
                    writer,
                    command_id="cmd-stale-charter",
                    command_type=OPEN_COMPANY_PAPER_BOOK,
                    preconditions={"charter_revision": "charter-old"},
                    payload={
                        "account_id": "paper-stale",
                        "book_name": "Stale Book",
                        "starting_minor_units": 10_000_000,
                        "exposure_limit_minor_units": 5_000_000,
                    },
                    principal_hint="board-chair",
                )
                self.assertEqual(stale_out.state, "denied")
                self.assertEqual(stale_out.denial_code, "conflicting_revision")

                # Board control creates new paper book!
                open_payload = {
                    "account_id": "paper-eth-mr",
                    "book_name": "ETH Mean Reversion",
                    "starting_minor_units": 20_000_000,
                    "exposure_limit_minor_units": 10_000_000,
                    "strategy_release_id": "rel-eth-v2",
                    "fee_bps": 4,
                    "slippage_bps": 2,
                }
                applied = execute_execution_tier_command(
                    writer,
                    command_id="cmd-open-eth-1",
                    command_type=OPEN_COMPANY_PAPER_BOOK,
                    preconditions={"charter_revision": "charter-v7"},
                    payload=open_payload,
                    principal_hint="board-chair",
                )
                self.assertEqual(applied.state, "applied")
                self.assertEqual(applied.result["account_id"], "paper-eth-mr")
                binding_id = applied.result["binding_id"]

                # Exact replay returns cached receipt idempotently
                replayed = execute_execution_tier_command(
                    writer,
                    command_id="cmd-open-eth-1",
                    command_type=OPEN_COMPANY_PAPER_BOOK,
                    preconditions={"charter_revision": "charter-v7"},
                    payload=open_payload,
                    principal_hint="board-chair",
                )
                self.assertEqual(replayed.state, "applied")
                self.assertEqual(replayed.result["binding_id"], binding_id)

                # Reusing command_id with mutated payload raises conflict (409)
                with self.assertRaises(BoardControlConflict):
                    execute_execution_tier_command(
                        writer,
                        command_id="cmd-open-eth-1",
                        command_type=OPEN_COMPANY_PAPER_BOOK,
                        preconditions={"charter_revision": "charter-v7"},
                        payload=open_payload | {"book_name": "Tampered"},
                        principal_hint="board-chair",
                    )

                # Fill a position then trigger on-demand Board reconcile!
                submit_company_paper_book_order(
                    writer,
                    binding_id=binding_id,
                    actor_id=ceo_id,
                    action_id="eth-fill-1",
                    instrument_id="ETH-USDT",
                    side="buy",
                    quantity=10,
                    limit_minor_units=360_000,
                    market_price_minor_units=350_000,
                    quote_observed_at_ns=now,
                    now_ns=now,
                )
                quotes = StaticQuoteProvider(
                    {
                        "ETH-USDT": MarketQuote(
                            "ETH-USDT", 380_000, time_ns(), "binance-spot"
                        )
                    }
                )
                rec_out = execute_execution_tier_command(
                    writer,
                    command_id="cmd-rec-1",
                    command_type=RECONCILE_TRADING_BOOKS,
                    preconditions={"charter_revision": "charter-v7"},
                    payload={"max_quote_age_seconds": 60},
                    principal_hint="board-chair",
                    quote_provider=quotes,
                )
                self.assertEqual(rec_out.state, "applied")
                self.assertEqual(len(rec_out.result["books_marked"]), 1)
                self.assertEqual(
                    rec_out.result["books_marked"][0]["instruments_marked"],
                    ["ETH-USDT"],
                )

                # Plugin exposes both new commands in contribute_commands()
                plugin = TradingDomainPlugin(quote_provider=quotes)
                cmd_types = {c.command_type for c in plugin.contribute_commands()}
                self.assertIn(OPEN_COMPANY_PAPER_BOOK, cmd_types)
                self.assertIn(RECONCILE_TRADING_BOOKS, cmd_types)

    def test_open_paper_book_command_atomic_with_receipt_and_recurring_schedule_rearms(
        self,
    ) -> None:
        from unittest import mock

        from sourdough.agent_runtime.registry import AgentAdapterRegistry
        from sourdough.runtime.engine import CompanyRuntime
        from sourdough.runtime.handlers import build_production_duty_registry
        from sourdough.trading import commands as trading_commands

        with TemporaryDirectory(prefix="sd-pr193-fixes-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            found_out = _run_cli(
                "found",
                str(store),
                "--setup-identity",
                "sd-atomic-sched",
                "--charter-revision",
                "charter-s1",
                "--founder-scope",
                "paper",
                "--execution-profile",
                "profile-v1",
            )
            cid = found_out["company_id"]
            ceo_id = found_out["founder_id"]

            # 1. Atomicity: if admit_command_receipt fails mid-transaction, the paper book MUST roll back!
            with hold_company_writer(store, cid, lease_directory=leases) as writer:
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    enable_domain_v1(db, "trading")
                bind_board_principal(
                    writer, principal_id="board-chair", role_name=BOARD_CONTROL_ROLE
                )
                with (
                    mock.patch.object(
                        trading_commands,
                        "admit_command_receipt",
                        side_effect=RuntimeError("simulated crash before commit"),
                    ),
                    self.assertRaises(RuntimeError),
                ):
                    execute_execution_tier_command(
                        writer,
                        command_id="cmd-atomic-rollback",
                        command_type=OPEN_COMPANY_PAPER_BOOK,
                        preconditions={"charter_revision": "charter-s1"},
                        payload={
                            "account_id": "paper-should-rollback",
                            "book_name": "Atomic Rollback Book",
                            "starting_minor_units": 10_000_000,
                            "exposure_limit_minor_units": 5_000_000,
                        },
                        principal_hint="board-chair",
                    )
                with closing(open_reader(store)) as db, read_tx(db):
                    book_cnt = int(
                        db.execute(
                            """SELECT COUNT(*) FROM execution_environment_binding
                               WHERE company_id = ? AND account_id = 'paper-should-rollback'""",
                            (cid,),
                        ).fetchone()[0]
                    )
                    rcpt_cnt = int(
                        db.execute(
                            """SELECT COUNT(*) FROM command_receipt
                               WHERE company_id = ? AND command_id = 'cmd-atomic-rollback'""",
                            (cid,),
                        ).fetchone()[0]
                    )
                self.assertEqual(book_cnt, 0)
                self.assertEqual(rcpt_cnt, 0)

            # 2. Recurring schedule: arm-trading-schedule arms quant.reconcile with interval,
            #    and after Run 1 settles, a successor armed timer fires Run 2 automatically!
            arm_out = _run_cli(
                "arm-trading-schedule",
                str(store),
                "--company-id",
                cid,
                "--lease-dir",
                str(leases),
                "--agent-id",
                ceo_id,
                "--decision-delay",
                "300",
                "--reconcile-delay",
                "20",
            )
            rec_entry = next(
                c
                for c in arm_out["armed_conditions"]
                if c["duty_kind"] == "quant.reconcile"
            )
            self.assertEqual(rec_entry["interval_seconds"], 20)
            first_due = int(rec_entry["due_at_ns"])

            trading_plugin = TradingDomainPlugin(quote_provider=StaticQuoteProvider({}))
            dreg = build_production_duty_registry(
                adapter_registry=AgentAdapterRegistry(),
                profile_bindings={},
                domain_plugins=(trading_plugin,),
            )
            rt = CompanyRuntime(
                store,
                cid,
                dreg,
                lease_directory=leases,
            )
            # Tick 1 at first_due: fires quant.reconcile Run 1 and atomically rearms successor timer!
            step1 = rt.step(max_dispatches=1, now_ns=first_due + 1)
            self.assertEqual(len(step1.executed_runs), 1)
            with closing(open_reader(store)) as db, read_tx(db):
                rearmed_row = db.execute(
                    """SELECT condition_id, due_at_ns, cooldown_ns
                       FROM work_conditions
                       WHERE company_id = ? AND responsibility_id = ? AND status = 'armed'""",
                    (cid, rec_entry["responsibility_id"]),
                ).fetchone()
            self.assertIsNotNone(rearmed_row)
            assert rearmed_row is not None
            self.assertNotEqual(str(rearmed_row[0]), rec_entry["condition_id"])
            self.assertEqual(int(rearmed_row[2]), 20 * 1_000_000_000)
            second_due = int(rearmed_row[1])
            self.assertGreaterEqual(second_due, first_due + 20 * 1_000_000_000)

            # Tick 2 at second_due: fires quant.reconcile Run 2 and rearms Run 3!
            step2 = rt.step(max_dispatches=1, now_ns=second_due + 1)
            self.assertEqual(len(step2.executed_runs), 1)
            with closing(open_reader(store)) as db, read_tx(db):
                settled_reconciles = int(
                    db.execute(
                        """SELECT COUNT(*) FROM work_items
                           WHERE company_id = ? AND duty_kind = 'quant.reconcile' AND state = 'settled'""",
                        (cid,),
                    ).fetchone()[0]
                )
                still_armed = int(
                    db.execute(
                        """SELECT COUNT(*) FROM work_conditions
                           WHERE company_id = ? AND responsibility_id = ? AND status = 'armed'""",
                        (cid, rec_entry["responsibility_id"]),
                    ).fetchone()[0]
                )
            self.assertEqual(settled_reconciles, 2)
            self.assertEqual(still_armed, 1)


if __name__ == "__main__":
    unittest.main()
