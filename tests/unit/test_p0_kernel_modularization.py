"""Unit tests for PR-3 (P0-C) backend modularization, deduplication, and ExecutionClaim."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns

from sourdough.agent_runtime import PROVIDER_OPENAI, AgentProfileBinding
from sourdough.board_control import (
    BoardControlConflict,
    apply_company_restriction,
    bind_board_principal,
    post_ceo_message,
)
from sourdough.company import FoundingTerms, found
from sourdough.duty_run import (
    DutyClaim,
    ExecutionClaim,
    WorkRunClaim,
    resolve_run,
)
from sourdough.ipc import projections as projections_pkg
from sourdough.ipc import redaction as redaction_mod
from sourdough.legacy_duty import DutyClaim as LegacyDutyClaim
from sourdough.legacy_duty import start_run as legacy_start_run
from sourdough.runtime import work_kernel as wk_facade
from sourdough.runtime.kernel import (
    claim_work_run,
    create_work_item,
    evaluate_conditions,
    record_work_decision,
    register_responsibility,
    set_condition,
)
from sourdough.storage.db import StorageRefused, scoped_reader, write_uow
from sourdough.storage.ownership import audit_domain_write_ownership
from sourdough.trading.accounts import (
    open_agent_sandbox,
    open_company_paper_book,
)
from sourdough.trading.ledger import (
    read_agent_sandbox_portfolio,
    read_company_paper_book_portfolio_from_db,
    submit_agent_sandbox_order,
    submit_company_paper_book_order,
)
from sourdough.trading.policy import TRADING_WORK_POLICY, has_trading_committed_result
from sourdough.writer_lease import WriterRefused, hold_company_writer

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "sourdough"


def _package_import_graph(pkg_dir: Path) -> dict[str, set[str]]:
    modules = {p.stem for p in pkg_dir.glob("*.py") if p.stem != "__init__"}
    graph: dict[str, set[str]] = {m: set() for m in modules}
    for py_file in pkg_dir.glob("*.py"):
        mod = py_file.stem
        if mod == "__init__":
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                target = node.module.split(".")[0]
                if target in modules and target != mod:
                    graph[mod].add(target)
    return graph


def _find_cycle(graph: dict[str, set[str]]) -> list[str] | None:
    color: dict[str, int] = {node: 0 for node in graph}
    stack: list[str] = []

    def dfs(node: str) -> list[str] | None:
        color[node] = 1
        stack.append(node)
        for nxt in sorted(graph[node]):
            if color[nxt] == 1:
                idx = stack.index(nxt)
                return stack[idx:] + [nxt]
            if color[nxt] == 0:
                cycle = dfs(nxt)
                if cycle is not None:
                    return cycle
        stack.pop()
        color[node] = 2
        return None

    for node in sorted(graph):
        if color[node] == 0:
            cycle = dfs(node)
            if cycle is not None:
                return cycle
    return None


class TestP0KernelModularization(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        co_dir = self.root / "co"
        co_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.store = co_dir / "company.sqlite"
        self.profile = AgentProfileBinding(
            provider=PROVIDER_OPENAI,
            model="gpt-mock",
            revision="rev-1",
            credential_handle_ref="envref:OPENAI_API_KEY",
        )
        self.founding = found(
            self.store,
            FoundingTerms(
                "KernelMod Co",
                "charter-v1",
                "manage-company",
                self.profile.profile_id(),
            ),
        )
        self.company_id = self.founding.company_id
        self.founder_id = self.founding.founder_id
        self.grant_id = self.founding.grant_id

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_split_subpackages_are_strictly_acyclic_and_clean_of_trading_imports(
        self,
    ) -> None:
        for rel in ("runtime/kernel", "learning", "trials", "ipc/projections"):
            pkg_dir = _SRC_ROOT / rel
            graph = _package_import_graph(pkg_dir)
            self.assertGreater(len(graph), 2, f"Expected split modules in {rel}")
            self.assertIsNone(
                _find_cycle(graph),
                f"Circular import detected inside {rel}: {graph}",
            )
            for py_file in pkg_dir.glob("*.py"):
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            self.assertNotIn(
                                "trading",
                                alias.name.split("."),
                                f"Core module {py_file} must not import trading",
                            )
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        self.assertNotIn(
                            "trading",
                            node.module.split("."),
                            f"Core module {py_file} must not import trading",
                        )

    def test_storage_domain_ownership_and_facades_remain_intact(self) -> None:
        self.assertEqual(audit_domain_write_ownership(_SRC_ROOT), [])
        self.assertIs(DutyClaim, LegacyDutyClaim)
        self.assertIsNotNone(legacy_start_run)
        self.assertIs(wk_facade.create_work_item, create_work_item)
        self.assertIs(wk_facade.evaluate_conditions, evaluate_conditions)
        self.assertIs(wk_facade.record_work_decision, record_work_decision)
        self.assertIs(
            projections_pkg.redact_audit_entity_id_for_board,
            redaction_mod.redact_audit_entity_id_for_board,
        )
        self.assertEqual(TRADING_WORK_POLICY.domain_name, "trading")
        self.assertTrue(callable(has_trading_committed_result))

    def test_execution_claim_protocol_accepts_work_run_and_legacy_duty_claims(
        self,
    ) -> None:
        now = time_ns()
        with hold_company_writer(self.store, self.company_id) as w:
            with write_uow(w) as db:
                item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.assessment_review",
                    source_kind="unit",
                    source_id="src-claim",
                    now_ns=now,
                )
                run = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    lease_owner="unit-worker",
                    lease_ttl_ns=3_600_000_000_000,
                    now_ns=now,
                )
                work_claim = WorkRunClaim(
                    company_id=self.company_id,
                    founder_id=self.founder_id,
                    duty_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    purpose="manage-company",
                )
                self.assertIsInstance(work_claim, ExecutionClaim)
                auth = resolve_run(db, work_claim, now_ns=now)
                self.assertTrue(auth.is_current)
                self.assertEqual(auth.current_scope, "manage-company")

        legacy_claim = DutyClaim(
            company_id=self.company_id,
            founder_id=self.founder_id,
            grant_id=self.grant_id,
            duty_id="d-1",
            run_id="r-1",
            execution_profile=self.profile.profile_id(),
        )
        self.assertIsInstance(legacy_claim, ExecutionClaim)

    def test_board_control_write_uow_fences_stale_lease_and_enforces_idempotency(
        self,
    ) -> None:
        with hold_company_writer(self.store, self.company_id) as w:
            bind_board_principal(
                w, principal_id="board-owner", role_name="board_control"
            )
            first = post_ceo_message(
                w,
                command_id="cmd-ceo-1",
                target_agent_id=self.founder_id,
                charter_revision="charter-v1",
                body="Confirm portfolio posture.",
                principal_hint="board-owner",
            )
            replay = post_ceo_message(
                w,
                command_id="cmd-ceo-1",
                target_agent_id=self.founder_id,
                charter_revision="charter-v1",
                body="Confirm portfolio posture.",
                principal_hint="board-owner",
            )
            self.assertEqual(first, replay)
            self.assertEqual(replay.disposition, "applied")

            with self.assertRaises(BoardControlConflict):
                post_ceo_message(
                    w,
                    command_id="cmd-ceo-1",
                    target_agent_id=self.founder_id,
                    charter_revision="charter-v1",
                    body="Different body for same command_id.",
                    principal_hint="board-owner",
                )

            stale_writer = w

        # After writer lease is released, write_uow(writer) must refuse writes.
        with self.assertRaises((WriterRefused, StorageRefused)):
            apply_company_restriction(
                stale_writer,
                command_id="cmd-stale-lease",
                restriction_id="rest-stale",
                charter_revision="charter-v1",
                expected_restriction_revision=0,
                cause_category="board_pause",
                scope_ref="company:all",
                reason="Should fail with released lease",
                principal_hint="board-owner",
            )

    def test_unified_ledger_computes_identical_fill_and_position_math_for_sandbox_and_paper(
        self,
    ) -> None:
        now = time_ns()
        with hold_company_writer(self.store, self.company_id) as w:
            open_agent_sandbox(
                w,
                agent_id=self.founder_id,
                account_id="sbx-acct-1",
                starting_minor_units=10_000_000,
                exposure_limit_minor_units=5_000_000,
                fee_bps=10,
                slippage_bps=5,
            )
            cp = open_company_paper_book(
                w,
                account_id="cp-acct-1",
                book_name="Primary Paper",
                starting_minor_units=10_000_000,
                exposure_limit_minor_units=5_000_000,
                fee_bps=10,
                slippage_bps=5,
            )
            with write_uow(w) as db:
                item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.quant_paper_decision",
                    source_kind="unit",
                    source_id="src-ledger",
                    now_ns=now,
                )
                run = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    lease_owner="unit-worker",
                    lease_ttl_ns=3_600_000_000_000,
                    now_ns=now,
                )

            sb_buy = submit_agent_sandbox_order(
                w,
                agent_id=self.founder_id,
                action_id="sb-buy-1",
                instrument_id="BTC-USD",
                side="buy",
                quantity=2,
                limit_minor_units=1_010_000,
                market_price_minor_units=1_000_000,
                run_id=run.run_id,
            )
            cp_buy = submit_company_paper_book_order(
                w,
                binding_id=cp.binding_id,
                actor_id=self.founder_id,
                action_id="cp-buy-1",
                instrument_id="BTC-USD",
                side="buy",
                quantity=2,
                limit_minor_units=1_010_000,
                market_price_minor_units=1_000_000,
                run_id=run.run_id,
                now_ns=now + 10,
            )
            self.assertEqual(
                sb_buy.fill_price_minor_units, cp_buy["fill_price_minor_units"]
            )
            self.assertEqual(sb_buy.fee_minor_units, cp_buy["fee_minor_units"])

            # Partial sell at a higher price verifies identical realized PnL & remaining avg cost.
            sb_sell = submit_agent_sandbox_order(
                w,
                agent_id=self.founder_id,
                action_id="sb-sell-1",
                instrument_id="BTC-USD",
                side="sell",
                quantity=1,
                limit_minor_units=1_100_000,
                market_price_minor_units=1_200_000,
                run_id=run.run_id,
            )
            cp_sell = submit_company_paper_book_order(
                w,
                binding_id=cp.binding_id,
                actor_id=self.founder_id,
                action_id="cp-sell-1",
                instrument_id="BTC-USD",
                side="sell",
                quantity=1,
                limit_minor_units=1_100_000,
                market_price_minor_units=1_200_000,
                run_id=run.run_id,
                now_ns=now + 20,
            )
            self.assertEqual(
                sb_sell.fill_price_minor_units, cp_sell["fill_price_minor_units"]
            )
            self.assertEqual(sb_sell.fee_minor_units, cp_sell["fee_minor_units"])

            sb_port = read_agent_sandbox_portfolio(
                self.store, self.company_id, self.founder_id
            )
            with scoped_reader(self.store) as conn:
                cp_port = read_company_paper_book_portfolio_from_db(
                    conn, self.company_id, cp.binding_id
                )
            self.assertEqual(sb_port["cash_minor_units"], cp_port["cash_minor_units"])
            self.assertEqual(
                sb_port["realized_pnl_minor_units"],
                cp_port["realized_pnl_minor_units"],
            )
            self.assertEqual(
                sb_port["total_fees_minor_units"], cp_port["total_fees_minor_units"]
            )

    def test_kernel_timer_condition_evaluation_works_through_split_package(
        self,
    ) -> None:
        now = time_ns()
        with hold_company_writer(self.store, self.company_id) as w:
            with write_uow(w) as db:
                resp = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.assessment_review",
                    remit="Periodic timer check",
                    readable_scope="company",
                    now_ns=now,
                )
                set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    responsibility_id=resp.responsibility_id,
                    trigger_kind="timer",
                    due_at_ns=now + 100,
                    now_ns=now,
                )
                admitted = evaluate_conditions(
                    db,
                    company_id=self.company_id,
                    now_ns=now + 200,
                )
        self.assertEqual(len(admitted), 1)
        self.assertEqual(admitted[0].owner_agent_id, self.founder_id)


if __name__ == "__main__":
    unittest.main()
