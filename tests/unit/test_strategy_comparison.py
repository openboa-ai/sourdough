import json
import unittest
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
from sourdough.paper_account import PaperAccount, PaperMandate, bind
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.runtime.registry import DutyContext, DutyRegistry
from sourdough.runtime.work_kernel import WorkItem, WorkRun
from sourdough.storage.db import open_writer, write_tx
from sourdough.storage.kernel import admit_command_receipt
from sourdough.strategy_comparison import (
    ACTIVATE_STRATEGY_CMD,
    CHALLENGER_EVAL_DUTY_KIND,
    STRATEGY_DEPLOY_CONTRACT,
    AnalysisPlan,
    ArmObservation,
    StrategyComparisonRefused,
    activate_strategy_release,
    build_pit_file_entries,
    compute_anytime_valid_bounds,
    evaluate_challenger_trial,
    invalidate_challenger_trial,
    provision_isolated_arm_namespace,
    read_active_strategy_deployment,
    register_challenger_duties,
    register_challenger_trial,
    register_pit_catalog_manifest,
    register_strategy_release,
    rollback_strategy_deployment,
    verify_arm_isolation,
    verify_pit_manifest,
    verify_strategy_release_source,
)
from sourdough.writer_lease import hold_company_writer


def _hex(seed: str) -> str:
    return sha256(seed.encode("utf-8")).hexdigest()


def _sealed_statement(company_id: str, seed: str, *, complete: bool = True) -> dict:
    payload = {
        "window_id": seed,
        "company": {
            "scope": {"company_id": company_id, "series": "company_net"},
            "pnl_minor": 1000 if complete else None,
            "time_weighted_return": {"numerator": 1, "denominator": 100}
            if complete
            else None,
            "missing": [] if complete else ["missing-cost-receipt"],
        },
    }
    revision = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload | {"revision": revision}


class StrategyComparisonTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "live-company.sqlite"
        self.venue = FixedQuoteVenue(self.root / "live-venue.sqlite")
        self.leases = self.root / "leases"
        receipt = found(
            self.store, FoundingTerms("quant-co", "charter-1", "paper", "profile")
        )
        self.company = receipt.company_id
        self.author_id = receipt.founder_id
        account = self.venue.provision(
            PaperAccount("paper", "fixture", "rules", "USD", 10_000, "paper"), 100
        )
        self.binding = bind(
            self.store,
            PaperMandate("binding-1", self.company, "charter-1", account, 5_000),
            self.venue,
        )

        self.catalog_dir = self.root / "catalog"
        self.catalog_dir.mkdir()
        self.bar_file = self.catalog_dir / "btc_bars.parquet"
        self.bar_file.write_bytes(b"immutable-pit-bars-v1")
        self.feed_file = self.root / "shared_feed.jsonl"
        self.feed_file.write_text('{"ts":1,"price":50000}\n', encoding="utf-8")

        self.desk_baseline = self.root / "baseline_desk"
        self.desk_baseline.mkdir()
        (self.desk_baseline / "policy.md").write_text(
            "allowed baseline", encoding="utf-8"
        )
        (self.desk_baseline / "sealed_labels.json").write_text(
            '{"secret":"never-copy"}', encoding="utf-8"
        )

    def _writer(self):
        return hold_company_writer(
            self.store, self.company, lease_directory=self.leases
        )

    def _setup_releases(self):
        entries = build_pit_file_entries(
            self.catalog_dir,
            (("btc_bars.parquet", 1_000, 2_000, 2_000),),
        )
        with self._writer() as writer:
            manifest = register_pit_catalog_manifest(
                writer,
                manifest_id="pit-v1",
                dataset_name="btc-spot",
                files=entries,
            )
            incumbent = register_strategy_release(
                writer,
                release_id="rel-inc",
                owner_agent_id=self.author_id,
                strategy_source_sha256=_hex("source-inc"),
                parameters={"lookback": 10},
                instrument_rules_sha256=_hex("inst"),
                risk_config_sha256=_hex("risk"),
                fill_policy_sha256=_hex("fill"),
                pit_manifest_id=manifest.manifest_id,
                pit_manifest_sha256=manifest.manifest_sha256,
            )
            challenger = register_strategy_release(
                writer,
                release_id="rel-chal",
                owner_agent_id=self.author_id,
                parent_release_id=incumbent.release_id,
                strategy_source_sha256=_hex("source-chal"),
                parameters={"lookback": 20},
                instrument_rules_sha256=_hex("inst"),
                risk_config_sha256=_hex("risk"),
                fill_policy_sha256=_hex("fill"),
                pit_manifest_id=manifest.manifest_id,
                pit_manifest_sha256=manifest.manifest_sha256,
            )
        return manifest, incumbent, challenger

    def test_pit_manifest_anti_lookahead_and_release_immutability(self):
        manifest, incumbent, challenger = self._setup_releases()
        self.assertEqual(incumbent.research_trial_count_n, 1)
        self.assertEqual(challenger.research_trial_count_n, 2)

        # Valid decision cut passes; lookahead cut before available_at_ns fails closed.
        verify_pit_manifest(manifest, self.catalog_dir, decision_cut_ns=2_500)
        with self.assertRaisesRegex(StrategyComparisonRefused, "lookahead"):
            verify_pit_manifest(manifest, self.catalog_dir, decision_cut_ns=1_500)
        # Tampered manifest_sha256 fails closed.
        with self.assertRaisesRegex(StrategyComparisonRefused, "digest"):
            verify_pit_manifest(
                replace(manifest, manifest_sha256=_hex("forged")),
                self.catalog_dir,
                decision_cut_ns=2_500,
            )
        # Duplicate manifest under a second ID raises StrategyComparisonRefused cleanly.
        with self._writer() as writer:
            with self.assertRaisesRegex(StrategyComparisonRefused, "another ID"):
                register_pit_catalog_manifest(
                    writer,
                    manifest_id="pit-v1-dup",
                    dataset_name="btc-spot",
                    files=manifest.files,
                )

        # Mutating catalog bytes after registration is detected.
        self.bar_file.write_bytes(b"tampered-bytes")
        with self.assertRaisesRegex(StrategyComparisonRefused, "digest mismatch"):
            verify_pit_manifest(manifest, self.catalog_dir, decision_cut_ns=2_500)

        # Mutating strategy source after registration is detected; unsafe release_id & bad lock refused.
        verify_strategy_release_source(incumbent, b"source-inc")
        with self.assertRaisesRegex(StrategyComparisonRefused, "digest"):
            verify_strategy_release_source(incumbent, b"edited-branch-tip")
        with self._writer() as writer:
            with self.assertRaisesRegex(StrategyComparisonRefused, "safe identifier"):
                register_strategy_release(
                    writer,
                    release_id="bad/../escape",
                    owner_agent_id=self.author_id,
                    strategy_source_sha256=_hex("src"),
                    parameters={},
                    instrument_rules_sha256=_hex("inst"),
                    risk_config_sha256=_hex("risk"),
                    fill_policy_sha256=_hex("fill"),
                    pit_manifest_id=manifest.manifest_id,
                    pit_manifest_sha256=manifest.manifest_sha256,
                )
            with self.assertRaisesRegex(StrategyComparisonRefused, "lock hash"):
                register_strategy_release(
                    writer,
                    release_id="rel-bad-lock",
                    owner_agent_id=self.author_id,
                    strategy_source_sha256=_hex("src"),
                    parameters={},
                    instrument_rules_sha256=_hex("inst"),
                    risk_config_sha256=_hex("risk"),
                    fill_policy_sha256=_hex("fill"),
                    pit_manifest_id=manifest.manifest_id,
                    pit_manifest_sha256=manifest.manifest_sha256,
                    nautilus_lock_sha256=_hex("wrong-lock"),
                )

    def test_isolated_arms_symlink_guard_and_verdicts(self):
        _, incumbent, challenger = self._setup_releases()
        inc_arm = provision_isolated_arm_namespace(
            root_dir=self.root / "arms",
            role="incumbent",
            release=incumbent,
            charter_revision="charter-1",
            execution_profile="profile",
            shared_feed_path=self.feed_file,
            baseline_desk_dir=self.desk_baseline,
            permitted_desk_files=("policy.md",),
        )
        chal_arm = provision_isolated_arm_namespace(
            root_dir=self.root / "arms",
            role="challenger",
            release=challenger,
            charter_revision="charter-1",
            execution_profile="profile",
            shared_feed_path=self.feed_file,
            baseline_desk_dir=self.desk_baseline,
            permitted_desk_files=("policy.md",),
        )
        verify_arm_isolation(
            inc_arm, chal_arm, live_store=self.store, live_company_id=self.company
        )
        self.assertTrue((Path(inc_arm.desk_path) / "policy.md").exists())
        self.assertFalse((Path(inc_arm.desk_path) / "sealed_labels.json").exists())

        # Symlink pointing to live_store MUST be rejected!
        fake_symlink = self.root / "fake_chal.sqlite"
        fake_symlink.symlink_to(self.store)
        with self.assertRaisesRegex(StrategyComparisonRefused, "Symlink"):
            verify_arm_isolation(
                inc_arm,
                replace(chal_arm, store_path=str(fake_symlink)),
                live_store=self.store,
                live_company_id=self.company,
            )

        # ArmObservation rejects NaN and negative hard-gate fields.
        with self.assertRaises(StrategyComparisonRefused):
            ArmObservation(
                _sealed_statement(chal_arm.company_id, "bad"),
                (1.0,),
                realized_drawdown_bps=float("nan"),  # type: ignore[arg-type]
                peak_exposure_minor=100,
                operating_cost_minor=10,
            )
        with self.assertRaises(StrategyComparisonRefused):
            ArmObservation(
                _sealed_statement(chal_arm.company_id, "bad2"),
                (1.0,),
                realized_drawdown_bps=10,
                peak_exposure_minor=100,
                operating_cost_minor=-50,
            )
        # Extreme float overflow in compute_anytime_valid_bounds is refused cleanly.
        with self.assertRaises(StrategyComparisonRefused):
            compute_anytime_valid_bounds(
                (1e154, -1e154, 1e154, -1e154, 0.0),
                alpha=0.05,
                research_trial_count_n=1,
            )

        plan = AnalysisPlan(
            opens_ns=10_000,
            closes_ns=20_000,
            primary_metric="company_net_return_bps",
            worthwhile_margin_bps=5.0,
            futility_margin_bps=0.0,
            alpha=0.05,
            min_observations=10,
            max_drawdown_bps=250,
            max_exposure_minor=1_000,
            max_operating_cost_minor=500,
            incumbent_cost_share_num=1,
            incumbent_cost_share_den=2,
            changed_components=("signal-threshold",),
            stopping_rule="robbins-mixture-cs-v1",
        )

        # Case 1: Evaluating before opens_ns fails; forged statement seal fails; valid run PROMOTEs.
        with self._writer() as writer:
            register_challenger_trial(
                writer,
                comparison_id="cmp-promote",
                incumbent_release_id=incumbent.release_id,
                challenger_release_id=challenger.release_id,
                baseline_statement_revision=_hex("baseline-stmt"),
                analysis_plan=plan,
                incumbent_arm=inc_arm,
                challenger_arm=chal_arm,
                now_ns=5_000,
            )
            with self.assertRaisesRegex(StrategyComparisonRefused, "window opens"):
                evaluate_challenger_trial(
                    writer,
                    comparison_id="cmp-promote",
                    incumbent_obs=ArmObservation(
                        _sealed_statement(inc_arm.company_id, "inc-early"),
                        tuple(1.0 for _ in range(30)),
                        50,
                        500,
                        100,
                    ),
                    challenger_obs=ArmObservation(
                        _sealed_statement(chal_arm.company_id, "chal-early"),
                        tuple(15.0 for _ in range(30)),
                        60,
                        500,
                        100,
                    ),
                    now_ns=9_999,
                )
            forged = _sealed_statement(chal_arm.company_id, "chal-1") | {
                "revision": _hex("wrong")
            }
            with self.assertRaisesRegex(
                StrategyComparisonRefused, "cryptographic seal"
            ):
                evaluate_challenger_trial(
                    writer,
                    comparison_id="cmp-promote",
                    incumbent_obs=ArmObservation(
                        _sealed_statement(inc_arm.company_id, "inc-1"),
                        tuple(1.0 for _ in range(30)),
                        50,
                        500,
                        100,
                    ),
                    challenger_obs=ArmObservation(
                        forged, tuple(15.0 for _ in range(30)), 60, 500, 100
                    ),
                    now_ns=15_000,
                )
            promoted = evaluate_challenger_trial(
                writer,
                comparison_id="cmp-promote",
                incumbent_obs=ArmObservation(
                    _sealed_statement(inc_arm.company_id, "inc-1"),
                    tuple(1.0 for _ in range(30)),
                    50,
                    500,
                    100,
                ),
                challenger_obs=ArmObservation(
                    _sealed_statement(chal_arm.company_id, "chal-1"),
                    tuple(15.0 for _ in range(30)),
                    60,
                    500,
                    100,
                ),
                shared_trial_cost_minor=100,
                now_ns=15_000,
            )
            # Finalized trial cannot be overwritten by invalidate_challenger_trial.
            with self.assertRaisesRegex(StrategyComparisonRefused, "finalized"):
                invalidate_challenger_trial(writer, "cmp-promote", "retroactive tamper")
        self.assertEqual(promoted.verdict, "PROMOTE")

        # Case 2: Profitable challenger breaching drawdown hard gate -> REJECT.
        with self._writer() as writer:
            register_challenger_trial(
                writer,
                comparison_id="cmp-hard-gate",
                incumbent_release_id=incumbent.release_id,
                challenger_release_id=challenger.release_id,
                baseline_statement_revision=_hex("baseline-stmt"),
                analysis_plan=plan,
                incumbent_arm=inc_arm,
                challenger_arm=chal_arm,
                now_ns=5_000,
            )
            rejected = evaluate_challenger_trial(
                writer,
                comparison_id="cmp-hard-gate",
                incumbent_obs=ArmObservation(
                    _sealed_statement(inc_arm.company_id, "inc-2"),
                    tuple(1.0 for _ in range(30)),
                    50,
                    500,
                    100,
                ),
                challenger_obs=ArmObservation(
                    _sealed_statement(chal_arm.company_id, "chal-2"),
                    tuple(50.0 for _ in range(30)),
                    300,
                    500,
                    100,
                ),
                now_ns=15_000,
            )
        self.assertEqual(rejected.verdict, "REJECT")

        # Case 3: Incumbent reconciliation drift blocks PROMOTE -> INCONCLUSIVE.
        with self._writer() as writer:
            register_challenger_trial(
                writer,
                comparison_id="cmp-inc-drift",
                incumbent_release_id=incumbent.release_id,
                challenger_release_id=challenger.release_id,
                baseline_statement_revision=_hex("baseline-stmt"),
                analysis_plan=plan,
                incumbent_arm=inc_arm,
                challenger_arm=chal_arm,
                now_ns=5_000,
            )
            inc_drift = evaluate_challenger_trial(
                writer,
                comparison_id="cmp-inc-drift",
                incumbent_obs=ArmObservation(
                    _sealed_statement(inc_arm.company_id, "inc-3"),
                    tuple(1.0 for _ in range(30)),
                    20,
                    500,
                    100,
                    reconciliation_drift=True,
                ),
                challenger_obs=ArmObservation(
                    _sealed_statement(chal_arm.company_id, "chal-3"),
                    tuple(20.0 for _ in range(30)),
                    20,
                    500,
                    100,
                ),
                now_ns=15_000,
            )
        self.assertEqual(inc_drift.verdict, "INCONCLUSIVE")

        # Case 4: Mutating shared read-only feed after registration invalidates trial at eval!
        with self._writer() as writer:
            register_challenger_trial(
                writer,
                comparison_id="cmp-feed-tamper",
                incumbent_release_id=incumbent.release_id,
                challenger_release_id=challenger.release_id,
                baseline_statement_revision=_hex("baseline-stmt"),
                analysis_plan=plan,
                incumbent_arm=inc_arm,
                challenger_arm=chal_arm,
                now_ns=5_000,
            )
            self.feed_file.write_text('{"ts":1,"price":99999}\n', encoding="utf-8")
            invalidated = evaluate_challenger_trial(
                writer,
                comparison_id="cmp-feed-tamper",
                incumbent_obs=ArmObservation(
                    _sealed_statement(inc_arm.company_id, "inc-4"),
                    tuple(1.0 for _ in range(30)),
                    10,
                    100,
                    50,
                ),
                challenger_obs=ArmObservation(
                    _sealed_statement(chal_arm.company_id, "chal-4"),
                    tuple(20.0 for _ in range(30)),
                    10,
                    100,
                    50,
                ),
                now_ns=15_000,
            )
        self.assertEqual(invalidated.status, "invalidated")
        self.assertEqual(
            invalidated.invalidation_reason, "arm-isolation-or-feed-violation"
        )

    def test_deployment_binding_gate_rollback_guard_replay_and_idempotent_duty(self):
        manifest, incumbent, challenger = self._setup_releases()
        with self._writer() as writer:
            untested = register_strategy_release(
                writer,
                release_id="rel-untested",
                owner_agent_id=self.author_id,
                strategy_source_sha256=_hex("source-untested"),
                parameters={"lookback": 99},
                instrument_rules_sha256=_hex("inst"),
                risk_config_sha256=_hex("risk"),
                fill_policy_sha256=_hex("fill"),
                pit_manifest_id=manifest.manifest_id,
                pit_manifest_sha256=manifest.manifest_sha256,
            )

        inc_arm = provision_isolated_arm_namespace(
            root_dir=self.root / "arms2",
            role="incumbent",
            release=incumbent,
            charter_revision="charter-1",
            execution_profile="profile",
            shared_feed_path=self.feed_file,
        )
        chal_arm = provision_isolated_arm_namespace(
            root_dir=self.root / "arms2",
            role="challenger",
            release=challenger,
            charter_revision="charter-1",
            execution_profile="profile",
            shared_feed_path=self.feed_file,
        )

        def deployer(_db, _cid, _op, _rid):
            return "deployment-officer"

        with self._writer() as writer:
            # Wrong binding_revision is refused!
            with self.assertRaisesRegex(StrategyComparisonRefused, "paper mandate"):
                activate_strategy_release(
                    writer,
                    command_id="dep-bad-bind",
                    release_id=incumbent.release_id,
                    binding_revision="forged-binding",
                    authorize_deploy=deployer,
                )

            # Pre-existing denied receipt with matching command_id cannot be bypassed!
            with closing(open_writer(self.store, lease=writer)) as db, write_tx(db):
                admit_command_receipt(
                    db,
                    company_id=self.company,
                    command_id="dep-pre-denied",
                    actor_id="deployment-officer",
                    peer_pid=1,
                    command_type=ACTIVATE_STRATEGY_CMD,
                    contract_version=STRATEGY_DEPLOY_CONTRACT,
                    payload_sha256=_hex("placeholder"),
                    preconditions={"expected_active_deployment_id": None},
                    state="denied",
                    denial_code="previously_denied",
                )
            with self.assertRaises(StrategyComparisonRefused):
                activate_strategy_release(
                    writer,
                    command_id="dep-pre-denied",
                    release_id=incumbent.release_id,
                    binding_revision=self.binding.revision,
                    authorize_deploy=deployer,
                )

            # Activate initial incumbent and verify idempotent replay.
            first = activate_strategy_release(
                writer,
                command_id="dep-inc-1",
                release_id=incumbent.release_id,
                binding_revision=self.binding.revision,
                authorize_deploy=deployer,
            )
            self.assertEqual(
                activate_strategy_release(
                    writer,
                    command_id="dep-inc-1",
                    release_id=incumbent.release_id,
                    binding_revision=self.binding.revision,
                    authorize_deploy=deployer,
                ),
                first,
            )

            # Register & evaluate via idempotent DutyRegistry handler.
            plan = AnalysisPlan(
                opens_ns=10_000,
                closes_ns=20_000,
                primary_metric="company_net_return_bps",
                worthwhile_margin_bps=4.0,
                futility_margin_bps=0.0,
                alpha=0.05,
                min_observations=10,
                max_drawdown_bps=250,
                max_exposure_minor=1_000,
                max_operating_cost_minor=500,
                incumbent_cost_share_num=1,
                incumbent_cost_share_den=2,
                changed_components=("alpha",),
                stopping_rule="robbins-mixture-cs-v1",
            )
            register_challenger_trial(
                writer,
                comparison_id="cmp-duty",
                incumbent_release_id=incumbent.release_id,
                challenger_release_id=challenger.release_id,
                baseline_statement_revision=_hex("base"),
                analysis_plan=plan,
                incumbent_arm=inc_arm,
                challenger_arm=chal_arm,
                now_ns=5_000,
            )

            observe_calls = 0

            def observe(_trial):
                nonlocal observe_calls
                observe_calls += 1
                return (
                    ArmObservation(
                        _sealed_statement(inc_arm.company_id, "inc-d"),
                        tuple(1.0 for _ in range(30)),
                        30,
                        400,
                        80,
                    ),
                    ArmObservation(
                        _sealed_statement(chal_arm.company_id, "chal-d"),
                        tuple(14.0 for _ in range(30)),
                        40,
                        400,
                        80,
                    ),
                    40,
                )

            registry = DutyRegistry()
            register_challenger_duties(registry, observe_arms=observe)
            now = 15_000
            ctx = DutyContext(
                writer=writer,
                company_id=self.company,
                work_item=WorkItem(
                    "wi-1",
                    self.company,
                    self.author_id,
                    CHALLENGER_EVAL_DUTY_KIND,
                    "running",
                    "manual",
                    "s1",
                    None,
                    None,
                    json.dumps({"comparison_id": "cmp-duty"}),
                    now,
                    now,
                ),
                run=WorkRun(
                    "run-1",
                    self.company,
                    "wi-1",
                    self.author_id,
                    CHALLENGER_EVAL_DUTY_KIND,
                    "running",
                    1,
                    "w",
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
            outcome1 = registry.get(CHALLENGER_EVAL_DUTY_KIND).execute(ctx)
            outcome2 = registry.get(CHALLENGER_EVAL_DUTY_KIND).execute(ctx)
            self.assertEqual(outcome1.result["verdict"], "PROMOTE")
            self.assertEqual(outcome2, outcome1)
            self.assertEqual(observe_calls, 1)

            # Promote challenger.
            second = activate_strategy_release(
                writer,
                command_id="dep-chal-2",
                release_id=challenger.release_id,
                binding_revision=self.binding.revision,
                comparison_id="cmp-duty",
                expected_active_deployment_id=first.deployment_id,
                authorize_deploy=deployer,
            )

            # Rollback refuses untested release, active release, and author self-rollback!
            with self.assertRaisesRegex(
                StrategyComparisonRefused, "previously deployed"
            ):
                rollback_strategy_deployment(
                    writer,
                    command_id="rb-untested",
                    target_release_id=untested.release_id,
                    expected_active_deployment_id=second.deployment_id,
                    authorize_deploy=deployer,
                )
            with self.assertRaisesRegex(StrategyComparisonRefused, "self-activate"):
                rollback_strategy_deployment(
                    writer,
                    command_id="rb-self",
                    target_release_id=incumbent.release_id,
                    expected_active_deployment_id=second.deployment_id,
                    authorize_deploy=lambda _db, _cid, _op, _rid: self.author_id,
                )

            # Legitimate rollback restores previously deployed incumbent.
            restored = rollback_strategy_deployment(
                writer,
                command_id="dep-rb-3",
                target_release_id=incumbent.release_id,
                expected_active_deployment_id=second.deployment_id,
                authorize_deploy=deployer,
            )

            # Consumed PROMOTE comparison_id CANNOT be replayed to re-promote rolled-back challenger!
            with self.assertRaisesRegex(StrategyComparisonRefused, "already consumed"):
                activate_strategy_release(
                    writer,
                    command_id="dep-replay-cmp",
                    release_id=challenger.release_id,
                    binding_revision=self.binding.revision,
                    comparison_id="cmp-duty",
                    expected_active_deployment_id=restored.deployment_id,
                    authorize_deploy=deployer,
                )

        self.assertEqual(
            read_active_strategy_deployment(self.store, self.company), restored
        )
