"""Unit tests for canonical v1 store migration (pre-cleanup core+paper -> core+trading)."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.storage import migrate_v1_store_to_canonical
from sourdough.storage.db import StorageRefused, open_reader, read_tx
from sourdough.storage.migration import RETIRED_TRADING_TABLES
from sourdough.storage.schema import (
    installed_domains,
    load_domain_pack,
    verify_v1_schema,
)


def _seed_precanonical_store(store: Path, company_id: str = "co-mig-1") -> None:
    """Create a realistic pre-cleanup v1 SQLite store with Gen-1 + Gen-2 tables & NULL cols."""
    core_sql = "\n".join(sql for _name, sql in load_domain_pack("core").files)
    core_sql = core_sql.replace(
        "domain TEXT PRIMARY KEY CHECK (domain IN ('core', 'trading'))",
        "domain TEXT PRIMARY KEY CHECK (domain IN ('core', 'paper', 'trading'))",
    )
    # Simulate pre-M2 NULLable duty.state and decision_question.application_status
    core_sql = core_sql.replace(
        "CREATE TABLE duty (\n    id TEXT PRIMARY KEY NOT NULL,\n    agent_id TEXT NOT NULL REFERENCES agent(id),\n    state TEXT NOT NULL CHECK (",
        "CREATE TABLE duty (\n    id TEXT PRIMARY KEY NOT NULL,\n    agent_id TEXT NOT NULL REFERENCES agent(id),\n    state TEXT CHECK (state IS NULL OR ",
        1,
    ).replace(
        "    application_status TEXT NOT NULL DEFAULT 'pending'",
        "    application_status TEXT",
        1,
    )

    conn = sqlite3.connect(store)
    try:
        conn.execute("PRAGMA user_version = 1")
        conn.executescript("PRAGMA foreign_keys = OFF;\n" + core_sql)
        conn.executescript(
            """
            -- Gen 1 legacy paper tables
            CREATE TABLE paper_binding (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                revision TEXT NOT NULL,
                company_id TEXT NOT NULL,
                charter_revision TEXT NOT NULL,
                exposure_limit_minor_units INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                connector_revision TEXT NOT NULL,
                instrument_rules_digest TEXT NOT NULL,
                currency TEXT NOT NULL,
                starting_minor_units INTEGER NOT NULL,
                environment TEXT NOT NULL,
                destination_id TEXT NOT NULL
            ) STRICT;
            CREATE TABLE paper_order (
                order_id TEXT PRIMARY KEY,
                run_id TEXT,
                mandate_revision TEXT NOT NULL,
                account_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                instrument_rules_digest TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                limit_minor_units INTEGER NOT NULL,
                reserved_minor_units INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE paper_fill (
                fill_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL REFERENCES paper_order(order_id),
                quantity INTEGER NOT NULL,
                price_minor_units INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE paper_fill_fee (
                fill_id TEXT PRIMARY KEY REFERENCES paper_fill(fill_id),
                fee_minor_units INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE paper_cancellation (
                order_id TEXT PRIMARY KEY REFERENCES paper_order(order_id),
                event_id TEXT NOT NULL,
                filled_quantity INTEGER NOT NULL,
                released INTEGER NOT NULL,
                position_quantity INTEGER,
                available_at INTEGER,
                outcome TEXT NOT NULL
            ) STRICT;
            CREATE TABLE paper_review_quote (
                run_id TEXT PRIMARY KEY NOT NULL REFERENCES duty_run(run_id),
                account_id TEXT,
                connector_revision TEXT,
                quote_minor_units INTEGER,
                observed_at INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE paper_decision (
                decision_id TEXT PRIMARY KEY,
                duty_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                result_revision TEXT NOT NULL,
                action TEXT NOT NULL,
                rationale TEXT NOT NULL,
                decided_at INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE paper_valuation (
                capture_id TEXT PRIMARY KEY NOT NULL,
                binding_revision TEXT NOT NULL,
                policy TEXT NOT NULL,
                evidence TEXT NOT NULL,
                trading_nav_atoms INTEGER NOT NULL CHECK (trading_nav_atoms >= 0),
                revision TEXT NOT NULL UNIQUE
            ) STRICT;
            CREATE TABLE paper_risk_state (
                company_id TEXT PRIMARY KEY,
                suspended INTEGER NOT NULL,
                effective_revision INTEGER NOT NULL,
                last_command_id TEXT,
                updated_at_ns INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE paper_operating_window (
                window_id TEXT PRIMARY KEY NOT NULL,
                contract TEXT NOT NULL,
                committed_ns INTEGER CHECK (committed_ns IS NULL OR committed_ns > 0),
                revision TEXT UNIQUE,
                CHECK ((committed_ns IS NULL) = (revision IS NULL))
            ) STRICT;
            CREATE TABLE paper_statement (
                request_id TEXT PRIMARY KEY NOT NULL,
                window_id TEXT NOT NULL REFERENCES paper_operating_window(window_id),
                supersedes TEXT UNIQUE REFERENCES paper_statement(request_id),
                payload TEXT NOT NULL,
                revision TEXT NOT NULL UNIQUE
            ) STRICT;
            CREATE TABLE paper_funding_settlement (
                settlement_id TEXT PRIMARY KEY NOT NULL,
                company_id TEXT NOT NULL REFERENCES company(id),
                destination_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                instrument_id TEXT NOT NULL,
                interval_end_ns INTEGER NOT NULL,
                funding_rate_e9 INTEGER,
                mark_price_atoms INTEGER,
                signed_position_lots INTEGER NOT NULL,
                funding_cashflow_atoms INTEGER,
                status TEXT NOT NULL,
                unavailable_reason TEXT,
                evidence_sha256 TEXT NOT NULL,
                journal_id TEXT,
                recorded_at_ns INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE paper_fill_accounting_source (
                fill_id TEXT PRIMARY KEY NOT NULL REFERENCES paper_fill_fee(fill_id),
                destination_id TEXT NOT NULL,
                currency TEXT NOT NULL,
                mapping_revision TEXT NOT NULL,
                recorded_at_ns INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE paper_fee_delivery (
                fill_id TEXT PRIMARY KEY NOT NULL REFERENCES paper_fill_accounting_source(fill_id),
                delivery_digest TEXT NOT NULL,
                acknowledged INTEGER NOT NULL,
                journal_id TEXT
            ) STRICT;
            CREATE TABLE paper_pit_catalog_manifest (
                manifest_id TEXT PRIMARY KEY NOT NULL,
                company_id TEXT NOT NULL REFERENCES company(id),
                dataset_name TEXT NOT NULL,
                valid_from_ns INTEGER NOT NULL,
                valid_through_ns INTEGER NOT NULL,
                available_at_ns INTEGER NOT NULL,
                files_json TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL UNIQUE,
                recorded_at_ns INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE paper_strategy_release (
                release_id TEXT PRIMARY KEY NOT NULL,
                company_id TEXT NOT NULL REFERENCES company(id),
                owner_agent_id TEXT NOT NULL REFERENCES agent(id),
                parent_release_id TEXT,
                strategy_source_sha256 TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                nautilus_lock_sha256 TEXT NOT NULL,
                instrument_rules_sha256 TEXT NOT NULL,
                risk_config_sha256 TEXT NOT NULL,
                fill_policy_sha256 TEXT NOT NULL,
                pit_manifest_id TEXT NOT NULL REFERENCES paper_pit_catalog_manifest(manifest_id),
                pit_manifest_sha256 TEXT NOT NULL,
                research_trial_count_n INTEGER NOT NULL,
                bundle_sha256 TEXT NOT NULL UNIQUE,
                recorded_at_ns INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE paper_challenger_trial (
                comparison_id TEXT PRIMARY KEY NOT NULL,
                company_id TEXT NOT NULL REFERENCES company(id),
                incumbent_release_id TEXT NOT NULL REFERENCES paper_strategy_release(release_id),
                challenger_release_id TEXT NOT NULL REFERENCES paper_strategy_release(release_id),
                baseline_statement_revision TEXT NOT NULL,
                analysis_plan_json TEXT NOT NULL,
                analysis_plan_sha256 TEXT NOT NULL,
                incumbent_arm_json TEXT NOT NULL,
                challenger_arm_json TEXT NOT NULL,
                status TEXT NOT NULL,
                verdict TEXT,
                verdict_json TEXT,
                invalidation_reason TEXT,
                registered_at_ns INTEGER NOT NULL,
                evaluated_at_ns INTEGER
            ) STRICT;
            CREATE TABLE paper_strategy_deployment (
                deployment_id TEXT PRIMARY KEY NOT NULL,
                company_id TEXT NOT NULL REFERENCES company(id),
                release_id TEXT NOT NULL REFERENCES paper_strategy_release(release_id),
                program_identity TEXT NOT NULL,
                binding_revision TEXT NOT NULL,
                comparison_id TEXT,
                status TEXT NOT NULL,
                command_id TEXT NOT NULL,
                activated_at_ns INTEGER NOT NULL,
                retired_at_ns INTEGER
            ) STRICT;

            -- Gen 2 split tables + partial execution_environment_binding
            CREATE TABLE execution_environment_binding (
                binding_id TEXT PRIMARY KEY NOT NULL,
                company_id TEXT NOT NULL REFERENCES company(id),
                tier TEXT NOT NULL,
                environment TEXT NOT NULL,
                owner_agent_id TEXT,
                account_id TEXT NOT NULL,
                destination_id TEXT NOT NULL,
                connector_revision TEXT NOT NULL,
                instrument_rules_digest TEXT NOT NULL,
                currency TEXT NOT NULL,
                starting_minor_units INTEGER NOT NULL,
                exposure_limit_minor_units INTEGER NOT NULL,
                fee_bps INTEGER NOT NULL,
                slippage_bps INTEGER NOT NULL,
                quote_source TEXT NOT NULL,
                credential_handle_ref TEXT,
                status TEXT NOT NULL,
                created_at_ns INTEGER NOT NULL,
                updated_at_ns INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE company_paper_book (
                binding_id TEXT PRIMARY KEY NOT NULL,
                company_id TEXT NOT NULL,
                book_name TEXT NOT NULL,
                strategy_release_id TEXT,
                role TEXT NOT NULL,
                shadow_of_live_binding_id TEXT,
                nominated_question_id TEXT,
                created_at_ns INTEGER NOT NULL,
                updated_at_ns INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE trading_book_valuation (
                valuation_id TEXT PRIMARY KEY NOT NULL,
                binding_id TEXT NOT NULL,
                environment TEXT NOT NULL CHECK (environment IN ('paper', 'live')),
                company_id TEXT NOT NULL REFERENCES company(id),
                label TEXT NOT NULL,
                nav_minor_units INTEGER NOT NULL,
                cash_minor_units INTEGER NOT NULL,
                gross_exposure_minor_units INTEGER NOT NULL,
                realized_pnl_minor_units INTEGER NOT NULL,
                unrealized_pnl_minor_units INTEGER NOT NULL,
                total_fees_minor_units INTEGER NOT NULL,
                source TEXT NOT NULL,
                recorded_at_ns INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE agent_sandbox_order (
                order_id TEXT PRIMARY KEY NOT NULL,
                binding_id TEXT NOT NULL,
                company_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                instrument_id TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                limit_minor_units INTEGER NOT NULL,
                market_price_minor_units INTEGER NOT NULL,
                quote_observed_at_ns INTEGER NOT NULL,
                quote_source TEXT NOT NULL,
                fee_bps INTEGER NOT NULL,
                slippage_bps INTEGER NOT NULL,
                status TEXT NOT NULL,
                filled_quantity INTEGER NOT NULL,
                fill_price_minor_units INTEGER,
                fee_minor_units INTEGER NOT NULL,
                created_at_ns INTEGER NOT NULL,
                updated_at_ns INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE agent_sandbox_fill (
                fill_id TEXT PRIMARY KEY NOT NULL,
                order_id TEXT NOT NULL,
                binding_id TEXT NOT NULL,
                company_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                instrument_id TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price_minor_units INTEGER NOT NULL,
                fee_minor_units INTEGER NOT NULL,
                realized_pnl_minor_units INTEGER NOT NULL,
                filled_at_ns INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE agent_sandbox_position (
                binding_id TEXT NOT NULL,
                company_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                instrument_id TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                avg_entry_minor_units INTEGER NOT NULL,
                mark_price_minor_units INTEGER NOT NULL,
                realized_pnl_minor_units INTEGER NOT NULL,
                total_fees_minor_units INTEGER NOT NULL,
                updated_at_ns INTEGER NOT NULL,
                PRIMARY KEY (binding_id, instrument_id)
            ) STRICT;
            CREATE TABLE company_paper_order (
                order_id TEXT PRIMARY KEY NOT NULL,
                binding_id TEXT NOT NULL,
                company_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                instrument_id TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                limit_minor_units INTEGER NOT NULL,
                market_price_minor_units INTEGER NOT NULL,
                quote_observed_at_ns INTEGER NOT NULL,
                quote_source TEXT NOT NULL,
                fee_bps INTEGER NOT NULL,
                slippage_bps INTEGER NOT NULL,
                status TEXT NOT NULL,
                filled_quantity INTEGER NOT NULL,
                fill_price_minor_units INTEGER,
                fee_minor_units INTEGER NOT NULL,
                created_at_ns INTEGER NOT NULL,
                updated_at_ns INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE company_paper_fill (
                fill_id TEXT PRIMARY KEY NOT NULL,
                order_id TEXT NOT NULL,
                binding_id TEXT NOT NULL,
                company_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                instrument_id TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price_minor_units INTEGER NOT NULL,
                fee_minor_units INTEGER NOT NULL,
                realized_pnl_minor_units INTEGER NOT NULL,
                filled_at_ns INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE company_paper_position (
                binding_id TEXT NOT NULL,
                company_id TEXT NOT NULL,
                instrument_id TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                avg_entry_minor_units INTEGER NOT NULL,
                mark_price_minor_units INTEGER NOT NULL,
                realized_pnl_minor_units INTEGER NOT NULL,
                total_fees_minor_units INTEGER NOT NULL,
                updated_at_ns INTEGER NOT NULL,
                PRIMARY KEY (binding_id, instrument_id)
            ) STRICT;
            CREATE TABLE venue_qualification_receipt (
                receipt_id TEXT PRIMARY KEY NOT NULL,
                company_id TEXT NOT NULL REFERENCES company(id),
                adapter_revision TEXT NOT NULL,
                environment TEXT NOT NULL,
                destination_id TEXT NOT NULL,
                instrument_rules_digest TEXT NOT NULL,
                core_digest TEXT NOT NULL,
                verdict TEXT NOT NULL,
                cases_passed INTEGER NOT NULL,
                cases_failed INTEGER NOT NULL,
                cases_unrun INTEGER NOT NULL,
                evidence_sha256 TEXT NOT NULL,
                recorded_at_ns INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE environment_promotion_record (
                promotion_id TEXT PRIMARY KEY NOT NULL,
                company_id TEXT NOT NULL REFERENCES company(id),
                command_id TEXT NOT NULL,
                action TEXT NOT NULL,
                from_binding_id TEXT,
                to_binding_id TEXT NOT NULL,
                qualification_receipt_id TEXT,
                payload_hash TEXT NOT NULL,
                reason TEXT NOT NULL,
                recorded_at_ns INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE paper_live_migration_record (
                migration_id TEXT PRIMARY KEY NOT NULL,
                company_id TEXT NOT NULL REFERENCES company(id),
                question_id TEXT REFERENCES decision_question(question_id),
                source_paper_binding_id TEXT NOT NULL REFERENCES execution_environment_binding(binding_id),
                target_live_binding_id TEXT NOT NULL REFERENCES execution_environment_binding(binding_id),
                replaced_live_binding_id TEXT REFERENCES execution_environment_binding(binding_id),
                qualification_receipt_id TEXT NOT NULL REFERENCES venue_qualification_receipt(receipt_id),
                migration_mode TEXT NOT NULL,
                source_paper_disposition TEXT NOT NULL,
                positions_copied INTEGER NOT NULL DEFAULT 0,
                pre_check_summary_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                recorded_at_ns INTEGER NOT NULL
            ) STRICT;
            """
        )

        conn.execute(
            "INSERT INTO installed_domains VALUES ('core', 1, 'old-core', 'old-core-sql', 100)"
        )
        conn.execute(
            "INSERT INTO installed_domains VALUES ('paper', 1, 'old-paper', 'old-paper-sql', 101)"
        )
        conn.execute(
            "INSERT INTO company (singleton, id, schema_revision, charter_revision) VALUES (1, ?, 1, 'charter-v1')",
            (company_id,),
        )
        conn.execute(
            "INSERT INTO agent (id, company_id, execution_profile) VALUES ('ceo-1', ?, 'profile-v1')",
            (company_id,),
        )
        conn.execute(
            "INSERT INTO agent (id, company_id, execution_profile) VALUES ('quant-1', ?, 'profile-v2')",
            (company_id,),
        )
        conn.execute(
            "INSERT INTO standing_grant (id, agent_id, scope) VALUES ('grant-1', 'ceo-1', 'paper')"
        )
        conn.execute(
            "INSERT INTO duty (id, agent_id, state) VALUES ('duty-1', 'ceo-1', 'due')"
        )
        conn.execute(
            "INSERT INTO founding_receipt (setup_identity, company_id, founder_id, grant_id, due_work_id) VALUES ('setup-1', ?, 'ceo-1', 'grant-1', 'duty-1')",
            (company_id,),
        )
        conn.execute(
            """INSERT INTO decision_question (
                   question_id, company_id, kind, title, summary, impact_json,
                   options_json, revision, payload_hash, state, respond_by_ns,
                   created_by_agent_id, created_at_ns, application_status, delivery_status
               ) VALUES (
                   'q-1', ?, 'approval', 'Approve', 'Summary', '{}',
                   '["approve","decline"]', 1, 'hash-1', 'open', NULL,
                   'ceo-1', 100, 'pending', 'delivered'
               )""",
            (company_id,),
        )
        conn.execute(
            """INSERT INTO command_receipt VALUES (
                   ?, 'cmd-risk-1', 'board-1', NULL, 'suspend_new_risk', 'paper-risk-command-v1',
                   'sha-cmd', '{}', 'applied', NULL, NULL, 1, 100, 101
               )""",
            (company_id,),
        )

        # Gen 1 paper_binding + paper_order (with run_id missing from work_runs) + fill + fee + valuation + risk
        conn.execute(
            """INSERT INTO paper_binding VALUES (
                   1, 'mandate-gen1', ?, 'charter-v1', 500000, 'acct-gen1',
                   'conn-1', 'BTC-USDT', 'USD', 1000000, 'paper', 'dest-sim'
               )""",
            (company_id,),
        )
        conn.execute(
            """INSERT INTO paper_order VALUES (
                   'ord-gen1', 'stale-run-999', 'mandate-gen1', 'acct-gen1',
                   'act-gen1', 'BTC-USDT', 2, 50000, 100000
               )"""
        )
        conn.execute(
            "INSERT INTO paper_fill VALUES ('fill-gen1', 'ord-gen1', 2, 49500)"
        )
        conn.execute("INSERT INTO paper_fill_fee VALUES ('fill-gen1', 40)")
        conn.execute(
            "INSERT INTO paper_fill_accounting_source VALUES ('fill-gen1', 'dest-sim', 'USD', 'map-1', 100)"
        )
        conn.execute(
            "INSERT INTO paper_fee_delivery VALUES ('fill-gen1', 'digest-gen1', 1, NULL)"
        )
        conn.execute(
            """INSERT INTO paper_review_quote VALUES (
                   'stale-run-999', 'acct-gen1', 'conn-1', 49500, 900
               )"""
        )
        conn.execute(
            """INSERT INTO paper_valuation VALUES (
                   'cap-gen1', 'mandate-gen1', 'mark_to_market', '{"label":"Gen1 Cut"}',
                   999960, 'val-rev-1'
               )"""
        )
        conn.execute(
            "INSERT INTO paper_risk_state VALUES (?, 1, 3, 'cmd-risk-1', 500)",
            (company_id,),
        )
        conn.execute(
            "INSERT INTO paper_operating_window VALUES ('win-1', '24h', 2000, 'win-rev-1')"
        )
        conn.execute(
            """INSERT INTO paper_statement VALUES (
                   'stmt-1', 'win-1', NULL, '{"nav":999960}', 'stmt-rev-1'
               )"""
        )
        conn.execute(
            """INSERT INTO paper_pit_catalog_manifest VALUES (
                   'pit-1', ?, 'btc-spot', 100, 200, 200, '[]', 'pit-sha', 300
               )""",
            (company_id,),
        )
        conn.execute(
            """INSERT INTO paper_strategy_release VALUES (
                   'rel-1', ?, 'quant-1', NULL, 'src-sha', '{}',
                   'lock-sha', 'rules-sha', 'risk-sha', 'fill-sha',
                   'pit-1', 'pit-sha', 1, 'bundle-sha-1', 301
               )""",
            (company_id,),
        )
        conn.execute(
            """INSERT INTO paper_strategy_release VALUES (
                   'rel-2', ?, 'quant-1', 'rel-1', 'src-sha-2', '{}',
                   'lock-sha', 'rules-sha', 'risk-sha', 'fill-sha',
                   'pit-1', 'pit-sha', 2, 'bundle-sha-2', 302
               )""",
            (company_id,),
        )
        conn.execute(
            """INSERT INTO paper_challenger_trial VALUES (
                   'cmp-1', ?, 'rel-1', 'rel-2', 'stmt-rev-1',
                   '{}', 'plan-sha', '{}', '{}',
                   'completed', 'PROMOTE', '{}', NULL, 302, 303
               )""",
            (company_id,),
        )
        conn.execute(
            """INSERT INTO paper_strategy_deployment VALUES (
                   'dep-1', ?, 'rel-1', 'strat-a', 'mandate-gen1', 'cmp-1', 'active', 'cmd-risk-1', 304, NULL
               )""",
            (company_id,),
        )

        # Gen 2 bindings + company_paper + agent_sandbox rows
        conn.execute(
            """INSERT INTO execution_environment_binding VALUES (
                   'bind-paper-2', ?, 'company_paper', 'paper', NULL, 'acct-paper-2',
                   'dest-sim', 'conn-1', 'BTC-USDT', 'USD', 2000000, 1000000,
                   4, 2, 'live_read_only', NULL, 'active', 400, 401
               )""",
            (company_id,),
        )
        conn.execute(
            """INSERT INTO company_paper_book VALUES (
                   'bind-paper-2', ?, 'Alpha Book', 'rel-1', 'active', NULL, NULL, 400, 402
               )""",
            (company_id,),
        )
        conn.execute(
            """INSERT INTO company_paper_order VALUES (
                   'ord-cp-2', 'bind-paper-2', ?, 'ceo-1', 'act-cp-2', 'BTC-USDT',
                   'buy', 1, 60000, 59900, 405, 'live_read_only', 4, 2,
                   'filled', 1, 59900, 24, 405, 406
               )""",
            (company_id,),
        )
        conn.execute(
            """INSERT INTO company_paper_fill VALUES (
                   'fill-cp-2', 'ord-cp-2', 'bind-paper-2', ?, 'ceo-1', 'BTC-USDT',
                   'buy', 1, 59900, 24, 0, 406
               )""",
            (company_id,),
        )
        conn.execute(
            """INSERT INTO company_paper_position VALUES (
                   'bind-paper-2', ?, 'BTC-USDT', 1, 59900, 60100, 0, 24, 407
               )""",
            (company_id,),
        )
        conn.execute(
            """INSERT INTO trading_book_valuation VALUES (
                   'val-cp-2', 'bind-paper-2', 'paper', ?, 'Cut #1', 2000176, 1940076,
                   60100, 0, 200, 24, 'book_valuation', 408
               )""",
            (company_id,),
        )
        conn.execute(
            """INSERT INTO execution_environment_binding VALUES (
                   'bind-sbx-1', ?, 'agent_sandbox', 'agent_sandbox', 'quant-1', 'sbx-acct-1',
                   'agent-sandbox-sim', 'sandbox-sim-v1', 'ETH-USDT', 'USD', 500000, 500000,
                   4, 2, 'live_read_only', NULL, 'active', 410, 410
               )""",
            (company_id,),
        )
        conn.execute(
            """INSERT INTO agent_sandbox_order VALUES (
                   'ord-sbx-1', 'bind-sbx-1', ?, 'quant-1', 'act-sbx-1', 'ETH-USDT',
                   'buy', 2, 3000, 2990, 411, 'live_read_only', 4, 2,
                   'filled', 2, 2990, 5, 411, 412
               )""",
            (company_id,),
        )
        conn.execute(
            """INSERT INTO agent_sandbox_fill VALUES (
                   'fill-sbx-1', 'ord-sbx-1', 'bind-sbx-1', ?, 'quant-1', 'ETH-USDT',
                   'buy', 2, 2990, 5, 0, 412
               )""",
            (company_id,),
        )
        conn.execute(
            """INSERT INTO agent_sandbox_position VALUES (
                   'bind-sbx-1', ?, 'quant-1', 'ETH-USDT', 2, 2990, 3010, 0, 5, 413
               )""",
            (company_id,),
        )
        conn.commit()
    finally:
        conn.close()


class CanonicalV1SchemaMigrationTests(unittest.TestCase):
    def test_migrate_precanonical_core_and_paper_store_losslessly_and_idempotently(
        self,
    ):
        with TemporaryDirectory() as tmp:
            store = Path(tmp) / "legacy.sqlite"
            _seed_precanonical_store(store, "co-mig-1")

            domains = migrate_v1_store_to_canonical(store)
            self.assertEqual(domains, frozenset({"core", "trading"}))

            with closing(open_reader(store)) as reader, read_tx(reader):
                self.assertEqual(
                    verify_v1_schema(reader), frozenset({"core", "trading"})
                )
                self.assertEqual(set(installed_domains(reader)), {"core", "trading"})
                self.assertEqual(
                    reader.execute("PRAGMA foreign_key_check").fetchall(), []
                )
                # M2 NULL hardening preserved
                self.assertEqual(
                    reader.execute(
                        "SELECT state FROM duty WHERE id = 'duty-1'"
                    ).fetchone(),
                    ("due",),
                )
                self.assertEqual(
                    reader.execute(
                        "SELECT application_status FROM decision_question WHERE question_id = 'q-1'"
                    ).fetchone(),
                    ("pending",),
                )

                # Retired/source tables dropped
                existing_tables = {
                    row[0]
                    for row in reader.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                for retired in RETIRED_TRADING_TABLES:
                    self.assertNotIn(retired, existing_tables)

                # Gen 1 + Gen 2 orders/fills/valuations/bindings consolidated
                orders = {
                    row[0]: (row[1], row[2], row[3])
                    for row in reader.execute(
                        "SELECT order_id, environment, status, run_id FROM trading_book_order"
                    ).fetchall()
                }
                self.assertEqual(
                    orders,
                    {
                        "ord-gen1": ("paper", "filled", None),
                        "ord-cp-2": ("paper", "filled", None),
                        "ord-sbx-1": ("agent_sandbox", "filled", None),
                    },
                )
                self.assertEqual(
                    reader.execute("SELECT COUNT(*) FROM trading_book_fill").fetchone()[
                        0
                    ],
                    3,
                )
                self.assertEqual(
                    reader.execute(
                        "SELECT COUNT(*) FROM trading_book_valuation"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    reader.execute(
                        "SELECT delivery_digest, acknowledged FROM trading_fee_delivery WHERE fill_id = 'fill-gen1'"
                    ).fetchone(),
                    ("digest-gen1", 1),
                )
                self.assertEqual(
                    reader.execute(
                        "SELECT book_name, book_role FROM execution_environment_binding WHERE binding_id = 'bind-paper-2'"
                    ).fetchone(),
                    ("Alpha Book", "active"),
                )
                self.assertEqual(
                    reader.execute(
                        "SELECT tier, environment, owner_agent_id FROM execution_environment_binding WHERE binding_id = 'bind-sbx-1'"
                    ).fetchone(),
                    ("agent_sandbox", "agent_sandbox", "quant-1"),
                )
                # Active paper_risk_state migrated to execution:all trading_risk_pause restriction
                self.assertEqual(
                    reader.execute(
                        """SELECT cause_category, scope_ref, active
                           FROM restriction_cause
                           WHERE company_id = 'co-mig-1' AND scope_ref = 'execution:all'"""
                    ).fetchone(),
                    ("trading_risk_pause", "execution:all", 1),
                )

            # Second migration run is strictly idempotent
            domains_again = migrate_v1_store_to_canonical(store)
            self.assertEqual(domains_again, frozenset({"core", "trading"}))

    def test_migrate_rejects_null_duty_state_and_application_status(self):
        with TemporaryDirectory() as tmp:
            store_duty = Path(tmp) / "null-duty.sqlite"
            _seed_precanonical_store(store_duty, "co-null-1")
            conn = sqlite3.connect(store_duty)
            conn.execute("UPDATE duty SET state = NULL WHERE id = 'duty-1'")
            conn.commit()
            conn.close()
            with self.assertRaisesRegex(StorageRefused, "Invalid duty.state"):
                migrate_v1_store_to_canonical(store_duty)

            store_q = Path(tmp) / "null-q.sqlite"
            _seed_precanonical_store(store_q, "co-null-2")
            conn = sqlite3.connect(store_q)
            conn.execute(
                "UPDATE decision_question SET application_status = NULL WHERE question_id = 'q-1'"
            )
            conn.commit()
            conn.close()
            with self.assertRaisesRegex(
                StorageRefused, "Invalid decision_question.application_status"
            ):
                migrate_v1_store_to_canonical(store_q)

    def test_migrate_rolls_back_atomically_on_corrupted_orphan_fee_delivery(self):
        with TemporaryDirectory() as tmp:
            store = Path(tmp) / "bad.sqlite"
            _seed_precanonical_store(store, "co-bad-1")
            conn = sqlite3.connect(store)
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "INSERT INTO paper_fee_delivery VALUES ('orphan-fill', 'd', 0, NULL)"
            )
            conn.commit()
            conn.close()

            with self.assertRaisesRegex(StorageRefused, "Orphan paper_fee_delivery"):
                migrate_v1_store_to_canonical(store)

            # Original installed_domains ('paper') remains untouched after rollback
            check = sqlite3.connect(store)
            domains = {
                r[0]
                for r in check.execute(
                    "SELECT domain FROM installed_domains"
                ).fetchall()
            }
            check.close()
            self.assertEqual(domains, {"core", "paper"})

    def test_cli_migrate_store_command(self):
        with TemporaryDirectory() as tmp:
            store = Path(tmp) / "cli.sqlite"
            _seed_precanonical_store(store, "co-cli-1")
            proc = subprocess.run(
                [
                    sys.executable,
                    "-IB",
                    "-m",
                    "sourdough.cli",
                    "migrate-store",
                    str(store),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("trading", proc.stdout)
            with closing(open_reader(store)) as reader, read_tx(reader):
                self.assertEqual(
                    verify_v1_schema(reader), frozenset({"core", "trading"})
                )

    def test_migrate_paper_order_with_work_runs_backed_run_id_uses_created_at_ns(
        self,
    ):
        with TemporaryDirectory() as tmp:
            store = Path(tmp) / "workrun-order.sqlite"
            _seed_precanonical_store(store, "co-wr-1")
            conn = sqlite3.connect(store)
            try:
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.execute(
                    """INSERT INTO work_items (
                           work_item_id, company_id, owner_agent_id, duty_kind,
                           state, source_kind, source_id, parent_work_item_id,
                           dedupe_key, payload_json, created_at_ns, updated_at_ns
                       ) VALUES (
                           'wi-wr-1', 'co-wr-1', 'quant-1', 'trading.paper_decision',
                           'settled', 'duty', 'duty-1', NULL,
                           NULL, '{}', 1700000000000000000, 1700000000000000000
                       )"""
                )
                conn.execute(
                    """INSERT INTO work_runs (
                           run_id, company_id, work_item_id, agent_id, duty_kind,
                           status, fencing_token, lease_owner, lease_expires_at_ns,
                           wait_until_ns, wait_event_key, budget_ceiling_minor,
                           dedupe_key, outcome_json, created_at_ns, updated_at_ns
                       ) VALUES (
                           'wr-gen1-live', 'co-wr-1', 'wi-wr-1', 'quant-1', 'trading.paper_decision',
                           'completed', 1, 'lease-1', NULL,
                           NULL, NULL, 0,
                           NULL, '{}', 1700000000123456789, 1700000000123456999
                       )"""
                )
                conn.execute(
                    """INSERT INTO paper_order VALUES (
                           'ord-wr-1', 'wr-gen1-live', 'mandate-gen1', 'acct-gen1',
                           'act-wr-1', 'BTC-USDT', 1, 50000, 50000
                       )"""
                )
                conn.commit()
            finally:
                conn.close()

            domains = migrate_v1_store_to_canonical(store)
            self.assertEqual(domains, frozenset({"core", "trading"}))
            with closing(open_reader(store)) as reader, read_tx(reader):
                row = reader.execute(
                    """SELECT run_id, actor_id, created_at_ns, status
                       FROM trading_book_order
                       WHERE order_id = 'ord-wr-1'"""
                ).fetchone()
                self.assertEqual(
                    row,
                    ("wr-gen1-live", "quant-1", 1700000000123456789, "open"),
                )

    def test_migrate_fails_closed_with_atomic_rollback_on_constraint_violations(self):
        cases = (
            (
                "bad_live_mig_positions_copied",
                lambda c: c.execute(
                    """INSERT INTO paper_live_migration_record VALUES (
                           'mig-bad-1', 'co-mig-1', 'q-1', 'bind-paper-2', 'bind-paper-2',
                           NULL, 'qual-missing', 'clean_start', 'keep_shadow', 5, '{}', 'bad', 500
                       )"""
                ),
            ),
            (
                "bad_funding_interval_end_ns",
                lambda c: c.execute(
                    """INSERT INTO paper_funding_settlement VALUES (
                           'fund-bad-1', 'co-mig-1', 'dest-sim', 'acct-paper-2', 'BTC-USDT',
                           12345, NULL, NULL, 1, NULL, 'unavailable', 'missing', 'sha-1', NULL, 500
                       )"""
                ),
            ),
            (
                "bad_fill_less_company_paper_order_side",
                lambda c: c.execute(
                    """INSERT INTO company_paper_order VALUES (
                           'ord-cp-bad', 'bind-paper-2', 'co-mig-1', 'ceo-1', 'act-cp-bad',
                           'BTC-USDT', 'short', 1, 60000, 59900, 405, 'live_read_only',
                           4, 2, 'open', 0, NULL, 0, 405, 406
                       )"""
                ),
            ),
        )
        for label, mutate in cases:
            with self.subTest(case=label), TemporaryDirectory() as tmp:
                store = Path(tmp) / f"{label}.sqlite"
                _seed_precanonical_store(store, "co-mig-1")
                conn = sqlite3.connect(store)
                try:
                    conn.execute("PRAGMA foreign_keys = OFF")
                    mutate(conn)
                    conn.commit()
                finally:
                    conn.close()

                with self.assertRaises(StorageRefused):
                    migrate_v1_store_to_canonical(store)

                check = sqlite3.connect(store)
                try:
                    domains = {
                        r[0]
                        for r in check.execute(
                            "SELECT domain FROM installed_domains"
                        ).fetchall()
                    }
                    self.assertEqual(domains, {"core", "paper"})
                finally:
                    check.close()

    def test_migrate_rejects_null_paper_valuation_columns_with_atomic_rollback(self):
        columns = (
            "capture_id",
            "binding_revision",
            "policy",
            "evidence",
            "trading_nav_atoms",
            "revision",
        )
        for col in columns:
            with self.subTest(column=col), TemporaryDirectory() as tmp:
                store = Path(tmp) / f"null-val-{col}.sqlite"
                _seed_precanonical_store(store, "co-val-null")
                conn = sqlite3.connect(store)
                try:
                    conn.execute("PRAGMA foreign_keys = OFF")
                    # Rebuild paper_valuation without NOT NULL to simulate legacy/relaxed DDL.
                    conn.execute("DROP TABLE paper_valuation")
                    conn.execute(
                        """CREATE TABLE paper_valuation (
                               capture_id TEXT,
                               binding_revision TEXT,
                               policy TEXT,
                               evidence TEXT,
                               trading_nav_atoms INTEGER,
                               revision TEXT
                           )"""
                    )
                    vals = {
                        "capture_id": "cap-null-test",
                        "binding_revision": "mandate-gen1",
                        "policy": "mark_to_market",
                        "evidence": '{"label":"Cut"}',
                        "trading_nav_atoms": 999960,
                        "revision": "val-rev-null-test",
                    }
                    vals[col] = None
                    conn.execute(
                        """INSERT INTO paper_valuation (
                               capture_id, binding_revision, policy, evidence,
                               trading_nav_atoms, revision
                           ) VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            vals["capture_id"],
                            vals["binding_revision"],
                            vals["policy"],
                            vals["evidence"],
                            vals["trading_nav_atoms"],
                            vals["revision"],
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                with self.assertRaises(StorageRefused):
                    migrate_v1_store_to_canonical(store)

                check = sqlite3.connect(store)
                try:
                    domains = {
                        r[0]
                        for r in check.execute(
                            "SELECT domain FROM installed_domains"
                        ).fetchall()
                    }
                    self.assertEqual(domains, {"core", "paper"})
                    src_rows = check.execute(
                        f"SELECT {col} FROM paper_valuation"
                    ).fetchall()
                    self.assertEqual(src_rows, [(None,)])
                finally:
                    check.close()

    def test_migrate_rejects_null_required_paper_order_binding_and_fee_columns(self):
        order_required = (
            "order_id",
            "mandate_revision",
            "account_id",
            "action_id",
            "instrument_rules_digest",
            "quantity",
            "limit_minor_units",
        )
        for col in order_required:
            with (
                self.subTest(table="paper_order", column=col),
                TemporaryDirectory() as tmp,
            ):
                store = Path(tmp) / f"null-ord-{col}.sqlite"
                _seed_precanonical_store(store, "co-ord-null")
                conn = sqlite3.connect(store)
                try:
                    conn.execute("PRAGMA foreign_keys = OFF")
                    conn.execute("DROP TABLE paper_order")
                    conn.execute(
                        """CREATE TABLE paper_order (
                               order_id TEXT,
                               run_id TEXT,
                               mandate_revision TEXT,
                               account_id TEXT,
                               action_id TEXT,
                               instrument_rules_digest TEXT,
                               quantity INTEGER,
                               limit_minor_units INTEGER,
                               reserved_minor_units INTEGER
                           )"""
                    )
                    vals: dict[str, object] = {
                        "order_id": "ord-null-test",
                        "run_id": None,
                        "mandate_revision": "mandate-gen1",
                        "account_id": "acct-gen1",
                        "action_id": "act-null-test",
                        "instrument_rules_digest": "BTC-USDT",
                        "quantity": 1,
                        "limit_minor_units": 50000,
                        "reserved_minor_units": 50000,
                    }
                    vals[col] = None
                    conn.execute(
                        "INSERT INTO paper_order VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        tuple(vals.values()),
                    )
                    conn.commit()
                finally:
                    conn.close()

                with self.assertRaises(StorageRefused):
                    migrate_v1_store_to_canonical(store)

                check = sqlite3.connect(store)
                try:
                    domains = {
                        r[0]
                        for r in check.execute(
                            "SELECT domain FROM installed_domains"
                        ).fetchall()
                    }
                    self.assertEqual(domains, {"core", "paper"})
                finally:
                    check.close()

        # Verify legitimately nullable paper_order.run_id = NULL still migrates cleanly
        with TemporaryDirectory() as tmp:
            store_ok = Path(tmp) / "null-run-id-ok.sqlite"
            _seed_precanonical_store(store_ok, "co-ord-nullrun")
            conn = sqlite3.connect(store_ok)
            try:
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.execute(
                    """INSERT INTO paper_order VALUES (
                           'ord-null-run', NULL, 'mandate-gen1', 'acct-gen1',
                           'act-null-run', 'BTC-USDT', 1, 50000, 50000
                       )"""
                )
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(
                migrate_v1_store_to_canonical(store_ok), frozenset({"core", "trading"})
            )
            with closing(open_reader(store_ok)) as reader, read_tx(reader):
                row = reader.execute(
                    "SELECT run_id, actor_id, status FROM trading_book_order WHERE order_id = 'ord-null-run'"
                ).fetchone()
                self.assertEqual(row, (None, "system", "open"))

        binding_required = (
            "revision",
            "company_id",
            "exposure_limit_minor_units",
            "account_id",
            "starting_minor_units",
            "destination_id",
        )
        for col in binding_required:
            with (
                self.subTest(table="paper_binding", column=col),
                TemporaryDirectory() as tmp,
            ):
                store = Path(tmp) / f"null-pb-{col}.sqlite"
                _seed_precanonical_store(store, "co-pb-null")
                conn = sqlite3.connect(store)
                try:
                    conn.execute("PRAGMA foreign_keys = OFF")
                    conn.execute("DROP TABLE paper_binding")
                    conn.execute(
                        """CREATE TABLE paper_binding (
                               singleton INTEGER,
                               revision TEXT,
                               company_id TEXT,
                               charter_revision TEXT,
                               exposure_limit_minor_units INTEGER,
                               account_id TEXT,
                               connector_revision TEXT,
                               instrument_rules_digest TEXT,
                               currency TEXT,
                               starting_minor_units INTEGER,
                               environment TEXT,
                               destination_id TEXT
                           )"""
                    )
                    vals = {
                        "singleton": 1,
                        "revision": "mandate-gen1",
                        "company_id": "co-pb-null",
                        "charter_revision": "charter-v1",
                        "exposure_limit_minor_units": 500000,
                        "account_id": "acct-gen1",
                        "connector_revision": "conn-1",
                        "instrument_rules_digest": "BTC-USDT",
                        "currency": "USD",
                        "starting_minor_units": 1000000,
                        "environment": "paper",
                        "destination_id": "dest-sim",
                    }
                    vals[col] = None
                    conn.execute(
                        "INSERT INTO paper_binding VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        tuple(vals.values()),
                    )
                    conn.commit()
                finally:
                    conn.close()

                with self.assertRaises(StorageRefused):
                    migrate_v1_store_to_canonical(store)

                check = sqlite3.connect(store)
                try:
                    domains = {
                        r[0]
                        for r in check.execute(
                            "SELECT domain FROM installed_domains"
                        ).fetchall()
                    }
                    self.assertEqual(domains, {"core", "paper"})
                finally:
                    check.close()

        fee_required = ("fill_id", "delivery_digest", "acknowledged")
        for col in fee_required:
            with (
                self.subTest(table="paper_fee_delivery", column=col),
                TemporaryDirectory() as tmp,
            ):
                store = Path(tmp) / f"null-fee-{col}.sqlite"
                _seed_precanonical_store(store, "co-fee-null")
                conn = sqlite3.connect(store)
                try:
                    conn.execute("PRAGMA foreign_keys = OFF")
                    conn.execute("DROP TABLE paper_fee_delivery")
                    conn.execute(
                        """CREATE TABLE paper_fee_delivery (
                               fill_id TEXT,
                               delivery_digest TEXT,
                               acknowledged INTEGER,
                               journal_id TEXT
                           )"""
                    )
                    vals: dict[str, object] = {
                        "fill_id": "fill-gen1",
                        "delivery_digest": "digest-gen1",
                        "acknowledged": 1,
                        "journal_id": None,
                    }
                    vals[col] = None
                    conn.execute(
                        "INSERT INTO paper_fee_delivery VALUES (?, ?, ?, ?)",
                        tuple(vals.values()),
                    )
                    conn.commit()
                finally:
                    conn.close()

                with self.assertRaises(StorageRefused):
                    migrate_v1_store_to_canonical(store)

                check = sqlite3.connect(store)
                try:
                    domains = {
                        r[0]
                        for r in check.execute(
                            "SELECT domain FROM installed_domains"
                        ).fetchall()
                    }
                    self.assertEqual(domains, {"core", "paper"})
                finally:
                    check.close()

    def test_migrate_tolerates_partial_dual_write_conflicts_in_sandbox_valuation_and_windows(
        self,
    ):
        with TemporaryDirectory() as tmp:
            store = Path(tmp) / "dual-write.sqlite"
            _seed_precanonical_store(store, "co-dual-1")
            conn = sqlite3.connect(store)
            try:
                conn.execute(
                    """CREATE TABLE trading_book_order (
                           order_id TEXT PRIMARY KEY NOT NULL,
                           binding_id TEXT NOT NULL,
                           environment TEXT NOT NULL,
                           company_id TEXT NOT NULL,
                           actor_id TEXT NOT NULL,
                           action_id TEXT NOT NULL,
                           external_order_id TEXT,
                           run_id TEXT,
                           decision_id TEXT,
                           mandate_revision TEXT,
                           cancelled_at_ns INTEGER,
                           cancel_reason TEXT,
                           instrument_id TEXT NOT NULL,
                           side TEXT NOT NULL,
                           quantity INTEGER NOT NULL,
                           limit_minor_units INTEGER NOT NULL,
                           market_price_minor_units INTEGER NOT NULL,
                           quote_observed_at_ns INTEGER NOT NULL,
                           quote_source TEXT NOT NULL,
                           fee_bps INTEGER NOT NULL,
                           slippage_bps INTEGER NOT NULL,
                           status TEXT NOT NULL,
                           filled_quantity INTEGER NOT NULL,
                           fill_price_minor_units INTEGER,
                           fee_minor_units INTEGER NOT NULL,
                           created_at_ns INTEGER NOT NULL,
                           updated_at_ns INTEGER NOT NULL
                       ) STRICT"""
                )
                conn.execute(
                    """INSERT INTO trading_book_order VALUES (
                           'ord-sbx-1', 'bind-sbx-1', 'agent_sandbox', 'co-dual-1', 'quant-1',
                           'act-sbx-1', NULL, NULL, NULL, NULL, NULL, NULL,
                           'ETH-USDT', 'buy', 2, 3000, 2990, 411, 'live_read_only',
                           4, 2, 'filled', 2, 2990, 5, 411, 412
                       )"""
                )
                conn.execute(
                    """INSERT INTO trading_book_valuation VALUES (
                           'cap-gen1', 'bind-paper-2', 'paper', 'co-dual-1', 'mark_to_market',
                           999960, 999960, 0, 0, 0, 0, 'paper_valuation', 415
                       )"""
                )
                conn.execute(
                    """CREATE TABLE trading_operating_window (
                           window_id TEXT PRIMARY KEY NOT NULL,
                           contract TEXT NOT NULL,
                           committed_ns INTEGER,
                           revision TEXT UNIQUE
                       ) STRICT"""
                )
                conn.execute(
                    "INSERT INTO trading_operating_window VALUES ('win-1', '24h', 2000, 'win-rev-1')"
                )
                conn.commit()
            finally:
                conn.close()

            domains = migrate_v1_store_to_canonical(store)
            self.assertEqual(domains, frozenset({"core", "trading"}))
            with closing(open_reader(store)) as reader, read_tx(reader):
                self.assertEqual(
                    verify_v1_schema(reader), frozenset({"core", "trading"})
                )
                self.assertEqual(
                    reader.execute(
                        "SELECT COUNT(*) FROM trading_operating_window"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    reader.execute(
                        "SELECT COUNT(*) FROM trading_book_order WHERE order_id = 'ord-sbx-1'"
                    ).fetchone()[0],
                    1,
                )
