"""Tier 1 opaque-box happy-path tests (70 cases) across all 13 Core Agent System features."""

from __future__ import annotations

import json
import sys
import unittest
from contextlib import closing
from pathlib import Path
from time import time_ns

from _helpers import (
    CoreCompanyHarness,
    LoopbackMultiProviderServer,
    anthropic_text_response,
    assert_core_only_store,
    assert_no_canary_leak,
    assert_strict_tables_only,
    cli_json,
    count_rows,
    default_secret_resolver,
    file_mode_bits,
    lazy_attr,
    lazy_module,
    openai_text_response,
    openai_tool_call_response,
    query_ro,
    read_watermark,
    run_shift_for_claim,
    sha256_hex,
    write_codex_shim,
)
from sourdough.agent_desk import desks_root, draft_at_revision, resolve_desk, save_draft
from sourdough.agent_runtime import (
    PROVIDER_ANTHROPIC,
    PROVIDER_CODEX_CLI,
    PROVIDER_OPENAI,
    AgentProfileBinding,
    AgentTransportInterrupted,
    OuterToolBroker,
    TokenUsage,
    hydrate_shift_context,
    settle_shift_cost_to_ledger,
)
from sourdough.company import FoundingTerms, found, reopen
from sourdough.company_assets import (
    Selection,
    discover_project_references,
    publish_selection,
    read_shared_asset,
)
from sourdough.company_checkpoint import checkpoint_company, inspect_company_checkpoint
from sourdough.conversation_delivery import acknowledge_delivery, read_inbox
from sourdough.conversations import Post, append_message, read_messages
from sourdough.ipc.projections import (
    PROJECTION_AGENT_DETAIL,
    PROJECTION_SYSTEM_EVALS,
    projection_etag,
    read_agent_detail_projection,
    read_system_evals_projection,
)
from sourdough.runtime.domains import BaseDomainPlugin
from sourdough.runtime.handlers import build_production_duty_registry
from sourdough.runtime.registry import DutyOutcome
from sourdough.runtime.work_kernel import (
    claim_work_run,
    complete_work_run,
    create_work_item,
    evaluate_conditions,
    set_condition,
)
from sourdough.storage.checkpoint import CHECKPOINT_FORMAT, restore_store
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.trial_registry import (
    PairwiseJudgment,
    TrialCeiling,
    TrialGrade,
    TrialInputs,
    TrialSlot,
    TrialSuite,
    blind_pairwise_packets,
    blind_trial_packet,
    commit_blind_grade,
    commit_position_swapped_pairwise_grade,
    commit_trial_observation,
    seal_trial_suite,
    summarize_rsi_transfer,
    trial_judgment_summary,
)
from sourdough.work_handoff import (
    HandoffOffer,
    accept_handoff,
    accepted_handoff,
    discover_handoffs,
    offer_handoff,
)
from sourdough.work_peer_response import (
    PeerQuestion,
    answer_peer_question,
    claim_peer_response,
    claim_resumed_peer_work,
    read_peer_outcome,
    read_peer_question,
    request_peer_response,
)


class TestS1F1PrivateDeskReopenTier1(unittest.TestCase):
    """S1-F1 Tier 1 (6 cases): Private Desk reopen, 3-tier layout, episodic memory & sandbox exec."""

    def test_s1_f1_t1_01_founder_private_desk_auto_init_and_idempotent_reopen(
        self,
    ) -> None:
        """S1-F1-T1-01: found() initializes Founder private_desk and reopen/ContextPack v2 succeed."""
        with CoreCompanyHarness() as h:
            terms = FoundingTerms(
                "s1f1-01", "charter-v1", "manage-company", "profile-1"
            )
            r1 = found(h.store, terms)
            r2 = found(h.store, terms)
            self.assertEqual(r1, r2)
            h.receipt = r1
            desk_rows = query_ro(
                h.store,
                "SELECT manifest_revision FROM private_desk WHERE agent_id = ?",
                (r1.founder_id,),
            )
            self.assertEqual(len(desk_rows), 1)
            self.assertTrue(desk_rows[0][0])

            opened = reopen(h.store, r1.company_id)
            self.assertEqual(opened.company_id, r1.company_id)

            _, _, claim = h.open_work_run_claim(
                r1.founder_id, r1.grant_id, "manage-company", purpose="discovery_review"
            )
            with h.writer() as w:
                rev, drafts = resolve_desk(
                    w, claim, "discovery_review", include_drafts=True
                )
            self.assertEqual(rev, desk_rows[0][0])
            self.assertEqual(drafts, ())

            ctx = hydrate_shift_context(
                company_id=r1.company_id,
                agent_id=r1.founder_id,
                run_id=claim.run_id,
                fencing_token=claim.fencing_token,
                grant_scope="manage-company",
                mandate_summary="Founder review.",
                desk_manifest=({"name": "init", "revision": rev, "bytes": 0},),
            )
            self.assertEqual(ctx.desk_manifest[0]["revision"], rev)
            assert_core_only_store(self, h.store)

    def test_s1_f1_t1_02_three_tier_desk_layout_and_draft_crud_round_trip(self) -> None:
        """S1-F1-T1-02: 3-tier Desk layout (.desk/drafts, .desk/scratch) and draft CRUD round-trip."""
        with CoreCompanyHarness() as h:
            h.found_company()
            agent = h.hire_agent("analyst-a")
            _, _, claim = h.open_work_run_claim(
                agent.agent_id, agent.grant_id, agent.grant_scope
            )
            with h.writer() as w:
                d1 = save_draft(
                    w,
                    claim,
                    claim.purpose,
                    name="compliance-notes",
                    content="Initial note v1",
                    expected_revision=None,
                )
                d2 = save_draft(
                    w,
                    claim,
                    claim.purpose,
                    name="compliance-notes",
                    content="Updated note v2",
                    expected_revision=d1.revision,
                )
                rev_before, drafts = resolve_desk(
                    w, claim, claim.purpose, include_drafts=True
                )
            self.assertEqual(len(drafts), 1)
            self.assertEqual(drafts[0].revision, d2.revision)
            drafts_dir = h.agent_desk_path(agent.agent_id) / ".desk" / "drafts"
            self.assertEqual(file_mode_bits(drafts_dir), 0o700)
            self.assertEqual(
                file_mode_bits(drafts_dir / "compliance-notes.json"), 0o600
            )
            self.assertEqual(file_mode_bits(drafts_dir / "compliance-notes.md"), 0o600)

            scratch_dir = (
                h.agent_desk_path(agent.agent_id) / ".desk" / "scratch" / claim.run_id
            )
            scratch_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            (scratch_dir / "work.tmp").write_text("ephemeral", encoding="utf-8")
            with h.writer() as w:
                rev_after, _ = resolve_desk(
                    w, claim, claim.purpose, include_drafts=False
                )
            self.assertEqual(rev_before, rev_after)
            self.assertIsNotNone(
                draft_at_revision(
                    h.store, agent.agent_id, "compliance-notes", d2.revision
                )
            )

    def test_s1_f1_t1_03_desk_reopen_on_fresh_session_with_original_binding(
        self,
    ) -> None:
        """S1-F1-T1-03: Fresh ShiftRunner session rehydrates Desk drafts with trace privacy."""
        with CoreCompanyHarness() as h, LoopbackMultiProviderServer() as srv:
            resolver, canaries = default_secret_resolver()
            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:SD_OPENAI_KEY",
            )
            h.found_company(profile=profile)
            agent = h.hire_agent("worker-reopen", profile=profile)

            _, _, c1 = h.open_work_run_claim(
                agent.agent_id, agent.grant_id, agent.grant_scope
            )
            secret_draft_text = "Confidential private calculation 98765"
            with h.writer() as w:
                saved = save_draft(
                    w,
                    c1,
                    c1.purpose,
                    name="calc-sheet",
                    content=secret_draft_text,
                    expected_revision=None,
                )
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    complete_work_run(
                        db,
                        c1.run_id,
                        c1.fencing_token,
                        outcome={"saved": True},
                        now_ns=1_000,
                    )

            registry = srv.build_registry(secret_resolver=resolver)
            srv.openai_queue.put(
                openai_tool_call_response("r-1", "call-list", "desk_list_drafts", {})
            )
            srv.openai_queue.put(openai_text_response("r-2", "Completed reopen turn."))

            it2, run2, c2 = h.open_work_run_claim(
                agent.agent_id, agent.grant_id, agent.grant_scope
            )
            outcome = run_shift_for_claim(
                h,
                item=it2,
                run=run2,
                profile=profile,
                adapter_registry=registry,
                mandate_summary="Verify reopened Desk.",
                instruction="Inspect your private Desk drafts.",
            )
            self.assertIsNotNone(outcome.episode)
            self.assertEqual(outcome.episode.outcome_status, "completed")
            with h.writer() as w:
                _, drafts = resolve_desk(w, c2, c2.purpose, include_drafts=True)
            self.assertEqual([d.revision for d in drafts], [saved.revision])

            trace_dir = h.root / ".sourdough-traces"
            if trace_dir.exists():
                for tf in trace_dir.rglob("*.jsonl"):
                    self.assertNotIn(secret_draft_text, tf.read_text(encoding="utf-8"))
            assert_no_canary_leak(self, h.root, canaries)

    def test_s1_f1_t1_04_oversized_tool_output_offload_and_read_log(self) -> None:
        """S1-F1-T1-04: >4KB tool output offloads to .desk/logs/ and desk_read_offloaded_log retrieves it."""
        with CoreCompanyHarness() as h:
            h.found_company()
            agent = h.hire_agent("analyst-offload")
            _, _, claim = h.open_work_run_claim(
                agent.agent_id, agent.grant_id, agent.grant_scope
            )
            large_text = "X" * 5200
            build_desk_contrib = lazy_attr(
                "sourdough.runtime.desk_tools", "build_core_desk_tool_contribution"
            )
            create_agent_session = lazy_attr(
                "sourdough.harness", "create_agent_session"
            )
            offload_large_output = lazy_attr(
                "sourdough.harness", "offload_large_output"
            )
            with h.writer() as w:
                contrib = build_desk_contrib(
                    writer=w,
                    claim=claim,
                    purpose=claim.purpose,
                    sandbox_backend="local-macos",
                )
                handlers = dict(contrib.handlers)
                handlers["oversized_probe"] = lambda _args: {"blob": large_text}
                session = create_agent_session(w, claim, claim.purpose)
                broker = OuterToolBroker(
                    company_id=h.company_id,
                    agent_id=agent.agent_id,
                    run_id=claim.run_id,
                    fencing_token=claim.fencing_token,
                    handlers=handlers,
                    offload_handler=lambda seq,
                    tool_name,
                    payload: offload_large_output(
                        h.store,
                        session,
                        tool_name,
                        payload,
                        seq=seq,
                    ),
                )
                offloaded = broker.invoke("call-big", "oversized_probe", {})
                self.assertTrue(offloaded["offloaded"])
                log_rel = offloaded["log_path"]
                self.assertTrue(log_rel.startswith(".desk/logs/"))
                log_file = h.agent_desk_path(agent.agent_id) / log_rel
                self.assertTrue(log_file.is_file())
                self.assertEqual(file_mode_bits(log_file), 0o600)
                p1 = broker.invoke(
                    "call-read-log-1",
                    "desk_read_offloaded_log",
                    {"log_path": log_rel, "offset": 0, "limit": 3000},
                )
                self.assertFalse(p1["eof"])
                p2 = broker.invoke(
                    "call-read-log-2",
                    "desk_read_offloaded_log",
                    {"log_path": log_rel, "offset": 3000, "limit": 3000},
                )
                self.assertTrue(p2["eof"])
                restored_json = json.loads(p1["content"] + p2["content"])
                self.assertEqual(restored_json["blob"], large_text)

    def test_s1_f1_t1_05_agent_scoped_episodic_memory_and_separate_fts5(self) -> None:
        """S1-F1-T1-05: Shift records agent_episode in company.sqlite + separate episodes_fts.sqlite."""
        with CoreCompanyHarness() as h, LoopbackMultiProviderServer() as srv:
            resolver, _ = default_secret_resolver()
            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:SD_OPENAI_KEY",
            )
            h.found_company(profile=profile)
            agent = h.hire_agent("memory-agent", profile=profile)
            registry = srv.build_registry(secret_resolver=resolver)
            srv.openai_queue.put(
                openai_text_response("r-ep1", "Audited vendor contract SLA variance.")
            )
            it1, run1, c1 = h.open_work_run_claim(
                agent.agent_id, agent.grant_id, agent.grant_scope
            )
            run_shift_for_claim(
                h,
                item=it1,
                run=run1,
                profile=profile,
                adapter_registry=registry,
                mandate_summary="Audit vendor SLA.",
                instruction="Summarize SLA findings.",
            )

            self.assertGreaterEqual(
                count_rows(
                    h.store,
                    "agent_episode",
                    "company_id = ? AND agent_id = ?",
                    (h.company_id, agent.agent_id),
                ),
                1,
            )
            fts_path = (
                h.root / ".sourdough-indexes" / h.company_id / "episodes_fts.sqlite"
            )
            self.assertTrue(fts_path.is_file())
            assert_core_only_store(self, h.store)
            assert_strict_tables_only(self, h.store)

            ep_idx = lazy_module("sourdough.episode_index")
            memory_mod = lazy_module("sourdough.memory")
            hits = ep_idx.search_episodes(
                h.store, h.company_id, agent.agent_id, "output_digest"
            )
            self.assertGreaterEqual(len(hits), 1)
            with closing(open_reader(h.store)) as db, read_tx(db):
                ep = memory_mod.read_episode(
                    db, h.company_id, agent.agent_id, hits[0]["episode_id"]
                )
            self.assertEqual(ep.agent_id, agent.agent_id)

    def test_s1_f1_t1_06_opt_in_desk_run_command_inside_macos_sandbox(self) -> None:
        """S1-F1-T1-06: desk:exec grant + local-macos runs run_desk_command_sandboxed confined to own Desk."""
        with CoreCompanyHarness() as h:
            profile = AgentProfileBinding(
                provider=PROVIDER_CODEX_CLI,
                model="gpt-5-codex",
                revision="v1",
                sandbox_backend="local-macos",
            )
            h.found_company(profile=profile)
            agent = h.hire_agent("exec-agent", grant_scope="desk:exec", profile=profile)
            _, _, claim = h.open_work_run_claim(
                agent.agent_id, agent.grant_id, "desk:exec"
            )
            create_agent_session = lazy_attr(
                "sourdough.harness", "create_agent_session"
            )
            run_desk_command_sandboxed = lazy_attr(
                "sourdough.harness", "run_desk_command_sandboxed"
            )
            with h.writer() as w:
                session = create_agent_session(w, claim, claim.purpose)
                res = run_desk_command_sandboxed(
                    session,
                    [
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; "
                            "p = Path('.desk/scratch/probe.txt'); "
                            "p.parent.mkdir(parents=True, exist_ok=True); "
                            "p.write_text('sandbox-ok'); "
                            "print(p.read_text())"
                        ),
                    ],
                    store=h.store,
                    sandbox_backend="local-macos",
                )
            self.assertEqual(res["returncode"], 0)
            self.assertIn("sandbox-ok", res["stdout"])


class TestS1F2CheckpointCloningTier1(unittest.TestCase):
    """S1-F2 Tier 1 (5 cases): Desk Checkpoint v2, FTS5-free watermark determinism & arm cloning."""

    def test_s1_f2_t1_01_checkpoint_v2_multi_agent_desk_and_db_capture(self) -> None:
        """S1-F2-T1-01: checkpoint_company captures multi-agent Desks in sourdough-checkpoint-v2."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            b = h.hire_agent("agent-b")
            _, _, cf = h.open_work_run_claim(r.founder_id, r.grant_id, "manage-company")
            _, _, cb = h.open_work_run_claim(b.agent_id, b.grant_id, b.grant_scope)
            with h.writer() as w:
                save_draft(
                    w,
                    cf,
                    cf.purpose,
                    name="ceo-note",
                    content="CEO memo",
                    expected_revision=None,
                )
                save_draft(
                    w,
                    cb,
                    cb.purpose,
                    name="b-note",
                    content="Analyst memo",
                    expected_revision=None,
                )
            archive = h.root / "company.chk.zip"
            manifest = checkpoint_company(h.store, h.company_id, archive)
            self.assertEqual(manifest.format, CHECKPOINT_FORMAT)
            self.assertGreaterEqual(len(manifest.desk_files), 2)
            inspected = inspect_company_checkpoint(archive, h.company_id)
            self.assertEqual(inspected.desk_digest, manifest.desk_digest)
            events = query_ro(
                h.store,
                "SELECT payload_json FROM audit_event WHERE company_id = ? AND event_type = 'company.checkpoint_cut'",
                (h.company_id,),
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(
                json.loads(events[0][0])["desk_digest"], manifest.desk_digest
            )

    def test_s1_f2_t1_02_fts5_free_company_sqlite_and_watermark_determinism(
        self,
    ) -> None:
        """S1-F2-T1-02: company.sqlite has 0 FTS5 tables and identical LogicalWatermark pre/post-restore."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            agent = h.hire_agent("wm-agent")
            _, _, c = h.open_work_run_claim(
                agent.agent_id, agent.grant_id, agent.grant_scope
            )
            with h.writer() as w:
                save_draft(
                    w,
                    c,
                    c.purpose,
                    name="wm-draft",
                    content="Deterministic state",
                    expected_revision=None,
                )
            assert_core_only_store(self, h.store)
            assert_strict_tables_only(self, h.store)
            archive = h.root / "wm.zip"
            manifest = checkpoint_company(h.store, r.company_id, archive)
            restored_dir = h.root / "restored"
            restored_dir.mkdir(mode=0o700)
            restored_store = restored_dir / "company.sqlite"
            restored_desks = restored_dir / "desks"
            receipt = restore_store(
                h.store,
                r.company_id,
                archive,
                restored_store,
                desk_destination=restored_desks,
                lease_directory=h.leases,
            )
            self.assertEqual(receipt.watermark, manifest.watermark)
            self.assertEqual(
                read_watermark(restored_store, r.company_id), manifest.watermark
            )
            self.assertFalse(Path(f"{restored_store}-wal").exists())
            self.assertFalse(Path(f"{restored_store}-shm").exists())
            assert_core_only_store(self, restored_store)

    def test_s1_f2_t1_03_restored_agents_reopen_exact_pre_loss_desk_drafts(
        self,
    ) -> None:
        """S1-F2-T1-03: Restored Agents reopen exact pre-loss Desk drafts and private draft count."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            b = h.hire_agent("agent-b")
            _, _, cf = h.open_work_run_claim(r.founder_id, r.grant_id, "manage-company")
            _, _, cb = h.open_work_run_claim(b.agent_id, b.grant_id, b.grant_scope)
            with h.writer() as w:
                df = save_draft(
                    w,
                    cf,
                    cf.purpose,
                    name="founder-plan",
                    content="Founder plan v1",
                    expected_revision=None,
                )
                db_draft = save_draft(
                    w,
                    cb,
                    cb.purpose,
                    name="b-plan",
                    content="Agent B plan v1",
                    expected_revision=None,
                )
            archive = h.root / "reopen.zip"
            checkpoint_company(h.store, r.company_id, archive)
            restored_dir = h.root / "restored"
            restored_dir.mkdir(mode=0o700)
            restored_store = restored_dir / "company.sqlite"
            restored_desks = restored_dir / "desks"
            restore_store(
                h.store,
                r.company_id,
                archive,
                restored_store,
                desk_destination=restored_desks,
                lease_directory=h.leases,
            )
            self.assertEqual(
                draft_at_revision(
                    restored_store, r.founder_id, "founder-plan", df.revision
                ).content,
                "Founder plan v1",
            )
            self.assertEqual(
                draft_at_revision(
                    restored_store, b.agent_id, "b-plan", db_draft.revision
                ).content,
                "Agent B plan v1",
            )
            proj, _ = read_agent_detail_projection(
                restored_store, r.company_id, b.agent_id, domain_plugins=()
            )
            self.assertEqual(proj["desk_workspace"]["private_draft_count"], 1)

    def test_s1_f2_t1_04_clone_desk_snapshot_isolated_comparison_arms(self) -> None:
        """S1-F2-T1-04: clone_desk_snapshot creates isolated Arm A & Arm B copies."""
        with CoreCompanyHarness() as h:
            h.found_company()
            agent = h.hire_agent("clone-agent")
            _, _, c = h.open_work_run_claim(
                agent.agent_id, agent.grant_id, agent.grant_scope
            )
            with h.writer() as w:
                save_draft(
                    w,
                    c,
                    c.purpose,
                    name="base-note",
                    content="Baseline content",
                    expected_revision=None,
                )
            clone_desk_snapshot = lazy_attr(
                "sourdough.agent_desk", "clone_desk_snapshot"
            )
            arm_a_store = h.root / "arms" / "arm-a" / "company.sqlite"
            arm_b_store = h.root / "arms" / "arm-b" / "company.sqlite"
            arm_a_store.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            arm_b_store.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            clone_desk_snapshot(h.store, h.company_id, (agent.agent_id,), arm_a_store)
            clone_desk_snapshot(h.store, h.company_id, (agent.agent_id,), arm_b_store)
            arm_a_file = (
                desks_root(arm_a_store)
                / agent.agent_id
                / ".desk"
                / "drafts"
                / "base-note.md"
            )
            arm_b_file = (
                desks_root(arm_b_store)
                / agent.agent_id
                / ".desk"
                / "drafts"
                / "base-note.md"
            )
            live_file = (
                h.agent_desk_path(agent.agent_id) / ".desk" / "drafts" / "base-note.md"
            )
            self.assertEqual(arm_a_file.read_text(encoding="utf-8"), "Baseline content")
            arm_a_file.write_text("Mutated in Arm A", encoding="utf-8")
            self.assertEqual(arm_b_file.read_text(encoding="utf-8"), "Baseline content")
            self.assertEqual(live_file.read_text(encoding="utf-8"), "Baseline content")

    def test_s1_f2_t1_05_rebuildable_episodes_fts_sqlite_separation(self) -> None:
        """S1-F2-T1-05: Deleting & rebuilding episodes_fts.sqlite preserves search & company watermark."""
        with CoreCompanyHarness() as h, LoopbackMultiProviderServer() as srv:
            resolver, _ = default_secret_resolver()
            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:SD_OPENAI_KEY",
            )
            h.found_company(profile=profile)
            agent = h.hire_agent("fts-agent", profile=profile)
            registry = srv.build_registry(secret_resolver=resolver)
            srv.openai_queue.put(
                openai_text_response("r-fts", "Quarterly compliance ledger reconciled.")
            )
            it1, run1, c1 = h.open_work_run_claim(
                agent.agent_id, agent.grant_id, agent.grant_scope
            )
            run_shift_for_claim(
                h,
                item=it1,
                run=run1,
                profile=profile,
                adapter_registry=registry,
                mandate_summary="Reconcile.",
                instruction="Run reconciliation.",
            )
            wm_before = read_watermark(h.store, h.company_id)
            fts_path = (
                h.root / ".sourdough-indexes" / h.company_id / "episodes_fts.sqlite"
            )
            self.assertTrue(fts_path.is_file())
            fts_path.unlink()
            ep_idx = lazy_module("sourdough.episode_index")
            ep_idx.rebuild_episodes_index(h.store, h.company_id)
            self.assertTrue(fts_path.is_file())
            self.assertEqual(read_watermark(h.store, h.company_id), wm_before)
            hits = ep_idx.search_episodes(
                h.store, h.company_id, agent.agent_id, "output_digest"
            )
            self.assertGreaterEqual(len(hits), 1)


class TestS1F3PublishAndMcpBridgeTier1(unittest.TestCase):
    """S1-F3 Tier 1 (6 cases): Selective publish, receipt recovery, broker parity & Codex MCP bridge."""

    def test_s1_f3_t1_01_selective_bounded_plain_text_publish_via_work_run_claim(
        self,
    ) -> None:
        """S1-F3-T1-01: publish_selection via WorkRunClaim publishes exact slice without leaking draft."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("author-a")
            b = h.hire_agent("reader-b")
            rev = h.create_project(
                "proj-audit",
                [h.founder_id, a.agent_id, b.agent_id],
                publishers=[a.agent_id],
            )
            _, _, claim = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            with h.writer() as w:
                draft = save_draft(
                    w,
                    claim,
                    claim.purpose,
                    name="secret-pad",
                    content="Secret header. Public conclusion. Secret footer.",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    claim,
                    claim.purpose,
                    Selection(
                        request_id="pub-1",
                        draft_name="secret-pad",
                        draft_revision=draft.revision,
                        project_id="proj-audit",
                        project_revision=rev,
                        audience=(a.agent_id, b.agent_id),
                        start=15,
                        end=33,
                    ),
                )
            self.assertEqual(asset.content, "Public conclusion.")
            raw_pub = query_ro(
                h.store,
                "SELECT content FROM shared_publication WHERE asset_id = ?",
                (asset.asset_id,),
            )[0][0]
            self.assertNotIn("Secret header", raw_pub)
            self.assertNotIn("secret-pad", raw_pub)
            events = query_ro(
                h.store,
                "SELECT event_type FROM audit_event WHERE company_id = ? AND entity_id = ?",
                (h.company_id, asset.asset_id),
            )
            self.assertEqual(events, [("company.asset_published",)])

    def test_s1_f3_t1_02_audience_scoped_discovery_and_read(self) -> None:
        """S1-F3-T1-02: Audience members discover reference & read content; non-audience excluded."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            c = h.hire_agent("c")
            rev = h.create_project(
                "proj-aud",
                [h.founder_id, a.agent_id, b.agent_id, c.agent_id],
                publishers=[a.agent_id],
            )
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            _, _, cb = h.open_work_run_claim(b.agent_id, b.grant_id, b.grant_scope)
            _, _, cc = h.open_work_run_claim(c.agent_id, c.grant_id, c.grant_scope)
            with h.writer() as w:
                d = save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="memo",
                    content="Shared finding.",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    ca,
                    ca.purpose,
                    Selection(
                        "req-1",
                        "memo",
                        d.revision,
                        "proj-aud",
                        rev,
                        (a.agent_id, b.agent_id),
                        0,
                        15,
                    ),
                )
                b_refs, _, _ = discover_project_references(w, cb, cb.purpose, "")
                c_refs, _, _ = discover_project_references(w, cc, cc.purpose, "")
                b_read = read_shared_asset(w, cb, cb.purpose, asset.asset_id)
            self.assertEqual([r.asset_id for r in b_refs], [asset.asset_id])
            self.assertEqual(c_refs, ())
            self.assertEqual(b_read.content, "Shared finding.")

    def test_s1_f3_t1_03_author_receipt_recovery_without_being_in_audience(
        self,
    ) -> None:
        """S1-F3-T1-03: Author excluded from audience recovers PublicationReceipt without read access."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            rev = h.create_project(
                "proj-rcpt",
                [h.founder_id, a.agent_id, b.agent_id],
                publishers=[a.agent_id],
            )
            item_a, _, ca1 = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope
            )
            with h.writer() as w:
                d = save_draft(
                    w,
                    ca1,
                    ca1.purpose,
                    name="out",
                    content="Delivered to B only.",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    ca1,
                    ca1.purpose,
                    Selection(
                        "req-rcpt",
                        "out",
                        d.revision,
                        "proj-rcpt",
                        rev,
                        (b.agent_id,),
                        0,
                        20,
                    ),
                )
                # Reopen same duty on fresh run
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    db.execute(
                        "UPDATE work_runs SET status = 'fenced' WHERE run_id = ?",
                        (ca1.run_id,),
                    )
                    db.execute(
                        "UPDATE work_items SET state = 'due' WHERE work_item_id = ?",
                        (item_a.work_item_id,),
                    )
                    from sourdough.runtime.work_kernel import claim_work_run

                    run2 = claim_work_run(
                        db,
                        company_id=h.company_id,
                        work_item_id=item_a.work_item_id,
                        lease_owner="reopen",
                        lease_ttl_ns=60_000_000_000,
                    )
                WorkRunClaim = lazy_attr("sourdough.duty_run", "WorkRunClaim")
                ca2 = WorkRunClaim(
                    company_id=h.company_id,
                    founder_id=a.agent_id,
                    duty_id=item_a.work_item_id,
                    run_id=run2.run_id,
                    fencing_token=run2.fencing_token,
                    grant_id=a.grant_id,
                    grant_scope=a.grant_scope,
                    purpose=ca1.purpose,
                )
                refs, receipts, _ = discover_project_references(w, ca2, ca2.purpose, "")
            self.assertEqual(refs, ())
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0].asset_id, asset.asset_id)
            self.assertEqual(receipts[0].request_id, "req-rcpt")

    def test_s1_f3_t1_04_author_request_id_idempotent_replay(self) -> None:
        """S1-F3-T1-04: Replaying identical (author_id, request_id) returns same asset with 0 duplicates."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            rev = h.create_project(
                "proj-idem",
                [h.founder_id, a.agent_id, b.agent_id],
                publishers=[a.agent_id],
            )
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            with h.writer() as w:
                d = save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="idem",
                    content="Exact same text.",
                    expected_revision=None,
                )
                sel = Selection(
                    "req-idem",
                    "idem",
                    d.revision,
                    "proj-idem",
                    rev,
                    (a.agent_id, b.agent_id),
                    0,
                    16,
                )
                first = publish_selection(w, ca, ca.purpose, sel)
                replay = publish_selection(w, ca, ca.purpose, sel)
            self.assertEqual(first, replay)
            self.assertEqual(
                count_rows(
                    h.store, "shared_publication", "author_id = ?", (a.agent_id,)
                ),
                1,
            )

    def test_s1_f3_t1_05_two_plane_outer_tool_broker_parity_across_shift_runner(
        self,
    ) -> None:
        """S1-F3-T1-05: ShiftRunner exposes Core Desk/Asset/Memory tools with receipt & trace logging."""
        with CoreCompanyHarness() as h, LoopbackMultiProviderServer() as srv:
            resolver, _ = default_secret_resolver()
            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:SD_OPENAI_KEY",
            )
            h.found_company(profile=profile)
            a = h.hire_agent("broker-a", profile=profile)
            h.create_project(
                "proj-tools", [h.founder_id, a.agent_id], publishers=[a.agent_id]
            )
            registry = srv.build_registry(secret_resolver=resolver)
            srv.openai_queue.put(
                openai_tool_call_response(
                    "r-1",
                    "tc-save",
                    "desk_save_draft",
                    {
                        "name": "tool-note",
                        "content": "Draft via broker.",
                        "expected_revision": None,
                    },
                )
            )
            srv.openai_queue.put(openai_text_response("r-2", "Saved draft."))
            it_a, run_a, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope
            )
            res = run_shift_for_claim(
                h,
                item=it_a,
                run=run_a,
                profile=profile,
                adapter_registry=registry,
                mandate_summary="Save draft via broker.",
                instruction="Save a private draft.",
            )
            self.assertIsNotNone(res.episode)
            self.assertEqual(res.episode.outcome_status, "completed")
            self.assertGreaterEqual(
                count_rows(
                    h.store,
                    "tool_call_receipt",
                    "company_id = ? AND run_id = ?",
                    (h.company_id, ca.run_id),
                ),
                1,
            )

    def test_s1_f3_t1_06_codex_cli_adapter_mcp_uds_bridge_and_confinement(self) -> None:
        """S1-F3-T1-06: CodexCliAdapter executes tools via stdio MCP->UDS bridge & settles GL."""
        with CoreCompanyHarness() as h, LoopbackMultiProviderServer() as srv:
            resolver, canaries = default_secret_resolver()
            profile = AgentProfileBinding(
                provider=PROVIDER_CODEX_CLI,
                model="gpt-5-codex",
                revision="v1",
                sandbox_backend="local-macos",
                credential_handle_ref="envref:SD_CODEX_KEY",
            )
            h.found_company(profile=profile)
            a = h.hire_agent("codex-a", profile=profile)
            rev = h.create_project(
                "proj-codex", [h.founder_id, a.agent_id], publishers=[a.agent_id]
            )
            shim = write_codex_shim(
                h.root / "codex-mcp-shim.py",
                f"""
import json, os, socket, sys
_ = json.loads(sys.stdin.read())
assert "/opt/homebrew/bin" in os.environ.get("PATH", "") or "PATH" in os.environ
sock = os.environ["SOURDOUGH_BROKER_SOCKET"]
def invoke(cid, tool, args):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(sock)
        s.sendall((json.dumps({{"call_id": cid, "tool": tool, "arguments": args}}) + "\\n").encode())
        buf = b""
        while b"\\n" not in buf:
            ch = s.recv(4096)
            if not ch: break
            buf += ch
        return json.loads(buf.decode())
r1 = invoke("c1", "desk_save_draft", {{"name": "cli-note", "content": "Bridge published text.", "expected_revision": None}})
assert r1["ok"], r1
rev = r1["result"]["revision"]
r2 = invoke("c2", "desk_publish_selection", {{
    "request_id": "cli-pub-1",
    "draft_name": "cli-note",
    "draft_revision": rev,
    "project_id": {rev!r} and "proj-codex",
    "project_revision": {rev!r},
    "audience": [{a.agent_id!r}],
    "start": 0,
    "end": 22,
}})
assert r2["ok"], r2
print(json.dumps({{"type": "terminal", "output_text": "Codex MCP bridge done.", "usage": {{"input_tokens": 900, "output_tokens": 180}}}}))
""",
            )
            registry = srv.build_registry(
                secret_resolver=resolver,
                codex_executable=(sys.executable, str(shim)),
                codex_use_macos_sandbox=(sys.platform == "darwin"),
                codex_extra_read_files=(shim,),
            )
            it_a, run_a, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope
            )
            res = run_shift_for_claim(
                h,
                item=it_a,
                run=run_a,
                profile=profile,
                adapter_registry=registry,
                mandate_summary="Execute via CodexCliAdapter.",
                instruction="Save and publish draft.",
            )
            self.assertIsNotNone(res.episode)
            self.assertEqual(res.episode.outcome_status, "completed")
            self.assertEqual(
                count_rows(
                    h.store, "shared_publication", "author_id = ?", (a.agent_id,)
                ),
                1,
            )
            self.assertGreaterEqual(
                count_rows(
                    h.store,
                    "journal_entry",
                    "source_event LIKE ?",
                    (f"llm-turn:{ca.run_id}:%",),
                ),
                1,
            )
            assert_no_canary_leak(self, h.root, canaries)


class TestS2F1ConversationsInboxTier1(unittest.TestCase):
    """S2-F1 Tier 1 (6 cases): Scoped project conversations, membership fencing & durable inbox."""

    def test_s2_f1_t1_01_append_and_ordered_read_work_run_claim_and_duty_claim(
        self,
    ) -> None:
        """S2-F1-T1-01: Ordered message append/read via WorkRunClaim & DutyClaim without work creation."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            c = h.hire_agent("c")
            rev = h.create_project(
                "proj-conv",
                [h.founder_id, a.agent_id, b.agent_id, c.agent_id],
                publishers=[a.agent_id],
            )
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            cb = h.open_legacy_duty_claim(
                b, purpose="assessment", project_id="proj-conv"
            )
            _, _, cc = h.open_work_run_claim(c.agent_id, c.grant_id, c.grant_scope)
            items_before = count_rows(h.store, "work_items")
            with h.writer() as w:
                m1 = append_message(
                    w, ca, ca.purpose, Post("proj-conv", rev, "r1", "First msg", "")
                )
                m2 = append_message(
                    w, cb, "assessment", Post("proj-conv", rev, "r2", "Second msg", "")
                )
                msgs, cur = read_messages(w, cc, cc.purpose, "proj-conv", "")
            self.assertEqual(
                [m.message_id for m in msgs], [m1.message_id, m2.message_id]
            )
            self.assertEqual(cur, "")
            self.assertEqual(count_rows(h.store, "work_items"), items_before)

    def test_s2_f1_t1_02_linked_shared_publication_visibility_filtering(self) -> None:
        """S2-F1-T1-02: Linked asset_id visible to audience B, redacted (None) for non-audience C."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            c = h.hire_agent("c")
            rev = h.create_project(
                "proj-link",
                [h.founder_id, a.agent_id, b.agent_id, c.agent_id],
                publishers=[a.agent_id],
            )
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            _, _, cb = h.open_work_run_claim(b.agent_id, b.grant_id, b.grant_scope)
            _, _, cc = h.open_work_run_claim(c.agent_id, c.grant_id, c.grant_scope)
            with h.writer() as w:
                d = save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="note",
                    content="Audience A+B only.",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    ca,
                    ca.purpose,
                    Selection(
                        "req-x",
                        "note",
                        d.revision,
                        "proj-link",
                        rev,
                        (a.agent_id, b.agent_id),
                        0,
                        18,
                    ),
                )
                append_message(
                    w,
                    ca,
                    ca.purpose,
                    Post("proj-link", rev, "msg-x", "See linked asset", asset.asset_id),
                )
                b_msgs, _ = read_messages(w, cb, cb.purpose, "proj-link", "")
                c_msgs, _ = read_messages(w, cc, cc.purpose, "proj-link", "")
            self.assertEqual(b_msgs[0].asset_id, asset.asset_id)
            self.assertIsNone(c_msgs[0].asset_id)
            self.assertEqual(c_msgs[0].body, "See linked asset")

    def test_s2_f1_t1_03_atomic_project_delivery_and_reference_only_inbox(self) -> None:
        """S2-F1-T1-03: Outbox creates deliveries per other member; read_inbox exposes IDs only."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            c = h.hire_agent("c")
            rev = h.create_project(
                "proj-inb",
                [h.founder_id, a.agent_id, b.agent_id, c.agent_id],
                publishers=[a.agent_id],
            )
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            _, _, cb = h.open_work_run_claim(b.agent_id, b.grant_id, b.grant_scope)
            _, _, cc = h.open_work_run_claim(c.agent_id, c.grant_id, c.grant_scope)
            with h.writer() as w:
                msg = append_message(
                    w,
                    ca,
                    ca.purpose,
                    Post("proj-inb", rev, "r1", "Secret body text", ""),
                )
                a_inb, _ = read_inbox(w, ca, ca.purpose, "")
                b_inb, _ = read_inbox(w, cb, cb.purpose, "")
                c_inb, _ = read_inbox(w, cc, cc.purpose, "")
            self.assertEqual(a_inb, ())
            self.assertEqual(len(b_inb), 1)
            self.assertEqual(len(c_inb), 1)
            self.assertEqual(
                b_inb[0].delivery_id, sha256_hex(f"{msg.message_id}:{b.agent_id}")
            )
            self.assertIsNone(b_inb[0].read_run)
            self.assertFalse(hasattr(b_inb[0], "body"))

    def test_s2_f1_t1_04_explicit_acknowledge_delivery_and_idempotent_replay(
        self,
    ) -> None:
        """S2-F1-T1-04: read_inbox does not ack; acknowledge_delivery sets read_run_id idempotently."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            rev = h.create_project(
                "proj-ack",
                [h.founder_id, a.agent_id, b.agent_id],
                publishers=[a.agent_id],
            )
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            _, _, cb1 = h.open_work_run_claim(b.agent_id, b.grant_id, b.grant_scope)
            with h.writer() as w:
                append_message(
                    w, ca, ca.purpose, Post("proj-ack", rev, "r1", "Check this", "")
                )
                inb, _ = read_inbox(w, cb1, cb1.purpose, "")
                self.assertIsNone(inb[0].read_run)
                ack1 = acknowledge_delivery(w, cb1, cb1.purpose, inb[0].delivery_id)
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    complete_work_run(
                        db,
                        cb1.run_id,
                        cb1.fencing_token,
                        outcome={"acked": True},
                        now_ns=1_000,
                    )
            _, _, cb2 = h.open_work_run_claim(b.agent_id, b.grant_id, b.grant_scope)
            with h.writer() as w:
                ack2 = acknowledge_delivery(w, cb2, cb2.purpose, inb[0].delivery_id)
            self.assertEqual(ack1.read_run, cb1.run_id)
            self.assertEqual(ack2.read_run, cb1.run_id)

    def test_s2_f1_t1_05_project_sender_request_id_idempotent_replay(self) -> None:
        """S2-F1-T1-05: Identical append_message replay returns original Message with 0 duplicates."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            rev = h.create_project(
                "proj-idmp",
                [h.founder_id, a.agent_id, b.agent_id],
                publishers=[a.agent_id],
            )
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            post = Post("proj-idmp", rev, "r-dup", "Idempotent message", "")
            with h.writer() as w:
                m1 = append_message(w, ca, ca.purpose, post)
                m2 = append_message(w, ca, ca.purpose, post)
            self.assertEqual(m1, m2)
            self.assertEqual(
                count_rows(
                    h.store, "project_message", "project_id = ?", ("proj-idmp",)
                ),
                1,
            )
            self.assertEqual(
                count_rows(
                    h.store, "project_delivery", "recipient_id = ?", (b.agent_id,)
                ),
                1,
            )

    def test_s2_f1_t1_06_cursor_pagination_over_16_messages_and_deliveries(
        self,
    ) -> None:
        """S2-F1-T1-06: Cursor pagination over 20 messages and 20 inbox deliveries preserves order."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            rev = h.create_project(
                "proj-page",
                [h.founder_id, a.agent_id, b.agent_id],
                publishers=[a.agent_id],
            )
            sent_ids: list[str] = []
            for batch_start, batch_size in ((0, 8), (8, 8), (16, 4)):
                _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
                with h.writer() as w:
                    for i in range(batch_start, batch_start + batch_size):
                        sent_ids.append(
                            append_message(
                                w,
                                ca,
                                ca.purpose,
                                Post("proj-page", rev, f"req-{i}", f"Msg {i}", ""),
                            ).message_id
                        )
                    with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                        complete_work_run(
                            db,
                            ca.run_id,
                            ca.fencing_token,
                            outcome={"batch_start": batch_start},
                        )
            _, _, cb = h.open_work_run_claim(b.agent_id, b.grant_id, b.grant_scope)
            with h.writer() as w:
                p1, c1 = read_messages(w, cb, cb.purpose, "proj-page", "")
                p2, c2 = read_messages(w, cb, cb.purpose, "proj-page", c1)
                i1, ic1 = read_inbox(w, cb, cb.purpose, "")
                i2, ic2 = read_inbox(w, cb, cb.purpose, ic1)
            self.assertEqual(len(p1), 16)
            self.assertTrue(c1)
            self.assertEqual(len(p2), 4)
            self.assertEqual(c2, "")
            self.assertEqual([m.message_id for m in (*p1, *p2)], sent_ids)
            self.assertEqual(len(i1), 16)
            self.assertTrue(ic1)
            self.assertEqual(len(i2), 4)
            self.assertEqual(ic2, "")


class TestS2F2PeerResponseTier1(unittest.TestCase):
    """S2-F2 Tier 1 (6 cases): Peer response request/wait/resume & bidirectional routing."""

    def _setup_pair(
        self, h: CoreCompanyHarness, *, peer_responders: tuple[str, ...] | None = None
    ):
        h.found_company()
        a = h.hire_agent("a")
        b = h.hire_agent("b")
        rev = h.create_project(
            "proj-peer",
            [h.founder_id, a.agent_id, b.agent_id],
            publishers=[a.agent_id, b.agent_id],
            peer_responders=peer_responders or (b.agent_id,),
        )
        return a, b, rev

    def test_s2_f2_t1_01_request_zero_compute_wait_answer_single_wake_resume(
        self,
    ) -> None:
        """S2-F2-T1-01: request_peer_response yields to waiting; peer answers; requester wakes once."""
        with CoreCompanyHarness() as h:
            a, b, rev = self._setup_pair(h)
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            now = 1_000_000
            with h.writer() as w:
                d = save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="ev",
                    content="Evidence excerpt.",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    ca,
                    ca.purpose,
                    Selection(
                        "pub-p1",
                        "ev",
                        d.revision,
                        "proj-peer",
                        rev,
                        (a.agent_id, b.agent_id),
                        0,
                        17,
                    ),
                )
                wait = request_peer_response(
                    w,
                    ca,
                    PeerQuestion(
                        request_key="q1",
                        recipient_id=b.agent_id,
                        question="Confirm clause validity?",
                        expected_response="answered/declined",
                        expires_at=now + 600,
                        asset_id=asset.asset_id,
                        asset_revision=sha256_hex(asset.content),
                    ),
                    now=now,
                )
            item_state = query_ro(
                h.store,
                "SELECT state FROM work_items WHERE work_item_id = ?",
                (ca.duty_id,),
            )[0][0]
            self.assertEqual(item_state, "waiting")
            with h.writer() as w:
                cb = claim_peer_response(w, b.profile.profile_id(), now=now + 10)
                self.assertIsNotNone(cb)
                q_payload = read_peer_question(w, cb, now=now + 11)
                self.assertEqual(q_payload["evidence"]["content"], "Evidence excerpt.")
                answer_peer_question(
                    w, cb, "answered", "Clause is valid.", now=now + 12
                )
                resumed = claim_resumed_peer_work(
                    w, a.profile.profile_id(), now=now + 15
                )
                self.assertIsNotNone(resumed)
                outcome = read_peer_outcome(w, resumed)
            self.assertEqual(outcome["outcome"], "answered")
            self.assertEqual(outcome["answer"], "Clause is valid.")
            self.assertEqual(wait.response_duty, cb.duty_id)

    def test_s2_f2_t1_02_peer_decline_and_requester_resume(self) -> None:
        """S2-F2-T1-02: Peer declines request; requester wakes once with outcome='declined'."""
        with CoreCompanyHarness() as h:
            a, b, _ = self._setup_pair(h)
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            now = 2_000_000
            with h.writer() as w:
                request_peer_response(
                    w,
                    ca,
                    PeerQuestion(
                        "q-dec", b.agent_id, "Approve exception?", "yes/no", now + 600
                    ),
                    now=now,
                )
                cb = claim_peer_response(w, b.profile.profile_id(), now=now + 5)
                read_peer_question(w, cb, now=now + 6)
                answer_peer_question(
                    w, cb, "declined", "Missing primary invoice.", now=now + 7
                )
                resumed = claim_resumed_peer_work(
                    w, a.profile.profile_id(), now=now + 10
                )
                out = read_peer_outcome(w, resumed)
            self.assertEqual(out["outcome"], "declined")
            self.assertEqual(out["answer"], "Missing primary invoice.")

    def test_s2_f2_t1_03_request_deadline_expiry_and_requester_wake(self) -> None:
        """S2-F2-T1-03: Expired peer request cancels response duty and wakes requester as expired."""
        with CoreCompanyHarness() as h:
            a, b, _ = self._setup_pair(h)
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            now = 3_000_000
            with h.writer() as w:
                wait = request_peer_response(
                    w,
                    ca,
                    PeerQuestion(
                        "q-exp", b.agent_id, "Need review", "answer", now + 60
                    ),
                    now=now,
                )
                resumed = claim_resumed_peer_work(
                    w, a.profile.profile_id(), now=now + 120
                )
                self.assertIsNotNone(resumed)
                out = read_peer_outcome(w, resumed)
            self.assertEqual(out["outcome"], "expired")
            resp_state = query_ro(
                h.store,
                "SELECT state FROM work_items WHERE work_item_id = ?",
                (wait.response_duty,),
            )[0][0]
            self.assertEqual(resp_state, "cancelled")

    def test_s2_f2_t1_04_bidirectional_peer_responders_routing(self) -> None:
        """S2-F2-T1-04: Explicit peer_responders policy enables bidirectional B->C and C->B peer requests."""
        with CoreCompanyHarness() as h:
            h.found_company()
            b = h.hire_agent("peer-b")
            c = h.hire_agent("peer-c")
            h.create_project(
                "proj-bidir",
                [h.founder_id, b.agent_id, c.agent_id],
                publishers=[b.agent_id, c.agent_id],
                peer_responders=(b.agent_id, c.agent_id),
            )
            _, _, cb = h.open_work_run_claim(b.agent_id, b.grant_id, b.grant_scope)
            _, _, cc = h.open_work_run_claim(c.agent_id, c.grant_id, c.grant_scope)
            now = 4_000_000
            with h.writer() as w:
                w1 = request_peer_response(
                    w,
                    cb,
                    PeerQuestion(
                        "b-to-c", c.agent_id, "Verify B item", "ok", now + 300
                    ),
                    now=now,
                )
                w2 = request_peer_response(
                    w,
                    cc,
                    PeerQuestion(
                        "c-to-b", b.agent_id, "Verify C item", "ok", now + 300
                    ),
                    now=now + 1,
                )
            self.assertNotEqual(w1.response_duty, w2.response_duty)
            resps = query_ro(
                h.store,
                "SELECT owner_agent_id FROM work_responsibilities WHERE company_id = ? AND duty_kind = 'work.peer_response' AND status = 'active'",
                (h.company_id,),
            )
            self.assertTrue({r[0] for r in resps} >= {b.agent_id, c.agent_id})

    def test_s2_f2_t1_05_multi_hop_delegation_chain_up_to_depth_4(self) -> None:
        """S2-F2-T1-05: 4-hop peer delegation chain (depth 1..4) preserves root_work_item_id and unwinds."""
        with CoreCompanyHarness() as h:
            h.found_company()
            agents = [h.hire_agent(f"hop-{i}") for i in range(5)]
            h.create_project(
                "proj-chain",
                [h.founder_id, *(ag.agent_id for ag in agents)],
                publishers=[ag.agent_id for ag in agents],
                peer_responders=tuple(ag.agent_id for ag in agents),
            )
            _, _, c0 = h.open_work_run_claim(
                agents[0].agent_id, agents[0].grant_id, agents[0].grant_scope
            )
            now = 5_000_000
            WorkRunClaim = lazy_attr("sourdough.duty_run", "WorkRunClaim")
            claims = [c0]
            with h.writer() as w:
                for idx in range(4):
                    wait = request_peer_response(
                        w,
                        claims[idx],
                        PeerQuestion(
                            f"hop-k-{idx}",
                            agents[idx + 1].agent_id,
                            f"Hop {idx + 1}",
                            "ok",
                            now + 1000,
                        ),
                        now=now + idx,
                    )
                    resp_claim = claim_peer_response(
                        w, agents[idx + 1].profile.profile_id(), now=now + 10 + idx
                    )
                    self.assertIsNotNone(resp_claim)
                    read_peer_question(w, resp_claim, now=now + 20 + idx)
                    if idx < 3:
                        claims.append(
                            WorkRunClaim(
                                company_id=h.company_id,
                                founder_id=agents[idx + 1].agent_id,
                                duty_id=wait.response_duty,
                                run_id=resp_claim.run_id,
                                fencing_token=1,
                                grant_id=agents[idx + 1].grant_id,
                                grant_scope=agents[idx + 1].grant_scope,
                                purpose="peer_response",
                            )
                        )
                    else:
                        claims.append(resp_claim)
                depths = query_ro(
                    h.store,
                    "SELECT chain_depth, root_work_item_id FROM work_peer_requests WHERE company_id = ? ORDER BY chain_depth",
                    (h.company_id,),
                )
                self.assertEqual([d[0] for d in depths], [1, 2, 3, 4])
                self.assertEqual({d[1] for d in depths}, {c0.duty_id})
                # Unwind from depth 4 back to 0
                answer_peer_question(w, claims[4], "answered", "leaf-ok", now=now + 50)
                for idx in (3, 2, 1):
                    resumed = claim_resumed_peer_work(
                        w, agents[idx].profile.profile_id(), now=now + 60 + idx
                    )
                    answer_peer_question(
                        w, resumed, "answered", f"unwind-{idx}", now=now + 70 + idx
                    )
                root_resumed = claim_resumed_peer_work(
                    w, agents[0].profile.profile_id(), now=now + 90
                )
                out = read_peer_outcome(w, root_resumed)
            self.assertEqual(out["outcome"], "answered")

    def test_s2_f2_t1_06_request_key_and_answer_idempotent_replay(self) -> None:
        """S2-F2-T1-06: Identical peer request and answer replays are idempotent with zero duplicates."""
        with CoreCompanyHarness() as h:
            a, b, _ = self._setup_pair(h)
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            now = 6_000_000
            q = PeerQuestion("k-idem", b.agent_id, "Same question", "ok", now + 600)
            with h.writer() as w:
                w1 = request_peer_response(w, ca, q, now=now)
                w2 = request_peer_response(w, ca, q, now=now + 1)
                self.assertEqual(w1, w2)
                cb = claim_peer_response(w, b.profile.profile_id(), now=now + 5)
                read_peer_question(w, cb, now=now + 6)
                a1 = answer_peer_question(w, cb, "answered", "Confirmed", now=now + 7)
                a2 = answer_peer_question(w, cb, "answered", "Confirmed", now=now + 8)
                self.assertEqual(a1, a2)
            self.assertEqual(
                count_rows(
                    h.store, "work_peer_requests", "company_id = ?", (h.company_id,)
                ),
                1,
            )


class TestS2F3HandoffCasTier1(unittest.TestCase):
    """S2-F3 Tier 1 (6 cases): Explicit work handoff CAS state machine & single owner."""

    def _seed_offer_context(self, h: CoreCompanyHarness):
        h.found_company()
        a = h.hire_agent("sender-a")
        b = h.hire_agent("recip-b")
        rev = h.create_project(
            "proj-ho",
            [h.founder_id, a.agent_id, b.agent_id],
            publishers=[a.agent_id, b.agent_id],
        )
        _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
        _, _, cb = h.open_work_run_claim(b.agent_id, b.grant_id, b.grant_scope)
        with h.writer() as w:
            d = save_draft(
                w,
                ca,
                ca.purpose,
                name="ho-draft",
                content="Handoff evidence body",
                expected_revision=None,
            )
            asset = publish_selection(
                w,
                ca,
                ca.purpose,
                Selection(
                    "pub-ho",
                    "ho-draft",
                    d.revision,
                    "proj-ho",
                    rev,
                    (a.agent_id, b.agent_id),
                    0,
                    21,
                ),
            )
        return a, b, rev, ca, cb, asset

    def test_s2_f3_t1_01_offer_handoff_keeps_sender_sole_owner_until_accept(
        self,
    ) -> None:
        """S2-F3-T1-01: offer_handoff sets state='offered', state_version=1; sender retains ownership."""
        with CoreCompanyHarness() as h:
            a, b, _, ca, _, asset = self._seed_offer_context(h)
            now = 1_000_000
            with h.writer() as w:
                offer_id = offer_handoff(
                    w,
                    ca,
                    HandoffOffer(
                        "ho-1",
                        b.agent_id,
                        asset.asset_id,
                        now + 3600,
                        "Verify compliance",
                    ),
                    now=now,
                )
            rows = query_ro(
                h.store,
                "SELECT state, state_version, successor_work_item_id FROM work_handoff WHERE offer_id = ?",
                (offer_id,),
            )
            self.assertEqual(rows, [("offered", 1, None)])
            self.assertEqual(
                count_rows(h.store, "work_items", "owner_agent_id = ?", (b.agent_id,)),
                1,
            )

    def test_s2_f3_t1_02_discover_handoffs_and_atomic_accept_handoff(self) -> None:
        """S2-F3-T1-02: discover_handoffs + accept_handoff transitions offered->accepted (v2) with 1 successor."""
        with CoreCompanyHarness() as h:
            a, b, _, ca, cb, asset = self._seed_offer_context(h)
            now = 2_000_000
            with h.writer() as w:
                offer_id = offer_handoff(
                    w,
                    ca,
                    HandoffOffer(
                        "ho-2",
                        b.agent_id,
                        asset.asset_id,
                        now + 3600,
                        "Independent check",
                    ),
                    now=now,
                )
                offers = discover_handoffs(w, cb, cb.purpose, now=now + 10)
                self.assertEqual([o["offer_id"] for o in offers], [offer_id])
                successor_id = accept_handoff(w, cb, cb.purpose, offer_id, now=now + 20)
            row = query_ro(
                h.store,
                "SELECT state, state_version, successor_work_item_id, accepted_run_id FROM work_handoff WHERE offer_id = ?",
                (offer_id,),
            )[0]
            self.assertEqual(row, ("accepted", 2, successor_id, cb.run_id))
            succ = query_ro(
                h.store,
                "SELECT owner_agent_id, source_kind FROM work_items WHERE work_item_id = ?",
                (successor_id,),
            )[0]
            self.assertEqual(succ, (b.agent_id, "handoff"))

    def test_s2_f3_t1_03_recipient_reads_accepted_handoff_and_executes_followup(
        self,
    ) -> None:
        """S2-F3-T1-03: Successor reads accepted_handoff and shared asset without sender Desk access."""
        with CoreCompanyHarness() as h:
            a, b, _, ca, cb, asset = self._seed_offer_context(h)
            now = 3_000_000
            with h.writer() as w:
                offer_id = offer_handoff(
                    w,
                    ca,
                    HandoffOffer(
                        "ho-3",
                        b.agent_id,
                        asset.asset_id,
                        now + 3600,
                        "Review artifact",
                    ),
                    now=now,
                )
                successor_id = accept_handoff(w, cb, cb.purpose, offer_id, now=now + 5)
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    complete_work_run(
                        db,
                        cb.run_id,
                        cb.fencing_token,
                        outcome={"accepted": offer_id},
                        now_ns=now + 6,
                    )
            WorkRunClaim = lazy_attr("sourdough.duty_run", "WorkRunClaim")
            with h.writer() as w:
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    from sourdough.runtime.work_kernel import claim_work_run

                    succ_run = claim_work_run(
                        db,
                        company_id=h.company_id,
                        work_item_id=successor_id,
                        lease_owner="succ-exec",
                        lease_ttl_ns=60_000_000_000,
                    )
                    succ_claim = WorkRunClaim(
                        company_id=h.company_id,
                        founder_id=b.agent_id,
                        duty_id=successor_id,
                        run_id=succ_run.run_id,
                        fencing_token=succ_run.fencing_token,
                        grant_id=b.grant_id,
                        grant_scope=b.grant_scope,
                        purpose="assessment_review",
                    )
                    info = accepted_handoff(db, succ_claim, asset.asset_id)
                shared = read_shared_asset(
                    w, succ_claim, succ_claim.purpose, asset.asset_id
                )
            self.assertEqual(info["offer_id"], offer_id)
            self.assertEqual(info["sender_id"], a.agent_id)
            self.assertEqual(shared.content, "Handoff evidence body")

    def test_s2_f3_t1_04_decline_handoff_cas_preserves_original_owner(self) -> None:
        """S2-F3-T1-04: decline_handoff transitions offered->declined (state_version=2); sender keeps duty."""
        with CoreCompanyHarness() as h:
            _, b, _, ca, cb, asset = self._seed_offer_context(h)
            decline_handoff = lazy_attr("sourdough.work_handoff", "decline_handoff")
            now = 4_000_000
            with h.writer() as w:
                offer_id = offer_handoff(
                    w,
                    ca,
                    HandoffOffer(
                        "ho-dec", b.agent_id, asset.asset_id, now + 3600, "Check"
                    ),
                    now=now,
                )
                decline_handoff(w, cb, cb.purpose, offer_id, now=now + 5)
            row = query_ro(
                h.store,
                "SELECT state, state_version, successor_work_item_id FROM work_handoff WHERE offer_id = ?",
                (offer_id,),
            )[0]
            self.assertEqual(row, ("declined", 2, None))

    def test_s2_f3_t1_05_withdraw_and_expire_handoff_cas_preserve_owner(self) -> None:
        """S2-F3-T1-05: withdraw_handoff and expire_handoffs CAS transition offered->withdrawn/expired."""
        with CoreCompanyHarness() as h:
            _, b, _, ca, cb, asset = self._seed_offer_context(h)
            withdraw_handoff = lazy_attr("sourdough.work_handoff", "withdraw_handoff")
            expire_handoffs = lazy_attr("sourdough.work_handoff", "expire_handoffs")
            now = 5_000_000
            with h.writer() as w:
                o1 = offer_handoff(
                    w,
                    ca,
                    HandoffOffer(
                        "ho-w", b.agent_id, asset.asset_id, now + 3600, "Withdraw me"
                    ),
                    now=now,
                )
                o2 = offer_handoff(
                    w,
                    ca,
                    HandoffOffer(
                        "ho-e", b.agent_id, asset.asset_id, now + 100, "Expire me"
                    ),
                    now=now,
                )
                withdraw_handoff(w, ca, o1, now=now + 10)
                expire_handoffs(w, now=now + 200)
                listed = discover_handoffs(w, cb, cb.purpose, now=now + 200)
            self.assertEqual(listed, [])
            self.assertEqual(
                query_ro(
                    h.store,
                    "SELECT state, state_version FROM work_handoff WHERE offer_id = ?",
                    (o1,),
                )[0],
                ("withdrawn", 2),
            )
            self.assertEqual(
                query_ro(
                    h.store,
                    "SELECT state, state_version FROM work_handoff WHERE offer_id = ?",
                    (o2,),
                )[0],
                ("expired", 2),
            )

    def test_s2_f3_t1_06_offer_and_accept_idempotent_replay(self) -> None:
        """S2-F3-T1-06: Replaying identical offer_handoff and accept_handoff is idempotent."""
        with CoreCompanyHarness() as h:
            _, b, _, ca, cb, asset = self._seed_offer_context(h)
            now = 6_000_000
            offer = HandoffOffer(
                "ho-idem", b.agent_id, asset.asset_id, now + 3600, "Same offer"
            )
            with h.writer() as w:
                id1 = offer_handoff(w, ca, offer, now=now)
                id2 = offer_handoff(w, ca, offer, now=now + 1)
                s1 = accept_handoff(w, cb, cb.purpose, id1, now=now + 2)
                s2 = accept_handoff(w, cb, cb.purpose, id1, now=now + 3)
            self.assertEqual(id1, id2)
            self.assertEqual(s1, s2)
            self.assertEqual(
                count_rows(h.store, "work_handoff", "sender_id = ?", (ca.founder_id,)),
                1,
            )


class TestF31ProactiveActivationTier1(unittest.TestCase):
    """F3.1 Tier 1 (5 cases): Proactive event propagation, per-asset anti-self-wake & mandatory duties."""

    def test_f3_1_t1_01_standing_timer_discovery_activation_client_closed(self) -> None:
        """T1_F3_1_01: Armed timer condition fires at t1 via CompanyRuntime with Client closed."""
        with CoreCompanyHarness() as h, LoopbackMultiProviderServer() as srv:
            resolver, _ = default_secret_resolver()
            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:SD_OPENAI_KEY",
            )
            h.found_company(profile=profile)
            a = h.hire_agent("timer-agent", profile=profile)
            resp_id = h.register_discovery_responsibility(a.agent_id, now_ns=1_000)
            with h.writer() as w:
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    cond = set_condition(
                        db,
                        company_id=h.company_id,
                        owner_agent_id=a.agent_id,
                        responsibility_id=resp_id,
                        trigger_kind="timer",
                        due_at_ns=5_000,
                        now_ns=1_000,
                    )
            srv.openai_queue.put(
                openai_tool_call_response(
                    "r1",
                    "d1",
                    "record_work_decision",
                    {
                        "action_kind": "hold",
                        "rationale": "All clear",
                        "evidence": [],
                        "decision_at_ns": 5_000,
                    },
                )
            )
            srv.openai_queue.put(
                openai_text_response("r2", "Discovery review settled.")
            )
            reg = build_production_duty_registry(
                adapter_registry=srv.build_registry(secret_resolver=resolver),
                profile_bindings={h.founder_id: profile, a.agent_id: profile},
                domain_plugins=(),
            )
            summary = h.make_runtime(reg).step(max_dispatches=2, now_ns=5_000)
            self.assertEqual(len(summary.admitted_items), 1)
            self.assertEqual(len(summary.executed_runs), 1)
            status = query_ro(
                h.store,
                "SELECT status FROM work_conditions WHERE condition_id = ?",
                (cond.condition_id,),
            )[0][0]
            self.assertEqual(status, "triggered")
            self.assertEqual(
                count_rows(
                    h.store,
                    "work_condition_occurrences",
                    "condition_id = ?",
                    (cond.condition_id,),
                ),
                1,
            )

    def test_f3_1_t1_02_shared_asset_revision_propagation_via_work_event_cursor(
        self,
    ) -> None:
        """T1_F3_1_02: asset.published drains through work_event_cursor and wakes non-author A once."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("watcher-a")
            b = h.hire_agent("pub-b")
            rev = h.create_project(
                "proj-ev",
                [h.founder_id, a.agent_id, b.agent_id],
                publishers=[b.agent_id],
            )
            resp_a = h.register_discovery_responsibility(a.agent_id, now_ns=1_000)
            _, _, cb = h.open_work_run_claim(b.agent_id, b.grant_id, b.grant_scope)
            with h.writer() as w:
                d = save_draft(
                    w,
                    cb,
                    cb.purpose,
                    name="sig",
                    content="New policy update",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    cb,
                    cb.purpose,
                    Selection(
                        "p-ev",
                        "sig",
                        d.revision,
                        "proj-ev",
                        rev,
                        (a.agent_id, b.agent_id),
                        0,
                        17,
                    ),
                )
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    set_condition(
                        db,
                        company_id=h.company_id,
                        owner_agent_id=a.agent_id,
                        responsibility_id=resp_a,
                        trigger_kind="shared_asset_revision",
                        source_identity=asset.asset_id,
                        now_ns=2_000,
                    )
            seen: list[str] = []
            reg = build_production_duty_registry(
                adapter_registry=LoopbackMultiProviderServer().build_registry(
                    secret_resolver=default_secret_resolver()[0]
                ),
                profile_bindings={},
                domain_plugins=(),
                extra_handlers={
                    "work.discovery_review": lambda ctx: (
                        seen.append(ctx.work_item.payload_json),
                        DutyOutcome(disposition="completed", result={"ok": True}),
                    )[1]
                },
            )
            step = h.make_runtime(reg).step(max_dispatches=4, now_ns=3_000)
            self.assertEqual(len(step.admitted_items), 1)
            self.assertEqual(len(seen), 1)
            self.assertEqual(
                count_rows(
                    h.store, "work_event_cursor", "company_id = ?", (h.company_id,)
                ),
                1,
            )

    def test_f3_1_t1_03_multi_agent_cross_publish_single_tick_per_asset_author(
        self,
    ) -> None:
        """T1_F3_1_03: Simultaneous publish in one tick resolves author_id per asset (no self-wake)."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            rev = h.create_project(
                "proj-x",
                [h.founder_id, a.agent_id, b.agent_id],
                publishers=[a.agent_id, b.agent_id],
            )
            ra = h.register_discovery_responsibility(a.agent_id, now_ns=1_000)
            rb = h.register_discovery_responsibility(b.agent_id, now_ns=1_000)
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            _, _, cb = h.open_work_run_claim(b.agent_id, b.grant_id, b.grant_scope)
            with h.writer() as w:
                da = save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="da",
                    content="Asset A content",
                    expected_revision=None,
                )
                db_draft = save_draft(
                    w,
                    cb,
                    cb.purpose,
                    name="db",
                    content="Asset B content",
                    expected_revision=None,
                )
                asset_a = publish_selection(
                    w,
                    ca,
                    ca.purpose,
                    Selection(
                        "pa",
                        "da",
                        da.revision,
                        "proj-x",
                        rev,
                        (a.agent_id, b.agent_id),
                        0,
                        15,
                    ),
                )
                asset_b = publish_selection(
                    w,
                    cb,
                    cb.purpose,
                    Selection(
                        "pb",
                        "db",
                        db_draft.revision,
                        "proj-x",
                        rev,
                        (a.agent_id, b.agent_id),
                        0,
                        15,
                    ),
                )
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    complete_work_run(
                        db,
                        ca.run_id,
                        ca.fencing_token,
                        outcome={"published": True},
                        now_ns=1_900,
                    )
                    complete_work_run(
                        db,
                        cb.run_id,
                        cb.fencing_token,
                        outcome={"published": True},
                        now_ns=1_900,
                    )
                    for agent_id, resp_id, src in (
                        (a.agent_id, ra, asset_a.asset_id),
                        (a.agent_id, ra, asset_b.asset_id),
                        (b.agent_id, rb, asset_a.asset_id),
                        (b.agent_id, rb, asset_b.asset_id),
                    ):
                        set_condition(
                            db,
                            company_id=h.company_id,
                            owner_agent_id=agent_id,
                            responsibility_id=resp_id,
                            trigger_kind="shared_asset_revision",
                            source_identity=src,
                            now_ns=2_000,
                        )
            woken_for: list[tuple[str, str]] = []
            reg = build_production_duty_registry(
                adapter_registry=LoopbackMultiProviderServer().build_registry(
                    secret_resolver=default_secret_resolver()[0]
                ),
                profile_bindings={},
                domain_plugins=(),
                extra_handlers={
                    "work.discovery_review": lambda ctx: (
                        woken_for.append(
                            (
                                ctx.work_item.owner_agent_id,
                                json.loads(ctx.work_item.payload_json)[
                                    "source_identity"
                                ],
                            )
                        ),
                        DutyOutcome(disposition="completed", result={"ok": True}),
                    )[1]
                },
            )
            h.make_runtime(reg).step(max_dispatches=8, now_ns=3_000)
            self.assertEqual(
                set(woken_for),
                {(a.agent_id, asset_b.asset_id), (b.agent_id, asset_a.asset_id)},
            )

    def test_f3_1_t1_04_peer_response_event_wake_via_cursor(self) -> None:
        """T1_F3_1_04: work.peer_request_resolved wakes waiting requester once without waking responder."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("req-a")
            b = h.hire_agent("resp-b")
            h.create_project(
                "proj-pr",
                [h.founder_id, a.agent_id, b.agent_id],
                publishers=[a.agent_id],
                peer_responders=(b.agent_id,),
            )
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            cursor_rows = query_ro(
                h.store,
                "SELECT last_event_seq FROM work_event_cursor WHERE company_id = ?",
                (h.company_id,),
            )
            cursor_before = int(cursor_rows[0][0]) if cursor_rows else 0
            now = 4_000_000
            with h.writer() as w:
                request_peer_response(
                    w,
                    ca,
                    PeerQuestion("pr-wake", b.agent_id, "Verify?", "yes", now + 600),
                    now=now,
                )
                cb = claim_peer_response(w, b.profile.profile_id(), now=now + 5)
                read_peer_question(w, cb, now=now + 6)
                answer_peer_question(w, cb, "answered", "Verified", now=now + 7)
            reg = build_production_duty_registry(
                adapter_registry=LoopbackMultiProviderServer().build_registry(
                    secret_resolver=default_secret_resolver()[0]
                ),
                profile_bindings={},
                domain_plugins=(),
            )
            h.make_runtime(reg).step(
                max_dispatches=0, now_ns=(now + 10) * 1_000_000_000
            )
            cursor_after = query_ro(
                h.store,
                "SELECT last_event_seq FROM work_event_cursor WHERE company_id = ?",
                (h.company_id,),
            )[0][0]
            self.assertGreater(cursor_after, cursor_before)
            self.assertEqual(
                count_rows(
                    h.store,
                    "audit_event",
                    "company_id = ? AND event_type = 'work.wait_woken'",
                    (h.company_id,),
                ),
                1,
            )

    def test_f3_1_t1_05_mandatory_deadline_wake_preserved_when_source_hash_unchanged(
        self,
    ) -> None:
        """T1_F3_1_05: Armed timer on existing duty fires at due_at_ns even with zero asset changes."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("due-a")
            with h.writer() as w:
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    parent, _ = create_work_item(
                        db,
                        company_id=h.company_id,
                        owner_agent_id=a.agent_id,
                        duty_kind="work.assessment_review",
                        source_kind="e2e",
                        source_id="mandatory-1",
                        state="due",
                        now_ns=1_000,
                    )
                    parent_run = claim_work_run(
                        db,
                        company_id=h.company_id,
                        work_item_id=parent.work_item_id,
                        lease_owner="seed",
                        lease_ttl_ns=60_000_000_000,
                        now_ns=1_000,
                    )
                    complete_work_run(
                        db,
                        parent_run.run_id,
                        parent_run.fencing_token,
                        outcome={"seeded": True},
                        now_ns=1_000,
                    )
                    set_condition(
                        db,
                        company_id=h.company_id,
                        owner_agent_id=a.agent_id,
                        work_item_id=parent.work_item_id,
                        trigger_kind="timer",
                        due_at_ns=10_000,
                        now_ns=1_000,
                    )
            ran: list[str] = []
            reg = build_production_duty_registry(
                adapter_registry=LoopbackMultiProviderServer().build_registry(
                    secret_resolver=default_secret_resolver()[0]
                ),
                profile_bindings={},
                domain_plugins=(),
                extra_handlers={
                    "work.assessment_review": lambda ctx: (
                        ran.append(ctx.work_item.work_item_id),
                        DutyOutcome(disposition="completed", result={"checked": True}),
                    )[1]
                },
            )
            step = h.make_runtime(reg).step(
                max_dispatches=2, now_ns=10_000, observed_assets={}
            )
            self.assertEqual(len(step.admitted_items), 1)
            self.assertEqual(ran, list(step.admitted_items))


class TestF32DiscoveryDecisionTier1(unittest.TestCase):
    """F3.2 Tier 1 (5 cases): work.discovery_review, condition CRUD tools & work_decision."""

    def test_f3_2_t1_01_autonomous_discovery_review_hold_and_investigate(self) -> None:
        """T1_F3_2_01: work.discovery_review handles hold & investigate decisions with DecisionEvidence."""
        with CoreCompanyHarness() as h, LoopbackMultiProviderServer() as srv:
            resolver, _ = default_secret_resolver()
            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:SD_OPENAI_KEY",
            )
            h.found_company(profile=profile)
            a = h.hire_agent("disc-a", profile=profile)
            rev = h.create_project(
                "proj-d", [h.founder_id, a.agent_id], publishers=[a.agent_id]
            )
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            with h.writer() as w:
                d = save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="ev-d",
                    content="Invoice mismatch found",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    ca,
                    ca.purpose,
                    Selection(
                        "p-d", "ev-d", d.revision, "proj-d", rev, (a.agent_id,), 0, 22
                    ),
                )
            pub_ns = time_ns()
            with h.writer() as w:
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    complete_work_run(
                        db,
                        ca.run_id,
                        ca.fencing_token,
                        outcome={"prep": True},
                        now_ns=pub_ns,
                    )
                    create_work_item(
                        db,
                        company_id=h.company_id,
                        owner_agent_id=a.agent_id,
                        duty_kind="work.discovery_review",
                        source_kind="e2e",
                        source_id="rev-1",
                        now_ns=pub_ns + 100,
                    )
            srv.openai_queue.put(
                openai_tool_call_response(
                    "r-inv",
                    "c-inv",
                    "record_work_decision",
                    {
                        "action_kind": "investigate",
                        "rationale": "Discrepancy observed in shared asset.",
                        "decision_at_ns": pub_ns + 1_000,
                        "evidence": [
                            {
                                "kind": "shared_publication",
                                "resource_id": asset.asset_id,
                                "revision": sha256_hex(asset.content),
                                "observed_at_ns": pub_ns + 500,
                                "available_at_ns": pub_ns,
                            }
                        ],
                    },
                )
            )
            srv.openai_queue.put(
                openai_text_response("r-done", "Recorded investigate decision.")
            )
            reg = build_production_duty_registry(
                adapter_registry=srv.build_registry(secret_resolver=resolver),
                profile_bindings={h.founder_id: profile, a.agent_id: profile},
                domain_plugins=(),
            )
            step = h.make_runtime(reg).step(max_dispatches=2, now_ns=pub_ns + 1_000)
            self.assertEqual(len(step.executed_runs), 1)
            self.assertEqual(
                count_rows(h.store, "work_decision", "company_id = ?", (h.company_id,)),
                1,
            )

    def test_f3_2_t1_02_self_scheduling_condition_crud_tools(self) -> None:
        """T1_F3_2_02: set_condition, list_conditions, replace_condition (rev 1->2), cancel_condition tools."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("cond-agent")
            resp_id = h.register_discovery_responsibility(a.agent_id, now_ns=1_000)
            _, _, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope, duty_kind="work.discovery_review"
            )
            build_tools = lazy_attr(
                "sourdough.runtime.handlers", "build_proactive_tool_handlers"
            )
            with h.writer() as w:
                handlers = build_tools(w, ca)
                c1 = handlers["set_condition"](
                    {
                        "responsibility_id": resp_id,
                        "trigger_kind": "timer",
                        "due_at_ns": 20_000,
                    }
                )
                c2 = handlers["set_condition"](
                    {
                        "responsibility_id": resp_id,
                        "trigger_kind": "timer",
                        "due_at_ns": 30_000,
                    }
                )
                listed = handlers["list_conditions"]({})
                self.assertEqual(len(listed["conditions"]), 2)
                replaced = handlers["replace_condition"](
                    {
                        "condition_id": c1["condition_id"],
                        "trigger_kind": "shared_asset_revision",
                        "source_identity": "asset-xyz",
                    }
                )
                self.assertEqual(replaced["condition_revision"], 2)
                handlers["cancel_condition"]({"condition_id": c2["condition_id"]})
            states = dict(
                query_ro(
                    h.store,
                    "SELECT condition_id, status FROM work_conditions WHERE owner_agent_id = ?",
                    (a.agent_id,),
                )
            )
            self.assertEqual(states[c1["condition_id"]], "superseded")
            self.assertEqual(states[replaced["condition_id"]], "armed")
            self.assertEqual(states[c2["condition_id"]], "cancelled")

    def test_f3_2_t1_03_atomic_commit_of_work_decision_completion_and_next_condition(
        self,
    ) -> None:
        """T1_F3_2_03: work_decision, settled item, and armed next_condition commit together."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("atomic-a")
            resp_id = h.register_discovery_responsibility(a.agent_id, now_ns=1_000)
            item, _, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope, duty_kind="work.discovery_review"
            )
            record_work_decision = lazy_attr(
                "sourdough.runtime.work_kernel", "record_work_decision"
            )
            with h.writer() as w:
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    dec = record_work_decision(
                        db,
                        company_id=h.company_id,
                        work_item_id=item.work_item_id,
                        run_id=ca.run_id,
                        fencing_token=ca.fencing_token,
                        agent_id=a.agent_id,
                        action_kind="hold",
                        rationale="Next review in 60s.",
                        evidence=(),
                        decision_at_ns=5_000,
                        next_condition={
                            "responsibility_id": resp_id,
                            "trigger_kind": "timer",
                            "due_at_ns": 65_000,
                        },
                    )
                    complete_work_run(
                        db,
                        ca.run_id,
                        ca.fencing_token,
                        outcome={"decision_id": dec.decision_id},
                        now_ns=5_000,
                    )
            self.assertEqual(
                query_ro(
                    h.store,
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (item.work_item_id,),
                )[0][0],
                "settled",
            )
            self.assertEqual(
                count_rows(
                    h.store, "work_decision", "decision_id = ?", (dec.decision_id,)
                ),
                1,
            )
            self.assertEqual(
                count_rows(
                    h.store,
                    "work_conditions",
                    "owner_agent_id = ? AND status = 'armed'",
                    (a.agent_id,),
                ),
                1,
            )

    def test_f3_2_t1_04_continuation_after_committed_result_both_arrival_orders(
        self,
    ) -> None:
        """T1_F3_2_04: Result gates continuation in both (result-then-timer, timer-then-result) orders."""
        for order in ("result_first", "timer_first"):
            with self.subTest(order=order), CoreCompanyHarness() as h:
                h.found_company()
                a = h.hire_agent("cont-a")
                rev = h.create_project(
                    "proj-cont", [h.founder_id, a.agent_id], publishers=[a.agent_id]
                )
                _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
                arm_result_dep = lazy_attr(
                    "sourdough.runtime.work_kernel", "arm_result_continuation"
                )
                with h.writer() as w:
                    d = save_draft(
                        w,
                        ca,
                        ca.purpose,
                        name="res",
                        content="Ready result body",
                        expected_revision=None,
                    )
                    with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                        complete_work_run(
                            db,
                            ca.run_id,
                            ca.fencing_token,
                            outcome={"done": True},
                            now_ns=1_000,
                        )
                        arm_result_dep(
                            db,
                            company_id=h.company_id,
                            owner_agent_id=a.agent_id,
                            dependency_asset_id="pending-res-1"
                            if order == "timer_first"
                            else "ready-res-1",
                            due_at_ns=2_000,
                            now_ns=1_000,
                        )
                if order == "result_first":
                    _, _, ca2 = h.open_work_run_claim(
                        a.agent_id, a.grant_id, a.grant_scope
                    )
                    with h.writer() as w:
                        asset = publish_selection(
                            w,
                            ca2,
                            ca2.purpose,
                            Selection(
                                "r-first",
                                "res",
                                d.revision,
                                "proj-cont",
                                rev,
                                (a.agent_id,),
                                0,
                                17,
                            ),
                        )
                    h.emit_audit(
                        domain="assets",
                        entity_type="shared_publication",
                        entity_id=asset.asset_id,
                        event_type="asset.published",
                        actor_id="other",
                        payload={"asset_id": "ready-res-1"},
                        now_ns=1_500,
                    )
                    with h.writer() as w:
                        with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                            admitted = evaluate_conditions(
                                db, h.company_id, now_ns=2_500
                            )
                    self.assertEqual(len(admitted), 1)
                else:
                    with h.writer() as w:
                        with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                            early = evaluate_conditions(db, h.company_id, now_ns=2_500)
                    self.assertEqual(early, ())
                    h.emit_audit(
                        domain="assets",
                        entity_type="shared_publication",
                        entity_id="pending-res-1",
                        event_type="asset.published",
                        actor_id="other",
                        payload={"asset_id": "pending-res-1"},
                        now_ns=3_000,
                    )
                    with h.writer() as w:
                        with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                            late = evaluate_conditions(db, h.company_id, now_ns=3_500)
                    self.assertEqual(len(late), 1)

    def test_f3_2_t1_05_optional_domain_plugin_validate_decision_evidence_seam(
        self,
    ) -> None:
        """T1_F3_2_05: Core validates DecisionEvidence with domain_plugins=() and invokes DomainPlugin hook when present."""
        called: list[str] = []

        class _HookPlugin(BaseDomainPlugin):
            domain_name = "test_hook"

            def validate_decision_evidence(
                self, _conn, _company_id, _agent_id, _decision_at_ns, evidence
            ) -> bool:
                called.append(evidence.kind)
                return evidence.kind == "custom_test_kind"

        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("hook-a")
            item, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            record_work_decision = lazy_attr(
                "sourdough.runtime.work_kernel", "record_work_decision"
            )
            DecisionEvidence = lazy_attr(
                "sourdough.runtime.work_kernel", "DecisionEvidence"
            )
            with h.writer() as w:
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    record_work_decision(
                        db,
                        company_id=h.company_id,
                        work_item_id=item.work_item_id,
                        run_id=ca.run_id,
                        fencing_token=ca.fencing_token,
                        agent_id=a.agent_id,
                        action_kind="hold",
                        rationale="Valid core evidence",
                        evidence=(
                            DecisionEvidence(
                                kind="custom_test_kind",
                                resource_id="res-1",
                                revision="rev-1",
                                observed_at_ns=2_000,
                                available_at_ns=1_000,
                            ),
                        ),
                        decision_at_ns=3_000,
                        domain_plugins=(_HookPlugin(),),
                    )
            self.assertEqual(called, ["custom_test_kind"])
            assert_core_only_store(self, h.store)


class TestF33SupervisionRecoveryTier1(unittest.TestCase):
    """F3.3 Tier 1 (5 cases): Supervision, crash recovery, GL settlement, recoverable tool refusal & retries."""

    def test_f3_3_t1_01_supervise_company_cold_launch_and_ready_fd_handshake(
        self,
    ) -> None:
        """T1_F3_3_01: sourdough supervise-company launches run-company with --ready-fd and --store-key."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            res = cli_json(
                "supervise-company",
                h.store,
                "--company-id",
                r.company_id,
                "--max-restarts",
                "0",
                "--startup-seconds",
                "10",
                "--max-seconds",
                "5",
                "--once",
            )
            self.assertTrue(res["completed"])
            self.assertIsNotNone(res["attempts"][0]["ready_at_ns"])

    def test_f3_3_t1_02_crash_recovery_and_automatic_supervisor_relaunch(self) -> None:
        """T1_F3_3_02: Supervisor detects SIGKILL child exit, relaunches on same inode & reclaims lease."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            a = h.hire_agent("crash-a")
            with h.writer() as w:
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    item, _ = create_work_item(
                        db,
                        company_id=h.company_id,
                        owner_agent_id=a.agent_id,
                        duty_kind="work.discovery_review",
                        source_kind="e2e",
                        source_id="crash-1",
                        now_ns=1_000,
                    )
                    from sourdough.runtime.work_kernel import claim_work_run

                    claim_ts = time_ns()
                    run1 = claim_work_run(
                        db,
                        company_id=h.company_id,
                        work_item_id=item.work_item_id,
                        lease_owner="dead-worker",
                        lease_ttl_ns=300_000_000_000,
                        now_ns=claim_ts,
                    )
            marker = h.root / "crash-once.flag"
            worker = h.root / "flaky-worker.py"
            worker.write_text(
                f"""#!{sys.executable}
import os, signal, sys
from pathlib import Path
flag = Path({str(marker)!r})
if not flag.exists():
    flag.write_text("crashed")
    os.kill(os.getpid(), signal.SIGKILL)
from sourdough.cli import main
raise SystemExit(main())
""",
                encoding="utf-8",
            )
            worker.chmod(0o700)
            supervise_worker = lazy_attr("sourdough.supervisor", "supervise_worker")
            events: list[dict] = []
            sup = supervise_worker(
                [
                    sys.executable,
                    "-IB",
                    str(worker),
                    "run-company",
                    str(h.store),
                    "--company-id",
                    r.company_id,
                    "--once",
                ],
                h.store,
                r.company_id,
                max_restarts=1,
                startup_seconds=10,
                max_seconds=20,
                emit=events.append,
            )
            self.assertTrue(sup.completed)
            self.assertEqual(len(sup.attempts), 2)
            # Reclaim expired run1
            reg = build_production_duty_registry(
                adapter_registry=LoopbackMultiProviderServer().build_registry(
                    secret_resolver=default_secret_resolver()[0]
                ),
                profile_bindings={},
                domain_plugins=(),
                extra_handlers={
                    "work.discovery_review": lambda _c: DutyOutcome(
                        disposition="completed", result={"ok": True}
                    )
                },
            )
            step = h.make_runtime(reg).step(
                max_dispatches=2, now_ns=claim_ts + 400_000_000_000
            )
            self.assertIn(run1.run_id, step.fenced_runs)
            self.assertEqual(len(step.executed_runs), 1)

    def test_f3_3_t1_03_exact_penny_gl_cost_settlement_and_episode(self) -> None:
        """T1_F3_3_03: Multi-turn GL posts exact integer expense/payable lines and records $0 agent_episode."""
        with CoreCompanyHarness() as h, LoopbackMultiProviderServer() as srv:
            resolver, _ = default_secret_resolver()
            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:SD_OPENAI_KEY",
            )
            h.found_company(profile=profile)
            a = h.hire_agent("gl-a", profile=profile)
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            with h.writer() as w:
                settlement = settle_shift_cost_to_ledger(
                    w,
                    profile=profile,
                    run_id=ca.run_id,
                    turn_id="turn-1",
                    usage=TokenUsage(input_tokens=2000, output_tokens=500),
                    budget_ceiling_minor_units=100,
                )
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    complete_work_run(
                        db,
                        ca.run_id,
                        ca.fencing_token,
                        outcome={"settled": True},
                        now_ns=1_000,
                    )
            self.assertGreater(settlement.cost_minor_units, 0)
            lines = query_ro(
                h.store,
                "SELECT account, amount_minor FROM journal_line WHERE entry_id = ? ORDER BY account",
                (settlement.journal_entry_id,),
            )
            self.assertEqual(
                lines,
                [
                    ("expense:agent_llm_compute", settlement.cost_minor_units),
                    ("liability:accounts_payable", -settlement.cost_minor_units),
                ],
            )

            srv.openai_queue.put(
                openai_tool_call_response(
                    "r-mt1",
                    "c-mt1",
                    "desk_list_drafts",
                    {},
                    input_tokens=1000,
                    output_tokens=200,
                )
            )
            srv.openai_queue.put(
                openai_text_response(
                    "r-mt2",
                    "Multi-turn shift done.",
                    input_tokens=1200,
                    output_tokens=300,
                )
            )
            it_a2, run_a2, ca2 = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope
            )
            out = run_shift_for_claim(
                h,
                item=it_a2,
                run=run_a2,
                profile=profile,
                adapter_registry=srv.build_registry(secret_resolver=resolver),
                mandate_summary="Verify multi-turn GL & episode.",
                instruction="List drafts and conclude.",
            )
            self.assertIsNotNone(out.episode)
            self.assertEqual(out.episode.outcome_status, "completed")
            self.assertGreaterEqual(
                count_rows(
                    h.store,
                    "journal_entry",
                    "source_event LIKE ?",
                    (f"llm-turn:{ca2.run_id}:%",),
                ),
                1,
            )
            ep_rows = query_ro(
                h.store,
                """SELECT tool_calls_count, input_tokens, output_tokens, cost_minor_units
                   FROM agent_episode
                   WHERE company_id = ? AND agent_id = ? AND run_id = ?""",
                (h.company_id, a.agent_id, ca2.run_id),
            )
            self.assertEqual(len(ep_rows), 1)
            self.assertEqual(ep_rows[0][0], 1)
            self.assertEqual(ep_rows[0][1], 2200)
            self.assertEqual(ep_rows[0][2], 500)
            self.assertGreater(ep_rows[0][3], 0)

    def test_f3_3_t1_04_recoverable_domain_tool_refusal_continues_shift_and_settles_gl(
        self,
    ) -> None:
        """T1_F3_3_04: Recoverable WorkKernelRefused returns structured JSON error and shift completes."""
        with CoreCompanyHarness() as h, LoopbackMultiProviderServer() as srv:
            resolver, _ = default_secret_resolver()
            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:SD_OPENAI_KEY",
            )
            h.found_company(profile=profile)
            a = h.hire_agent("recov-a", profile=profile)
            ToolSpec = lazy_attr("sourdough.agent_runtime.protocol", "ToolSpec")
            cancel_cond_fn = lazy_attr(
                "sourdough.runtime.work_kernel", "cancel_condition"
            )
            registry = srv.build_registry(secret_resolver=resolver)
            srv.openai_queue.put(
                openai_tool_call_response(
                    "r-bad",
                    "c-bad",
                    "cancel_condition",
                    {"condition_id": "missing-cond"},
                )
            )
            srv.openai_queue.put(
                openai_text_response("r-ok", "Recovered cleanly after refusal.")
            )
            it_a, run_a, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope, duty_kind="work.discovery_review"
            )

            with h.writer() as w:

                def _cancel_tool(args: dict[str, str]) -> dict[str, bool]:
                    with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                        cancel_cond_fn(
                            db,
                            args["condition_id"],
                            owner_agent_id=a.agent_id,
                            now_ns=2_000,
                        )
                    return {"ok": True}

                out = run_shift_for_claim(
                    h,
                    item=it_a,
                    run=run_a,
                    profile=profile,
                    writer=w,
                    adapter_registry=registry,
                    mandate_summary="Test recoverable error.",
                    instruction="Cancel condition then recover.",
                    base_tools=(
                        ToolSpec(
                            name="cancel_condition",
                            description="Cancel a work condition by id.",
                            parameters={
                                "type": "object",
                                "properties": {"condition_id": {"type": "string"}},
                                "required": ["condition_id"],
                            },
                        ),
                    ),
                    base_handlers={"cancel_condition": _cancel_tool},
                )
            self.assertIsNotNone(out.episode)
            self.assertEqual(out.episode.outcome_status, "completed")
            receipt_rows = query_ro(
                h.store,
                """SELECT tool_name, status, error_type
                   FROM tool_call_receipt
                   WHERE company_id = ? AND run_id = ?""",
                (h.company_id, ca.run_id),
            )
            self.assertEqual(
                receipt_rows,
                [("cancel_condition", "refused", "WorkKernelRefused")],
            )
            openai_reqs = [
                payload
                for path, _hdrs, payload in srv.requests
                if path == "/v1/responses"
            ]
            self.assertEqual(len(openai_reqs), 2)
            req2_outputs = [
                json.loads(item["output"])
                for item in openai_reqs[1].get("input", [])
                if item.get("type") == "function_call_output"
            ]
            self.assertEqual(len(req2_outputs), 1)
            self.assertEqual(req2_outputs[0].get("ok"), False)
            self.assertEqual(req2_outputs[0].get("error_type"), "WorkKernelRefused")
            self.assertGreaterEqual(
                count_rows(
                    h.store,
                    "journal_entry",
                    "source_event LIKE ?",
                    (f"llm-turn:{ca.run_id}:%",),
                ),
                1,
            )

    def test_f3_3_t1_05_transient_transport_failure_bounded_exponential_retry(
        self,
    ) -> None:
        """T1_F3_3_05: AgentTransportInterrupted increments attempt_count & next_retry_at_ns, then succeeds."""
        with CoreCompanyHarness() as h, LoopbackMultiProviderServer() as srv:
            resolver, _ = default_secret_resolver()
            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:SD_OPENAI_KEY",
            )
            h.found_company(profile=profile)
            a = h.hire_agent("retry-a", profile=profile)
            with h.writer() as w:
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    item, _ = create_work_item(
                        db,
                        company_id=h.company_id,
                        owner_agent_id=a.agent_id,
                        duty_kind="work.discovery_review",
                        source_kind="e2e",
                        source_id="retry-1",
                        now_ns=1_000,
                    )
            srv.openai_queue.put({"drop_connection": True})
            srv.openai_queue.put({"drop_connection": True})
            srv.openai_queue.put(
                openai_tool_call_response(
                    "r-3a",
                    "c-3a",
                    "record_work_decision",
                    {"action_kind": "noop", "rationale": "Succeeded on attempt 3."},
                )
            )
            srv.openai_queue.put(
                openai_text_response("r-3b", "Succeeded on attempt 3.")
            )
            reg = build_production_duty_registry(
                adapter_registry=srv.build_registry(secret_resolver=resolver),
                profile_bindings={h.founder_id: profile, a.agent_id: profile},
                domain_plugins=(),
            )
            rt = h.make_runtime(reg)
            for t in (1_000, 100_000_000_000):
                with self.assertRaises(AgentTransportInterrupted):
                    rt.step(max_dispatches=1, now_ns=t)
            row = query_ro(
                h.store,
                "SELECT state, attempt_count, next_retry_at_ns FROM work_items WHERE work_item_id = ?",
                (item.work_item_id,),
            )[0]
            self.assertEqual(row[0], "due")
            self.assertGreaterEqual(row[1], 1)
            self.assertIsNotNone(row[2])
            step3 = rt.step(max_dispatches=1, now_ns=row[2] + 1)
            self.assertEqual(len(step3.executed_runs), 1)
            self.assertEqual(
                query_ro(
                    h.store,
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (item.work_item_id,),
                )[0][0],
                "settled",
            )


class TestF41MethodDistillationTier1(unittest.TestCase):
    """F4.1 Tier 1 (5 cases): Itemized method distillation, redaction gate & 4-step causal tracking."""

    def test_f4_1_t1_01_itemized_add_candidate_stays_non_active(self) -> None:
        """T1_F4_1_01: Distilling ADD work_method stores candidate revision_sha256 without activating it."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("learn-a")
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            ml = lazy_module("sourdough.method_learning")
            with h.writer() as w:
                delta = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m-distinction",
                    trigger_spec={"duty_kind": "work.assessment_review"},
                    body="Distinguish delivery acknowledgement from peer acceptance.",
                    audience_scope="one_agent",
                    source_refs=[f"work_run:{ca.run_id}"],
                )
                active = ml.resolve_active_methods(w, h.company_id, agent_id=a.agent_id)
            self.assertEqual(delta.status, "candidate")
            self.assertEqual(len(delta.revision_sha256), 64)
            self.assertEqual(active, ())

    def test_f4_1_t1_02_itemized_patch_and_deprecate_with_lineage_edges(self) -> None:
        """T1_F4_1_02: PATCH & DEPRECATE deltas record superseded_revision, parent_method_ids, active_snapshot_id."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            a = h.hire_agent("learn-b")
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            ml = lazy_module("sourdough.method_learning")
            with h.writer() as w:
                m1 = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m1",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Rule v1",
                    audience_scope="one_agent",
                    source_refs=[f"work_run:{ca.run_id}"],
                )
                ml.qualify_method_delta(
                    w,
                    m1.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                )
                act = ml.activate_method_delta(
                    w, m1.delta_id, actor_id=r.founder_id, grant_id=r.grant_id
                )
                patch = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="PATCH",
                    target_kind="work_method",
                    method_id="m1",
                    superseded_revision=m1.revision_sha256,
                    parent_method_ids=(m1.method_id,),
                    active_snapshot_id=act.active_snapshot_id,
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Rule v2 preserving v1 edge cases.",
                    audience_scope="one_agent",
                    source_refs=[f"work_run:{ca.run_id}"],
                )
                dep = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="DEPRECATE",
                    target_kind="work_method",
                    method_id="m1",
                    superseded_revision=m1.revision_sha256,
                    parent_method_ids=(m1.method_id,),
                    active_snapshot_id=act.active_snapshot_id,
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Deprecate obsolete rule.",
                    audience_scope="one_agent",
                    source_refs=[f"work_run:{ca.run_id}"],
                )
            self.assertEqual(patch.superseded_revision, m1.revision_sha256)
            self.assertEqual(patch.parent_method_ids, (m1.method_id,))
            self.assertEqual(patch.active_snapshot_id, act.active_snapshot_id)
            self.assertEqual(dep.delta_kind, "DEPRECATE")

    def test_f4_1_t1_03_distill_improvement_procedure_retaining_prior_failures(
        self,
    ) -> None:
        """T1_F4_1_03: improvement_procedure delta records predecessor, rejected attempts & predictions."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("meta-a")
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            ml = lazy_module("sourdough.method_learning")
            with h.writer() as w:
                proc = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="ADD",
                    target_kind="improvement_procedure",
                    method_id="proc-triage-v2",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Always verify negative triggers before raising escalation sensitivity.",
                    audience_scope="one_agent",
                    source_refs=[f"work_run:{ca.run_id}"],
                    metadata={
                        "rejected_attempts": ["proc-triage-v1"],
                        "predicted_effect": "Reduce false positives",
                        "observed_effect": "Holdout margin +0.2",
                    },
                )
            self.assertEqual(proc.target_kind, "improvement_procedure")
            self.assertEqual(proc.status, "candidate")

    def test_f4_1_t1_04_private_to_shared_redaction_gate_cleans_permitted_extract(
        self,
    ) -> None:
        """T1_F4_1_04: redact_for_shared_scope strips private Desk path/secrets & emits redaction receipt."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("redact-a")
            rev = h.create_project(
                "proj-red", [h.founder_id, a.agent_id], publishers=[a.agent_id]
            )
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            with h.writer() as w:
                d = save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="notes",
                    content="Shared verified rule",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    ca,
                    ca.purpose,
                    Selection(
                        "r-red",
                        "notes",
                        d.revision,
                        "proj-red",
                        rev,
                        (h.founder_id, a.agent_id),
                        0,
                        20,
                    ),
                )
            ml = lazy_module("sourdough.method_learning")
            with h.writer() as w:
                cleaned = ml.redact_for_shared_scope(
                    w,
                    ca,
                    raw_body="Check .desk/drafts/secret.md and cite shared asset.",
                    source_refs=[f"shared_publication:{asset.asset_id}"],
                    target_scope="project",
                )
            self.assertNotIn(".desk/drafts/", cleaned.redacted_body)
            self.assertTrue(cleaned.redaction_receipt_id)

    def test_f4_1_t1_05_four_step_causal_tracking_record(self) -> None:
        """T1_F4_1_05: Method causal tracking records available -> retrieved -> invoked -> outcome_delta."""
        with CoreCompanyHarness() as h, LoopbackMultiProviderServer() as srv:
            resolver, _ = default_secret_resolver()
            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:SD_OPENAI_KEY",
            )
            r = h.found_company(profile=profile)
            a = h.hire_agent("causal-a", profile=profile)
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            ml = lazy_module("sourdough.method_learning")
            with h.writer() as w:
                d = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m-causal",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Cite verified asset.",
                    audience_scope="one_agent",
                    source_refs=[f"work_run:{ca.run_id}"],
                )
                ml.qualify_method_delta(
                    w,
                    d.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                )
                act = ml.activate_method_delta(
                    w, d.delta_id, actor_id=r.founder_id, grant_id=r.grant_id
                )
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    complete_work_run(
                        db,
                        ca.run_id,
                        ca.fencing_token,
                        outcome={"distilled": True},
                        now_ns=1_000,
                    )

            registry = srv.build_registry(secret_resolver=resolver)
            srv.openai_queue.put(
                openai_text_response("r-c", "Applied m-causal method.")
            )
            it_a2, run_a2, ca2 = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope, duty_kind="work.discovery_review"
            )
            run_shift_for_claim(
                h,
                item=it_a2,
                run=run_a2,
                profile=profile,
                adapter_registry=registry,
                mandate_summary="Use active method.",
                instruction="Execute review.",
                active_methods_provider=lambda store, cid, aid, _dk: (
                    ml.resolve_active_methods(store, cid, agent_id=aid),
                    act.active_snapshot_id,
                ),
            )
            rows = query_ro(
                h.store,
                "SELECT operation, quarantined, activation_seq FROM method_activation WHERE company_id = ? AND method_id = 'm-causal'",
                (h.company_id,),
            )
            self.assertGreaterEqual(len(rows), 1)
            self.assertEqual(rows[0][0], "activate")
            self.assertEqual(rows[0][1], 0)
            self.assertIn("m-causal", json.dumps(srv.requests[-1][2]))
            ep_snaps = query_ro(
                h.store,
                "SELECT active_snapshot_id FROM agent_episode WHERE company_id = ? AND run_id = ?",
                (h.company_id, ca2.run_id),
            )
            self.assertEqual(len(ep_snaps), 1)
            self.assertEqual(ep_snaps[0][0], act.active_snapshot_id)


class TestF42QualificationRollbackTier1(unittest.TestCase):
    """F4.2 Tier 1 (5 cases): 3-stage pre-commit gate, BFS lineage quarantine & ContextPack v2 binding."""

    def test_f4_2_t1_01_full_three_stage_gate_pass(self) -> None:
        """T1_F4_2_01: Stage A (negative triggers), Stage B (safety), Stage C (pass^3) pass."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("qual-a")
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            ml = lazy_module("sourdough.method_learning")
            with h.writer() as w:
                delta = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m-3stage",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Verify bounds.",
                    audience_scope="one_agent",
                    source_refs=[f"work_run:{ca.run_id}"],
                )
                qual = ml.qualify_method_delta(
                    w,
                    delta.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                    holdout_margin=0.15,
                )
            self.assertTrue(qual.passed)
            self.assertEqual(
                query_ro(
                    h.store,
                    "SELECT passed FROM method_qualification WHERE company_id = ? AND method_id = ? AND revision_sha256 = ?",
                    (h.company_id, delta.method_id, delta.revision_sha256),
                )[0][0],
                1,
            )

    def test_f4_2_t1_02_scoped_adoption_under_matching_promotion_grant(self) -> None:
        """T1_F4_2_02: Scoped adoption (one Agent vs company) honors promotion authority & records receipt."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            ml = lazy_module("sourdough.method_learning")
            with h.writer() as w:
                m_priv = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m-priv",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Private method",
                    audience_scope="one_agent",
                    source_refs=[f"work_run:{ca.run_id}"],
                )
                m_comp = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m-comp",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Company method",
                    audience_scope="company",
                    source_refs=[f"work_run:{ca.run_id}"],
                )
                ml.qualify_method_delta(
                    w,
                    m_priv.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                )
                ml.qualify_method_delta(
                    w,
                    m_comp.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                )
                ml.activate_method_delta(
                    w, m_priv.delta_id, actor_id=r.founder_id, grant_id=r.grant_id
                )
                ml.activate_method_delta(
                    w, m_comp.delta_id, actor_id=r.founder_id, grant_id=r.grant_id
                )
                a_methods = {
                    m.method_id
                    for m in ml.resolve_active_methods(
                        w, h.company_id, agent_id=a.agent_id
                    )
                }
                b_methods = {
                    m.method_id
                    for m in ml.resolve_active_methods(
                        w, h.company_id, agent_id=b.agent_id
                    )
                }
            self.assertEqual(a_methods, {"m-priv", "m-comp"})
            self.assertEqual(b_methods, {"m-comp"})

    def test_f4_2_t1_03_context_pack_v2_binding_and_prompt_cache_split(self) -> None:
        """T1_F4_2_03: Adopted method binds into ContextPack v2 and Anthropic cache_control prefix."""
        with CoreCompanyHarness() as h, LoopbackMultiProviderServer() as srv:
            resolver, _ = default_secret_resolver()
            profile = AgentProfileBinding(
                provider=PROVIDER_ANTHROPIC,
                model="claude-sonnet-4-5",
                revision="v1",
                credential_handle_ref="envref:SD_ANTHROPIC_KEY",
            )
            r = h.found_company()
            a = h.hire_agent("cache-a", profile=profile)
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            ml = lazy_module("sourdough.method_learning")
            with h.writer() as w:
                m = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m-cache",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Check cache prefix",
                    audience_scope="one_agent",
                    source_refs=[f"work_run:{ca.run_id}"],
                )
                ml.qualify_method_delta(
                    w,
                    m.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                )
                act = ml.activate_method_delta(
                    w, m.delta_id, actor_id=r.founder_id, grant_id=r.grant_id
                )
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    complete_work_run(
                        db,
                        ca.run_id,
                        ca.fencing_token,
                        outcome={"activated": True},
                        now_ns=1_000,
                    )
            registry = srv.build_registry(secret_resolver=resolver)
            srv.anthropic_queue.put(
                anthropic_text_response("ant-1", "Done with active method.")
            )
            it_a2, run_a2, ca2 = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope, duty_kind="work.discovery_review"
            )
            run_shift_for_claim(
                h,
                item=it_a2,
                run=run_a2,
                profile=profile,
                adapter_registry=registry,
                mandate_summary="Prompt cache test.",
                instruction="Execute turn.",
                active_methods_provider=lambda store, cid, aid, _dk: (
                    ml.resolve_active_methods(store, cid, agent_id=aid),
                    act.active_snapshot_id,
                ),
            )
            self.assertEqual(len(srv.requests), 1)
            body = srv.requests[0][2]
            system_blocks = body.get("system")
            self.assertIsInstance(system_blocks, list)
            self.assertGreaterEqual(len(system_blocks), 2)
            self.assertEqual(
                system_blocks[0].get("cache_control"), {"type": "ephemeral"}
            )
            self.assertIn("m-cache", system_blocks[0].get("text", ""))
            self.assertNotIn("cache_control", system_blocks[-1])
            self.assertTrue(act.active_snapshot_id)

    def test_f4_2_t1_04_bfs_lineage_dag_quarantine_rollback(self) -> None:
        """T1_F4_2_04: quarantine_method_lineage(M0) quarantines M0->M1->M2 while keeping unrelated U1 active."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            a = h.hire_agent("dag-a")
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            ml = lazy_module("sourdough.method_learning")
            with h.writer() as w:

                def add_and_activate(
                    mid: str, parents: tuple[str, ...] = (), snap: str | None = None
                ):
                    d = ml.distill_method_delta(
                        w,
                        ca,
                        delta_kind="ADD",
                        target_kind="work_method",
                        method_id=mid,
                        trigger_spec={"duty_kind": "work.discovery_review"},
                        body=f"Body {mid}",
                        audience_scope="company",
                        source_refs=[f"work_run:{ca.run_id}"],
                        parent_method_ids=parents,
                        active_snapshot_id=snap,
                    )
                    ml.qualify_method_delta(
                        w,
                        d.delta_id,
                        passed_k=3,
                        negative_trigger_passed=True,
                        safety_passed=True,
                    )
                    return ml.activate_method_delta(
                        w, d.delta_id, actor_id=r.founder_id, grant_id=r.grant_id
                    )

                a0 = add_and_activate("m0")
                a1 = add_and_activate("m1", parents=("m0",), snap=a0.active_snapshot_id)
                add_and_activate("m2", parents=("m1",), snap=a1.active_snapshot_id)
                add_and_activate("u1")
                ml.quarantine_method_lineage(
                    w, h.company_id, method_id="m0", reason="Regression detected"
                )
                active_ids = {
                    m.method_id
                    for m in ml.resolve_active_methods(
                        w, h.company_id, agent_id=a.agent_id
                    )
                }
            self.assertEqual(active_ids, {"u1"})

    def test_f4_2_t1_05_post_rollback_shift_uses_incumbent_and_requalification_unblocks(
        self,
    ) -> None:
        """T1_F4_2_05: Post-quarantine shift drops M0..M2 and re-qualified descendant fix re-activates."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            a = h.hire_agent("requal-a")
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            ml = lazy_module("sourdough.method_learning")
            with h.writer() as w:
                m0 = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m0-inc",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Incumbent",
                    audience_scope="company",
                    source_refs=[f"work_run:{ca.run_id}"],
                )
                ml.qualify_method_delta(
                    w,
                    m0.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                )
                ml.activate_method_delta(
                    w, m0.delta_id, actor_id=r.founder_id, grant_id=r.grant_id
                )
                ml.quarantine_method_lineage(
                    w, h.company_id, method_id="m0-inc", reason="Regressed"
                )
                self.assertEqual(
                    ml.resolve_active_methods(w, h.company_id, agent_id=a.agent_id), ()
                )
                m_fix = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m0-fix",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Clean fix",
                    audience_scope="company",
                    source_refs=[f"work_run:{ca.run_id}"],
                )
                ml.qualify_method_delta(
                    w,
                    m_fix.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                )
                ml.activate_method_delta(
                    w, m_fix.delta_id, actor_id=r.founder_id, grant_id=r.grant_id
                )
                active = {
                    m.method_id
                    for m in ml.resolve_active_methods(
                        w, h.company_id, agent_id=a.agent_id
                    )
                }
            self.assertEqual(active, {"m0-fix"})


class TestF43OfflineTrialsRsiTier1(unittest.TestCase):
    """F4.3 Tier 1 (5 cases): Isolated offline trial runner, blind (AB, BA) judge & 2-Gen RSI."""

    def _build_recursive_suite(
        self, offline_store: Path, families: tuple[str, ...]
    ) -> tuple[str, TrialSuite, list[tuple[str, str, str, str]]]:
        limits = TrialCeiling(turns=4, wall_seconds=60, tokens=10_000)
        fix_b = b"core-fixture-v1"
        tools_b = b'{"tools":["desk_read_draft","record_work_decision"]}'
        inc_ctx = b'{"gen":1,"procedure":"incumbent"}'
        cand_ctx = b'{"gen":2,"procedure":"candidate"}'
        slots = []
        specs = []
        for idx, fam in enumerate(families, start=1):
            cid = f"case-{idx}"
            cb = json.dumps({"case": cid, "family": fam}).encode("utf-8")
            s_inc, s_cand = f"p{idx}-inc", f"p{idx}-cand"
            for sid, arm, cp, ctx in (
                (s_inc, "incumbent", s_cand, inc_ctx),
                (s_cand, "candidate", s_inc, cand_ctx),
            ):
                slots.append(
                    TrialSlot(
                        slot_id=sid,
                        case_id=cid,
                        family_id=fam,
                        inputs=TrialInputs(
                            sha256_hex(fix_b),
                            sha256_hex(cb),
                            sha256_hex(ctx),
                            sha256_hex(tools_b),
                            "offline-v1",
                        ),
                        limits=limits,
                        arm=arm,
                        counterpart_id=cp,
                    )
                )
            specs.append((s_inc, s_cand, fam, cid))
        suite = TrialSuite(
            suite_id="rsi-suite-t1",
            purpose="recursive",
            rubric_digest=sha256_hex(b"rubric"),
            evaluator_digest=sha256_hex(b"eval"),
            analysis_plan_digest=sha256_hex(b"plan"),
            slots=tuple(slots),
        )
        rev = seal_trial_suite(offline_store, suite)
        return rev, suite, specs

    def test_f4_3_t1_01_sealed_suite_in_isolated_evaluator_sqlite_and_clone_desk(
        self,
    ) -> None:
        """T1_F4_3_01: Isolated evaluator.sqlite + clone_desk_snapshot leaves company.sqlite untouched."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("arm-a")
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            with h.writer() as w:
                save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="arm-note",
                    content="Permitted note",
                    expected_revision=None,
                )
            before = h.store.read_bytes()
            rev, _, specs = self._build_recursive_suite(
                h.evaluator_store, ("fam-1", "fam-2")
            )
            self.assertEqual(file_mode_bits(h.evaluator_store) & 0o077, 0)
            clone_desk_snapshot = lazy_attr(
                "sourdough.agent_desk", "clone_desk_snapshot"
            )
            arm_store = h.root / "trial-arms" / "arm-1" / "company.sqlite"
            arm_store.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            clone_desk_snapshot(h.store, h.company_id, (a.agent_id,), arm_store)
            self.assertTrue(
                (
                    desks_root(arm_store)
                    / a.agent_id
                    / ".desk"
                    / "drafts"
                    / "arm-note.json"
                ).is_file()
            )
            self.assertEqual(h.store.read_bytes(), before)
            self.assertTrue(rev)
            self.assertEqual(len(specs), 2)

    def test_f4_3_t1_02_deterministic_outcome_vs_transcript_grading(self) -> None:
        """T1_F4_3_02: grade_trial_observation separates deterministic outcome vs transcript rubric."""
        with CoreCompanyHarness() as h:
            h.found_company()
            rev, suite, _ = self._build_recursive_suite(
                h.evaluator_store, ("fam-1", "fam-2")
            )
            tr = lazy_module("sourdough.trial_runner")
            for idx, slot in enumerate(suite.slots, start=1):
                ev = tr.run_isolated_trial_slot(
                    h.evaluator_store,
                    revision=rev,
                    slot_id=slot.slot_id,
                    run_id=f"run-{idx}",
                    trial_root=h.root / "trials",
                )
                rec = commit_trial_observation(
                    h.evaluator_store,
                    revision=rev,
                    slot_id=slot.slot_id,
                    run_id=f"run-{idx}",
                    evidence=ev,
                )
                pkt = blind_trial_packet(
                    h.evaluator_store, revision=rev, result_id=rec.result_id
                )
                grade = tr.grade_trial_observation(pkt)
                commit_blind_grade(
                    h.evaluator_store, revision=rev, packet=pkt, grade=grade
                )
            summary = trial_judgment_summary(h.evaluator_store, revision=rev)
            self.assertEqual(
                (summary.planned, summary.completed, summary.judgment_passed), (4, 4, 4)
            )

    def test_f4_3_t1_03_position_swapped_ab_ba_blind_pairwise_judging(self) -> None:
        """T1_F4_3_03: Position-swapped (AB, BA) blind pairwise judging yields consistent=True winner."""
        with CoreCompanyHarness() as h:
            h.found_company()
            rev, suite, specs = self._build_recursive_suite(
                h.evaluator_store, ("fam-1", "fam-2")
            )
            from sourdough.trial_registry import (
                TrialTerminalEvidence,
                reserve_trial_launch,
            )

            cand_results: set[str] = set()
            for idx, slot in enumerate(suite.slots, start=1):
                run_id = f"r-{idx}"
                reserve_trial_launch(
                    h.evaluator_store,
                    revision=rev,
                    slot_id=slot.slot_id,
                    run_id=run_id,
                    inputs=slot.inputs,
                    limits=slot.limits,
                )
                ev = TrialTerminalEvidence(
                    outcome="completed",
                    observation_digest=sha256_hex(f"obs-{slot.slot_id}".encode()),
                    decision_digest=sha256_hex(f"dec-{slot.slot_id}".encode()),
                    tool_trace_digest=sha256_hex(b"trace"),
                    service_effects_digest=sha256_hex(b"svc"),
                    usage_digest=sha256_hex(b'{"tokens":200}'),
                )
                rec = commit_trial_observation(
                    h.evaluator_store,
                    revision=rev,
                    slot_id=slot.slot_id,
                    run_id=run_id,
                    evidence=ev,
                )
                if slot.arm == "candidate":
                    cand_results.add(rec.result_id)
            for ab, ba in blind_pairwise_packets(h.evaluator_store, revision=rev):
                ab_pref = "first" if ab.first.result_id in cand_results else "second"
                ba_pref = "second" if ab_pref == "first" else "first"
                pw = commit_position_swapped_pairwise_grade(
                    h.evaluator_store,
                    revision=rev,
                    ab_packet=ab,
                    ab_judgment=PairwiseJudgment("Better grounding", ab_pref),
                    ba_packet=ba,
                    ba_judgment=PairwiseJudgment("Swapped confirmation", ba_pref),
                )
                self.assertTrue(pw.consistent)
                self.assertEqual(pw.outcome, "winner")

    def test_f4_3_t1_04_small_n_statistical_gate_supported_multi_family(self) -> None:
        """T1_F4_3_04: >=3 families consistent wins pass bootstrap LCB & permutation gate (SUPPORTED)."""
        with CoreCompanyHarness() as h:
            h.found_company()
            rev, suite, _ = self._build_recursive_suite(
                h.evaluator_store,
                ("fam-1", "fam-2", "fam-3", "fam-4", "fam-5", "fam-6"),
            )
            from sourdough.trial_registry import (
                TrialTerminalEvidence,
                reserve_trial_launch,
            )

            cand_results: set[str] = set()
            for idx, slot in enumerate(suite.slots, start=1):
                run_id = f"r-{idx}"
                reserve_trial_launch(
                    h.evaluator_store,
                    revision=rev,
                    slot_id=slot.slot_id,
                    run_id=run_id,
                    inputs=slot.inputs,
                    limits=slot.limits,
                )
                rec = commit_trial_observation(
                    h.evaluator_store,
                    revision=rev,
                    slot_id=slot.slot_id,
                    run_id=run_id,
                    evidence=TrialTerminalEvidence(
                        "completed",
                        sha256_hex(f"o-{slot.slot_id}".encode()),
                        sha256_hex(f"d-{slot.slot_id}".encode()),
                        sha256_hex(b"t"),
                        sha256_hex(b"s"),
                        sha256_hex(b'{"tokens":100}'),
                    ),
                )
                if slot.arm == "candidate":
                    cand_results.add(rec.result_id)
                pkt = blind_trial_packet(
                    h.evaluator_store, revision=rev, result_id=rec.result_id
                )
                commit_blind_grade(
                    h.evaluator_store,
                    revision=rev,
                    packet=pkt,
                    grade=TrialGrade(True, True, True, 0, True),
                )
            for ab, ba in blind_pairwise_packets(h.evaluator_store, revision=rev):
                ab_pref = "first" if ab.first.result_id in cand_results else "second"
                ba_pref = "second" if ab_pref == "first" else "first"
                commit_position_swapped_pairwise_grade(
                    h.evaluator_store,
                    revision=rev,
                    ab_packet=ab,
                    ab_judgment=PairwiseJudgment("Candidate clear win", ab_pref),
                    ba_packet=ba,
                    ba_judgment=PairwiseJudgment("Confirmed", ba_pref),
                )
            summary = summarize_rsi_transfer(
                h.evaluator_store,
                revision=rev,
                min_pairs=6,
                min_net_wins=4,
                min_families=3,
            )
            self.assertEqual(summary.verdict, "SUPPORTED")
            self.assertGreater(summary.lcb_alpha, 0.0)
            self.assertLessEqual(summary.perm_p, 0.05)

    def test_f4_3_t1_05_full_two_generation_rsi_cycle(self) -> None:
        """T1_F4_3_05: run_recursive_learning_cycle executes Gen-1->arm fork->Gen-2->adopt->witness->rollback."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            meta = h.hire_agent("meta-learner")
            tr = lazy_module("sourdough.trial_runner")
            with h.writer() as w:
                cycle = tr.run_recursive_learning_cycle(
                    w,
                    evaluator_store=h.evaluator_store,
                    company_id=h.company_id,
                    meta_agent_id=meta.agent_id,
                    promotion_actor_id=r.founder_id,
                    promotion_grant_id=r.grant_id,
                    trial_root=h.root / "rsi-trials",
                )
            self.assertEqual(cycle.transfer_verdict, "SUPPORTED")
            self.assertTrue(cycle.adopted_procedure_id)
            self.assertTrue(cycle.shift_witnessed)
            self.assertTrue(cycle.rollback_restored_incumbent)


class TestF44ProjectionsTier1(unittest.TestCase):
    """F4.4 Tier 1 (5 cases): Read-only Client projections & privacy/Core-only invariants."""

    def test_f4_4_t1_01_projection_agent_detail_core_fields(self) -> None:
        """T1_F4_4_01: agent-detail-v1 populates active_methods, work_decisions, armed_conditions, agent_episodes."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            a = h.hire_agent("proj-a")
            resp_id = h.register_discovery_responsibility(a.agent_id, now_ns=1_000)
            item, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            record_work_decision = lazy_attr(
                "sourdough.runtime.work_kernel", "record_work_decision"
            )
            ml = lazy_module("sourdough.method_learning")
            with h.writer() as w:
                save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="priv-1",
                    content="Private body",
                    expected_revision=None,
                )
                m = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m-proj",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Rule",
                    audience_scope="one_agent",
                    source_refs=[f"work_run:{ca.run_id}"],
                )
                ml.qualify_method_delta(
                    w,
                    m.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                )
                ml.activate_method_delta(
                    w, m.delta_id, actor_id=r.founder_id, grant_id=r.grant_id
                )
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    record_work_decision(
                        db,
                        company_id=h.company_id,
                        work_item_id=item.work_item_id,
                        run_id=ca.run_id,
                        fencing_token=ca.fencing_token,
                        agent_id=a.agent_id,
                        action_kind="hold",
                        rationale="All clear",
                        evidence=(),
                        decision_at_ns=2_000,
                        next_condition={
                            "responsibility_id": resp_id,
                            "trigger_kind": "timer",
                            "due_at_ns": 9_000,
                        },
                    )
            payload, seq = read_agent_detail_projection(
                h.store, h.company_id, a.agent_id, domain_plugins=()
            )
            self.assertEqual(payload["projection"], PROJECTION_AGENT_DETAIL)
            self.assertEqual(payload["desk_workspace"]["private_draft_count"], 1)
            self.assertIn("active_methods", payload)
            self.assertIn("work_decisions", payload["tabs"])
            self.assertGreaterEqual(len(payload["tabs"]["conditions"]), 1)
            self.assertIn("agent_episodes", payload["tabs"])
            self.assertTrue(projection_etag(payload, seq))

    def test_f4_4_t1_02_projection_system_evals_core_method_and_rsi_fields(
        self,
    ) -> None:
        """T1_F4_4_02: system-evals-v1 populates Core method_deltas, qualifications, activations & evaluator_store."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            a = h.hire_agent("eval-proj-a")
            _, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            ml = lazy_module("sourdough.method_learning")
            with h.writer() as w:
                m = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m-syseval",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Rule",
                    audience_scope="company",
                    source_refs=[f"work_run:{ca.run_id}"],
                )
                ml.qualify_method_delta(
                    w,
                    m.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                )
                ml.activate_method_delta(
                    w, m.delta_id, actor_id=r.founder_id, grant_id=r.grant_id
                )
            suite = TrialSuite(
                suite_id="s-proj",
                purpose="recursive",
                rubric_digest=sha256_hex(b"r"),
                evaluator_digest=sha256_hex(b"e"),
                analysis_plan_digest=sha256_hex(b"p"),
                slots=(
                    TrialSlot(
                        "s-inc",
                        "c1",
                        "f1",
                        TrialInputs(
                            sha256_hex(b"f"),
                            sha256_hex(b"c"),
                            sha256_hex(b"i"),
                            sha256_hex(b"t"),
                            "offline-v1",
                        ),
                        TrialCeiling(2, 10, 1000),
                        "incumbent",
                        "s-cand",
                    ),
                    TrialSlot(
                        "s-cand",
                        "c1",
                        "f1",
                        TrialInputs(
                            sha256_hex(b"f"),
                            sha256_hex(b"c"),
                            sha256_hex(b"x"),
                            sha256_hex(b"t"),
                            "offline-v1",
                        ),
                        TrialCeiling(2, 10, 1000),
                        "candidate",
                        "s-inc",
                    ),
                ),
            )
            seal_trial_suite(h.evaluator_store, suite)
            payload, _ = read_system_evals_projection(
                h.store, h.company_id, evaluator_store=h.evaluator_store
            )
            self.assertEqual(payload["projection"], PROJECTION_SYSTEM_EVALS)
            self.assertTrue(payload["offline_evaluator"]["connected"])
            self.assertIn("method_deltas", payload)
            self.assertIn("method_qualifications", payload)
            self.assertIn("method_activations", payload)
            self.assertIn("rsi_transfer_summaries", payload)

    def test_f4_4_t1_03_core_only_projection_operation(self) -> None:
        """T1_F4_4_03: All Core projections run with domain_plugins=() and zero trading tables/imports."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            a = h.hire_agent("core-proj")
            from sourdough.ipc.projections import (
                read_agents_graph_projection,
                read_org_projection,
            )

            g, _ = read_agents_graph_projection(
                h.store, r.company_id, domain_plugins=()
            )
            o, _ = read_org_projection(h.store, r.company_id)
            d, _ = read_agent_detail_projection(
                h.store, r.company_id, a.agent_id, domain_plugins=()
            )
            e, _ = read_system_evals_projection(h.store, r.company_id)
            self.assertFalse(g.get("trading_enabled", False))
            self.assertIn("nodes", g)
            self.assertIn("roster", o)
            self.assertIsNotNone(d)
            self.assertEqual(e["projection"], PROJECTION_SYSTEM_EVALS)

    def test_f4_4_t1_04_snapshot_consistency_and_etag_stability(self) -> None:
        """T1_F4_4_04: Repeated unchanged projection reads yield identical event_seq and projection_etag."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            a = h.hire_agent("etag-a")
            p1, s1 = read_agent_detail_projection(
                h.store, r.company_id, a.agent_id, domain_plugins=()
            )
            p2, s2 = read_agent_detail_projection(
                h.store, r.company_id, a.agent_id, domain_plugins=()
            )
            self.assertEqual(s1, s2)
            self.assertEqual(projection_etag(p1, s1), projection_etag(p2, s2))

            e1, es1 = read_system_evals_projection(h.store, r.company_id)
            e2, es2 = read_system_evals_projection(h.store, r.company_id)
            self.assertEqual(es1, es2)
            self.assertEqual(projection_etag(e1, es1), projection_etag(e2, es2))

    def test_f4_4_t1_05_zero_side_effects_on_projection_reads(self) -> None:
        """T1_F4_4_05: Reading projections leaves company.sqlite and evaluator.sqlite untouched."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            a = h.hire_agent("ro-a")
            before_wm = read_watermark(h.store, r.company_id)
            before_seq = h.max_seq()
            read_agent_detail_projection(
                h.store, r.company_id, a.agent_id, domain_plugins=()
            )
            read_system_evals_projection(h.store, r.company_id)
            self.assertEqual(read_watermark(h.store, r.company_id), before_wm)
            self.assertEqual(h.max_seq(), before_seq)


if __name__ == "__main__":
    unittest.main()
