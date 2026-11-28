"""Tier 3 opaque-box cross-feature pairwise combination tests (24 chains)."""

from __future__ import annotations

import json
import sys
import unittest
from contextlib import closing

from _helpers import (
    CoreCompanyHarness,
    LoopbackMultiProviderServer,
    assert_core_only_store,
    assert_no_canary_leak,
    count_rows,
    default_secret_resolver,
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
from sourdough.agent_desk import (
    desks_root,
    draft_at_revision,
    resolve_desk,
    save_draft,
)
from sourdough.agent_runtime import (
    PROVIDER_CODEX_CLI,
    PROVIDER_OPENAI,
    AgentBudgetExceeded,
    AgentProfileBinding,
)
from sourdough.company_assets import Selection, publish_selection, read_shared_asset
from sourdough.company_checkpoint import checkpoint_company
from sourdough.conversation_delivery import acknowledge_delivery, read_inbox
from sourdough.conversations import Post, append_message, read_messages
from sourdough.duty_run import DutyRefused
from sourdough.ipc.projections import (
    read_agent_detail_projection,
    read_system_evals_projection,
)
from sourdough.retirement import (
    TERMINATION_SCOPE,
    Retirement,
    employment_revision,
    retire_agent,
)
from sourdough.runtime.handlers import build_production_duty_registry
from sourdough.runtime.registry import DutyOutcome
from sourdough.runtime.work_kernel import (
    WorkKernelRefused,
    complete_work_run,
    create_work_item,
    evaluate_conditions,
    set_condition,
)
from sourdough.storage.checkpoint import restore_store
from sourdough.storage.db import open_writer, write_tx
from sourdough.trial_registry import (
    TrialCeiling,
    TrialInputs,
    TrialSlot,
    TrialSuite,
    invalidate_trial_slot,
    seal_trial_suite,
    summarize_rsi_transfer,
)
from sourdough.work_handoff import (
    HandoffOffer,
    accept_handoff,
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


class TestS1S2Pairwise(unittest.TestCase):
    """Tier 3 Subsystems 1 & 2 Pairwise Chains (10 cases: T3-S1S2-01..10)."""

    def test_t3_s1s2_01_desk_publish_plus_conversations_and_inbox(self) -> None:
        """T3-S1S2-01: Publish slice to (A, B) -> append_message to (A, B, C) -> B sees asset, C redacted."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            c = h.hire_agent("c")
            rev = h.create_project(
                "p-1",
                [h.founder_id, a.agent_id, b.agent_id, c.agent_id],
                publishers=[a.agent_id],
            )
            it_a, run_a, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope
            )
            _, _, cb = h.open_work_run_claim(b.agent_id, b.grant_id, b.grant_scope)
            _, _, cc = h.open_work_run_claim(c.agent_id, c.grant_id, c.grant_scope)
            items_before = count_rows(h.store, "work_items")
            with h.writer() as w:
                d = save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="notes",
                    content="Private prefix. Shared finding.",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    ca,
                    ca.purpose,
                    Selection(
                        "s1",
                        "notes",
                        d.revision,
                        "p-1",
                        rev,
                        (a.agent_id, b.agent_id),
                        16,
                        31,
                    ),
                )
                append_message(
                    w,
                    ca,
                    ca.purpose,
                    Post("p-1", rev, "m1", "Review finding", asset.asset_id),
                )
                b_inb, _ = read_inbox(w, cb, cb.purpose, "")
                c_inb, _ = read_inbox(w, cc, cc.purpose, "")
                acknowledge_delivery(w, cb, cb.purpose, b_inb[0].delivery_id)
                acknowledge_delivery(w, cc, cc.purpose, c_inb[0].delivery_id)
                b_msgs, _ = read_messages(w, cb, cb.purpose, "p-1", "")
                c_msgs, _ = read_messages(w, cc, cc.purpose, "p-1", "")
                self.assertEqual(
                    read_shared_asset(w, cb, cb.purpose, asset.asset_id).content,
                    "Shared finding.",
                )
                with self.assertRaises(DutyRefused):
                    read_shared_asset(w, cc, cc.purpose, asset.asset_id)
            self.assertEqual(b_msgs[0].asset_id, asset.asset_id)
            self.assertIsNone(c_msgs[0].asset_id)
            self.assertEqual(count_rows(h.store, "work_items"), items_before)

    def test_t3_s1s2_02_publish_plus_peer_response_plus_session_reopen(self) -> None:
        """T3-S1S2-02: Publish excerpt -> peer request -> yield -> B answers -> fresh A session resumes."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            rev = h.create_project(
                "p-2",
                [h.founder_id, a.agent_id, b.agent_id],
                publishers=[a.agent_id],
                peer_responders=(b.agent_id,),
            )
            it_a, run_a, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope
            )
            now = 2_000_000
            with h.writer() as w:
                d = save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="draft-a",
                    content="Private memo. Excerpt X.",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    ca,
                    ca.purpose,
                    Selection(
                        "pub-x",
                        "draft-a",
                        d.revision,
                        "p-2",
                        rev,
                        (a.agent_id, b.agent_id),
                        14,
                        24,
                    ),
                )
                request_peer_response(
                    w,
                    ca,
                    PeerQuestion(
                        "q-x",
                        b.agent_id,
                        "Validate X",
                        "ok",
                        now + 600,
                        asset.asset_id,
                        sha256_hex(asset.content),
                    ),
                    now=now,
                )
                cb = claim_peer_response(w, b.profile.profile_id(), now=now + 5)
                q = read_peer_question(w, cb, now=now + 6)
                self.assertEqual(q["evidence"]["content"], "Excerpt X.")
                answer_peer_question(w, cb, "answered", "Validated X.", now=now + 7)
                resumed = claim_resumed_peer_work(
                    w, a.profile.profile_id(), now=now + 10
                )
                _, drafts = resolve_desk(
                    w, resumed, resumed.purpose, include_drafts=True
                )
                out = read_peer_outcome(w, resumed)
            self.assertEqual([dr.revision for dr in drafts], [d.revision])
            self.assertEqual(out["answer"], "Validated X.")

    def test_t3_s1s2_03_desk_run_command_plus_codex_cli_adapter_mcp_bridge(
        self,
    ) -> None:
        """T3-S1S2-03: CodexCliAdapter runs desk_run_command, saves draft, publishes & settles GL."""
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
            a = h.hire_agent("codex-exec", grant_scope="desk:exec", profile=profile)
            rev = h.create_project(
                "p-cli", [h.founder_id, a.agent_id], publishers=[a.agent_id]
            )
            shim = write_codex_shim(
                h.root / "shim.py",
                f"""
import json, os, socket, sys
_ = json.loads(sys.stdin.read())
sock = os.environ["SOURDOUGH_BROKER_SOCKET"]
def call(cid, tool, args):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(sock)
        s.sendall((json.dumps({{"call_id": cid, "tool": tool, "arguments": args}}) + "\\n").encode())
        return json.loads(s.makefile().readline())
r0 = call("c0", "desk_run_command", {{"command": [sys.executable, "-c", "print('computed=42')"]}})
assert r0["ok"] and "computed=42" in r0["result"]["stdout"], r0
r1 = call("c1", "desk_save_draft", {{"name": "calc", "content": "computed=42 summary", "expected_revision": None}})
assert r1["ok"], r1
r2 = call("c2", "desk_publish_selection", {{
    "request_id": "cli-p", "draft_name": "calc", "draft_revision": r1["result"]["revision"],
    "project_id": "p-cli", "project_revision": {rev!r}, "audience": [{a.agent_id!r}], "start": 0, "end": 19,
}})
assert r2["ok"], r2
print(json.dumps({{"type": "terminal", "output_text": "Done", "usage": {{"input_tokens": 600, "output_tokens": 120}}}}))
""",
            )
            registry = srv.build_registry(
                secret_resolver=resolver,
                codex_executable=(sys.executable, str(shim)),
                codex_use_macos_sandbox=(sys.platform == "darwin"),
                codex_extra_read_files=(shim,),
            )
            it_a, run_a, ca = h.open_work_run_claim(a.agent_id, a.grant_id, "desk:exec")
            res = run_shift_for_claim(
                h,
                item=it_a,
                run=run_a,
                profile=profile,
                adapter_registry=registry,
                mandate_summary="Compute & publish.",
                instruction="Run desk command and publish.",
            )
            self.assertIsNotNone(res.episode)
            self.assertEqual(res.episode.outcome_status, "completed")
            self.assertEqual(
                count_rows(
                    h.store, "shared_publication", "author_id = ?", (a.agent_id,)
                ),
                1,
            )
            assert_no_canary_leak(self, h.root, canaries)

    def test_t3_s1s2_04_checkpoint_v2_restore_plus_waiting_peer_request(self) -> None:
        """T3-S1S2-04: Checkpoint v2 during waiting peer request restores identically; B answers and A resumes."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            h.create_project(
                "p-cp",
                [h.founder_id, a.agent_id, b.agent_id],
                publishers=[a.agent_id],
                peer_responders=(b.agent_id,),
            )
            it_a, run_a, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope
            )
            now = 3_000_000
            with h.writer() as w:
                d = save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="pre-loss",
                    content="Draft before checkpoint",
                    expected_revision=None,
                )
                request_peer_response(
                    w,
                    ca,
                    PeerQuestion("q-cp", b.agent_id, "Check?", "ok", now + 600),
                    now=now,
                )
            archive = h.root / "wait-cp.zip"
            manifest = checkpoint_company(h.store, r.company_id, archive)
            rdir = h.root / "restored-wait"
            rdir.mkdir(mode=0o700)
            rstore = rdir / "company.sqlite"
            restore_store(
                h.store,
                r.company_id,
                archive,
                rstore,
                desk_destination=rdir / "desks",
                lease_directory=h.leases,
            )
            self.assertEqual(read_watermark(rstore, r.company_id), manifest.watermark)
            from sourdough.writer_lease import hold_company_writer

            with hold_company_writer(
                rstore, r.company_id, lease_directory=h.leases
            ) as rw:
                cb = claim_peer_response(rw, b.profile.profile_id(), now=now + 5)
                read_peer_question(rw, cb, now=now + 6)
                answer_peer_question(rw, cb, "answered", "Restored answer", now=now + 7)
                resumed = claim_resumed_peer_work(
                    rw, a.profile.profile_id(), now=now + 10
                )
                out = read_peer_outcome(rw, resumed)
            self.assertEqual(out["answer"], "Restored answer")
            self.assertEqual(
                draft_at_revision(rstore, a.agent_id, "pre-loss", d.revision).content,
                "Draft before checkpoint",
            )

    def test_t3_s1s2_05_checkpoint_v2_restore_plus_offered_handoff_cas(self) -> None:
        """T3-S1S2-05: Offered handoff survives Checkpoint v2 restore; B accepts CAS on restored store."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            rev = h.create_project(
                "p-hc", [h.founder_id, a.agent_id, b.agent_id], publishers=[a.agent_id]
            )
            it_a, run_a, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope
            )
            _, _, cb = h.open_work_run_claim(b.agent_id, b.grant_id, b.grant_scope)
            now = 4_000_000
            with h.writer() as w:
                d = save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="ho",
                    content="Handoff asset",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    ca,
                    ca.purpose,
                    Selection(
                        "p-hc",
                        "ho",
                        d.revision,
                        "p-hc",
                        rev,
                        (a.agent_id, b.agent_id),
                        0,
                        13,
                    ),
                )
                offer_id = offer_handoff(
                    w,
                    ca,
                    HandoffOffer(
                        "ho-cp", b.agent_id, asset.asset_id, now + 600, "Verify"
                    ),
                    now=now,
                )
            archive = h.root / "ho-cp.zip"
            checkpoint_company(h.store, r.company_id, archive)
            rdir = h.root / "restored-ho"
            rdir.mkdir(mode=0o700)
            rstore = rdir / "company.sqlite"
            restore_store(
                h.store,
                r.company_id,
                archive,
                rstore,
                desk_destination=rdir / "desks",
                lease_directory=h.leases,
            )
            from sourdough.writer_lease import hold_company_writer

            with hold_company_writer(
                rstore, r.company_id, lease_directory=h.leases
            ) as rw:
                succ = accept_handoff(rw, cb, cb.purpose, offer_id, now=now + 10)
            self.assertTrue(succ)
            self.assertEqual(
                query_ro(
                    rstore,
                    "SELECT state, state_version FROM work_handoff WHERE offer_id = ?",
                    (offer_id,),
                )[0],
                ("accepted", 2),
            )

    def test_t3_s1s2_06_membership_revision_fencing_plus_audience_revocation(
        self,
    ) -> None:
        """T3-S1S2-06: Removing C revokes rev1; stale rev1 publish/message fail; rev2 excludes C."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            c = h.hire_agent("c")
            rev1 = h.create_project(
                "p-mr",
                [h.founder_id, a.agent_id, b.agent_id, c.agent_id],
                publishers=[a.agent_id],
            )
            rev2 = h.create_project(
                "p-mr",
                [h.founder_id, a.agent_id, b.agent_id],
                publishers=[a.agent_id],
                expected_revision=rev1,
            )
            it_a, run_a, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope
            )
            _, _, cc = h.open_work_run_claim(c.agent_id, c.grant_id, c.grant_scope)
            with h.writer() as w:
                d = save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="m",
                    content="Updated notice",
                    expected_revision=None,
                )
                with self.assertRaises(DutyRefused):
                    publish_selection(
                        w,
                        ca,
                        ca.purpose,
                        Selection(
                            "s",
                            "m",
                            d.revision,
                            "p-mr",
                            rev1,
                            (a.agent_id, b.agent_id),
                            0,
                            14,
                        ),
                    )
                with self.assertRaises(DutyRefused):
                    append_message(
                        w, ca, ca.purpose, Post("p-mr", rev1, "m-stale", "Msg", "")
                    )
                asset = publish_selection(
                    w,
                    ca,
                    ca.purpose,
                    Selection(
                        "s2",
                        "m",
                        d.revision,
                        "p-mr",
                        rev2,
                        (a.agent_id, b.agent_id),
                        0,
                        14,
                    ),
                )
                append_message(
                    w,
                    ca,
                    ca.purpose,
                    Post("p-mr", rev2, "m-ok", "Msg rev2", asset.asset_id),
                )
                with self.assertRaises(DutyRefused):
                    read_messages(w, cc, cc.purpose, "p-mr", "")

    def test_t3_s1s2_07_peer_response_plus_handoff_cas_chain(self) -> None:
        """T3-S1S2-07: A requests B -> B answers -> A publishes & offers handoff to C -> C accepts."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            c = h.hire_agent("c")
            rev = h.create_project(
                "p-ph",
                [h.founder_id, a.agent_id, b.agent_id, c.agent_id],
                publishers=[a.agent_id],
                peer_responders=(b.agent_id,),
            )
            it_a, run_a, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope
            )
            _, _, cc = h.open_work_run_claim(c.agent_id, c.grant_id, c.grant_scope)
            now = 5_000_000
            with h.writer() as w:
                request_peer_response(
                    w,
                    ca,
                    PeerQuestion("q-ph", b.agent_id, "Input?", "ok", now + 600),
                    now=now,
                )
                cb = claim_peer_response(w, b.profile.profile_id(), now=now + 5)
                read_peer_question(w, cb, now=now + 6)
                answer_peer_question(w, cb, "answered", "Confirmed data", now=now + 7)
                resumed_a = claim_resumed_peer_work(
                    w, a.profile.profile_id(), now=now + 10
                )
                d = save_draft(
                    w,
                    resumed_a,
                    resumed_a.purpose,
                    name="final",
                    content="Verified by B: Confirmed data",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    resumed_a,
                    resumed_a.purpose,
                    Selection(
                        "p-final",
                        "final",
                        d.revision,
                        "p-ph",
                        rev,
                        (a.agent_id, c.agent_id),
                        0,
                        29,
                    ),
                )
                offer_id = offer_handoff(
                    w,
                    resumed_a,
                    HandoffOffer(
                        "ho-c", c.agent_id, asset.asset_id, now + 600, "Final signoff"
                    ),
                    now=now + 12,
                )
                succ_c = accept_handoff(w, cc, cc.purpose, offer_id, now=now + 15)
            self.assertEqual(
                query_ro(
                    h.store,
                    "SELECT owner_agent_id FROM work_items WHERE work_item_id = ?",
                    (succ_c,),
                )[0][0],
                c.agent_id,
            )

    def test_t3_s1s2_08_o1_offboarding_with_open_peer_request_and_handoff(self) -> None:
        """T3-S1S2-08: Archiving A mid-flight cancels open peer request without restarting A; asset stays attributed."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            c = h.hire_agent("c")
            hr = h.hire_agent("hr-term", grant_scope=TERMINATION_SCOPE)
            rev = h.create_project(
                "p-o1c",
                [h.founder_id, a.agent_id, b.agent_id, c.agent_id],
                publishers=[a.agent_id],
                peer_responders=(b.agent_id,),
            )
            it_a, run_a, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope
            )
            _, _, cc = h.open_work_run_claim(c.agent_id, c.grant_id, c.grant_scope)
            now = 6_000_000
            with h.writer() as w:
                d = save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="pub",
                    content="Attribution preserved",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    ca,
                    ca.purpose,
                    Selection(
                        "p-o",
                        "pub",
                        d.revision,
                        "p-o1c",
                        rev,
                        (a.agent_id, c.agent_id),
                        0,
                        21,
                    ),
                )
                request_peer_response(
                    w,
                    ca,
                    PeerQuestion("q-o", b.agent_id, "Q?", "ok", now + 600),
                    now=now,
                )
                emp = employment_revision(w, hr.agent_id, hr.grant_id, a.agent_id)
                retire_agent(
                    w,
                    hr.agent_id,
                    hr.grant_id,
                    Retirement("r-o1", a.agent_id, emp, "Departed"),
                )
                self.assertIsNone(
                    claim_resumed_peer_work(w, a.profile.profile_id(), now=now + 10)
                )
                self.assertEqual(
                    read_shared_asset(w, cc, cc.purpose, asset.asset_id).author_id,
                    a.agent_id,
                )

    def test_t3_s1s2_09_clone_desk_snapshot_plus_episodes_fts_isolation(self) -> None:
        """T3-S1S2-09: Cloned arm Desk stays isolated from live Desk and episodes_fts.sqlite stays external."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            item, run_a, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope
            )
            with h.writer() as w:
                save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="base",
                    content="Live base",
                    expected_revision=None,
                )
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    memory_mod = lazy_module("sourdough.memory")
                    memory_mod.record_episode(
                        db,
                        company_id=h.company_id,
                        agent_id=a.agent_id,
                        work_item_id=item.work_item_id,
                        run_id=run_a.run_id,
                        duty_kind="work.assessment_review",
                        context_digest="a" * 64,
                        outcome_status="completed",
                        summary_text="Quarterly vendor compliance audit completed.",
                        recorded_at_ns=5_000,
                    )
                    complete_work_run(
                        db,
                        run_a.run_id,
                        run_a.fencing_token,
                        outcome={"done": True},
                        now_ns=5_000,
                    )
            wm_before = read_watermark(h.store, h.company_id)
            ep_idx = lazy_module("sourdough.episode_index")
            ep_idx.sync_episodes_index(h.store, h.company_id)
            fts_path = ep_idx.episode_index_path(h.store, h.company_id)
            self.assertTrue(fts_path.is_file())
            self.assertEqual(
                len(
                    ep_idx.search_episodes(
                        h.store, h.company_id, a.agent_id, "compliance audit"
                    )
                ),
                1,
            )
            self.assertEqual(
                ep_idx.search_episodes(
                    h.store, h.company_id, b.agent_id, "compliance audit"
                ),
                [],
            )
            clone_desk_snapshot = lazy_attr(
                "sourdough.agent_desk", "clone_desk_snapshot"
            )
            arm1_store = h.root / "arm1" / "company.sqlite"
            arm2_store = h.root / "arm2" / "company.sqlite"
            arm1_store.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            arm2_store.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            clone_desk_snapshot(h.store, h.company_id, (a.agent_id,), arm1_store)
            clone_desk_snapshot(h.store, h.company_id, (a.agent_id,), arm2_store)
            arm1_desk = desks_root(arm1_store) / a.agent_id
            arm2_desk = desks_root(arm2_store) / a.agent_id
            (arm1_desk / ".desk" / "drafts" / "base.md").write_text(
                "Arm1 mutated", encoding="utf-8"
            )
            self.assertEqual(
                (arm2_desk / ".desk" / "drafts" / "base.md").read_text(
                    encoding="utf-8"
                ),
                "Live base",
            )
            self.assertFalse(any(arm1_desk.rglob("*episodes_fts*")))
            self.assertFalse(any(arm2_desk.rglob("*episodes_fts*")))
            self.assertEqual(read_watermark(h.store, h.company_id), wm_before)
            assert_core_only_store(self, h.store)

    def test_t3_s1s2_10_shift_runner_offload_plus_recoverable_refusal_plus_peer_request(
        self,
    ) -> None:
        """T3-S1S2-10: Offloaded >4KB tool output -> recoverable refusal -> valid peer request yields cleanly."""
        with CoreCompanyHarness() as h, LoopbackMultiProviderServer() as srv:
            resolver, _ = default_secret_resolver()
            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:SD_OPENAI_KEY",
            )
            h.found_company(profile=profile)
            a = h.hire_agent("a", profile=profile)
            b = h.hire_agent("b", profile=profile)
            h.create_project(
                "p-sr",
                [h.founder_id, a.agent_id, b.agent_id],
                publishers=[a.agent_id],
                peer_responders=(b.agent_id,),
            )
            it_a, run_a, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope, duty_kind="work.discovery_review"
            )
            with h.writer() as w:
                save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="big-report",
                    content="R" * 5500,
                    expected_revision=None,
                    limits=lazy_attr("sourdough.agent_desk", "PRODUCTION_DESK_LIMITS"),
                )
            srv.openai_queue.put(
                openai_tool_call_response(
                    "r0", "c0", "desk_read_draft", {"name": "big-report"}
                )
            )
            srv.openai_queue.put(
                openai_tool_call_response(
                    "r1", "c1", "cancel_condition", {"condition_id": "missing"}
                )
            )
            srv.openai_queue.put(
                openai_tool_call_response(
                    "r2",
                    "c2",
                    "request_peer_response",
                    {
                        "request_key": "k-sr",
                        "recipient_id": b.agent_id,
                        "question": "Please confirm?",
                        "expected_response": "yes/no",
                        "expires_at": 4_102_444_800,
                    },
                )
            )
            srv.openai_queue.put(openai_text_response("r3", "Waiting on B."))
            registry = srv.build_registry(secret_resolver=resolver)
            build_proactive = lazy_attr(
                "sourdough.runtime.proactive_tools",
                "build_core_proactive_tool_contribution",
            )
            build_collab = lazy_attr(
                "sourdough.runtime.collab_tools",
                "build_core_collab_tool_contribution",
            )
            merge_contribs = lazy_attr(
                "sourdough.runtime.domains", "merge_tool_contributions"
            )
            with h.writer() as w:
                proactive = build_proactive(writer=w, claim=ca)
                collab = build_collab(
                    writer=w,
                    claim=ca,
                    purpose=ca.purpose,
                    include_messaging=True,
                    include_peer_request=True,
                    include_peer_respond=False,
                    include_handoffs=True,
                    include_roster=True,
                )
                extra_tools, extra_handlers, extra_recoverable = merge_contribs(
                    (), {}, (proactive, collab)
                )
                run_shift_for_claim(
                    h,
                    item=it_a,
                    run=run_a,
                    profile=profile,
                    adapter_registry=registry,
                    writer=w,
                    base_tools=extra_tools,
                    base_handlers=extra_handlers,
                    extra_recoverable_exceptions=extra_recoverable,
                    mandate_summary="Test offload, recovery, then peer wait.",
                    instruction="Read big draft, recover, and ask peer B.",
                )
            logs_dir = h.agent_desk_path(a.agent_id) / ".desk" / "logs"
            self.assertGreaterEqual(len(list(logs_dir.glob("*.json"))), 1)
            self.assertGreaterEqual(
                count_rows(
                    h.store,
                    "tool_call_receipt",
                    "company_id = ? AND run_id = ? AND offloaded_log_path IS NOT NULL",
                    (h.company_id, ca.run_id),
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


class TestFullStackPairwise(unittest.TestCase):
    """Tier 3 S3/S4 & Full-Stack Pairwise Chains (14 cases: T3_01..T3_14)."""

    def test_t3_01_desk_publish_to_work_event_cursor_propagation(self) -> None:
        """T3_01: Desk publish emits asset.published -> work_event_cursor wakes watcher A (not author B)."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            rev = h.create_project(
                "p-cur", [h.founder_id, a.agent_id, b.agent_id], publishers=[b.agent_id]
            )
            ra = h.register_discovery_responsibility(a.agent_id, now_ns=1_000)
            rb = h.register_discovery_responsibility(b.agent_id, now_ns=1_000)
            _, _, cb = h.open_work_run_claim(b.agent_id, b.grant_id, b.grant_scope)
            with h.writer() as w:
                d = save_draft(
                    w,
                    cb,
                    cb.purpose,
                    name="pub",
                    content="Cursor wake signal",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    cb,
                    cb.purpose,
                    Selection(
                        "p-c",
                        "pub",
                        d.revision,
                        "p-cur",
                        rev,
                        (a.agent_id, b.agent_id),
                        0,
                        18,
                    ),
                )
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    set_condition(
                        db,
                        company_id=h.company_id,
                        owner_agent_id=a.agent_id,
                        responsibility_id=ra,
                        trigger_kind="shared_asset_revision",
                        source_identity=asset.asset_id,
                        now_ns=2_000,
                    )
                    set_condition(
                        db,
                        company_id=h.company_id,
                        owner_agent_id=b.agent_id,
                        responsibility_id=rb,
                        trigger_kind="shared_asset_revision",
                        source_identity=asset.asset_id,
                        now_ns=2_000,
                    )
            woken: list[str] = []
            reg = build_production_duty_registry(
                adapter_registry=LoopbackMultiProviderServer().build_registry(
                    secret_resolver=default_secret_resolver()[0]
                ),
                profile_bindings={},
                domain_plugins=(),
                extra_handlers={
                    "work.discovery_review": lambda ctx: (
                        woken.append(ctx.work_item.owner_agent_id),
                        DutyOutcome(disposition="completed", result={"ok": True}),
                    )[1]
                },
            )
            h.make_runtime(reg).step(max_dispatches=4, now_ns=3_000)
            self.assertEqual(woken, [a.agent_id])

    def test_t3_02_proactive_discovery_to_peer_request_wait_wake_decision(self) -> None:
        """T3_02: Timer discovery -> request_peer_response -> wait -> B responds -> cursor wake -> record_work_decision."""
        with CoreCompanyHarness() as h, LoopbackMultiProviderServer() as srv:
            resolver, _ = default_secret_resolver()
            p_openai = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:SD_OPENAI_KEY",
            )
            h.found_company(profile=p_openai)
            a = h.hire_agent("a", profile=p_openai)
            b = h.hire_agent("b", profile=p_openai)
            h.create_project(
                "p-pw",
                [h.founder_id, a.agent_id, b.agent_id],
                publishers=[a.agent_id],
                peer_responders=(b.agent_id,),
            )
            ra = h.register_discovery_responsibility(a.agent_id, now_ns=1_000)
            with h.writer() as w:
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    set_condition(
                        db,
                        company_id=h.company_id,
                        owner_agent_id=a.agent_id,
                        responsibility_id=ra,
                        trigger_kind="timer",
                        due_at_ns=2_000,
                        now_ns=1_000,
                    )
            rt = h.make_runtime(
                build_production_duty_registry(
                    adapter_registry=srv.build_registry(secret_resolver=resolver),
                    profile_bindings={
                        h.founder_id: p_openai,
                        a.agent_id: p_openai,
                        b.agent_id: p_openai,
                    },
                    domain_plugins=(),
                )
            )
            # Step 1: A's timer fires -> A calls request_peer_response and enters waiting
            srv.openai_queue.put(
                openai_tool_call_response(
                    "r-a1",
                    "c-a1",
                    "request_peer_response",
                    {
                        "request_key": "t3-02-q",
                        "recipient_id": b.agent_id,
                        "question": "Confirm variance?",
                        "expected_response": "answered/declined",
                        "expires_at": 4_102_444_800,
                    },
                )
            )
            srv.openai_queue.put(openai_text_response("r-a2", "Waiting on B."))
            step1 = rt.step(max_dispatches=1, now_ns=2_000)
            self.assertEqual(len(step1.admitted_items), 1)
            a_item = step1.admitted_items[0]
            self.assertEqual(
                query_ro(
                    h.store,
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (a_item,),
                )[0][0],
                "waiting",
            )

            # Step 2: B answers peer request via respond_peer_request
            srv.openai_queue.put(
                openai_tool_call_response(
                    "r-b1",
                    "c-b1",
                    "respond_peer_request",
                    {"outcome": "answered", "answer": "Variance confirmed."},
                )
            )
            srv.openai_queue.put(openai_text_response("r-b2", "Answered A."))
            rt.step(max_dispatches=1, now_ns=3_000)

            # Step 3: work_event_cursor wakes A -> A calls record_work_decision and settles
            srv.openai_queue.put(
                openai_tool_call_response(
                    "r-a3",
                    "c-a3",
                    "record_work_decision",
                    {
                        "action_kind": "hold",
                        "rationale": "Peer confirmed variance.",
                        "evidence": [],
                        "decision_at_ns": 4_000,
                    },
                )
            )
            srv.openai_queue.put(openai_text_response("r-a4", "Decision recorded."))
            rt.step(max_dispatches=1, now_ns=4_000)
            self.assertEqual(
                count_rows(
                    h.store,
                    "work_decision",
                    "company_id = ? AND agent_id = ?",
                    (h.company_id, a.agent_id),
                ),
                1,
            )
            self.assertEqual(
                query_ro(
                    h.store,
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (a_item,),
                )[0][0],
                "settled",
            )

    def test_t3_03_multi_agent_simultaneous_publish_plus_coalesced_cooldown(
        self,
    ) -> None:
        """T3_03: Simultaneous publish resolves per-asset author_id and coalesces bursts during cooldown."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            rev = h.create_project(
                "p-coal",
                [h.founder_id, a.agent_id, b.agent_id],
                publishers=[a.agent_id, b.agent_id],
            )
            ra = h.register_discovery_responsibility(a.agent_id, now_ns=1_000)
            it_a, run_a, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope
            )
            _, _, cb = h.open_work_run_claim(b.agent_id, b.grant_id, b.grant_scope)
            with h.writer() as w:
                da = save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="a",
                    content="A text",
                    expected_revision=None,
                )
                db_d = save_draft(
                    w,
                    cb,
                    cb.purpose,
                    name="b",
                    content="B text",
                    expected_revision=None,
                )
                aa = publish_selection(
                    w,
                    ca,
                    ca.purpose,
                    Selection(
                        "pa",
                        "a",
                        da.revision,
                        "p-coal",
                        rev,
                        (a.agent_id, b.agent_id),
                        0,
                        6,
                    ),
                )
                ab = publish_selection(
                    w,
                    cb,
                    cb.purpose,
                    Selection(
                        "pb",
                        "b",
                        db_d.revision,
                        "p-coal",
                        rev,
                        (a.agent_id, b.agent_id),
                        0,
                        6,
                    ),
                )
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    set_condition(
                        db,
                        company_id=h.company_id,
                        owner_agent_id=a.agent_id,
                        responsibility_id=ra,
                        trigger_kind="shared_asset_revision",
                        source_identity=ab.asset_id,
                        cooldown_ns=10_000,
                        now_ns=1_000,
                    )
                    admitted = evaluate_conditions(
                        db,
                        h.company_id,
                        now_ns=2_000,
                        observed_assets={aa.asset_id: "r1", ab.asset_id: "r1"},
                        triggering_actor_id=None,
                    )
            self.assertEqual(len(admitted), 1)
            self.assertIn(ab.asset_id, admitted[0].payload_json)

    def test_t3_04_continuation_after_result_plus_crash_recovery(self) -> None:
        """T3_04: Continuation item reclaimed after worker crash fences old token and settles cleanly."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            a = h.hire_agent("a")
            with h.writer() as w:
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    item, _ = create_work_item(
                        db,
                        company_id=h.company_id,
                        owner_agent_id=a.agent_id,
                        duty_kind="work.discovery_review",
                        source_kind="e2e",
                        source_id="cont-crash",
                        now_ns=1_000,
                    )
                    from sourdough.runtime.work_kernel import claim_work_run

                    run1 = claim_work_run(
                        db,
                        company_id=h.company_id,
                        work_item_id=item.work_item_id,
                        lease_owner="crashed",
                        lease_ttl_ns=500,
                        now_ns=1_000,
                    )
            reg = build_production_duty_registry(
                adapter_registry=LoopbackMultiProviderServer().build_registry(
                    secret_resolver=default_secret_resolver()[0]
                ),
                profile_bindings={},
                domain_plugins=(),
                extra_handlers={
                    "work.discovery_review": lambda _c: DutyOutcome(
                        disposition="completed", result={"recovered": True}
                    )
                },
            )
            step = h.make_runtime(reg).step(max_dispatches=2, now_ns=5_000)
            self.assertIn(run1.run_id, step.fenced_runs)
            self.assertEqual(len(step.executed_runs), 1)
            self.assertTrue(r.company_id)

    def test_t3_05_recoverable_tool_refusal_to_valid_decision_and_gl_settlement(
        self,
    ) -> None:
        """T3_05: Recoverable WorkKernelRefused -> valid record_work_decision -> GL cost settled."""
        with CoreCompanyHarness() as h, LoopbackMultiProviderServer() as srv:
            resolver, _ = default_secret_resolver()
            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:SD_OPENAI_KEY",
            )
            h.found_company(profile=profile)
            a = h.hire_agent("a", profile=profile)
            srv.openai_queue.put(
                openai_tool_call_response(
                    "r1", "c1", "cancel_condition", {"condition_id": "bad"}
                )
            )
            srv.openai_queue.put(
                openai_tool_call_response(
                    "r2",
                    "c2",
                    "record_work_decision",
                    {
                        "action_kind": "hold",
                        "rationale": "Recovered",
                        "evidence": [],
                        "decision_at_ns": 5_000,
                    },
                )
            )
            srv.openai_queue.put(openai_text_response("r3", "Done."))
            with h.writer() as w:
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    create_work_item(
                        db,
                        company_id=h.company_id,
                        owner_agent_id=a.agent_id,
                        duty_kind="work.discovery_review",
                        source_kind="e2e",
                        source_id="t3-05",
                        now_ns=1_000,
                    )
            reg = build_production_duty_registry(
                adapter_registry=srv.build_registry(secret_resolver=resolver),
                profile_bindings={h.founder_id: profile, a.agent_id: profile},
                domain_plugins=(),
            )
            h.make_runtime(reg).step(max_dispatches=1, now_ns=5_000)
            self.assertEqual(count_rows(h.store, "work_decision"), 1)
            self.assertGreaterEqual(count_rows(h.store, "journal_entry"), 1)

    def test_t3_06_budget_exceeded_during_discovery_and_peer_work_continues(
        self,
    ) -> None:
        """T3_06: A exceeds budget -> GL settled, A blocked; B continues its discovery_review cleanly."""
        with CoreCompanyHarness() as h, LoopbackMultiProviderServer() as srv:
            resolver, _ = default_secret_resolver()
            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:SD_OPENAI_KEY",
            )
            h.found_company(profile=profile)
            a = h.hire_agent("a", profile=profile)
            b = h.hire_agent("b", profile=profile)
            with h.writer() as w:
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    ia, _ = create_work_item(
                        db,
                        company_id=h.company_id,
                        owner_agent_id=a.agent_id,
                        duty_kind="work.discovery_review",
                        source_kind="e2e",
                        source_id="a",
                        now_ns=1_000,
                    )
                    ib, _ = create_work_item(
                        db,
                        company_id=h.company_id,
                        owner_agent_id=b.agent_id,
                        duty_kind="work.discovery_review",
                        source_kind="e2e",
                        source_id="b",
                        now_ns=2_000,
                    )
            srv.openai_queue.put(
                openai_text_response(
                    "r1", "Over budget", input_tokens=200_000, output_tokens=80_000
                )
            )
            srv.openai_queue.put(
                openai_tool_call_response(
                    "r2a",
                    "c2a",
                    "record_work_decision",
                    {"action_kind": "noop", "rationale": "Within budget."},
                    input_tokens=5,
                    output_tokens=2,
                )
            )
            srv.openai_queue.put(
                openai_text_response(
                    "r2b", "Within budget.", input_tokens=5, output_tokens=2
                )
            )
            reg = build_production_duty_registry(
                adapter_registry=srv.build_registry(secret_resolver=resolver),
                profile_bindings={
                    h.founder_id: profile,
                    a.agent_id: profile,
                    b.agent_id: profile,
                },
                default_budget_ceiling_minor=1,
                domain_plugins=(),
            )
            rt = h.make_runtime(reg)
            with self.assertRaises(AgentBudgetExceeded):
                rt.step(max_dispatches=1, now_ns=3_000)
            rt.step(max_dispatches=1, now_ns=3_500)
            self.assertEqual(
                query_ro(
                    h.store,
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (ia.work_item_id,),
                )[0][0],
                "blocked",
            )
            self.assertEqual(
                query_ro(
                    h.store,
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (ib.work_item_id,),
                )[0][0],
                "settled",
            )

    def test_t3_07_future_leak_refusal_to_episode_recording_and_method_distillation(
        self,
    ) -> None:
        """T3_07: Future-leak decision refusal captured in episode -> distilled as work_method correction."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            item, _, ca = h.open_work_run_claim(a.agent_id, a.grant_id, a.grant_scope)
            record_work_decision = lazy_attr(
                "sourdough.runtime.work_kernel", "record_work_decision"
            )
            DecisionEvidence = lazy_attr(
                "sourdough.runtime.work_kernel", "DecisionEvidence"
            )
            with h.writer() as w:
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    with self.assertRaises(WorkKernelRefused):
                        record_work_decision(
                            db,
                            company_id=h.company_id,
                            work_item_id=item.work_item_id,
                            run_id=ca.run_id,
                            fencing_token=ca.fencing_token,
                            agent_id=a.agent_id,
                            action_kind="hold",
                            rationale="Future leak",
                            evidence=(
                                DecisionEvidence(
                                    "shared_publication", "x", "r", 200, 999
                                ),
                            ),
                            decision_at_ns=100,
                        )
            ml = lazy_module("sourdough.method_learning")
            with h.writer() as w:
                delta = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m-no-future-leak",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Verify available_at_ns <= decision_at_ns before recording work_decision.",
                    audience_scope="one_agent",
                    source_refs=[f"work_run:{ca.run_id}"],
                )
            self.assertEqual(delta.status, "candidate")

    def test_t3_08_private_desk_note_to_redaction_gate_to_3_stage_gate_to_adoption(
        self,
    ) -> None:
        """T3_08: Mixed private/shared note passes redact_for_shared_scope -> 3-stage gate -> company adoption."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            a = h.hire_agent("a")
            rev = h.create_project(
                "p-m", [h.founder_id, a.agent_id], publishers=[a.agent_id]
            )
            it_a, run_a, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope
            )
            with h.writer() as w:
                d = save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="note",
                    content="Verified compliance rule.",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    ca,
                    ca.purpose,
                    Selection(
                        "r-m",
                        "note",
                        d.revision,
                        "p-m",
                        rev,
                        (h.founder_id, a.agent_id),
                        0,
                        25,
                    ),
                )
            ml = lazy_module("sourdough.method_learning")
            with h.writer() as w:
                clean = ml.redact_for_shared_scope(
                    w,
                    ca,
                    raw_body="See .desk/drafts/note.md; enforce compliance rule.",
                    source_refs=[f"shared_publication:{asset.asset_id}"],
                    target_scope="company",
                )
                delta = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m-adopted",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body=clean.redacted_body,
                    audience_scope="company",
                    source_refs=[f"shared_publication:{asset.asset_id}"],
                )
                ml.qualify_method_delta(
                    w,
                    delta.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                )
                act = ml.activate_method_delta(
                    w, delta.delta_id, actor_id=r.founder_id, grant_id=r.grant_id
                )
            self.assertTrue(act.activation_id)

    def test_t3_09_adopted_method_context_pack_v2_proactive_shift_and_causal_tracking(
        self,
    ) -> None:
        """T3_09: Adopted method binds into ContextPack v2 on next proactive shift with 4-step causal tracking."""
        with CoreCompanyHarness() as h, LoopbackMultiProviderServer() as srv:
            resolver, _ = default_secret_resolver()
            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:SD_OPENAI_KEY",
            )
            r = h.found_company(profile=profile)
            a = h.hire_agent("a", profile=profile)
            it_a, run_a, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope
            )
            ml = lazy_module("sourdough.method_learning")
            with h.writer() as w:
                d = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m-ctx",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Always inspect SLA.",
                    audience_scope="company",
                    source_refs=[f"work_run:{ca.run_id}"],
                )
                ml.qualify_method_delta(
                    w,
                    d.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                )
                ml.activate_method_delta(
                    w, d.delta_id, actor_id=r.founder_id, grant_id=r.grant_id
                )
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    complete_work_run(
                        db,
                        run_a.run_id,
                        run_a.fencing_token,
                        outcome={"distilled": True},
                        now_ns=900,
                    )
                    create_work_item(
                        db,
                        company_id=h.company_id,
                        owner_agent_id=a.agent_id,
                        duty_kind="work.discovery_review",
                        source_kind="e2e",
                        source_id="s-ctx",
                        now_ns=1_000,
                    )
            srv.openai_queue.put(openai_text_response("r1", "Applied m-ctx."))
            reg = build_production_duty_registry(
                adapter_registry=srv.build_registry(secret_resolver=resolver),
                profile_bindings={h.founder_id: profile, a.agent_id: profile},
                domain_plugins=(),
            )
            h.make_runtime(reg).step(max_dispatches=1, now_ns=2_000)
            self.assertIn("m-ctx", json.dumps(srv.requests[0][2]))

    def test_t3_10_lineage_quarantine_rollback_to_shift_fallback_and_system_evals_projection(
        self,
    ) -> None:
        """T3_10: Quarantining M0 drops M0..M2 from subsequent shift and surfaces blast radius in projections."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            a = h.hire_agent("a")
            it_a, run_a, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope
            )
            ml = lazy_module("sourdough.method_learning")
            with h.writer() as w:
                d0 = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m0",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Root",
                    audience_scope="company",
                    source_refs=[f"work_run:{ca.run_id}"],
                )
                ml.qualify_method_delta(
                    w,
                    d0.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                )
                a0 = ml.activate_method_delta(
                    w, d0.delta_id, actor_id=r.founder_id, grant_id=r.grant_id
                )
                d1 = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m1",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Child",
                    audience_scope="company",
                    source_refs=[f"work_run:{ca.run_id}"],
                    parent_method_ids=("m0",),
                    active_snapshot_id=a0.active_snapshot_id,
                )
                ml.qualify_method_delta(
                    w,
                    d1.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                )
                ml.activate_method_delta(
                    w, d1.delta_id, actor_id=r.founder_id, grant_id=r.grant_id
                )
                ml.quarantine_method_lineage(
                    w, h.company_id, method_id="m0", reason="Regressed"
                )
                self.assertEqual(
                    ml.resolve_active_methods(w, h.company_id, agent_id=a.agent_id), ()
                )
            evals, _ = read_system_evals_projection(h.store, h.company_id)
            self.assertIn("method_activations", evals)

    def test_t3_11_handoff_cas_plus_proactive_followup_plus_offboarding(self) -> None:
        """T3_11: A offers handoff to B -> B accepts CAS -> A archived -> A's conditions cancelled, B runs."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            b = h.hire_agent("b")
            hr = h.hire_agent("hr-term", grant_scope=TERMINATION_SCOPE)
            rev = h.create_project(
                "p-ho-off",
                [h.founder_id, a.agent_id, b.agent_id],
                publishers=[a.agent_id],
            )
            ra = h.register_discovery_responsibility(a.agent_id, now_ns=1_000)
            it_a, run_a, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope
            )
            _, _, cb = h.open_work_run_claim(b.agent_id, b.grant_id, b.grant_scope)
            now = 7_000_000
            with h.writer() as w:
                with closing(open_writer(h.store, lease=w)) as db, write_tx(db):
                    set_condition(
                        db,
                        company_id=h.company_id,
                        owner_agent_id=a.agent_id,
                        responsibility_id=ra,
                        trigger_kind="timer",
                        due_at_ns=now + 1000,
                        now_ns=now,
                    )
                d = save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="ho",
                    content="Audit result",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    ca,
                    ca.purpose,
                    Selection(
                        "r-ho",
                        "ho",
                        d.revision,
                        "p-ho-off",
                        rev,
                        (a.agent_id, b.agent_id),
                        0,
                        12,
                    ),
                )
                oid = offer_handoff(
                    w,
                    ca,
                    HandoffOffer(
                        "ho-off", b.agent_id, asset.asset_id, now + 600, "Continue"
                    ),
                    now=now,
                )
                succ = accept_handoff(w, cb, cb.purpose, oid, now=now + 5)
                emp = employment_revision(w, hr.agent_id, hr.grant_id, a.agent_id)
                retire_agent(
                    w,
                    hr.agent_id,
                    hr.grant_id,
                    Retirement("ret-a", a.agent_id, emp, "Done"),
                )
            self.assertEqual(
                count_rows(
                    h.store,
                    "work_conditions",
                    "owner_agent_id = ? AND status = 'armed'",
                    (a.agent_id,),
                ),
                0,
            )
            self.assertTrue(succ)

    def test_t3_12_disputed_receipt_rejection_plus_offline_trial_invalidation(
        self,
    ) -> None:
        """T3_12: Disputed method claim and invalidated trial slot both fail closed without promotion."""
        with CoreCompanyHarness() as h:
            h.found_company()
            a = h.hire_agent("a")
            it_a, run_a, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope
            )
            ml = lazy_module("sourdough.method_learning")
            with h.writer() as w:
                disp = ml.distill_method_delta(
                    w,
                    ca,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m-disp",
                    trigger_spec={"duty_kind": "work.discovery_review"},
                    body="Missing source",
                    audience_scope="one_agent",
                    source_refs=["work_decision:missing-id"],
                )
                self.assertEqual(disp.status, "disputed")
            plan_doc = {"min_pairs": 1, "min_net_wins": 1}
            plan_digest = sha256_hex(
                json.dumps(plan_doc, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            suite = TrialSuite(
                "s-inv",
                "recursive",
                sha256_hex(b"r"),
                sha256_hex(b"e"),
                plan_digest,
                slots=(
                    TrialSlot(
                        "s1",
                        "c1",
                        "f1",
                        TrialInputs(
                            sha256_hex(b"f"),
                            sha256_hex(b"c"),
                            sha256_hex(b"i"),
                            sha256_hex(b"t"),
                            "p1",
                        ),
                        TrialCeiling(2, 10, 100),
                        "incumbent",
                        "s2",
                    ),
                    TrialSlot(
                        "s2",
                        "c1",
                        "f1",
                        TrialInputs(
                            sha256_hex(b"f"),
                            sha256_hex(b"c"),
                            sha256_hex(b"x"),
                            sha256_hex(b"t"),
                            "p1",
                        ),
                        TrialCeiling(2, 10, 100),
                        "candidate",
                        "s1",
                    ),
                ),
            )
            rev = seal_trial_suite(h.evaluator_store, suite, analysis_plan=plan_doc)
            invalidate_trial_slot(
                h.evaluator_store,
                revision=rev,
                slot_id="s2",
                evidence_digest=sha256_hex(b"leak"),
            )
            self.assertNotEqual(
                summarize_rsi_transfer(
                    h.evaluator_store, revision=rev, min_pairs=1, min_net_wins=1
                ).verdict,
                "SUPPORTED",
            )

    def test_t3_13_two_gen_rsi_clone_desk_to_blind_judge_to_small_n_gate_to_projection(
        self,
    ) -> None:
        """T3_13: Isolated cloned arms execute 2-Gen RSI -> blind (AB, BA) -> SUPPORTED -> projection."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            meta = h.hire_agent("meta")
            tr = lazy_module("sourdough.trial_runner")
            with h.writer() as w:
                cycle = tr.run_recursive_learning_cycle(
                    w,
                    evaluator_store=h.evaluator_store,
                    company_id=h.company_id,
                    meta_agent_id=meta.agent_id,
                    promotion_actor_id=r.founder_id,
                    promotion_grant_id=r.grant_id,
                    trial_root=h.root / "rsi",
                )
            self.assertEqual(cycle.transfer_verdict, "SUPPORTED")
            evals, _ = read_system_evals_projection(
                h.store, r.company_id, evaluator_store=h.evaluator_store
            )
            self.assertTrue(evals["offline_evaluator"]["connected"])
            self.assertGreaterEqual(len(evals["rsi_transfer_summaries"]), 1)

    def test_t3_14_agent_detail_and_system_evals_privacy_across_full_lifecycle(
        self,
    ) -> None:
        """T3_14: Both projections stay coherent and zero-leak after drafts, decisions, episodes & methods."""
        with CoreCompanyHarness() as h:
            r = h.found_company()
            a = h.hire_agent("a")
            it_a, run_a, ca = h.open_work_run_claim(
                a.agent_id, a.grant_id, a.grant_scope
            )
            sentinel = "PRIVATE_DESK_SENTINEL_XYZ"
            with h.writer() as w:
                save_draft(
                    w,
                    ca,
                    ca.purpose,
                    name="secret-draft",
                    content=sentinel,
                    expected_revision=None,
                )
            det, s1 = read_agent_detail_projection(
                h.store, r.company_id, a.agent_id, domain_plugins=()
            )
            ev, s2 = read_system_evals_projection(h.store, r.company_id)
            self.assertEqual(det["desk_workspace"]["private_draft_count"], 1)
            self.assertNotIn("secret-draft", json.dumps(det))
            self.assertNotIn(sentinel, json.dumps(det))
            self.assertNotIn(sentinel, json.dumps(ev))
            self.assertEqual(s1, s2)


if __name__ == "__main__":
    unittest.main()
