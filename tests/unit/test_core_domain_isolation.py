"""Tests verifying Core Company OS isolation from trading/domain-specific code."""

import ast
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.abandoned_work import requeue_uncommitted_work
from sourdough.agent_runtime import (
    InMemorySecretResolver,
    build_default_adapter_registry,
)
from sourdough.company import FoundingTerms, found
from sourdough.company_checkpoint import checkpoint_company
from sourdough.company_restore import CheckpointRefused, restore_core_checkpoint
from sourdough.employee_process import EmployeeProcessRefused
from sourdough.first_duty import claim_first_duty
from sourdough.runtime.domains import DomainPluginRegistry, load_domain_plugins
from sourdough.runtime.handlers import (
    DUTY_BOARD_APPLY_DECISION,
    DUTY_CEO_RESPOND_MESSAGE,
    build_production_duty_registry,
)
from sourdough.storage.db import open_writer, write_tx
from sourdough.storage.schema import enable_domain_v1
from sourdough.trading.accounts import open_company_paper_book
from sourdough.trading.duty import (
    PAPER_EXECUTION,
    has_trading_committed_result,
)
from sourdough.trading.ledger import submit_company_paper_book_order
from sourdough.work_projection import read_local_board_timeline_with_seq
from sourdough.writer_lease import hold_company_writer

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "sourdough"

_FORBIDDEN_TRADING_ROOTS = frozenset(
    {
        "trading",
        "binance_emulator",
        "binance_venue",
        "company_run",
        "credentials",
        "execution_tiers",
        "external_venue",
        "first_trade",
        "fund_returns",
        "hired_paper_run",
        "market_runtime",
        "market_worker",
        "nautilus_paper",
        "nautilus_paper_worker",
        "nautilus_replay_worker",
        "nautilus_runtime",
        "paper_account",
        "paper_books",
        "paper_decision",
        "paper_execution",
        "paper_fee_bridge",
        "paper_funding",
        "paper_order",
        "paper_reconcile",
        "paper_research",
        "paper_restore",
        "paper_resume",
        "paper_review",
        "paper_risk",
        "paper_statement_store",
        "paper_valuation",
        "paper_venue",
        "paper_window",
        "replay_tape",
        "sender_lease",
        "spot_cash",
        "strategy_comparison",
        "trading_engine",
        "venue_coordinator",
        "venue_qualification",
        "window_costs",
        "window_movement",
        "window_run",
        "window_sampler",
        "window_statement",
    }
)

_CORE_SINGLE_MODULES = (
    "abandoned_work.py",
    "accounting_close.py",
    "agent_desk.py",
    "assessment_run.py",
    "board_control.py",
    "cli.py",
    "collaboration.py",
    "company.py",
    "company_assessment.py",
    "company_assets.py",
    "company_checkpoint.py",
    "company_restore.py",
    "conversation_delivery.py",
    "conversations.py",
    "duty_run.py",
    "employee_process.py",
    "episode_index.py",
    "evidence_review.py",
    "first_duty.py",
    "general_ledger.py",
    "harness.py",
    "hiring_run.py",
    "memory.py",
    "onboarding.py",
    "retirement.py",
    "review_qualification_store.py",
    "review_tool_activation.py",
    "staffing.py",
    "staffing_tools.py",
    "supervisor.py",
    "trial_execution.py",
    "trial_registry.py",
    "work.py",
    "work_handoff.py",
    "work_peer_response.py",
    "work_projection.py",
    "writer_lease.py",
)


def _core_module_paths() -> list[Path]:
    paths = [_SRC_ROOT / name for name in _CORE_SINGLE_MODULES]
    for package_dir in ("agent_runtime", "ipc", "runtime", "storage"):
        paths.extend(sorted((_SRC_ROOT / package_dir).rglob("*.py")))
    return paths


def _resolve_import_target(file_path: Path, module: str | None, level: int) -> str:
    rel_parts = file_path.relative_to(_SRC_ROOT).with_suffix("").parts
    pkg_parts = rel_parts[:-1]
    if level == 0:
        if module is None or module == "sourdough":
            return ""
        if module.startswith("sourdough."):
            return module[len("sourdough.") :]
        return module
    up = level - 1
    base = pkg_parts[: len(pkg_parts) - up] if up <= len(pkg_parts) else ()
    if module:
        return ".".join((*base, module))
    return ".".join(base)


class CoreDomainIsolationTests(unittest.TestCase):
    def test_core_modules_have_no_trading_imports_and_retired_files_removed(self):
        self.assertFalse((_SRC_ROOT / "company_run.py").exists())
        self.assertFalse((_SRC_ROOT / "runtime" / "venue_coordinator.py").exists())
        self.assertFalse((_SRC_ROOT / "trading" / "company_run.py").exists())
        self.assertFalse((_SRC_ROOT / "storage" / "schema" / "v1" / "paper").exists())

        root_trading_files = [
            path.name
            for path in _SRC_ROOT.glob("*.py")
            if path.stem in _FORBIDDEN_TRADING_ROOTS
        ]
        self.assertEqual(root_trading_files, [])

        violations: list[str] = []
        for path in _core_module_paths():
            self.assertTrue(path.is_file(), f"Missing core module {path}")
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        target = _resolve_import_target(path, alias.name, 0)
                        root = target.split(".", 1)[0]
                        if root in _FORBIDDEN_TRADING_ROOTS:
                            violations.append(
                                f"{path.relative_to(_SRC_ROOT)}:{node.lineno}: import {alias.name}"
                            )
                elif isinstance(node, ast.ImportFrom):
                    target = _resolve_import_target(path, node.module, node.level)
                    root = target.split(".", 1)[0]
                    if root in _FORBIDDEN_TRADING_ROOTS:
                        violations.append(
                            f"{path.relative_to(_SRC_ROOT)}:{node.lineno}: from {target}"
                        )
                    elif target == "" and any(
                        alias.name in _FORBIDDEN_TRADING_ROOTS for alias in node.names
                    ):
                        violations.append(
                            f"{path.relative_to(_SRC_ROOT)}:{node.lineno}: from sourdough import ..."
                        )
        self.assertEqual(violations, [])

    def test_core_duty_registry_without_plugins_exposes_only_core_duties(self):
        registry = build_production_duty_registry(
            adapter_registry=build_default_adapter_registry(
                secret_resolver=InMemorySecretResolver({})
            ),
            profile_bindings={},
            domain_plugins=(),
        )
        self.assertEqual(
            set(registry.registered_kinds()),
            {DUTY_CEO_RESPOND_MESSAGE, DUTY_BOARD_APPLY_DECISION},
        )

    def test_core_work_projection_without_plugins_emits_empty_domains(self):
        with TemporaryDirectory(prefix="sd-core-iso-") as tmp:
            store = Path(tmp) / "company.sqlite"
            receipt = found(
                store, FoundingTerms("core-co", "charter-v1", "manage", "profile-v1")
            )
            projection, _seq = read_local_board_timeline_with_seq(
                store, receipt.company_id, domain_plugins=()
            )
            self.assertEqual(projection["company_id"], receipt.company_id)
            self.assertEqual(projection["domains"], {})
            self.assertNotIn("paper", projection)
            self.assertNotIn("risk_control", projection)

    def test_core_restore_fails_closed_for_active_non_core_domain_without_guard(self):
        with TemporaryDirectory(prefix="sd-core-restore-guard-") as tmp:
            root = Path(tmp)
            source = root / "source.sqlite"
            archive = root / "archive.sqlite"
            target = root / "restored.sqlite"
            receipt = found(
                source, FoundingTerms("paper-co", "charter-v1", "paper", "profile-v1")
            )
            with closing(open_writer(source)) as conn, write_tx(conn):
                enable_domain_v1(
                    conn, "paper", company_id=receipt.company_id, actor_id="test"
                )
            checkpoint_company(source, receipt.company_id, archive)
            with self.assertRaisesRegex(
                CheckpointRefused, "requires an independent restore reconciler"
            ):
                restore_core_checkpoint(
                    source,
                    receipt.company_id,
                    archive,
                    target,
                    domain_guards={},
                )

    def test_abandoned_work_ignores_trading_tables_unless_predicate_provided(self):
        with TemporaryDirectory(prefix="sd-core-abandoned-") as tmp:
            store = Path(tmp) / "company.sqlite"
            receipt = found(
                store,
                FoundingTerms("abandoned-co", "charter-v1", "paper", "profile-v1"),
            )
            with closing(open_writer(store)) as conn, write_tx(conn):
                enable_domain_v1(
                    conn, "trading", company_id=receipt.company_id, actor_id="test"
                )
            claim = claim_first_duty(
                store,
                receipt.company_id,
                "profile-v1",
                scope="paper",
                purpose=PAPER_EXECUTION,
            )
            with hold_company_writer(store, receipt.company_id) as writer:
                binding = open_company_paper_book(
                    writer,
                    binding_id="paper-main",
                    book_name="Main Paper",
                    account_id="acct-paper",
                    currency="USD",
                    starting_minor_units=10_000,
                    exposure_limit_minor_units=5_000,
                )
                submit_company_paper_book_order(
                    writer,
                    binding_id=binding.binding_id,
                    actor_id=claim.founder_id,
                    action_id="act-abandoned-1",
                    instrument_id="BTC-USDT",
                    side="buy",
                    quantity=1,
                    limit_minor_units=100,
                    market_price_minor_units=100,
                    run_id=claim.run_id,
                )
                # With trading predicate registered, committed domain result prevents requeue.
                self.assertEqual(
                    requeue_uncommitted_work(
                        writer,
                        committed_predicates=(has_trading_committed_result,),
                    ),
                    (),
                )
                # Without domain predicates, Core does not inspect trading tables and requeues cleanly.
                self.assertEqual(
                    requeue_uncommitted_work(writer, committed_predicates=()),
                    (claim.run_id,),
                )

    def test_employee_process_refused_is_core_without_first_trade_dependency(self):
        self.assertEqual(
            EmployeeProcessRefused.__module__, "sourdough.employee_process"
        )
        self.assertFalse((_SRC_ROOT / "first_trade.py").exists())

    def test_load_domain_plugins_discovers_trading_plugin(self):
        plugins = load_domain_plugins()
        names = [plugin.domain_name for plugin in plugins]
        self.assertIn("trading", names)
        registry = DomainPluginRegistry(plugins)
        self.assertIsNotNone(registry.get("trading"))
        self.assertIsNone(registry.get("core"))

    def test_standalone_employee_process_import_has_no_circular_import(self):
        import subprocess
        import sys

        for stmt in (
            "import sourdough.employee_process, sourdough.harness, sourdough.agent_runtime, sourdough.runtime",
            "import sourdough.agent_runtime, sourdough.harness, sourdough.employee_process",
        ):
            with self.subTest(stmt=stmt):
                proc = subprocess.run(
                    [sys.executable, "-I", "-c", stmt],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_core_only_pause_company_never_leaks_trading_domain_into_work_timeline(
        self,
    ):
        from sourdough.board_control import (
            BOARD_CONTROL_ROLE,
            apply_company_restriction,
            bind_board_principal,
        )
        from sourdough.trading import TradingDomainPlugin

        with TemporaryDirectory(prefix="sd-core-pause-tl-") as tmp:
            root = Path(tmp)
            store = root / "company.sqlite"
            leases = root / "leases"
            receipt = found(
                store, FoundingTerms("core-co", "charter-v1", "manage", "profile-v1")
            )
            with hold_company_writer(
                store, receipt.company_id, lease_directory=leases
            ) as writer:
                bind_board_principal(
                    writer, principal_id="board-1", role_name=BOARD_CONTROL_ROLE
                )
                apply_company_restriction(
                    writer,
                    command_id="core-pause-1",
                    restriction_id="rest-core-1",
                    charter_revision="charter-v1",
                    expected_restriction_revision=0,
                    cause_category="board_pause",
                    scope_ref="company:all",
                    reason="Core operational pause",
                    principal_hint="board-1",
                )
            projection, _seq = read_local_board_timeline_with_seq(
                store, receipt.company_id, domain_plugins=(TradingDomainPlugin(),)
            )
            self.assertEqual(projection["domains"], {})


if __name__ == "__main__":
    unittest.main()
