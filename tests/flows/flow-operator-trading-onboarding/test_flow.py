"""Connected Flow E2E test for Operator Trading Onboarding & Day-2 Board Desk Controls."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from typing import Any

from sourdough.ipc import ClientWebBridge, CompanyDaemon
from sourdough.trading import TradingDomainPlugin
from sourdough.trading.commands import (
    EXECUTION_TIER_COMMAND,
    OPEN_COMPANY_PAPER_BOOK,
    RECONCILE_TRADING_BOOKS,
)
from sourdough.trading.ledger import submit_company_paper_book_order
from sourdough.trading.quotes import MarketQuote, StaticQuoteProvider
from sourdough.writer_lease import hold_company_writer


def _run_cli(*args: str) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-m", "sourdough.cli", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def _bridge_json(
    url: str,
    token: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
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
        try:
            err_body = json.loads(err.read().decode("utf-8"))
        finally:
            err.close()
        return err.code, err_body


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self) -> None:
        with (
            TemporaryDirectory(prefix="sd-onboard-flow-") as tmp,
            TemporaryDirectory(prefix="sd-", dir="/tmp") as sock_tmp,
        ):
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            sock_dir = Path(sock_tmp)
            pending_dir = root / "pending"

            # 1. Zero-script operator bootstrap purely through `sourdough.cli`!
            found_out = _run_cli(
                "found",
                str(store),
                "--setup-identity",
                "sourdough-turnkey-fund",
                "--charter-revision",
                "charter-turnkey-v1",
                "--founder-scope",
                "paper",
                "--execution-profile",
                "profile-v1",
            )
            cid = found_out["company_id"]
            ceo_id = found_out["founder_id"]

            _run_cli(
                "bind-board",
                str(store),
                "--company-id",
                cid,
                "--principal-id",
                "board-chair",
                "--role",
                "board_control",
            )
            _run_cli(
                "bind-board",
                str(store),
                "--company-id",
                cid,
                "--principal-id",
                "board-viewer-1",
                "--role",
                "board_viewer",
            )
            _run_cli(
                "enable-trading-domain",
                str(store),
                "--company-id",
                cid,
                "--lease-dir",
                str(leases),
            )
            first_book = _run_cli(
                "open-paper-book",
                str(store),
                "--company-id",
                cid,
                "--lease-dir",
                str(leases),
                "--account-id",
                "paper-btc-spot",
                "--book-name",
                "BTC Spot Momentum",
                "--starting-minor",
                "50000000",
                "--exposure-limit-minor",
                "25000000",
                "--strategy-release",
                "rel-btc-v1",
            )
            _run_cli(
                "register-live-candidate",
                str(store),
                "--company-id",
                cid,
                "--lease-dir",
                str(leases),
                "--binding-id",
                "live-binance-prod-1",
                "--account-id",
                "binance-prod-01",
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
                "envref:BINANCE_PROD",
            )
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
                "qual-turnkey-1",
            )
            self.assertEqual(qual_out["receipt"]["verdict"], "PASS")
            _run_cli(
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
                "60",
            )

            # Seed one fill in the CLI-created paper book so on-demand mark-to-market has open exposure.
            now = time_ns()
            with hold_company_writer(store, cid, lease_directory=leases) as writer:
                submit_company_paper_book_order(
                    writer,
                    binding_id=first_book["binding_id"],
                    actor_id=ceo_id,
                    action_id="turnkey-fill-1",
                    instrument_id="BTC-USDT",
                    side="buy",
                    quantity=2,
                    limit_minor_units=6_900_000,
                    market_price_minor_units=6_800_000,
                    quote_observed_at_ns=now,
                    now_ns=now,
                )

            quotes = StaticQuoteProvider(
                {
                    "BTC-USDT": MarketQuote(
                        "BTC-USDT", 7_150_000, time_ns(), "binance-spot"
                    )
                }
            )
            trading_plugin = TradingDomainPlugin(quote_provider=quotes)

            # 2. Boot CompanyDaemon + Board Control & Viewer Web Bridges and test Day-2 HTTP commands!
            with (
                CompanyDaemon(
                    store,
                    cid,
                    socket_dir=sock_dir,
                    lease_directory=leases,
                    domain_plugins=(trading_plugin,),
                ) as daemon,
                ClientWebBridge(
                    daemon.socket_path,
                    port=0,
                    pending_dir=pending_dir,
                    principal="board-chair",
                ) as bridge_ctrl,
                ClientWebBridge(
                    daemon.socket_path,
                    port=0,
                    pending_dir=pending_dir,
                    principal="board-viewer-1",
                ) as bridge_viewer,
            ):
                base_ctrl = bridge_ctrl.info.url.rstrip("/")
                tok_ctrl = bridge_ctrl.info.session_token
                base_view = bridge_viewer.info.url.rstrip("/")
                tok_view = bridge_viewer.info.session_token

                # Confirm meta advertises both new commands dynamically from TradingDomainPlugin!
                st_meta, meta = _bridge_json(f"{base_ctrl}/api/v1/meta", tok_ctrl)
                self.assertEqual(st_meta, 200)
                self.assertIn(OPEN_COMPANY_PAPER_BOOK, meta["supported_commands"])
                self.assertIn(RECONCILE_TRADING_BOOKS, meta["supported_commands"])

                # Viewer cannot open paper books (403)!
                st_v, _ = _bridge_json(
                    f"{base_view}/api/v1/commands",
                    tok_view,
                    method="POST",
                    idempotency_key="viewer-open-denied",
                    payload={
                        "company_id": cid,
                        "command_id": "viewer-open-denied",
                        "command_type": OPEN_COMPANY_PAPER_BOOK,
                        "contract_version": EXECUTION_TIER_COMMAND,
                        "preconditions": {"charter_revision": "charter-turnkey-v1"},
                        "payload": {
                            "account_id": "paper-forbidden",
                            "book_name": "Viewer Attempt",
                            "starting_minor_units": 10_000_000,
                            "exposure_limit_minor_units": 5_000_000,
                        },
                    },
                )
                self.assertEqual(st_v, 403)

                # Board Control opens a 2nd concurrent Paper Book over HTTP!
                st_open, open_res = _bridge_json(
                    f"{base_ctrl}/api/v1/commands",
                    tok_ctrl,
                    method="POST",
                    idempotency_key="board-open-eth-1",
                    payload={
                        "company_id": cid,
                        "command_id": "board-open-eth-1",
                        "command_type": OPEN_COMPANY_PAPER_BOOK,
                        "contract_version": EXECUTION_TIER_COMMAND,
                        "preconditions": {"charter_revision": "charter-turnkey-v1"},
                        "payload": {
                            "account_id": "paper-eth-mr",
                            "book_name": "ETH Mean Reversion",
                            "starting_minor_units": 30_000_000,
                            "exposure_limit_minor_units": 15_000_000,
                            "strategy_release_id": "rel-eth-v1",
                            "fee_bps": 4,
                            "slippage_bps": 2,
                        },
                    },
                )
                self.assertEqual(st_open, 200, open_res)
                self.assertEqual(open_res["state"], "applied")

                # Board Control triggers on-demand Sync Quotes & Reconcile over HTTP!
                st_rec, rec_res = _bridge_json(
                    f"{base_ctrl}/api/v1/commands",
                    tok_ctrl,
                    method="POST",
                    idempotency_key="board-reconcile-1",
                    payload={
                        "company_id": cid,
                        "command_id": "board-reconcile-1",
                        "command_type": RECONCILE_TRADING_BOOKS,
                        "contract_version": EXECUTION_TIER_COMMAND,
                        "preconditions": {"charter_revision": "charter-turnkey-v1"},
                        "payload": {"max_quote_age_seconds": 60},
                    },
                )
                self.assertEqual(st_rec, 200, rec_res)
                self.assertEqual(rec_res["state"], "applied")
                self.assertEqual(len(rec_res["result"]["books_marked"]), 1)

                # Trading Desk projection shows both concurrent Paper Books + positive mark-to-market PnL!
                st_desk, desk = _bridge_json(
                    f"{base_ctrl}/api/v1/projections/trading-desk?environment=paper&book_id={first_book['binding_id']}",
                    tok_ctrl,
                )
                self.assertEqual(st_desk, 200)
                self.assertEqual(len(desk["books"]), 2)
                self.assertGreater(desk["kpis"]["trading_pnl"]["value_minor"], 0)
                # Live candidates show our CLI-registered candidate with PASS receipt ready!
                st_live, live_desk = _bridge_json(
                    f"{base_ctrl}/api/v1/projections/trading-desk?environment=live",
                    tok_ctrl,
                )
                self.assertEqual(st_live, 200)
                self.assertEqual(len(live_desk["governance"]["live_candidates"]), 1)
                self.assertEqual(
                    live_desk["governance"]["live_candidates"][0][
                        "matching_qualification_receipt_id"
                    ],
                    "qual-turnkey-1",
                )


if __name__ == "__main__":
    unittest.main()
