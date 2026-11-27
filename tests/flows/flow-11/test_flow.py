"""Connected Flow 11: Unified risk pause/resume (restriction-command-v1), restore fencing, and trading_book_fill."""

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns

from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    PAUSE_COMPANY,
    RESTRICTION_COMMAND,
    RESUME_COMPANY,
    bind_board_principal,
)
from sourdough.company import FoundingRefused, FoundingTerms, found
from sourdough.company_checkpoint import CheckpointRefused
from sourdough.company_restore import restore_core_checkpoint
from sourdough.ipc import CompanyDaemon, ControlPlaneClient
from sourdough.storage.checkpoint import checkpoint_store, restore_store
from sourdough.storage.db import open_writer, write_tx
from sourdough.trading.accounts import open_company_paper_book
from sourdough.trading.domain import ExecutionTierRefused
from sourdough.trading.engine import VERSION
from sourdough.trading.execution import VenueRefused
from sourdough.trading.ledger import submit_company_paper_book_order
from sourdough.trading.market_worker import SpotInstrumentTerms
from sourdough.trading.nautilus_paper import NautilusPaperVenue, SandboxPlan
from sourdough.trading.risk import SCOPE_EXECUTION_ALL, TRADING_RISK_CAUSE_CATEGORY
from sourdough.trading.sender_lease import hold_sender_lease
from sourdough.writer_lease import hold_company_writer


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self) -> None:
        root = Path(self.enterContext(TemporaryDirectory(prefix="sd-f11-")))
        sock_root = Path(
            self.enterContext(TemporaryDirectory(prefix="sd-", dir="/tmp"))
        )
        source = root / "company.sqlite"
        leases = root / "leases"
        sock_dir = sock_root / "s"
        checkpoint = root / "checkpoint.sd"
        replacement = root / "replacement.sqlite"
        engine = root / "engine"

        company = found(source, FoundingTerms("board", "charter", "paper", "profile"))
        company_id = company.company_id
        founder_id = company.founder_id

        runtime = engine / f"nautilus-{VERSION}"
        runtime.mkdir(parents=True)
        venue = NautilusPaperVenue(runtime / "sandbox.sqlite")
        terms = SpotInstrumentTerms(
            "BTCUSDT", "BTC", "USDT", "0.01", "0.00001", "0.00001", "5"
        )
        venue.provision(SandboxPlan(company_id, "paper", terms, 100_000_000_000, 0))
        generation = "fixture-sender"
        venue.prove_account(generation, "BINANCE-001", "USDT", venue.start_generation())
        stale_ns = time_ns() - 30_000_000_000
        venue.publish_quote(generation, 60_000_000, stale_ns, stale_ns)

        # 1. Open Company Paper Book, record one filled order + one resting open order.
        with hold_company_writer(source, company_id, lease_directory=leases) as writer:
            bind_board_principal(
                writer, principal_id="board-chair", role_name=BOARD_CONTROL_ROLE
            )
            book = open_company_paper_book(
                writer,
                account_id="paper-acct-1",
                book_name="Flow 11 Paper Book",
                starting_minor_units=100_000_000,
                exposure_limit_minor_units=50_000_000,
            )
            filled_order = submit_company_paper_book_order(
                writer,
                binding_id=book.binding_id,
                actor_id=founder_id,
                action_id="initial-fill",
                instrument_id="BTC-USDT",
                side="buy",
                quantity=2,
                limit_minor_units=6_100_000,
                market_price_minor_units=6_000_000,
                fill_mode="immediate",
            )
            open_order = submit_company_paper_book_order(
                writer,
                binding_id=book.binding_id,
                actor_id=founder_id,
                action_id="resting-open",
                instrument_id="BTC-USDT",
                side="buy",
                quantity=1,
                limit_minor_units=5_800_000,
                market_price_minor_units=6_000_000,
                fill_mode="resting",
            )
            self.assertEqual(filled_order["status"], "filled")
            self.assertEqual(open_order["status"], "open")

        # 2. Suspend execution via restriction-command-v1 over CompanyDaemon UDS.
        with CompanyDaemon(
            source,
            company_id,
            socket_dir=sock_dir,
            lease_directory=leases,
            engine_directory=engine,
        ) as daemon:
            client = ControlPlaneClient(daemon.socket_path)
            code, resp = client.submit_command(
                company_id=company_id,
                command_id="pause-exec-1",
                command_type=PAUSE_COMPANY,
                contract_version=RESTRICTION_COMMAND,
                preconditions={
                    "charter_revision": "charter",
                    "restriction_id": "rest-exec-all",
                    "expected_restriction_revision": 0,
                },
                payload={
                    "scope_ref": SCOPE_EXECUTION_ALL,
                    "cause_category": TRADING_RISK_CAUSE_CATEGORY,
                    "reason": "Review open exposure",
                },
                principal="board-chair",
            )
            self.assertEqual(code, 200, resp)
            self.assertEqual(resp["state"], "applied")

            _, _, desk = client.projection(
                "trading-desk?environment=paper", expected_company_id=company_id
            )
            assert desk is not None
            self.assertTrue(desk["risk_gate"]["suspended"])

            # Resume must fail closed while open order exists in trading_book_order.
            code_open, body_open = client.submit_command(
                company_id=company_id,
                command_id="resume-blocked-open",
                command_type=RESUME_COMPANY,
                contract_version=RESTRICTION_COMMAND,
                preconditions={
                    "charter_revision": "charter",
                    "restriction_id": "rest-exec-all",
                    "expected_restriction_revision": 1,
                },
                payload={"reason": "Premature resume"},
                principal="board-chair",
            )
            self.assertEqual(code_open, 400)
            self.assertIn("open order", body_open["error"])

        # 3. Verify suspended order submission fails closed and restore gates active sender/domain guard.
        with hold_company_writer(source, company_id, lease_directory=leases) as writer:
            with self.assertRaisesRegex(
                ExecutionTierRefused, "blocked by active restriction"
            ):
                submit_company_paper_book_order(
                    writer,
                    binding_id=book.binding_id,
                    actor_id=founder_id,
                    action_id="new-risk",
                    instrument_id="BTC-USDT",
                    side="buy",
                    quantity=1,
                    limit_minor_units=6_100_000,
                    market_price_minor_units=6_000_000,
                )

        checkpoint_store(source, company_id, checkpoint, lease_directory=leases)
        with self.assertRaisesRegex(
            CheckpointRefused, "independent restore reconciler"
        ):
            restore_core_checkpoint(source, company_id, checkpoint, replacement)

        def fail_on_active_sender(_conn: sqlite3.Connection) -> None:
            with hold_sender_lease(venue.journal):
                pass

        with hold_sender_lease(venue.journal):
            with self.assertRaisesRegex(VenueRefused, "active or pending sender"):
                restore_store(
                    source,
                    company_id,
                    checkpoint,
                    replacement,
                    check_original=fail_on_active_sender,
                    lease_directory=leases,
                )
        self.assertFalse(replacement.exists())

        # 4. Settle/cancel open order, re-checkpoint, and restore idempotently with source retirement.
        with hold_company_writer(source, company_id, lease_directory=leases) as writer:
            with closing(open_writer(source, lease=writer)) as db, write_tx(db):
                db.execute(
                    "UPDATE trading_book_order SET status = 'cancelled', cancelled_at_ns = ? WHERE order_id = ?",
                    (time_ns(), open_order["order_id"]),
                )

        checkpoint_settled = root / "checkpoint-settled.sd"
        checkpoint_store(source, company_id, checkpoint_settled, lease_directory=leases)
        restored = restore_store(
            source,
            company_id,
            checkpoint_settled,
            replacement,
            lease_directory=leases,
        )
        self.assertEqual(
            restore_store(
                source,
                company_id,
                checkpoint_settled,
                replacement,
                lease_directory=leases,
            ),
            restored,
        )
        with self.assertRaisesRegex(FoundingRefused, "retired"):
            with hold_company_writer(source, company_id, lease_directory=leases):
                self.fail("The source regained writer authority")

        with closing(
            sqlite3.connect(replacement.as_uri() + "?mode=ro", uri=True)
        ) as database:
            self.assertEqual(
                database.execute(
                    "SELECT order_id, fill_id, quantity FROM trading_book_fill"
                ).fetchall(),
                [(filled_order["order_id"], filled_order["fill_id"], 2)],
            )
            self.assertEqual(
                database.execute(
                    """SELECT scope_ref, cause_category, active
                       FROM restriction_cause
                       WHERE company_id = ? AND restriction_id = 'rest-exec-all'""",
                    (company_id,),
                ).fetchall(),
                [(SCOPE_EXECUTION_ALL, TRADING_RISK_CAUSE_CATEGORY, 1)],
            )
            self.assertEqual(
                database.execute("PRAGMA foreign_key_check").fetchall(), []
            )

        # 5. On restored store: stale quote (>15s) rejects resume; fresh quote clears and allows new fill.
        with CompanyDaemon(
            replacement,
            company_id,
            socket_dir=sock_dir,
            lease_directory=leases,
            engine_directory=engine,
        ) as daemon:
            client = ControlPlaneClient(daemon.socket_path)
            code_stale, body_stale = client.submit_command(
                company_id=company_id,
                command_id="resume-stale-quote",
                command_type=RESUME_COMPANY,
                contract_version=RESTRICTION_COMMAND,
                preconditions={
                    "charter_revision": "charter",
                    "restriction_id": "rest-exec-all",
                    "expected_restriction_revision": 1,
                },
                payload={"reason": "Stale quote attempt"},
                principal="board-chair",
            )
            self.assertEqual(code_stale, 400)
            self.assertIn("fresh quote", body_stale["error"])

            fresh_ns = time_ns()
            with venue._connect("rw") as vconn, vconn:
                vconn.execute(
                    "UPDATE sandbox_quote SET event_ns = ?, exchange_trade_ns = ?, received_ns = ?",
                    (fresh_ns, fresh_ns, fresh_ns),
                )

            code_ok, resp_ok = client.submit_command(
                company_id=company_id,
                command_id="resume-fresh-quote",
                command_type=RESUME_COMPANY,
                contract_version=RESTRICTION_COMMAND,
                preconditions={
                    "charter_revision": "charter",
                    "restriction_id": "rest-exec-all",
                    "expected_restriction_revision": 1,
                },
                payload={"reason": "Fresh quote verified"},
                principal="board-chair",
            )
            self.assertEqual(code_ok, 200, resp_ok)
            self.assertEqual(resp_ok["state"], "applied")

            _, _, desk_after = client.projection(
                "trading-desk?environment=paper", expected_company_id=company_id
            )
            assert desk_after is not None
            self.assertFalse(desk_after["risk_gate"]["suspended"])

        with hold_company_writer(
            replacement, company_id, lease_directory=leases
        ) as writer:
            resumed_fill = submit_company_paper_book_order(
                writer,
                binding_id=book.binding_id,
                actor_id=founder_id,
                action_id="post-resume-fill",
                instrument_id="BTC-USDT",
                side="buy",
                quantity=1,
                limit_minor_units=6_100_000,
                market_price_minor_units=6_000_000,
                fill_mode="immediate",
            )
            self.assertEqual(resumed_fill["status"], "filled")
