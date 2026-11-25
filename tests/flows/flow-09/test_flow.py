import json
import sqlite3
import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns

from sourdough.company import FoundingTerms, found
from sourdough.fund_returns import ReturnScope, measure
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_funding import (
    FUNDING_INTERVAL_NS,
    FundingChart,
    FundingRateEvidence,
    MarkPriceEvidence,
    book_funding_settlement,
)
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.runtime.registry import DutyContext, DutyRegistry
from sourdough.runtime.work_kernel import WorkItem, WorkRun
from sourdough.strategy_comparison import (
    CHALLENGER_EVAL_DUTY_KIND,
    AnalysisPlan,
    ArmObservation,
    StrategyComparisonRefused,
    activate_strategy_release,
    build_pit_file_entries,
    provision_isolated_arm_namespace,
    read_active_strategy_deployment,
    register_challenger_duties,
    register_challenger_trial,
    register_pit_catalog_manifest,
    register_strategy_release,
    rollback_strategy_deployment,
    verify_arm_isolation,
    verify_pit_manifest,
)
from sourdough.writer_lease import hold_company_writer


def _statement_from_fund_returns(
    company_id: str,
    account_id: str,
    opening_minor: int,
    closing_minor: int,
    source_id: str,
) -> dict:
    scope = ReturnScope(
        company_id, account_id, "USDT", "company_net", "valuation-policy-v1"
    )
    measurement = measure(
        scope,
        start_ns=10_000,
        end_ns=20_000,
        opening_minor=opening_minor,
        closing_minor=closing_minor,
        flows=(),
        sources=(source_id,),
    )
    twr = measurement.time_weighted_return
    assert twr is not None
    payload = {
        "company": {
            "scope": {"company_id": company_id, "series": "company_net"},
            "pnl_minor": measurement.pnl_minor,
            "time_weighted_return": {
                "numerator": twr.numerator,
                "denominator": twr.denominator,
            },
            "missing": [],
            "sources": list(measurement.sources),
        }
    }
    revision = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload | {"revision": revision}


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self):
        with TemporaryDirectory(prefix="sd-f09-") as scratch:
            root = Path(scratch)
            store = root / "company.sqlite"
            leases = root / "leases"
            receipt = found(
                store, FoundingTerms("flow-09", "charter-1", "paper", "profile")
            )
            company_id = receipt.company_id
            author_id = receipt.founder_id
            venue = FixedQuoteVenue(root / "live-venue.sqlite")
            account = venue.provision(
                PaperAccount("acct-main", "fixture", "rules", "USDT", 100_000, "paper"),
                100,
            )
            bind(
                store,
                PaperMandate(
                    "binding-paper-1", company_id, "charter-1", account, 50_000
                ),
                venue,
            )

            # 1. Book deterministic 8h perpetual funding into core GL + paper_funding_settlement.
            chart = FundingChart(
                "funding-v1",
                "USDT",
                100_000_000,
                "funding-expense",
                "funding-income",
                "paper-cash",
            )
            interval_end = 62_310 * FUNDING_INTERVAL_NS
            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                settled = book_funding_settlement(
                    writer,
                    destination_id="perp-sandbox",
                    account_id="acct-main",
                    instrument_id="BTCUSDT-PERP",
                    interval_end_ns=interval_end,
                    signed_position_lots=2,
                    funding=FundingRateEvidence(
                        "BTCUSDT-PERP",
                        interval_end,
                        100_000,
                        interval_end,
                        "funding-src-1",
                    ),
                    mark=MarkPriceEvidence(
                        "BTCUSDT-PERP",
                        interval_end,
                        50_000_000_000,
                        100_000_000,
                        "mark-src-1",
                    ),
                    chart=chart,
                )
                self.assertEqual(settled.status, "booked")
                self.assertEqual(settled.funding_cashflow_atoms, -10_000_000)

                # Missing funding evidence yields explicit UNAVAILABLE (never 0).
                unavail = book_funding_settlement(
                    writer,
                    destination_id="perp-sandbox",
                    account_id="acct-main",
                    instrument_id="BTCUSDT-PERP",
                    interval_end_ns=interval_end + FUNDING_INTERVAL_NS,
                    signed_position_lots=2,
                    funding=None,
                    mark=None,
                    chart=chart,
                )
                self.assertEqual(unavail.status, "unavailable")
                self.assertIsNone(unavail.funding_cashflow_atoms)

                # 2. Register immutable PIT catalog + incumbent & challenger StrategyReleases.
                catalog_dir = root / "pit_catalog"
                catalog_dir.mkdir()
                (catalog_dir / "btc_perp.parquet").write_bytes(b"pit-market-data-v1")
                pit = register_pit_catalog_manifest(
                    writer,
                    manifest_id="pit-2026-11",
                    dataset_name="binance-btc-perp",
                    files=build_pit_file_entries(
                        catalog_dir, (("btc_perp.parquet", 1_000, 5_000, 5_000),)
                    ),
                )
                verify_pit_manifest(pit, catalog_dir, decision_cut_ns=5_500)

                incumbent = register_strategy_release(
                    writer,
                    release_id="strat-inc-v1",
                    owner_agent_id=author_id,
                    strategy_source_sha256=sha256(b"inc-src").hexdigest(),
                    parameters={"threshold_bps": 10},
                    instrument_rules_sha256=sha256(b"inst").hexdigest(),
                    risk_config_sha256=sha256(b"risk").hexdigest(),
                    fill_policy_sha256=sha256(b"fill").hexdigest(),
                    pit_manifest_id=pit.manifest_id,
                    pit_manifest_sha256=pit.manifest_sha256,
                )
                challenger = register_strategy_release(
                    writer,
                    release_id="strat-chal-v2",
                    owner_agent_id=author_id,
                    parent_release_id=incumbent.release_id,
                    strategy_source_sha256=sha256(b"chal-src").hexdigest(),
                    parameters={"threshold_bps": 15},
                    instrument_rules_sha256=sha256(b"inst").hexdigest(),
                    risk_config_sha256=sha256(b"risk").hexdigest(),
                    fill_policy_sha256=sha256(b"fill").hexdigest(),
                    pit_manifest_id=pit.manifest_id,
                    pit_manifest_sha256=pit.manifest_sha256,
                )
                self.assertEqual(incumbent.research_trial_count_n, 1)
                self.assertEqual(challenger.research_trial_count_n, 2)

                # 3. Activate initial incumbent under separate deployment authority.
                def deployer(_db, _cid, _op, _rid):
                    return "quant-deployer"

                dep_inc = activate_strategy_release(
                    writer,
                    command_id="cmd-activate-inc",
                    release_id=incumbent.release_id,
                    binding_revision="binding-paper-1",
                    authorize_deploy=deployer,
                )

                # 4. Provision two physically isolated simulated arms sharing one read-only feed.
                shared_feed = root / "public_feed.jsonl"
                shared_feed.write_text('{"seq":1,"mid":50000}\n', encoding="utf-8")
                inc_arm = provision_isolated_arm_namespace(
                    root_dir=root / "isolated_arms",
                    role="incumbent",
                    release=incumbent,
                    charter_revision="charter-1",
                    execution_profile="profile",
                    shared_feed_path=shared_feed,
                )
                chal_arm = provision_isolated_arm_namespace(
                    root_dir=root / "isolated_arms",
                    role="challenger",
                    release=challenger,
                    charter_revision="charter-1",
                    execution_profile="profile",
                    shared_feed_path=shared_feed,
                )
                verify_arm_isolation(inc_arm, chal_arm, live_store=store)

                # 5. Preregister prospective paired comparison after baseline statement.
                baseline = _statement_from_fund_returns(
                    company_id, "acct-main", 100_000, 100_500, "baseline-win"
                )
                plan = AnalysisPlan(
                    opens_ns=10_000,
                    closes_ns=20_000,
                    primary_metric="company_net_return_bps",
                    worthwhile_margin_bps=5.0,
                    futility_margin_bps=0.0,
                    alpha=0.05,
                    min_observations=12,
                    max_drawdown_bps=250,
                    max_exposure_minor=50_000,
                    max_operating_cost_minor=1_000,
                    incumbent_cost_share_num=1,
                    incumbent_cost_share_den=2,
                    changed_components=("signal-threshold",),
                    stopping_rule="robbins-mixture-cs-v1",
                )
                register_challenger_trial(
                    writer,
                    comparison_id="cmp-2026-11",
                    incumbent_release_id=incumbent.release_id,
                    challenger_release_id=challenger.release_id,
                    baseline_statement_revision=baseline["revision"],
                    analysis_plan=plan,
                    incumbent_arm=inc_arm,
                    challenger_arm=chal_arm,
                    now_ns=5_000,
                )

                # 6. Evaluate paired arms through DutyRegistry using Flow 08 fund_returns.measure.
                def observe_arms(_trial):
                    inc_stmt = _statement_from_fund_returns(
                        inc_arm.company_id,
                        inc_arm.account_id,
                        100_000,
                        100_200,
                        "inc-stmt",
                    )
                    chal_stmt = _statement_from_fund_returns(
                        chal_arm.company_id,
                        chal_arm.account_id,
                        100_000,
                        103_000,
                        "chal-stmt",
                    )
                    return (
                        ArmObservation(
                            inc_stmt, tuple(1.0 for _ in range(25)), 40, 20_000, 100
                        ),
                        ArmObservation(
                            chal_stmt, tuple(15.0 for _ in range(25)), 50, 20_000, 100
                        ),
                        100,
                    )

                registry = DutyRegistry()
                register_challenger_duties(registry, observe_arms=observe_arms)
                now = time_ns()
                outcome = registry.get(CHALLENGER_EVAL_DUTY_KIND).execute(
                    DutyContext(
                        writer=writer,
                        company_id=company_id,
                        work_item=WorkItem(
                            "wi-eval",
                            company_id,
                            author_id,
                            CHALLENGER_EVAL_DUTY_KIND,
                            "running",
                            "manual",
                            "src-eval",
                            None,
                            None,
                            json.dumps({"comparison_id": "cmp-2026-11"}),
                            now,
                            now,
                        ),
                        run=WorkRun(
                            "run-eval",
                            company_id,
                            "wi-eval",
                            author_id,
                            CHALLENGER_EVAL_DUTY_KIND,
                            "running",
                            1,
                            "worker-1",
                            now + 60_000_000_000,
                            None,
                            None,
                            0,
                            None,
                            None,
                            now,
                            now,
                        ),
                        _clock=now,
                    )
                )
                self.assertEqual(outcome.disposition, "completed")
                self.assertEqual(outcome.result["verdict"], "PROMOTE")

                # 7. Author cannot self-promote; separate deployer promotes challenger cleanly.
                with self.assertRaises(StrategyComparisonRefused):
                    activate_strategy_release(
                        writer,
                        command_id="cmd-author-fail",
                        release_id=challenger.release_id,
                        binding_revision="binding-paper-1",
                        comparison_id="cmp-2026-11",
                        expected_active_deployment_id=dep_inc.deployment_id,
                        authorize_deploy=lambda _db, _cid, _op, _rid: author_id,
                    )
                dep_chal = activate_strategy_release(
                    writer,
                    command_id="cmd-promote-chal",
                    release_id=challenger.release_id,
                    binding_revision="binding-paper-1",
                    comparison_id="cmp-2026-11",
                    expected_active_deployment_id=dep_inc.deployment_id,
                    authorize_deploy=deployer,
                )
                self.assertEqual(dep_chal.release_id, challenger.release_id)

                # 8. Rollback fences challenger and restores incumbent cleanly.
                dep_rb = rollback_strategy_deployment(
                    writer,
                    command_id="cmd-rollback-inc",
                    target_release_id=incumbent.release_id,
                    expected_active_deployment_id=dep_chal.deployment_id,
                    authorize_deploy=deployer,
                )
                self.assertEqual(dep_rb.release_id, incumbent.release_id)

            self.assertEqual(read_active_strategy_deployment(store, company_id), dep_rb)
            with closing(sqlite3.connect(store)) as db:
                self.assertEqual(
                    db.execute("SELECT sum(amount_minor) FROM journal_line").fetchone(),
                    (0,),
                )
