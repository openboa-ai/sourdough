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
from sourdough.first_trade import TradeRefused
from sourdough.runtime.domains import DomainPluginRegistry, load_domain_plugins
from sourdough.runtime.handlers import (
    DUTY_BOARD_APPLY_DECISION,
    DUTY_CEO_RESPOND_MESSAGE,
    build_production_duty_registry,
)
from sourdough.storage.db import open_writer, write_tx
from sourdough.storage.schema import enable_domain_v1
from sourdough.trading.duty import (
    PAPER_EXECUTION,
    has_trading_committed_result,
)
from sourdough.work_projection import read_local_board_timeline_with_seq
from sourdough.writer_lease import hold_company_writer

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "sourdough"

_FORBIDDEN_TRADING_ROOTS = frozenset(
    {
        "trading",
        "execution_tiers",
        "paper_account",
        "paper_books",
        "paper_decision",
        "paper_execution",
        "paper_fees",
        "paper_order",
        "paper_reconcile",
        "paper_research",
        "paper_restore",
        "paper_resume",
        "paper_risk",
        "paper_statement_store",
        "paper_valuation",
        "paper_venue",
        "paper_window",
        "first_trade",
        "hired_paper_run",
        "strategy_comparison",
        "trading_engine",
        "nautilus_paper",
        "nautilus_runtime",
        "market_worker",
        "binance_venue",
        "venue_qualification",
        "company_run",
        "window_run",
        "credentials",
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
    "evidence_review.py",
    "first_duty.py",
    "general_ledger.py",
    "harness.py",
    "hiring_run.py",
    "onboarding.py",
    "retirement.py",
    "review_qualification_store.py",
    "review_tool_activation.py",
    "staffing.py",
    "staffing_tools.py",
    "supervisor.py",
    "work.py",
    "work_handoff.py",
    "work_peer_response.py",
    "work_projection.py",
    "writer_lease.py",
)


def _core_module_paths() -> list[Path]:
    paths = [_SRC_ROOT / name for name in _CORE_SINGLE_MODULES]
    for package_dir in ("agent_runtime", "runtime", "storage"):
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
                    conn, "paper", company_id=receipt.company_id, actor_id="test"
                )
            claim = claim_first_duty(
                store,
                receipt.company_id,
                "profile-v1",
                scope="paper",
                purpose=PAPER_EXECUTION,
            )
            with closing(open_writer(store)) as conn, write_tx(conn):
                conn.execute(
                    "INSERT INTO paper_research_finding VALUES (?, ?, ?, ?, ?)",
                    (claim.run_id, "find-1", "rev-1", 2, "hold position"),
                )
                conn.execute(
                    "INSERT INTO paper_research_decision VALUES (?, ?, ?, ?, ?, ?)",
                    (claim.duty_id, claim.run_id, "find-1", "dec-1", 3, "hold"),
                )
            with hold_company_writer(store, receipt.company_id) as writer:
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
        self.assertTrue(issubclass(TradeRefused, EmployeeProcessRefused))

    def test_load_domain_plugins_discovers_trading_plugin(self):
        plugins = load_domain_plugins()
        names = [plugin.domain_name for plugin in plugins]
        self.assertIn("trading", names)
        registry = DomainPluginRegistry(plugins)
        self.assertIsNotNone(registry.get("trading"))
        self.assertIsNone(registry.get("core"))


if __name__ == "__main__":
    unittest.main()
