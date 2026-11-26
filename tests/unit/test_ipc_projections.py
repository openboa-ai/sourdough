"""Unit tests for M3 IPC consolidation: 10 Core + 3 Trading projections & domain gating (D7)."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    PAUSE_COMPANY,
    RESTRICTION_COMMAND,
    bind_board_principal,
)
from sourdough.company import FoundingTerms, found
from sourdough.ipc import CompanyDaemon, ControlPlaneClient, IpcRefused
from sourdough.ipc.projections import (
    ALL_CLIENT_PROJECTIONS,
    CORE_CLIENT_PROJECTIONS,
)
from sourdough.trading.accounts import (
    open_agent_sandbox,
    open_company_paper_book,
)
from sourdough.trading.commands import (
    ACTIVATE_STRATEGY_RELEASE,
    EXECUTION_TIER_COMMAND,
    PROMOTE_EXECUTION_ENVIRONMENT,
    STRATEGY_COMMAND,
)
from sourdough.trading.domain import ENV_LIVE, ENV_PAPER
from sourdough.trading.ledger import (
    submit_agent_sandbox_order,
    submit_company_paper_book_order,
)
from sourdough.trading.projections import (
    read_trading_desk_projection,
)
from sourdough.trading.risk import SCOPE_EXECUTION_PAPER, TRADING_RISK_CAUSE_CATEGORY
from sourdough.writer_lease import hold_company_writer


class IpcConsolidationTests(unittest.TestCase):
    def test_core_only_daemon_gates_trading_projections_and_commands(self) -> None:
        with (
            TemporaryDirectory(prefix="sd-ipc-core-") as tmp,
            TemporaryDirectory(dir="/tmp", prefix="sd-s-") as sock_tmp,
        ):
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            sock_dir = Path(sock_tmp)
            founding = found(
                store,
                FoundingTerms("core-only", "charter-v1", "manage", "profile-v1"),
            )
            cid = founding.company_id
            with hold_company_writer(store, cid, lease_directory=leases) as writer:
                bind_board_principal(
                    writer, principal_id="board-1", role_name=BOARD_CONTROL_ROLE
                )

            with CompanyDaemon(
                store, cid, socket_dir=sock_dir, lease_directory=leases
            ) as daemon:
                client = ControlPlaneClient(daemon.socket_path)
                meta = client.meta()
                self.assertEqual(meta["active_domains"], ["core"])
                self.assertNotIn("paper_risk_suspended", meta)
                self.assertNotIn("paper_risk_effective_revision", meta)
                self.assertEqual(len(CORE_CLIENT_PROJECTIONS), 10)
                self.assertEqual(len(ALL_CLIENT_PROJECTIONS), 13)
                self.assertEqual(len(meta["supported_projections"]), 12)
                self.assertNotIn("trading-desk-v1", meta["supported_projections"])
                self.assertNotIn("live-trading-v1", meta["supported_projections"])
                self.assertNotIn("execution-tiers-v1", meta["supported_projections"])
                self.assertNotIn(
                    PROMOTE_EXECUTION_ENVIRONMENT, meta["supported_commands"]
                )
                self.assertNotIn(ACTIVATE_STRATEGY_RELEASE, meta["supported_commands"])

                # Trading & retired projections must 404 on Core-only store.
                for suffix in (
                    "trading-desk?environment=live",
                    "orders/ord-1",
                    "strategies",
                    "live-trading",
                    "execution-tiers",
                ):
                    with self.assertRaisesRegex(IpcRefused, "HTTP 404"):
                        client.projection(suffix, principal="board-1")

                # Core projections succeed with neutral non-trading defaults.
                s_fin, _, fin = client.projection("finance", principal="board-1")
                self.assertEqual(s_fin, 200)
                assert fin is not None
                self.assertFalse(fin["trading_enabled"])
                self.assertEqual(fin["funding_settlements"], [])

                s_rest, _, rest = client.projection("restrictions", principal="board-1")
                self.assertEqual(s_rest, 200)
                assert rest is not None
                self.assertIsNone(rest["trading_risk"])

                s_graph, _, graph = client.projection(
                    "agents-graph", principal="board-1"
                )
                self.assertEqual(s_graph, 200)
                assert graph is not None
                self.assertFalse(graph["nodes"][0]["sandbox_authorized"])

                # Trading commands & execution:* restrictions rejected 422 on Core-only store
                # (including uppercase/mixed-case bypass attempts).
                for bad_id, bad_scope, bad_cat in (
                    (
                        "cmd-exec-422",
                        SCOPE_EXECUTION_PAPER,
                        TRADING_RISK_CAUSE_CATEGORY,
                    ),
                    ("cmd-exec-upper-paper", "EXECUTION:PAPER", "board_pause"),
                    ("cmd-exec-upper-all", "  Execution:All  ", "board_pause"),
                    ("cmd-exec-upper-cat", "company:all", "TRADING_RISK_PAUSE"),
                ):
                    code_exec, _ = client.submit_command(
                        company_id=cid,
                        command_id=bad_id,
                        command_type=PAUSE_COMPANY,
                        contract_version=RESTRICTION_COMMAND,
                        preconditions={
                            "charter_revision": "charter-v1",
                            "restriction_id": f"rest-{bad_id}",
                            "expected_restriction_revision": 0,
                        },
                        payload={
                            "cause_category": bad_cat,
                            "scope_ref": bad_scope,
                            "reason": "Core-only must fail",
                        },
                        principal="board-1",
                    )
                    self.assertEqual(code_exec, 422, bad_id)

                code_tier, _ = client.submit_command(
                    company_id=cid,
                    command_id="cmd-tier-422",
                    command_type=PROMOTE_EXECUTION_ENVIRONMENT,
                    contract_version=EXECUTION_TIER_COMMAND,
                    preconditions={
                        "charter_revision": "charter-v1",
                        "target_binding_id": "b-1",
                        "qualification_receipt_id": "q-1",
                        "expected_payload_hash": "0" * 64,
                    },
                    payload={"reason": "Core-only must fail"},
                    principal="board-1",
                )
                self.assertEqual(code_tier, 422)

                code_strat, _ = client.submit_command(
                    company_id=cid,
                    command_id="cmd-strat-422",
                    command_type=ACTIVATE_STRATEGY_RELEASE,
                    contract_version=STRATEGY_COMMAND,
                    preconditions={"binding_revision": "1"},
                    payload={"release_id": "rel-1"},
                    principal="board-1",
                )
                self.assertEqual(code_strat, 422)

    def test_trading_daemon_exposes_15_projections_and_order_trace_across_tiers(
        self,
    ) -> None:
        with (
            TemporaryDirectory(prefix="sd-ipc-tr-") as tmp,
            TemporaryDirectory(dir="/tmp", prefix="sd-s-") as sock_tmp,
        ):
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            sock_dir = Path(sock_tmp)
            founding = found(
                store,
                FoundingTerms("trading-co", "charter-v1", "paper", "profile-v1"),
            )
            cid = founding.company_id
            founder = founding.founder_id

            with hold_company_writer(store, cid, lease_directory=leases) as writer:
                bind_board_principal(
                    writer, principal_id="board-1", role_name=BOARD_CONTROL_ROLE
                )
                open_agent_sandbox(writer, agent_id=founder, account_id="sbx-acct-1")
                sbx_ord = submit_agent_sandbox_order(
                    writer,
                    agent_id=founder,
                    action_id="sbx-act-1",
                    instrument_id="ETH-USDT",
                    side="buy",
                    quantity=1,
                    limit_minor_units=3_100_000,
                    market_price_minor_units=3_000_000,
                )
                pbook = open_company_paper_book(
                    writer,
                    account_id="paper-acct-1",
                    book_name="Main Paper",
                    starting_minor_units=50_000_000,
                    exposure_limit_minor_units=25_000_000,
                )
                p_ord = submit_company_paper_book_order(
                    writer,
                    binding_id=pbook.binding_id,
                    actor_id=founder,
                    action_id="paper-act-1",
                    instrument_id="BTC-USDT",
                    side="buy",
                    quantity=1,
                    limit_minor_units=6_100_000,
                    market_price_minor_units=6_000_000,
                )

            with CompanyDaemon(
                store, cid, socket_dir=sock_dir, lease_directory=leases
            ) as daemon:
                client = ControlPlaneClient(daemon.socket_path)
                meta = client.meta()
                self.assertEqual(meta["active_domains"], ["core", "trading"])
                self.assertEqual(len(meta["supported_projections"]), 15)
                self.assertIn("trading-desk-v1", meta["supported_projections"])
                self.assertIn("order-trace-v1", meta["supported_projections"])
                self.assertIn("strategies-v1", meta["supported_projections"])
                self.assertIn(PROMOTE_EXECUTION_ENVIRONMENT, meta["supported_commands"])

                # Standby live trading desk uses neutral standby copy.
                s_live, _, live_desk = client.projection(
                    "trading-desk?environment=live", principal="board-1"
                )
                self.assertEqual(s_live, 200)
                assert live_desk is not None
                self.assertTrue(live_desk["trading_enabled"])
                self.assertEqual(
                    live_desk["kpis"]["portfolio_value"]["source"],
                    "Standby (0% live capital deployed)",
                )

                # Scope execution:paper pause suspends ONLY paper trading-desk, leaving live clear!
                c_pause, _ = client.submit_command(
                    company_id=cid,
                    command_id="pause-p",
                    command_type=PAUSE_COMPANY,
                    contract_version=RESTRICTION_COMMAND,
                    preconditions={
                        "charter_revision": "charter-v1",
                        "restriction_id": "rest-p",
                        "expected_restriction_revision": 0,
                    },
                    payload={
                        "cause_category": TRADING_RISK_CAUSE_CATEGORY,
                        "scope_ref": SCOPE_EXECUTION_PAPER,
                        "reason": "Pause paper only",
                    },
                    principal="board-1",
                )
                self.assertEqual(c_pause, 200)
                paper_after, _ = read_trading_desk_projection(
                    store, cid, environment=ENV_PAPER
                )
                live_after, _ = read_trading_desk_projection(
                    store, cid, environment=ENV_LIVE
                )
                self.assertTrue(paper_after["risk_gate"]["suspended"])
                self.assertFalse(live_after["risk_gate"]["suspended"])

                # order-trace-v1 serves both agent_sandbox and paper orders cleanly.
                s_p, _, p_trace = client.projection(
                    f"orders/{p_ord['order_id']}", principal="board-1"
                )
                self.assertEqual(s_p, 200)
                assert p_trace is not None
                self.assertEqual(p_trace["order"]["environment"], "paper")
                self.assertEqual(p_trace["order"]["back_link"], "/trading/paper")

                s_s, _, s_trace = client.projection(
                    f"orders/{sbx_ord.order_id}", principal="board-1"
                )
                self.assertEqual(s_s, 200)
                assert s_trace is not None
                self.assertEqual(s_trace["order"]["environment"], "agent_sandbox")
                self.assertEqual(s_trace["order"]["back_link"], f"/agents/{founder}")

                with self.assertRaisesRegex(IpcRefused, "HTTP 404"):
                    client.projection("orders/nonexistent", principal="board-1")

                # strategies-v1 works & reflects trading_enabled=True.
                s_st, _, strat = client.projection("strategies", principal="board-1")
                self.assertEqual(s_st, 200)
                assert strat is not None
                self.assertTrue(strat["trading_enabled"])
                self.assertIsNotNone(strat["adapter"])
