"""Unit tests for ToolSpec.flow_class, ShiftFlowGuard, ProvenanceIndex, SQLite UoW, and tool_kit."""

from __future__ import annotations

import logging
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from unittest import mock

from sourdough.agent_desk import save_draft
from sourdough.agent_runtime import (
    PROVIDER_OPENAI,
    AgentProfileBinding,
    AgentRuntimeRefused,
    OuterToolBroker,
    ShiftRequest,
    ShiftResult,
    TokenUsage,
    ToolSpec,
    hydrate_shift_context,
)
from sourdough.agent_runtime._http import compute_stable_prefix_sha256
from sourdough.company import FoundingTerms, found
from sourdough.company_assets import Selection, assign_project, publish_selection
from sourdough.conversations import Post, append_message
from sourdough.duty_run import DutyRefused, WorkRunClaim
from sourdough.harness import create_agent_session
from sourdough.runtime.registry import DutyContext
from sourdough.runtime.shift import (
    ProvenanceIndex,
    ShiftFlowGuard,
    classify_tool_flow,
    finalize_shift_run,
)
from sourdough.runtime.shift import finalizer as finalizer_mod
from sourdough.runtime.tool_kit import FencedToolBase
from sourdough.runtime.work_kernel import (
    RunFenced,
    claim_work_run,
    complete_work_run,
    create_work_item,
)
from sourdough.storage import db as db_mod
from sourdough.storage.db import (
    StorageRefused,
    open_writer,
    scoped_reader,
    write_tx,
    write_uow,
)
from sourdough.writer_lease import hold_company_writer


class TestShiftFlowGuardAndUow(unittest.TestCase):
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
                "FlowGuard Co",
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

    def _hire(self, agent_id: str, scope: str = "assessment") -> tuple[str, str]:
        grant_id = f"grant-{agent_id}"
        with hold_company_writer(self.store, self.company_id) as w:
            with write_uow(w) as db:
                db.execute(
                    "INSERT INTO agent VALUES (?, ?, ?)",
                    (agent_id, self.company_id, self.profile.profile_id()),
                )
                db.execute(
                    "INSERT INTO standing_grant VALUES (?, ?, ?)",
                    (grant_id, agent_id, scope),
                )
                db.execute(
                    "INSERT INTO private_desk VALUES (?, ?)",
                    (agent_id, f"desk-{agent_id}"),
                )
        return agent_id, grant_id

    def _open_claim(
        self,
        agent_id: str | None = None,
        grant_id: str | None = None,
        scope: str = "manage-company",
    ):
        aid = agent_id or self.founder_id
        gid = grant_id or self.grant_id
        now = time_ns()
        with hold_company_writer(self.store, self.company_id) as w:
            with write_uow(w) as db:
                item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=aid,
                    duty_kind="work.assessment_review",
                    source_kind="unit",
                    source_id=f"src-{aid}-{now}",
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
                claim = WorkRunClaim(
                    company_id=self.company_id,
                    founder_id=aid,
                    duty_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    grant_id=gid,
                    grant_scope=scope,
                    purpose=scope,
                )
        return item, run, claim

    def test_tool_spec_flow_class_validation_and_prefix_invariance(self) -> None:
        with self.assertRaises(AgentRuntimeRefused):
            ToolSpec(
                name="bad_tool",
                description="Invalid class",
                parameters={"type": "object"},
                flow_class="not_a_class",  # type: ignore[arg-type]
            )

        neutral = ToolSpec(
            name="custom_connector_post",
            description="Post payload",
            parameters={"type": "object"},
            flow_class="neutral",
        )
        sink = ToolSpec(
            name="custom_connector_post",
            description="Post payload",
            parameters={"type": "object"},
            flow_class="external_write_sink",
        )
        self.assertFalse(neutral.requires_desk_isolation())
        self.assertTrue(sink.requires_desk_isolation())
        self.assertEqual(classify_tool_flow(sink.name, sink), "external_write_sink")

        # Prompt-cache stable prefix digest must ignore host-only flow_class metadata.
        ctx_pack = hydrate_shift_context(
            company_id=self.company_id,
            agent_id=self.founder_id,
            run_id="run-1",
            fencing_token=1,
            grant_scope="manage-company",
            mandate_summary="Mandate.",
        )
        req_neutral = ShiftRequest(
            company_id=self.company_id,
            agent_id=self.founder_id,
            run_id="run-1",
            turn_id="turn-1",
            fencing_token=1,
            instruction="Check.",
            context=ctx_pack,
            tools=(neutral,),
            budget_ceiling_minor_units=10_000,
        )
        req_sink = ShiftRequest(
            company_id=self.company_id,
            agent_id=self.founder_id,
            run_id="run-1",
            turn_id="turn-1",
            fencing_token=1,
            instruction="Check.",
            context=ctx_pack,
            tools=(sink,),
            budget_ceiling_minor_units=10_000,
        )
        self.assertEqual(
            compute_stable_prefix_sha256(req_neutral),
            compute_stable_prefix_sha256(req_sink),
        )

    def test_shift_flow_guard_enforces_custom_external_write_sink_taint(self) -> None:
        custom_sink = ToolSpec(
            name="dispatch_webhook_action",
            description="Custom external write sink",
            parameters={"type": "object"},
            flow_class="external_write_sink",
        )
        custom_priv = ToolSpec(
            name="custom_private_note_read",
            description="Custom private read",
            parameters={"type": "object"},
            flow_class="private_desk_read",
        )
        custom_peer = ToolSpec(
            name="custom_peer_feed_read",
            description="Custom peer read",
            parameters={"type": "object"},
            flow_class="peer_input",
        )
        specs = {s.name: s for s in (custom_sink, custom_priv, custom_peer)}
        prov = ProvenanceIndex(self.store, self.company_id, self.founder_id, "wi-1")

        # 1. Private desk read taints shift -> custom external write sink refused.
        guard_priv = ShiftFlowGuard(
            agent_id=self.founder_id,
            provenance=prov,
            specs_by_name=specs,
            public_offloaded_paths=set(),
        )
        guard_priv.check_before_call(custom_priv.name, {})
        guard_priv.record_after_call(custom_priv.name, {}, {"ok": True})
        self.assertTrue(guard_priv.read_private_desk)
        with self.assertRaises(DutyRefused):
            guard_priv.check_before_call(custom_sink.name, {})

        # 2. Peer input taints shift -> custom external write sink AND custom private read refused.
        guard_peer = ShiftFlowGuard(
            agent_id=self.founder_id,
            provenance=prov,
            specs_by_name=specs,
            public_offloaded_paths=set(),
        )
        guard_peer.check_before_call(custom_peer.name, {})
        guard_peer.record_after_call(custom_peer.name, {}, {"ok": True})
        self.assertTrue(guard_peer.read_peer_input)
        with self.assertRaises(DutyRefused):
            guard_peer.check_before_call(custom_sink.name, {})
        with self.assertRaises(DutyRefused):
            guard_peer.check_before_call(custom_priv.name, {})

    def test_provenance_index_memoizes_and_flow_guard_blocks_cross_project_exfil(
        self,
    ) -> None:
        peer_a, _ = self._hire("peer-a")
        peer_b, _ = self._hire("peer-b")
        item, _, claim = self._open_claim()

        with hold_company_writer(self.store, self.company_id) as w:
            rev_a = assign_project(
                w,
                self.grant_id,
                "proj-secret-a",
                (self.founder_id, peer_a),
                publishers=(self.founder_id, peer_a),
            )
            assign_project(
                w,
                self.grant_id,
                "proj-wide-b",
                (self.founder_id, peer_a, peer_b),
                publishers=(self.founder_id,),
            )
            draft = save_draft(
                w,
                claim,
                claim.purpose,
                name="memo-draft",
                content="Secret project A note",
                expected_revision=None,
            )
            pub = publish_selection(
                w,
                claim,
                claim.purpose,
                Selection(
                    request_id="pub-a-1",
                    draft_name=draft.name,
                    draft_revision=draft.revision,
                    project_id="proj-secret-a",
                    project_revision=rev_a,
                    audience=(self.founder_id, peer_a),
                    start=0,
                    end=len(draft.content),
                ),
            )
            msg = append_message(
                w,
                claim,
                claim.purpose,
                Post(
                    request_id="msg-a-1",
                    project_id="proj-secret-a",
                    project_revision=rev_a,
                    body="Check asset",
                    asset_id=pub.asset_id,
                ),
            )

        prov = ProvenanceIndex(
            self.store, self.company_id, self.founder_id, item.work_item_id
        )
        orig_open_reader = db_mod.open_reader
        open_calls = 0

        def _counting_open_reader(path: Path):
            nonlocal open_calls
            open_calls += 1
            return orig_open_reader(path)

        with mock.patch.object(db_mod, "open_reader", _counting_open_reader):
            # Single message_audience resolves message + project_members + asset_provenance in ONE reader open!
            pid, aud = prov.message_audience(msg.message_id)
            self.assertEqual(pid, "proj-secret-a")
            self.assertEqual(aud, {self.founder_id, peer_a})
            self.assertEqual(open_calls, 1)

            # Subsequent lookups hit shift-local cache (zero extra open_reader calls)!
            self.assertEqual(
                prov.project_members("proj-secret-a"), {self.founder_id, peer_a}
            )
            self.assertEqual(
                prov.asset_provenance(pub.asset_id),
                ("proj-secret-a", {self.founder_id, peer_a}),
            )
            self.assertEqual(
                prov.message_audience(msg.message_id),
                ("proj-secret-a", {self.founder_id, peer_a}),
            )
            self.assertEqual(open_calls, 1)

        # Audience lattice check: after reading msg from proj-secret-a, posting to proj-wide-b
        # or messaging peer_b must fail closed, and discovery output must strip proj-wide-b!
        guard = ShiftFlowGuard(
            agent_id=self.founder_id,
            provenance=prov,
            specs_by_name={},
            public_offloaded_paths=set(),
        )
        guard.record_after_call(
            "read_messages",
            {"project_id": "proj-secret-a"},
            {"messages": [{"message_id": msg.message_id}]},
        )
        self.assertEqual(guard.allowed_audience, {self.founder_id, peer_a})
        with self.assertRaises(DutyRefused):
            guard.check_before_call("append_message", {"project_id": "proj-wide-b"})
        with self.assertRaises(DutyRefused):
            guard.check_before_call("request_peer_response", {"recipient_id": peer_b})
        # Posting within allowed project succeeds.
        guard.check_before_call("append_message", {"project_id": "proj-secret-a"})

        filtered = guard.filter_discovery_output(
            "discover_project_references",
            {
                "projects": [
                    {
                        "project_id": "proj-secret-a",
                        "members": [self.founder_id, peer_a],
                    },
                    {
                        "project_id": "proj-wide-b",
                        "members": [self.founder_id, peer_a, peer_b],
                    },
                ],
                "shared_assets": [
                    {"asset_id": "a1", "project_id": "proj-secret-a"},
                    {"asset_id": "b1", "project_id": "proj-wide-b"},
                ],
                "publication_receipts": [
                    {"asset_id": "a1", "project_id": "proj-secret-a"},
                    {"asset_id": "b1", "project_id": "proj-wide-b"},
                ],
            },
        )
        self.assertEqual(
            [p["project_id"] for p in filtered["projects"]], ["proj-secret-a"]
        )
        self.assertEqual([a["asset_id"] for a in filtered["shared_assets"]], ["a1"])

    def test_write_uow_avoids_duplicate_schema_verify_and_enforces_rollback(
        self,
    ) -> None:
        orig_verify = db_mod._verify_store_schema
        verify_calls = 0

        def _counting_verify(conn, *, allow_empty: bool):
            nonlocal verify_calls
            verify_calls += 1
            return orig_verify(conn, allow_empty=allow_empty)

        with hold_company_writer(self.store, self.company_id) as w:
            with mock.patch.object(db_mod, "_verify_store_schema", _counting_verify):
                # Legacy open_writer + write_tx verifies twice.
                with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                    db.execute("SELECT 1").fetchone()
                self.assertEqual(verify_calls, 2)

                # write_uow verifies strictly once inside BEGIN IMMEDIATE!
                verify_calls = 0
                with write_uow(w) as db:
                    db.execute("SELECT 1").fetchone()
                self.assertEqual(verify_calls, 1)

            # Exception inside write_uow rolls back cleanly.
            with self.assertRaises(RuntimeError):
                with write_uow(w) as db:
                    db.execute(
                        "INSERT INTO agent VALUES (?, ?, ?)",
                        (
                            "rolled-back-agent",
                            self.company_id,
                            self.profile.profile_id(),
                        ),
                    )
                    raise RuntimeError("boom")

        with scoped_reader(self.store) as db:
            row = db.execute(
                "SELECT 1 FROM agent WHERE id = ?", ("rolled-back-agent",)
            ).fetchone()
            self.assertIsNone(row)

        # Retired store still fails closed under write_uow (verify runs inside write_tx).
        with hold_company_writer(self.store, self.company_id) as w:
            with write_uow(w) as db:
                db.execute(
                    "UPDATE company SET schema_revision = -1 WHERE singleton = 1"
                )
            with self.assertRaises(StorageRefused):
                with write_uow(w) as db:
                    db.execute("SELECT 1").fetchone()

    def test_tool_kit_fenced_reader_single_open_and_fencing_enforcement(self) -> None:
        _, run, claim = self._open_claim()
        orig_open_reader = db_mod.open_reader
        open_calls = 0

        def _counting_open_reader(path: Path):
            nonlocal open_calls
            open_calls += 1
            return orig_open_reader(path)

        with hold_company_writer(self.store, self.company_id) as w:
            kit = FencedToolBase(writer=w, claim=claim)
            with mock.patch.object(db_mod, "open_reader", _counting_open_reader):
                with kit.fenced_reader(verify_desk=True, verify_writer=True) as db:
                    val = db.execute("SELECT 1").fetchone()[0]
                    self.assertEqual(val, 1)
            self.assertEqual(open_calls, 1)

            # Completing run fences claim -> fenced_reader fails closed!
            with write_uow(w) as db:
                complete_work_run(
                    db, run.run_id, run.fencing_token, outcome={"done": True}
                )
            with self.assertRaises(RunFenced):
                with kit.fenced_reader():
                    pass

    def test_finalizer_logs_warning_on_episode_persistence_failure(self) -> None:
        item, run, claim = self._open_claim()
        ctx_pack = hydrate_shift_context(
            company_id=self.company_id,
            agent_id=self.founder_id,
            run_id=run.run_id,
            fencing_token=run.fencing_token,
            grant_scope="manage-company",
            mandate_summary="Test.",
        )
        with hold_company_writer(self.store, self.company_id) as w:
            session = create_agent_session(w, claim, claim.purpose)
            broker = OuterToolBroker(
                company_id=self.company_id,
                agent_id=self.founder_id,
                run_id=run.run_id,
                fencing_token=run.fencing_token,
                handlers={},
            )
            ctx = DutyContext(
                company_id=self.company_id,
                writer=w,
                work_item=item,
                run=run,
                _clock=time_ns(),
            )
            res = ShiftResult(
                provider=PROVIDER_OPENAI,
                model="gpt-mock",
                run_id=run.run_id,
                turn_id="turn-1",
                output_text="Done.",
                usage=TokenUsage(input_tokens=10, output_tokens=5),
                events=(),
                tool_calls_executed=0,
            )
            with (
                mock.patch.object(
                    finalizer_mod,
                    "record_episode",
                    side_effect=RuntimeError("synthetic episode db failure"),
                ),
                self.assertLogs(finalizer_mod._LOGGER, level=logging.WARNING) as logs,
            ):
                finalized = finalize_shift_run(
                    ctx,
                    agent_id=self.founder_id,
                    profile=self.profile,
                    turn_id="turn-1",
                    budget_ceiling=10_000,
                    session=session,
                    broker=broker,
                    context_pack=ctx_pack,
                    active_snapshot_id=None,
                    available_method_ids=[],
                    retrieved_method_ids=[],
                    invoked_method_ids=[],
                    stable_prefix_sha256="abc",
                    shift_result=res,
                    shift_error=None,
                )
            self.assertIsNotNone(finalized.settlement)
            self.assertTrue(
                any("Episode persistence failed" in msg for msg in logs.output)
            )

    def test_flow_guard_refreshes_destination_project_membership_mid_shift(
        self,
    ) -> None:
        """PR #195 Codex P1: outbound append_message refreshes live project membership."""
        peer_a, _ = self._hire("peer-p1-a")
        peer_b, _ = self._hire("peer-p1-b")
        item, _, claim = self._open_claim()

        with hold_company_writer(self.store, self.company_id) as w:
            rev_1 = assign_project(
                w,
                self.grant_id,
                "proj-expanding",
                (self.founder_id, peer_a),
                publishers=(self.founder_id, peer_a),
            )
            msg = append_message(
                w,
                claim,
                claim.purpose,
                Post(
                    request_id="msg-exp-1",
                    project_id="proj-expanding",
                    project_revision=rev_1,
                    body="Restricted 2-party note",
                    asset_id="",
                ),
            )

        prov = ProvenanceIndex(
            self.store, self.company_id, self.founder_id, item.work_item_id
        )
        guard = ShiftFlowGuard(
            agent_id=self.founder_id,
            provenance=prov,
            specs_by_name={},
            public_offloaded_paths=set(),
        )
        # Reading caches the initial 2-member project and narrows audience to {founder, peer_a}.
        guard.record_after_call(
            "read_messages",
            {"project_id": "proj-expanding"},
            {"messages": [{"message_id": msg.message_id}]},
        )
        self.assertEqual(guard.allowed_audience, {self.founder_id, peer_a})
        guard.check_before_call("append_message", {"project_id": "proj-expanding"})

        # Mid-shift enrollment expands proj-expanding to include peer_b!
        with hold_company_writer(self.store, self.company_id) as w:
            assign_project(
                w,
                self.grant_id,
                "proj-expanding",
                (self.founder_id, peer_a, peer_b),
                publishers=(self.founder_id, peer_a),
                expected_revision=rev_1,
            )

        # Outbound authorization MUST refresh membership and refuse posting to the expanded project!
        with self.assertRaises(DutyRefused):
            guard.check_before_call("append_message", {"project_id": "proj-expanding"})

    def test_custom_peer_input_narrows_fail_closed_and_blocks_custom_outbound(
        self,
    ) -> None:
        """PR #195 Codex P2: custom peer_input narrows audience to {agent_id} and blocks outbound sinks."""
        peer_a, _ = self._hire("peer-custom-in-a")
        with hold_company_writer(self.store, self.company_id) as w:
            assign_project(
                w,
                self.grant_id,
                "proj-broad",
                (self.founder_id, peer_a),
                publishers=(self.founder_id,),
            )

        custom_in = ToolSpec(
            name="connector_read_peer_feed",
            description="Custom peer feed reader",
            parameters={"type": "object"},
            flow_class="peer_input",
        )
        custom_out = ToolSpec(
            name="connector_send_peer_note",
            description="Custom outbound peer sender",
            parameters={"type": "object"},
            flow_class="peer_outbound",
        )
        prov = ProvenanceIndex(self.store, self.company_id, self.founder_id, "wi-c")
        guard = ShiftFlowGuard(
            agent_id=self.founder_id,
            provenance=prov,
            specs_by_name={custom_in.name: custom_in, custom_out.name: custom_out},
            public_offloaded_paths=set(),
        )
        guard.record_after_call(custom_in.name, {}, {"items": ["peer-text"]})
        self.assertTrue(guard.read_peer_input)
        self.assertEqual(guard.allowed_audience, {self.founder_id})
        with self.assertRaises(DutyRefused):
            guard.check_before_call("append_message", {"project_id": "proj-broad"})
        with self.assertRaises(DutyRefused):
            guard.check_before_call("request_peer_response", {"recipient_id": peer_a})
        with self.assertRaises(DutyRefused):
            guard.check_before_call(custom_out.name, {"body": "leak"})

    def test_custom_private_desk_read_receipt_redacted_regardless_of_name(
        self,
    ) -> None:
        """PR #195 Codex P2: custom private_desk_read tool redacts payload in tool_call_receipt."""
        import json

        from sourdough.runtime.shift import ShiftReceiptStore

        item, run, claim = self._open_claim()
        custom_priv = ToolSpec(
            name="connector_read_private",
            description="Custom private note reader",
            parameters={"type": "object"},
            flow_class="private_desk_read",
        )
        with hold_company_writer(self.store, self.company_id) as w:
            session = create_agent_session(w, claim, claim.purpose)
            receipt_store = ShiftReceiptStore(
                writer=w,
                session=session,
                specs_by_name={custom_priv.name: custom_priv},
                public_offloaded_paths=set(),
                stable_prefix_sha256="prefix-1",
                work_item_id=item.work_item_id,
            )
            receipt_store.record_receipt(
                {
                    "company_id": self.company_id,
                    "run_id": run.run_id,
                    "agent_id": self.founder_id,
                    "call_id": "call-priv-1",
                    "tool_name": custom_priv.name,
                    "arg_digest": "arg-1",
                    "status": "ok",
                    "result_digest": "res-1",
                    "result_json": json.dumps({"secret": "private-draft-canary"}),
                    "error_type": None,
                    "error_message": None,
                    "offloaded_log_path": None,
                    "recorded_at_ns": time_ns(),
                }
            )

        with scoped_reader(self.store) as db:
            row = db.execute(
                "SELECT result_json FROM tool_call_receipt WHERE run_id = ? AND call_id = ?",
                (run.run_id, "call-priv-1"),
            ).fetchone()
            assert row is not None
            raw = str(row[0])
            self.assertNotIn("private-draft-canary", raw)
            parsed = json.loads(raw)
            self.assertEqual(parsed["redacted"], "private_desk_payload")

    def test_resumed_work_item_restores_custom_peer_input_taint_fail_closed(
        self,
    ) -> None:
        """PR #195 Codex P2: resumed work item restores declarative peer_input taint across runs."""
        import json

        from sourdough.runtime.shift import (
            ShiftReceiptStore,
            hydrate_shift_session,
        )
        from sourdough.runtime.domains import ToolContribution
        from sourdough.runtime.work_kernel import fail_work_run

        item, run1, claim1 = self._open_claim()
        custom_peer = ToolSpec(
            name="connector_fetch_external_peer",
            description="Custom peer input",
            parameters={"type": "object"},
            flow_class="peer_input",
        )
        custom_sink = ToolSpec(
            name="connector_dispatch_action",
            description="Custom write sink",
            parameters={"type": "object"},
            flow_class="external_write_sink",
        )
        now = time_ns()
        with hold_company_writer(self.store, self.company_id) as w:
            session1 = create_agent_session(w, claim1, claim1.purpose)
            rs = ShiftReceiptStore(
                writer=w,
                session=session1,
                specs_by_name={custom_peer.name: custom_peer},
                public_offloaded_paths=set(),
                stable_prefix_sha256="p1",
                work_item_id=item.work_item_id,
            )
            rs.record_receipt(
                {
                    "company_id": self.company_id,
                    "run_id": run1.run_id,
                    "agent_id": self.founder_id,
                    "call_id": "call-peer-r1",
                    "tool_name": custom_peer.name,
                    "arg_digest": "arg-p1",
                    "status": "ok",
                    "result_digest": "res-p1",
                    "result_json": json.dumps({"note": "untrusted-peer-content"}),
                    "error_type": None,
                    "error_message": None,
                    "offloaded_log_path": None,
                    "recorded_at_ns": now,
                }
            )
            with write_uow(w) as db:
                fail_work_run(
                    db,
                    run1.run_id,
                    run1.fencing_token,
                    error_summary="transient",
                    return_to_due=True,
                    now_ns=now + 10,
                )
                run2 = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    lease_owner="unit-worker-2",
                    lease_ttl_ns=3_600_000_000_000,
                    now_ns=now + 20,
                )

            ctx2 = DutyContext(
                company_id=self.company_id,
                writer=w,
                work_item=item,
                run=run2,
                _clock=now + 20,
            )
            # Resume WITHOUT custom_peer in base_tools — taint must still restore from persisted flow metadata!
            hydrated = hydrate_shift_session(
                ctx2,
                persisted_profile=self.profile.profile_id(),
                grant_scope="manage-company",
                profile=self.profile,
                mandate_summary="Resume.",
                purpose="manage-company",
                base_tools=(custom_sink,),
                base_handlers={custom_sink.name: lambda _a: {"ok": True}},
                history_notes=(),
                open_receipts=(),
                include_core_desk_tools=True,
                include_core_skill_tools=None,
                include_plugin_tools=False,
                extra_recoverable_exceptions=(),
                pre_resolved_active_methods=((), None),
                allowed_tool_names=None,
                contribute_tools=lambda _q: ToolContribution(specs=(), handlers={}),
                active_methods_provider=lambda *_a: ((), None),
            )
            self.assertTrue(hydrated.prior_had_peer_input)
            self.assertTrue(hydrated.initial_had_peer_input)
            self.assertFalse(hydrated.effective_include_desk_tools)

            prov2 = ProvenanceIndex(
                self.store, self.company_id, self.founder_id, item.work_item_id
            )
            guard2 = ShiftFlowGuard(
                agent_id=self.founder_id,
                provenance=prov2,
                specs_by_name={s.name: s for s in hydrated.tool_specs},
                public_offloaded_paths=hydrated.public_offloaded_paths,
                read_private_desk=hydrated.has_hydrated_private_context,
                read_peer_input=hydrated.initial_had_peer_input,
            )
            guard2.seed_from_shift_context(
                work_item_source_kind=item.source_kind,
                work_item_payload_json=item.payload_json,
                open_receipts=(),
                prior_had_peer_input=hydrated.prior_had_peer_input,
                prior_peer_rows=hydrated.prior_peer_rows,
            )
            self.assertEqual(guard2.allowed_audience, {self.founder_id})
            with self.assertRaises(DutyRefused):
                guard2.check_before_call(custom_sink.name, {})


if __name__ == "__main__":
    unittest.main()
