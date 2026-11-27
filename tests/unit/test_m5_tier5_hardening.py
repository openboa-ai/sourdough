"""Tier 5 white-box adversarial regression tests covering Pillars 1-4, Always-On & Recs #1-#3."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import unittest
from contextlib import closing
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from typing import Any

from sourdough.agent_desk import (
    allocate_run_scratch,
    assert_writable_desk_relative_path,
    compute_agent_desk_digest,
    desk_directory,
    save_draft,
    write_desk_harness_files,
)
from sourdough.agent_runtime import (
    PROVIDER_ANTHROPIC,
    PROVIDER_GEMINI,
    PROVIDER_OPENAI,
    AdapterManifest,
    AgentBudgetExceeded,
    AgentProfileBinding,
    AgentRuntimeAdapter,
    AgentRuntimeRefused,
    InMemorySecretResolver,
    OuterToolBroker,
    ShiftRequest,
    ShiftResult,
    TokenUsage,
    build_default_adapter_registry,
)
from sourdough.agent_runtime.registry import AgentAdapterRegistry
from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    bind_board_principal,
    post_ceo_message,
)
from sourdough.company import FoundingTerms, found
from sourdough.company_assets import Selection, assign_project, publish_selection
from sourdough.conversation_delivery import read_inbox
from sourdough.conversations import Post, append_message, read_messages
from sourdough.duty_run import DutyRefused, WorkRunClaim
from sourdough.episode_index import (
    episode_index_path,
    search_episodes,
    upsert_episode_entry,
)
from sourdough.harness import (
    DeskCommandLimits,
    create_agent_session,
    run_desk_command_sandboxed,
)
from sourdough.memory import record_episode
from sourdough.method_learning import (
    MethodRefused,
    activate_method_delta,
    distill_method_delta,
    qualify_method_delta,
    quarantine_method_lineage,
    redact_for_shared_scope,
    sync_desk_harness_projections,
)
from sourdough.retirement import (
    TERMINATION_SCOPE,
    Retirement,
    employment_revision,
    retire_agent,
)
from sourdough.runtime import (
    CompanyRuntime,
    DutyOutcome,
    build_production_duty_registry,
    create_work_item,
    register_responsibility,
)
from sourdough.runtime.handlers import sync_board_triggers
from sourdough.runtime.collab_tools import build_core_collab_tool_contribution
from sourdough.runtime.method_tools import build_core_skill_tool_contribution
from sourdough.runtime.work_kernel import (
    DecisionEvidence,
    RunFenced,
    WorkItem,
    WorkKernelRefused,
    claim_work_run,
    complete_work_run,
    evaluate_conditions,
    open_peer_request,
    reclaim_expired_runs,
    record_work_decision,
    set_condition,
)
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.trial_runner import (
    _score_live_gen2_observables,
    run_recursive_learning_cycle,
)
from sourdough.work_handoff import HandoffOffer, accept_handoff, offer_handoff
from sourdough.work_peer_response import (
    answer_peer_question,
    claim_peer_response,
    read_peer_question,
)
from sourdough.writer_lease import hold_company_writer


class _LoopbackJsonServer:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                _ = self.rfile.read(length)
                encoded = json.dumps(parent.body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._srv.server_address
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=2.0)


def _hire_active_agent(
    writer: Any,
    *,
    company_id: str,
    grant_id: str,
    agent_id: str,
    profile_id: str,
    scope: str = "assessment",
) -> tuple[str, str]:
    del grant_id
    hire_grant = f"grant-{agent_id}"
    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        db.execute(
            "INSERT INTO agent VALUES (?, ?, ?)",
            (agent_id, company_id, profile_id),
        )
        db.execute(
            "INSERT INTO standing_grant VALUES (?, ?, ?)",
            (hire_grant, agent_id, scope),
        )
        db.execute(
            "INSERT INTO private_desk VALUES (?, ?)",
            (agent_id, f"desk-{agent_id}"),
        )
    return agent_id, hire_grant


def _open_work_run_claim(
    db: sqlite3.Connection,
    *,
    company_id: str,
    agent_id: str,
    grant_id: str,
    grant_scope: str,
    duty_kind: str = "work.assessment_review",
    purpose: str = "assessment_review",
    now_ns: int = 1_000,
) -> tuple[WorkItem, Any, WorkRunClaim]:
    lease_ttl_ns = 3_600_000_000_000
    eff_now = time_ns() if now_ns == 1_000 else now_ns
    item, _ = create_work_item(
        db,
        company_id=company_id,
        owner_agent_id=agent_id,
        duty_kind=duty_kind,
        source_kind="unit",
        source_id=f"src-{agent_id}-{duty_kind}-{eff_now}-{os.urandom(3).hex()}",
        now_ns=eff_now,
    )
    run = claim_work_run(
        db,
        company_id=company_id,
        work_item_id=item.work_item_id,
        lease_owner="unit-worker",
        lease_ttl_ns=lease_ttl_ns,
        now_ns=eff_now,
    )
    claim = WorkRunClaim(
        company_id=company_id,
        founder_id=agent_id,
        duty_id=item.work_item_id,
        run_id=run.run_id,
        fencing_token=run.fencing_token,
        grant_id=grant_id,
        grant_scope=grant_scope,
        purpose=purpose,
    )
    return item, run, claim


class TestM5Tier5Hardening(unittest.TestCase):
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
                "Tier5 Co",
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

    def _open_claim(
        self,
        agent_id: str | None = None,
        grant_id: str | None = None,
        scope: str = "manage-company",
    ):
        aid = agent_id or self.founder_id
        gid = grant_id or self.grant_id
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                return _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=aid,
                    grant_id=gid,
                    grant_scope=scope,
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )

    def test_rec1_host_managed_roots_casefold_and_unicode(self) -> None:
        for blocked in (
            "AGENTS.md",
            "Agents.md",
            "agents.md",
            "AGENTS.MD",
            "\uff21GENTS.md",
            "skills/foo/SKILL.md",
            "SKILLS/foo/SKILL.md",
            "Skills/bar",
            ".desk/drafts/x",
            ".DESK/drafts/x",
            ".Desk/logs/a.json",
            ".sourdough-traces/t.jsonl",
            ".SOURDOUGH-TRACES/t.jsonl",
        ):
            with self.subTest(path=blocked):
                with self.assertRaises(DutyRefused):
                    assert_writable_desk_relative_path(blocked)
        self.assertEqual(
            assert_writable_desk_relative_path("notes/analysis.md").as_posix(),
            "notes/analysis.md",
        )

    def test_rec2_seatbelt_denies_drafts_and_logs_writes_symlinks_renames(self) -> None:
        if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            self.skipTest("macOS sandbox-exec required")

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                db.execute(
                    "UPDATE standing_grant SET scope = 'desk:exec' WHERE id = ?",
                    (self.grant_id,),
                )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _item, _run, claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="desk:exec",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
            save_draft(
                w,
                claim,
                claim.purpose,
                name="legit",
                content="safe draft body",
                expected_revision=None,
            )
            session = create_agent_session(w, claim, claim.purpose)

        # Ensure .desk/logs exists too and probe writes/symlinks/renames
        logs_dir = session.desk_path / ".desk" / "logs"
        logs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        probe_script = (
            "import json, os\n"
            "res = {}\n"
            "with open('tmp_src.txt', 'w') as f: f.write('ok')\n"
            "res['regular'] = 'ALLOWED'\n"
            "def try_op(name, fn):\n"
            "    try:\n"
            "        fn(); res[name] = 'ALLOWED'\n"
            "    except PermissionError:\n"
            "        res[name] = 'BLOCKED'\n"
            "try_op('open_draft', lambda: open('.desk/drafts/evil.json', 'w'))\n"
            "try_op('symlink_draft', lambda: os.symlink('/etc/passwd', '.desk/drafts/s'))\n"
            "try_op('rename_draft', lambda: os.rename('tmp_src.txt', '.desk/drafts/r.json'))\n"
            "try_op('open_log', lambda: open('.desk/logs/evil.log', 'w'))\n"
            "try_op('symlink_log', lambda: os.symlink('/tmp', '.desk/logs/s'))\n"
            "try_op('rename_dotdesk', lambda: os.rename('.desk', '.desk_evil'))\n"
            "try_op('mkdir_dotdesk_x', lambda: os.mkdir('.desk/x'))\n"
            "try_op('open_dotdesk_pwn', lambda: open('.desk/pwn', 'w'))\n"
            "try_op('rename_dotdesk_scratch', lambda: os.rename('.desk/scratch', '.desk/scratch_evil'))\n"
            "tmpdir = os.environ['TMPDIR']\n"
            "try_op('rename_scratch_self', lambda: os.rename(tmpdir, tmpdir + '_evil'))\n"
            "with open(os.path.join(tmpdir, 'scratch_ok.txt'), 'w') as sf:\n"
            "    sf.write('scratch-allowed')\n"
            "res['scratch_write'] = 'ALLOWED'\n"
            "print(json.dumps(res))\n"
        )
        out = run_desk_command_sandboxed(
            session,
            [sys.executable, "-c", probe_script],
            store=self.store,
            limits=DeskCommandLimits(timeout_seconds=10.0),
        )
        self.assertTrue(out["ok"], out)
        parsed = json.loads(out["stdout"])
        self.assertEqual(
            parsed,
            {
                "regular": "ALLOWED",
                "open_draft": "BLOCKED",
                "symlink_draft": "BLOCKED",
                "rename_draft": "BLOCKED",
                "open_log": "BLOCKED",
                "symlink_log": "BLOCKED",
                "rename_dotdesk": "BLOCKED",
                "mkdir_dotdesk_x": "BLOCKED",
                "open_dotdesk_pwn": "BLOCKED",
                "rename_dotdesk_scratch": "BLOCKED",
                "rename_scratch_self": "BLOCKED",
                "scratch_write": "ALLOWED",
            },
        )
        # Also verify non-existent .desk/drafts cannot be symlinked!
        drafts_dir = session.desk_path / ".desk" / "drafts"
        for child in drafts_dir.iterdir():
            child.unlink()
        drafts_dir.rmdir()
        out_missing = run_desk_command_sandboxed(
            session,
            [
                sys.executable,
                "-c",
                "import os\ntry:\n os.symlink('/etc/passwd', '.desk/drafts')\n print('ALLOWED')\nexcept PermissionError:\n print('BLOCKED')",
            ],
            store=self.store,
            limits=DeskCommandLimits(timeout_seconds=10.0),
        )
        self.assertTrue(out_missing["ok"], out_missing)
        self.assertEqual(out_missing["stdout"].strip(), "BLOCKED")
        self.assertEqual(
            len(compute_agent_desk_digest(self.store, self.founder_id)), 64
        )

    def test_p1_01_skill_slug_case_collision_disambiguation(self) -> None:
        _, _, claim = self._open_claim()
        with hold_company_writer(self.store, self.company_id) as w:
            for mid, body in (("SkillCase", "UPPER BODY"), ("skillcase", "LOWER BODY")):
                d = distill_method_delta(
                    w,
                    claim,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id=mid,
                    trigger_spec={"duty_kind": "work.assessment_review"},
                    body=body,
                    audience_scope="company",
                    source_refs=[f"work_run:{claim.run_id}"],
                    now_ns=2_000,
                )
                qualify_method_delta(
                    w,
                    d.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                    now_ns=2_100,
                )
                activate_method_delta(
                    w,
                    d.delta_id,
                    actor_id=self.founder_id,
                    grant_id=self.grant_id,
                    now_ns=2_200,
                )

            desk = sync_desk_harness_projections(
                self.store, self.company_id, self.founder_id
            )
            assert desk is not None
            skill_dirs = sorted(p.name for p in (desk / "skills").iterdir())
            self.assertEqual(len(skill_dirs), 2)

            contrib = build_core_skill_tool_contribution(writer=w, claim=claim)
            listed = contrib.handlers["skill_list_active"]({})
            self.assertEqual(len(listed["skills"]), 2)
            self.assertEqual(len({s["skill_name"] for s in listed["skills"]}), 2)

            upper = contrib.handlers["skill_read"]({"method_id": "SkillCase"})
            lower = contrib.handlers["skill_read"]({"method_id": "skillcase"})
            self.assertEqual(upper["skill"]["body_text"], "UPPER BODY")
            self.assertIn("UPPER BODY", upper["skill_markdown"])
            self.assertNotIn("LOWER BODY", upper["skill_markdown"])
            self.assertEqual(lower["skill"]["body_text"], "LOWER BODY")
            self.assertIn("LOWER BODY", lower["skill_markdown"])
            self.assertNotIn("UPPER BODY", lower["skill_markdown"])

            with self.assertRaises(DutyRefused):
                write_desk_harness_files(
                    self.store,
                    self.founder_id,
                    agents_md="# Test\n",
                    skills={
                        "Alpha": {"SKILL.md": b"a"},
                        "alpha": {"SKILL.md": b"b"},
                    },
                )

    def test_p1_01_skill_list_active_duty_filter_preserves_disambiguated_slugs(
        self,
    ) -> None:
        _, _, claim = self._open_claim()
        with hold_company_writer(self.store, self.company_id) as w:
            for mid, duty_kind, body in (
                ("SkillCase", "work.assessment_review", "ASSESS BODY"),
                ("skillcase", "work.discovery_review", "DISC BODY"),
            ):
                d = distill_method_delta(
                    w,
                    claim,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id=mid,
                    trigger_spec={"duty_kind": duty_kind},
                    body=body,
                    audience_scope="company",
                    source_refs=[f"work_run:{claim.run_id}"],
                    now_ns=2_000,
                )
                qualify_method_delta(
                    w,
                    d.delta_id,
                    passed_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                    now_ns=2_100,
                )
                activate_method_delta(
                    w,
                    d.delta_id,
                    actor_id=self.founder_id,
                    grant_id=self.grant_id,
                    now_ns=2_200,
                )

            contrib = build_core_skill_tool_contribution(writer=w, claim=claim)
            filtered = contrib.handlers["skill_list_active"](
                {"duty_kind": "work.assessment_review"}
            )
            self.assertEqual(len(filtered["skills"]), 1)
            skill_entry = filtered["skills"][0]
            self.assertEqual(skill_entry["method_id"], "SkillCase")
            self.assertIn("-", skill_entry["skill_name"])
            read_back = contrib.handlers["skill_read"](
                {"skill_path": skill_entry["skill_path"]}
            )
            self.assertEqual(read_back["skill"]["method_id"], "SkillCase")
            self.assertEqual(read_back["skill"]["body_text"], "ASSESS BODY")

    def test_p1_02_stale_scratch_symlink_pruned_and_projection_error_surfaced(
        self,
    ) -> None:
        outside = self.root / "outside_secret.txt"
        outside.write_text("do-not-delete", encoding="utf-8")
        crashed_dir = (
            desk_directory(self.store, self.founder_id)
            / ".desk"
            / "scratch"
            / "crashed-run"
        )
        crashed_dir.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, crashed_dir / "bad_link")

        new_scratch = allocate_run_scratch(self.store, self.founder_id, "fresh-run")
        self.assertTrue(new_scratch.is_dir())
        self.assertFalse(os.path.lexists(crashed_dir))
        self.assertEqual(outside.read_text(encoding="utf-8"), "do-not-delete")

        # Also test reclaim_expired_runs + ShiftRunner surfacing Desk projection tamper error
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                exp_item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    source_kind="manual",
                    source_id="exp-scratch",
                    now_ns=1_000,
                )
                exp_run = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=exp_item.work_item_id,
                    lease_owner="w",
                    lease_ttl_ns=100,
                    now_ns=1_000,
                )
            exp_scratch = allocate_run_scratch(
                self.store, self.founder_id, exp_run.run_id
            )
            os.symlink(outside, exp_scratch / "dangling")
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                fenced = reclaim_expired_runs(db, self.company_id, now_ns=5_000)
            self.assertEqual(fenced, (exp_run.run_id,))
            self.assertFalse(os.path.lexists(exp_scratch))
            self.assertEqual(outside.read_text(encoding="utf-8"), "do-not-delete")

        # ShiftRunner surfaces symlink tamper inside skills/ rather than swallowing
        rogue_link = desk_directory(self.store, self.founder_id) / "skills" / "rogue"
        rogue_link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, rogue_link)

        class _NoopAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(self, profile, request, broker):
                return ShiftResult(
                    output_text="done",
                    usage=TokenUsage(1, 1),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=0,
                    events=(),
                )

        reg = AgentAdapterRegistry()
        reg.register(_NoopAdapter())

        def _custom_handler(ctx):
            from sourdough.runtime.shift_runner import ShiftRunner

            sr = ShiftRunner(
                adapter_registry=reg,
                profile_bindings={self.founder_id: self.profile},
                default_budget_ceiling_minor=10_000,
                read_agent_state=lambda s, c, a: (
                    self.profile.profile_id(),
                    "manage-company",
                ),
                resolve_profile=lambda b, a: b[a],
                contribute_tools=lambda _q: (
                    __import__(
                        "sourdough.runtime.domains", fromlist=["ToolContribution"]
                    ).ToolContribution()
                ),
            )
            sr.run(ctx, mandate_summary="m", instruction="i")
            return DutyOutcome(disposition="completed", result={})

        duty_reg = build_production_duty_registry(
            adapter_registry=reg,
            profile_bindings={self.founder_id: self.profile},
            extra_handlers={"work.assessment_review": _custom_handler},
        )
        runtime = CompanyRuntime(
            self.store,
            self.company_id,
            duty_reg,
            clock=lambda: 10_000,
        )
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.assessment_review",
                    source_kind="manual",
                    source_id="tamper-test",
                    now_ns=10_000,
                )
        with self.assertRaises(DutyRefused):
            runtime.step(max_dispatches=1, now_ns=10_000)

    def test_p1_03_upsert_episode_entry_recovers_from_corrupt_fts_db(self) -> None:
        _, _, claim = self._open_claim()
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                ep = record_episode(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    work_item_id=claim.duty_id,
                    run_id=claim.run_id,
                    duty_kind="work.assessment_review",
                    context_digest="0" * 64,
                    outcome_status="completed",
                    summary_text="Unique quarterly liquidity audit completed cleanly.",
                    recorded_at_ns=5_000,
                )
        idx_path = episode_index_path(self.store, self.company_id)
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        idx_path.write_bytes(b"CORRUPTED_NON_SQLITE_BYTES")

        upsert_episode_entry(self.store, ep)
        hits = search_episodes(
            self.store, self.company_id, self.founder_id, "liquidity"
        )
        self.assertEqual([h["episode_id"] for h in hits], [ep.episode_id])

    def test_p2_01_open_peer_request_fencing_before_replay(self) -> None:
        with hold_company_writer(self.store, self.company_id) as w:
            peer_id, _ = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="peer-p201",
                profile_id=self.profile.profile_id(),
            )
            assign_project(
                w,
                self.grant_id,
                "proj-p201",
                (self.founder_id, peer_id),
                publishers=(self.founder_id,),
                peer_responders=(peer_id,),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                item, run, _claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
                req, replayed = open_peer_request(
                    db,
                    company_id=self.company_id,
                    requester_work_item_id=item.work_item_id,
                    requester_run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    recipient_agent_id=peer_id,
                    question="Check Q?",
                    expected_decision="yes_no",
                    evidence_refs=[],
                    deadline_ns=90_000,
                    request_id="req-p201-1",
                    request_key="rk-p201",
                    project_id="proj-p201",
                    now_ns=1_500,
                )
                self.assertFalse(replayed)
                # Stale fencing_token must raise RunFenced rather than returning replay
                with self.assertRaises(RunFenced):
                    open_peer_request(
                        db,
                        company_id=self.company_id,
                        requester_work_item_id=item.work_item_id,
                        requester_run_id=run.run_id,
                        fencing_token=999999,
                        recipient_agent_id=peer_id,
                        question="Check Q?",
                        expected_decision="yes_no",
                        evidence_refs=[],
                        deadline_ns=90_000,
                        request_id="req-p201-1",
                        request_key="rk-p201",
                        project_id="proj-p201",
                        now_ns=1_600,
                    )
                # Legitimate waiting-run replay with exact token still succeeds
                req2, replayed2 = open_peer_request(
                    db,
                    company_id=self.company_id,
                    requester_work_item_id=item.work_item_id,
                    requester_run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    recipient_agent_id=peer_id,
                    question="Check Q?",
                    expected_decision="yes_no",
                    evidence_refs=[],
                    deadline_ns=90_000,
                    request_id="req-p201-dup",
                    request_key="rk-p201",
                    project_id="proj-p201",
                    now_ns=1_600,
                )
                self.assertTrue(replayed2)
                self.assertEqual(req2.request_id, req.request_id)

    def test_p2_02_handoff_acceptance_cancels_armed_work_conditions(self) -> None:
        with hold_company_writer(self.store, self.company_id) as w:
            sender_id, sender_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="sender-p202",
                profile_id=self.profile.profile_id(),
            )
            recip_id, recip_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="recip-p202",
                profile_id=self.profile.profile_id(),
            )
            proj_rev = assign_project(
                w,
                self.grant_id,
                "proj-p202",
                (sender_id, recip_id),
                publishers=(sender_id,),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                s_item, _, cs = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=sender_id,
                    grant_id=sender_g,
                    grant_scope="assessment",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
                cond = set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=sender_id,
                    work_item_id=s_item.work_item_id,
                    trigger_kind="timer",
                    due_at_ns=5_000,
                    now_ns=1_500,
                )
            d = save_draft(
                w,
                cs,
                cs.purpose,
                name="ho-asset",
                content="Shared handoff artifact",
                expected_revision=None,
            )
            asset = publish_selection(
                w,
                cs,
                cs.purpose,
                Selection(
                    "pub-p202",
                    "ho-asset",
                    d.revision,
                    "proj-p202",
                    proj_rev,
                    (sender_id, recip_id),
                    0,
                    len(d.content),
                ),
            )
            offer_id = offer_handoff(
                w,
                cs,
                HandoffOffer(
                    "ho-req-p202", recip_id, asset.asset_id, 100, "Verify artifact"
                ),
                now=2,
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, cr = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=recip_id,
                    grant_id=recip_g,
                    grant_scope="assessment",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=3_000,
                )
            accept_handoff(w, cr, cr.purpose, offer_id, now=3)
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                admitted = evaluate_conditions(db, self.company_id, now_ns=10_000)
                c_status = db.execute(
                    "SELECT status FROM work_conditions WHERE condition_id = ?",
                    (cond.condition_id,),
                ).fetchone()[0]
            self.assertEqual(admitted, ())
            self.assertEqual(c_status, "cancelled")

    def test_p2_03_answer_peer_question_replay_enforces_active_agent_and_grant(
        self,
    ) -> None:
        with hold_company_writer(self.store, self.company_id) as w:
            req_id, req_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="req-p203",
                profile_id=self.profile.profile_id(),
            )
            resp_id, _ = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="resp-p203",
                profile_id=self.profile.profile_id(),
            )
            assign_project(
                w,
                self.grant_id,
                "proj-p203",
                (req_id, resp_id),
                publishers=(req_id,),
                peer_responders=(resp_id,),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                item, run, _ = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=req_id,
                    grant_id=req_g,
                    grant_scope="assessment",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
                open_peer_request(
                    db,
                    company_id=self.company_id,
                    requester_work_item_id=item.work_item_id,
                    requester_run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    recipient_agent_id=resp_id,
                    question="Approve?",
                    expected_decision="yes_no",
                    evidence_refs=[],
                    deadline_ns=90_000_000_000,
                    request_id="pr-p203",
                    request_key="rk-p203",
                    project_id="proj-p203",
                    now_ns=2_000,
                )
            cb = claim_peer_response(w, self.profile.profile_id(), now=5)
            assert cb is not None
            read_peer_question(w, cb, now=6)
            res = answer_peer_question(w, cb, "answered", "Looks good", now=7)
            self.assertEqual(res["outcome"], "answered")

            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                db.execute(
                    "UPDATE work_runs SET status = 'cancelled' WHERE run_id = ?",
                    (cb.run_id,),
                )
            with self.assertRaises(DutyRefused):
                answer_peer_question(w, cb, "answered", "Looks good", now=8)
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                db.execute(
                    "UPDATE work_runs SET status = 'completed' WHERE run_id = ?",
                    (cb.run_id,),
                )
            self.assertEqual(
                answer_peer_question(w, cb, "answered", "Looks good", now=8)["outcome"],
                "answered",
            )

            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                db.execute(
                    "UPDATE standing_grant SET scope = ? WHERE id = ?",
                    (TERMINATION_SCOPE, self.grant_id),
                )
            emp_rev = employment_revision(w, self.founder_id, self.grant_id, resp_id)
            retire_agent(
                w,
                self.founder_id,
                self.grant_id,
                Retirement(
                    request_id="ret-p203",
                    agent_id=resp_id,
                    employment_revision=emp_rev,
                    reason="offboard",
                ),
            )
            with self.assertRaises(DutyRefused):
                answer_peer_question(w, cb, "answered", "Looks good", now=9)

    def test_p2_04_read_inbox_scoped_project_pagination_and_cursor_validation(
        self,
    ) -> None:
        with hold_company_writer(self.store, self.company_id) as w:
            peer_id, peer_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="peer-p204",
                profile_id=self.profile.profile_id(),
            )
            rev_other = assign_project(
                w,
                self.grant_id,
                "proj-other",
                (self.founder_id, peer_id),
                publishers=(self.founder_id,),
            )
            rev_peer = assign_project(
                w,
                self.grant_id,
                "proj-peer",
                (self.founder_id, peer_id),
                publishers=(self.founder_id,),
            )
            ca = None
            for idx in range(17):
                if idx % 8 == 0:
                    if ca is not None:
                        with (
                            closing(open_writer(self.store, lease=w)) as db,
                            write_tx(db),
                        ):
                            complete_work_run(
                                db,
                                ca.run_id,
                                ca.fencing_token,
                                outcome={"ok": True},
                                now_ns=time_ns() + idx,
                            )
                    with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                        _, _, ca = _open_work_run_claim(
                            db,
                            company_id=self.company_id,
                            agent_id=self.founder_id,
                            grant_id=self.grant_id,
                            grant_scope="manage-company",
                            duty_kind="work.assessment_review",
                            purpose="assessment_review",
                            now_ns=time_ns() + idx,
                        )
                assert ca is not None
                append_message(
                    w,
                    ca,
                    ca.purpose,
                    Post("proj-other", rev_other, f"req-other-{idx}", f"Msg {idx}", ""),
                )
            assert ca is not None
            target_msg = append_message(
                w,
                ca,
                ca.purpose,
                Post("proj-peer", rev_peer, "req-peer-1", "Target msg", ""),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                complete_work_run(
                    db,
                    ca.run_id,
                    ca.fencing_token,
                    outcome={"ok": True},
                    now_ns=time_ns() + 100,
                )
                _, _, cp = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=peer_id,
                    grant_id=peer_g,
                    grant_scope="assessment",
                    duty_kind="work.peer_response",
                    purpose="peer_response",
                    now_ns=time_ns() + 200,
                )
            contrib = build_core_collab_tool_contribution(
                writer=w,
                claim=cp,
                purpose=cp.purpose,
                scoped_project_id="proj-peer",
            )
            page1 = contrib.handlers["read_inbox"]({})
            self.assertEqual(len(page1["deliveries"]), 1)
            self.assertEqual(page1["deliveries"][0]["project_id"], "proj-peer")
            self.assertEqual(page1["next_cursor"], "")

            with self.assertRaises(DutyRefused):
                read_inbox(w, cp, cp.purpose, "f" * 64)
            with self.assertRaises(DutyRefused):
                contrib.handlers["read_inbox"](
                    {
                        "after": sha256(
                            f"{target_msg.message_id}:{peer_id}".encode()
                        ).hexdigest()[:-1]
                        + (
                            "0"
                            if sha256(
                                f"{target_msg.message_id}:{peer_id}".encode()
                            ).hexdigest()[-1]
                            != "0"
                            else "1"
                        )
                    }
                )

    def test_p2_04_read_messages_rejects_unknown_foreign_and_non_audience_cursors(
        self,
    ) -> None:
        with hold_company_writer(self.store, self.company_id) as w:
            peer_id, peer_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="peer-msg-cur",
                profile_id=self.profile.profile_id(),
            )
            third_id, _ = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="third-msg-cur",
                profile_id=self.profile.profile_id(),
            )
            rev_a_initial = assign_project(
                w,
                self.grant_id,
                "proj-msg-a",
                (self.founder_id, third_id),
                publishers=(self.founder_id,),
            )
            rev_b = assign_project(
                w,
                self.grant_id,
                "proj-msg-b",
                (self.founder_id, peer_id),
                publishers=(self.founder_id,),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, ca = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=time_ns(),
                )
            msg_a_hidden_from_peer = append_message(
                w,
                ca,
                ca.purpose,
                Post(
                    "proj-msg-a",
                    rev_a_initial,
                    "req-hid-1",
                    "Only third before peer joined",
                    "",
                ),
            )
            rev_a = assign_project(
                w,
                self.grant_id,
                "proj-msg-a",
                (self.founder_id, peer_id, third_id),
                publishers=(self.founder_id,),
                expected_revision=rev_a_initial,
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, cp = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=peer_id,
                    grant_id=peer_g,
                    grant_scope="assessment",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=time_ns(),
                )
            msg_a_visible = append_message(
                w,
                ca,
                ca.purpose,
                Post("proj-msg-a", rev_a, "req-vis-1", "Visible to all", ""),
            )
            msg_b_other_proj = append_message(
                w,
                ca,
                ca.purpose,
                Post("proj-msg-b", rev_b, "req-b-1", "Other proj", ""),
            )

            # Valid visible cursor returns subsequent messages cleanly
            page, _ = read_messages(
                w, cp, cp.purpose, "proj-msg-a", msg_a_visible.message_id
            )
            self.assertEqual(page, ())

            # Unknown, cross-project, and non-audience cursors must raise DutyRefused
            for bad_cursor in (
                "nonexistent-msg-cursor",
                msg_b_other_proj.message_id,
                msg_a_hidden_from_peer.message_id,
            ):
                with self.subTest(bad_cursor=bad_cursor):
                    with self.assertRaises(DutyRefused):
                        read_messages(w, cp, cp.purpose, "proj-msg-a", bad_cursor)

    def test_p2_05_discovery_review_excludes_handoff_tools(self) -> None:
        seen_tools: dict[str, list[str]] = {}

        class _InspectAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ):
                seen_tools[request.run_id] = [t.name for t in request.tools]
                if "record_work_decision" in seen_tools[request.run_id]:
                    broker.invoke(
                        "c1",
                        "record_work_decision",
                        {
                            "action_kind": "noop",
                            "rationale": "Checked surface",
                            "evidence": [],
                            "action_id": f"act-{request.run_id}",
                        },
                    )
                return ShiftResult(
                    output_text="ok",
                    usage=TokenUsage(1, 1),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=1,
                    events=(),
                )

        reg = AgentAdapterRegistry()
        reg.register(_InspectAdapter())
        duty_reg = build_production_duty_registry(
            adapter_registry=reg, profile_bindings={self.founder_id: self.profile}
        )
        rt = CompanyRuntime(self.store, self.company_id, duty_reg, clock=lambda: 5_000)

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                disc_item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    source_kind="manual",
                    source_id="disc-surface",
                    now_ns=4_000,
                )
                assess_item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.assessment_review",
                    source_kind="manual",
                    source_id="assess-surface",
                    now_ns=4_100,
                )
        s1 = rt.step(max_dispatches=2, now_ns=5_000)
        self.assertEqual(len(s1.executed_runs), 2)
        with closing(open_reader(self.store)) as db, read_tx(db):
            runs_by_item = {
                str(r[0]): str(r[1])
                for r in db.execute(
                    "SELECT work_item_id, run_id FROM work_runs WHERE company_id = ?",
                    (self.company_id,),
                ).fetchall()
            }
        disc_tools = seen_tools[runs_by_item[disc_item.work_item_id]]
        assess_tools = seen_tools[runs_by_item[assess_item.work_item_id]]
        for ho_tool in (
            "offer_handoff",
            "discover_handoffs",
            "accept_handoff",
            "decline_handoff",
        ):
            self.assertNotIn(ho_tool, disc_tools)
            self.assertIn(ho_tool, assess_tools)

    def test_p2_06_accept_handoff_same_run_replay_with_original_state_version(
        self,
    ) -> None:
        with hold_company_writer(self.store, self.company_id) as w:
            sender_id, sender_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="sender-p206",
                profile_id=self.profile.profile_id(),
            )
            recip_id, recip_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="recip-p206",
                profile_id=self.profile.profile_id(),
            )
            proj_rev = assign_project(
                w,
                self.grant_id,
                "proj-p206",
                (sender_id, recip_id),
                publishers=(sender_id,),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, cs = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=sender_id,
                    grant_id=sender_g,
                    grant_scope="assessment",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
            d = save_draft(
                w,
                cs,
                cs.purpose,
                name="ho-206",
                content="Handoff data",
                expected_revision=None,
            )
            asset = publish_selection(
                w,
                cs,
                cs.purpose,
                Selection(
                    "pub-206",
                    "ho-206",
                    d.revision,
                    "proj-p206",
                    proj_rev,
                    (sender_id, recip_id),
                    0,
                    12,
                ),
            )
            offer_id = offer_handoff(
                w,
                cs,
                HandoffOffer("ho-req-206", recip_id, asset.asset_id, 100, "Follow up"),
                now=2,
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, cr = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=recip_id,
                    grant_id=recip_g,
                    grant_scope="assessment",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=3_000,
                )
            succ1 = accept_handoff(
                w, cr, cr.purpose, offer_id, now=3, expected_state_version=1
            )
            succ2 = accept_handoff(
                w, cr, cr.purpose, offer_id, now=4, expected_state_version=1
            )
            succ3 = accept_handoff(
                w, cr, cr.purpose, offer_id, now=4, expected_state_version=2
            )
            self.assertEqual(succ1, succ2)
            self.assertEqual(succ1, succ3)
            with self.assertRaises(DutyRefused):
                accept_handoff(
                    w, cr, cr.purpose, offer_id, now=4, expected_state_version=99
                )

    def test_gp3_1_discretionary_next_condition_never_escalates_priority_or_bypasses_budget(
        self,
    ) -> None:
        exec_order: list[str] = []

        class _OrderAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ):
                exec_order.append(request.run_id)
                broker.invoke(
                    "c1",
                    "record_work_decision",
                    {
                        "action_kind": "hold",
                        "rationale": "Discovery done",
                        "evidence": [],
                        "action_id": f"act-{request.run_id}",
                    },
                )
                return ShiftResult(
                    output_text="done",
                    usage=TokenUsage(1, 1),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=1,
                    events=(),
                )

        reg = AgentAdapterRegistry()
        reg.register(_OrderAdapter())
        duty_reg = build_production_duty_registry(
            adapter_registry=reg, profile_bindings={self.founder_id: self.profile}
        )
        rt = CompanyRuntime(
            self.store, self.company_id, duty_reg, clock=lambda: 1_000_000
        )

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                dream_resp = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.dreaming",
                    remit="Idle dreaming",
                    readable_scope={
                        "max_wakes_per_window": 1,
                        "wake_window_ns": 100_000,
                        "cooldown_ns": 1_000,
                    },
                    now_ns=1_000,
                )
                disc_resp = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    remit="Discovery review",
                    readable_scope=["shared"],
                    now_ns=1_000,
                )
                # First dreaming item with next_timer_delay_ns=100 re-arms via DutyOutcome.next_condition
                dream_item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.dreaming",
                    source_kind="manual",
                    source_id="dream-1",
                    payload={
                        "responsibility_id": dream_resp.responsibility_id,
                        "next_timer_delay_ns": 100,
                    },
                    now_ns=1_000,
                )
                # Pre-exhaust dream_resp budget
                db.execute(
                    """INSERT INTO work_discovery_state (
                           company_id, responsibility_id, consecutive_noops, cooldown_until_ns,
                           window_started_ns, window_wakes, updated_at_ns
                       ) VALUES (?, ?, 3, 999999, 1000, 99, 1000)""",
                    (self.company_id, dream_resp.responsibility_id),
                )
        rt.step(max_dispatches=1, now_ns=2_000)
        with closing(open_reader(self.store)) as db, read_tx(db):
            cond_row = db.execute(
                """SELECT work_item_id, status FROM work_conditions
                   WHERE company_id = ? AND responsibility_id = ?""",
                (self.company_id, dream_resp.responsibility_id),
            ).fetchone()
        self.assertIsNotNone(cond_row)
        self.assertIsNone(cond_row[0])  # NEVER linked to work_item_id!

        # Because budget & cooldown are exhausted, evaluate_conditions must NOT admit dreaming
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                admitted_blocked = evaluate_conditions(
                    db, self.company_id, now_ns=5_000
                )
                self.assertEqual(admitted_blocked, ())
                # Now reset budget & cooldown, trigger both dreaming and Tier 3 discovery_review!
                db.execute(
                    "DELETE FROM work_discovery_state WHERE company_id = ? AND responsibility_id = ?",
                    (self.company_id, dream_resp.responsibility_id),
                )
                set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    responsibility_id=disc_resp.responsibility_id,
                    trigger_kind="timer",
                    due_at_ns=4_000,
                    now_ns=3_000,
                )
                admitted_ok = evaluate_conditions(db, self.company_id, now_ns=10_000)
                self.assertEqual(len(admitted_ok), 2)
                self.assertEqual(
                    [it.duty_kind for it in admitted_ok],
                    ["work.discovery_review", "work.dreaming"],
                )
                self.assertTrue(
                    all(it.parent_work_item_id is None for it in admitted_ok)
                )

    def test_grec3_quarantined_retired_excluded_and_offloaded_skill_receipts_recovered(
        self,
    ) -> None:
        for bad_status in ("disputed", "quarantined", "retired"):
            draft = (
                "m-grec3",
                "r-1",
                "ADD",
                "work_method",
                json.dumps({"negative_triggers": ["no_deadline_item"]}),
                "Body",
                "",
                "Fails if valid item discarded",
                bad_status,
            )
            obs = _score_live_gen2_observables(
                shift_ok=True,
                action_kind="hold",
                evidence_count=0,
                rcpt_rows=(),
                drafted_methods=(draft,),
            )
            self.assertFalse(obs["drafted_counterexample_method"], bad_status)

        # Live Gen-2 cycle where active skill body > 2KB triggers offload on skill_read
        big_body = (
            "Boundary check rule: never discard valid no-deadline obligations. "
            + ("X" * 2800)
        )
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=time_ns(),
                )
            d_big = distill_method_delta(
                w,
                claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="big-grec3",
                trigger_spec={"duty_kind": "work.discovery_review"},
                body=big_body,
                audience_scope="company",
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=600,
            )
            qualify_method_delta(
                w,
                d_big.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=650,
            )
            activate_method_delta(
                w,
                d_big.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=700,
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                complete_work_run(
                    db,
                    claim.run_id,
                    claim.fencing_token,
                    outcome={"ok": True},
                    now_ns=750,
                )

        class _LiveOffloadAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ):
                has_cand_proc = False
                if request.tools and any(t.name == "skill_read" for t in request.tools):
                    sr = broker.invoke("c1", "skill_read", {"method_id": "big-grec3"})
                    if sr.get("offloaded"):
                        self.assert_offloaded = True
                    broker.invoke(
                        "c2", "skill_record_invocation", {"method_id": "big-grec3"}
                    )
                    for m in request.context.active_methods:
                        if m.get("item_kind") == "improvement_procedure":
                            pr = broker.invoke(
                                f"pr-{m['method_id']}",
                                "skill_read",
                                {"method_id": m["method_id"]},
                            )
                            pmeta = (pr.get("skill") or {}).get("procedure_meta") or {}
                            if pmeta.get("require_counterexample") is True:
                                has_cand_proc = True
                    if has_cand_proc:
                        broker.invoke(
                            "c3",
                            "skill_draft_candidate",
                            {
                                "method_id": f"cx-draft-{request.run_id[:12]}",
                                "delta_kind": "ADD",
                                "item_kind": "work_method",
                                "trigger_spec": {
                                    "duty_kind": "work.discovery_review",
                                    "negative_triggers": ["no_deadline_valid_item"],
                                },
                                "body": "Keep valid no-deadline obligations.",
                                "audience_scope": "one_agent",
                                "source_refs": [f"work_run:{request.run_id}"],
                                "falsification_criteria": "Reject if valid obligation dropped.",
                            },
                        )
                broker.invoke(
                    "c4",
                    "record_work_decision",
                    {
                        "action_kind": (
                            "counterexample_verified" if has_cand_proc else "hold"
                        ),
                        "rationale": "Checked big skill.",
                        "evidence": [],
                        "action_id": f"dec-{request.run_id}",
                    },
                )
                return ShiftResult(
                    output_text="done",
                    usage=TokenUsage(5, 5),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=4,
                    events=(),
                )

        adapter = _LiveOffloadAdapter()
        reg = AgentAdapterRegistry()
        reg.register(adapter)
        eval_dir = self.root / "evals_grec3"
        eval_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        eval_store = eval_dir / "evaluator.sqlite"
        import sourdough.trial_runner as tr_mod

        captured_traces: list[dict[str, Any]] = []
        orig_grade = tr_mod.grade_trial_observation

        def _capture_grade(pkt, *, observation, tool_trace, service_effects, decision):
            captured_traces.append(dict(tool_trace))
            return orig_grade(
                pkt,
                observation=observation,
                tool_trace=tool_trace,
                service_effects=service_effects,
                decision=decision,
            )

        tr_mod.grade_trial_observation = _capture_grade
        try:
            with hold_company_writer(self.store, self.company_id) as w:
                res = run_recursive_learning_cycle(
                    w,
                    evaluator_store=eval_store,
                    company_id=self.company_id,
                    meta_agent_id=self.founder_id,
                    promotion_actor_id=self.founder_id,
                    promotion_grant_id=self.grant_id,
                    trial_root=self.root / "trials",
                    min_families=3,
                    adapter_registry=reg,
                    profile_bindings={self.founder_id: self.profile},
                )
        finally:
            tr_mod.grade_trial_observation = orig_grade
        self.assertTrue(getattr(adapter, "assert_offloaded", False))
        self.assertGreaterEqual(len(captured_traces), 12)
        seen_skill_read = False
        seen_invoke = False
        seen_draft = False
        for tr in captured_traces:
            for c in tr["calls"]:
                if c["name"] == "skill_read" and c.get("method_id") == "big-grec3":
                    seen_skill_read = True
                    self.assertEqual(c["item_kind"], "work_method")
                    self.assertEqual(c["delta_status"], "active")
                elif c["name"] == "skill_record_invocation":
                    seen_invoke = True
                    self.assertEqual(c["item_kind"], "work_method")
                elif c["name"] == "skill_draft_candidate":
                    seen_draft = True
                    self.assertEqual(c["delta_status"], "candidate")
        self.assertTrue(seen_skill_read and seen_invoke and seen_draft)
        self.assertEqual(res.transfer_verdict, "SUPPORTED")

    def test_gp3_2_shared_asset_revision_enforces_project_and_audience_acl(
        self,
    ) -> None:
        with hold_company_writer(self.store, self.company_id) as w:
            b_id, _ = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="b-acl",
                profile_id=self.profile.profile_id(),
            )
            c_id, _ = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="c-outsider",
                profile_id=self.profile.profile_id(),
            )
            proj_rev = assign_project(
                w,
                self.grant_id,
                "proj-sec",
                (self.founder_id, b_id),
                publishers=(self.founder_id,),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, ca = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
            d = save_draft(
                w,
                ca,
                ca.purpose,
                name="sec-note",
                content="Confidential",
                expected_revision=None,
            )
            asset = publish_selection(
                w,
                ca,
                ca.purpose,
                Selection(
                    "pub-sec",
                    "sec-note",
                    d.revision,
                    "proj-sec",
                    proj_rev,
                    (self.founder_id, b_id),
                    0,
                    12,
                ),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                resp_c = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=c_id,
                    duty_kind="work.discovery_review",
                    remit="Outsider",
                    readable_scope=["shared"],
                    now_ns=1_100,
                )
                with self.assertRaises(WorkKernelRefused):
                    set_condition(
                        db,
                        company_id=self.company_id,
                        owner_agent_id=c_id,
                        responsibility_id=resp_c.responsibility_id,
                        trigger_kind="shared_asset_revision",
                        source_identity=asset.asset_id,
                        now_ns=1_200,
                    )
                resp_b = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=b_id,
                    duty_kind="work.discovery_review",
                    remit="Member B",
                    readable_scope=["shared"],
                    now_ns=1_200,
                )
                cond_b = set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=b_id,
                    responsibility_id=resp_b.responsibility_id,
                    trigger_kind="shared_asset_revision",
                    source_identity=asset.asset_id,
                    now_ns=1_300,
                )
            # Remove B from proj-sec before evaluate_conditions
            assign_project(
                w,
                self.grant_id,
                "proj-sec",
                (self.founder_id,),
                publishers=(self.founder_id,),
                expected_revision=proj_rev,
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                admitted = evaluate_conditions(
                    db,
                    self.company_id,
                    now_ns=2_000,
                    observed_assets={
                        asset.asset_id: sha256(b"Confidential").hexdigest()
                    },
                    triggering_actor_id=self.founder_id,
                )
                c_status = db.execute(
                    "SELECT status FROM work_conditions WHERE condition_id = ?",
                    (cond_b.condition_id,),
                ).fetchone()[0]
            self.assertEqual(admitted, ())
            self.assertEqual(c_status, "cancelled")

    def test_gp3_3_record_work_decision_rejects_nonexistent_core_evidence(self) -> None:
        item, run, claim = self._open_claim()
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                for kind in (
                    "shared_publication",
                    "peer_request",
                    "work_run",
                    "work_item",
                    "tool_call_receipt",
                ):
                    with self.subTest(kind=kind):
                        with self.assertRaises(WorkKernelRefused):
                            record_work_decision(
                                db,
                                company_id=self.company_id,
                                work_item_id=item.work_item_id,
                                run_id=claim.run_id,
                                fencing_token=claim.fencing_token,
                                agent_id=self.founder_id,
                                action_kind="hold",
                                rationale="Bad ref",
                                evidence=(
                                    DecisionEvidence(
                                        kind=kind,
                                        resource_id=f"nonexistent-{kind}-999",
                                        revision="rev-999",
                                        observed_at_ns=1_500,
                                        available_at_ns=1_000,
                                    ),
                                ),
                                decision_at_ns=2_000,
                                action_id=f"act-bad-{kind}",
                            )

    def test_gp3_4_ceo_respond_message_idempotent_across_crash_recovery(self) -> None:
        class _CeoAdapter(AgentRuntimeAdapter):
            def __init__(self) -> None:
                self.calls = 0

            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ):
                self.calls += 1
                return ShiftResult(
                    output_text="Confirmed by CEO.",
                    usage=TokenUsage(2, 3),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=0,
                    events=(),
                )

        reg = AgentAdapterRegistry()
        reg.register(_CeoAdapter())
        duty_reg = build_production_duty_registry(
            adapter_registry=reg, profile_bindings={self.founder_id: self.profile}
        )
        now_box = [10_000]
        rt = CompanyRuntime(
            self.store,
            self.company_id,
            duty_reg,
            lease_ttl_ns=1_000,
            clock=lambda: now_box[0],
        )
        with hold_company_writer(self.store, self.company_id) as w:
            bind_board_principal(
                w, principal_id="board-1", role_name=BOARD_CONTROL_ROLE
            )
            res = post_ceo_message(
                w,
                command_id="cmd-board-1",
                target_agent_id=self.founder_id,
                charter_revision="charter-v1",
                body="Status update?",
                principal_hint="board-1",
                now_ns=5_000,
            )
            inbound_id = res.result["message_id"]
            sync_board_triggers(w, self.company_id, now_ns=5_000)

        # Crash on first complete_work_run AFTER record_ceo_reply commits
        import sourdough.runtime.engine as eng_mod

        orig_complete = eng_mod.complete_work_run
        crash_once = [True]

        def _crash_once(*args, **kwargs):
            if crash_once[0]:
                crash_once[0] = False
                raise sqlite3.OperationalError(
                    "simulated crash before complete_work_run"
                )
            return orig_complete(*args, **kwargs)

        eng_mod.complete_work_run = _crash_once
        try:
            with self.assertRaises(sqlite3.OperationalError):
                rt.step(max_dispatches=1, now_ns=10_000)
        finally:
            eng_mod.complete_work_run = orig_complete

        # Advance past lease TTL -> reclaim & re-run -> idempotent!
        now_box[0] = 20_000
        rt.step(max_dispatches=1, now_ns=20_000)
        with closing(open_reader(self.store)) as db, read_tx(db):
            replies = db.execute(
                """SELECT message_id, body FROM ceo_message
                   WHERE company_id = ? AND sender_kind = 'ceo'""",
                (self.company_id,),
            ).fetchall()
            inbound_state = db.execute(
                "SELECT delivery_state FROM ceo_message WHERE message_id = ?",
                (inbound_id,),
            ).fetchone()[0]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0][1], "Confirmed by CEO.")
        self.assertEqual(inbound_state, "delivered")

    def test_gp4_1_activate_method_delta_cas_on_superseded_revision_and_quarantine(
        self,
    ) -> None:
        _, _, claim = self._open_claim()
        with hold_company_writer(self.store, self.company_id) as w:
            d_add = distill_method_delta(
                w,
                claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-cas",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="v1",
                audience_scope="company",
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=1_000,
            )
            qualify_method_delta(
                w,
                d_add.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=1_100,
            )
            activate_method_delta(
                w,
                d_add.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=1_200,
            )

            d_patch = distill_method_delta(
                w,
                claim,
                delta_kind="PATCH",
                target_kind="work_method",
                method_id="m-cas",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="v2-patch",
                audience_scope="company",
                superseded_revision=d_add.revision_sha256,
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=1_300,
            )
            qualify_method_delta(
                w,
                d_patch.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=1_400,
            )

            d_dep = distill_method_delta(
                w,
                claim,
                delta_kind="DEPRECATE",
                target_kind="work_method",
                method_id="m-cas",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="deprecate v1",
                audience_scope="company",
                superseded_revision=d_add.revision_sha256,
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=1_500,
            )
            qualify_method_delta(
                w,
                d_dep.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=1_600,
            )
            activate_method_delta(
                w,
                d_dep.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=1_700,
            )

            # Stale PATCH must fail CAS and not resurrect DEPRECATEd method
            with self.assertRaises(MethodRefused):
                activate_method_delta(
                    w,
                    d_patch.delta_id,
                    actor_id=self.founder_id,
                    grant_id=self.grant_id,
                    now_ns=1_800,
                )

            # Quarantined target method_id cannot be activated either
            d_q = distill_method_delta(
                w,
                claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-q-block",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="bad",
                audience_scope="company",
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=2_000,
            )
            qualify_method_delta(
                w,
                d_q.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=2_100,
            )
            activate_method_delta(
                w,
                d_q.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=2_200,
            )
            quarantine_method_lineage(
                w, self.company_id, method_id="m-q-block", reason="bad", now_ns=2_300
            )

            d_readd = distill_method_delta(
                w,
                claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-q-block",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="retry",
                audience_scope="company",
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=2_400,
            )
            qualify_method_delta(
                w,
                d_readd.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=2_500,
            )
            with self.assertRaises(MethodRefused):
                activate_method_delta(
                    w,
                    d_readd.delta_id,
                    actor_id=self.founder_id,
                    grant_id=self.grant_id,
                    now_ns=2_600,
                )

    def test_gp3_5_http_adapters_handle_null_usage_and_engine_blocks_type_value_errors(
        self,
    ) -> None:
        secrets = InMemorySecretResolver(
            {
                "envref:ANTHROPIC_API_KEY": "ant-key",
                "envref:OPENAI_API_KEY": "oai-key",
                "envref:GEMINI_API_KEY": "gem-key",
            }
        )
        srv_ant = _LoopbackJsonServer(
            {
                "content": [{"type": "text", "text": "ant ok"}],
                "usage": {
                    "input_tokens": None,
                    "cache_creation_input_tokens": None,
                    "cache_read_input_tokens": None,
                    "output_tokens": None,
                },
                "stop_reason": "end_turn",
            }
        )
        srv_oai = _LoopbackJsonServer(
            {
                "id": "resp-1",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "oai ok"}],
                    }
                ],
                "usage": {"input_tokens": None, "output_tokens": None},
            }
        )
        srv_gem = _LoopbackJsonServer(
            {
                "candidates": [
                    {"content": {"parts": [{"text": "gem ok"}]}, "finishReason": "STOP"}
                ],
                "usageMetadata": {
                    "promptTokenCount": None,
                    "candidatesTokenCount": None,
                },
            }
        )
        reg = build_default_adapter_registry(
            secret_resolver=secrets,
            openai_base_url=srv_oai.base_url,
            anthropic_base_url=srv_ant.base_url,
            gemini_base_url=srv_gem.base_url,
            allow_loopback_override=True,
        )
        try:
            for prov, model, sec in (
                (PROVIDER_ANTHROPIC, "claude-mock", "envref:ANTHROPIC_API_KEY"),
                (PROVIDER_OPENAI, "gpt-mock", "envref:OPENAI_API_KEY"),
                (PROVIDER_GEMINI, "gemini-mock", "envref:GEMINI_API_KEY"),
            ):
                prof = AgentProfileBinding(
                    provider=prov,
                    model=model,
                    revision="rev-1",
                    credential_handle_ref=sec,
                )
                req = ShiftRequest(
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    run_id="r-null",
                    turn_id="t-1",
                    fencing_token=1,
                    instruction="hi",
                    context=__import__(
                        "sourdough.agent_runtime", fromlist=["hydrate_shift_context"]
                    ).hydrate_shift_context(
                        company_id=self.company_id,
                        agent_id=self.founder_id,
                        run_id="r-null",
                        fencing_token=1,
                        grant_scope="manage-company",
                        mandate_summary="m",
                    ),
                    tools=(),
                    budget_ceiling_minor_units=10_000,
                )
                broker = OuterToolBroker(
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    run_id="r-null",
                    fencing_token=1,
                    handlers={},
                )
                res = reg.execute_shift(
                    persisted_profile_id=prof.profile_id(),
                    profile=prof,
                    request=req,
                    broker=broker,
                )
                self.assertEqual(res.usage, TokenUsage(0, 0))
        finally:
            srv_ant.close()
            srv_oai.close()
            srv_gem.close()

        rt = CompanyRuntime(
            self.store,
            self.company_id,
            build_production_duty_registry(
                adapter_registry=reg, profile_bindings={self.founder_id: self.profile}
            ),
        )
        item = WorkItem(
            "wi-1",
            self.company_id,
            self.founder_id,
            "work.discovery_review",
            "running",
            "manual",
            "s",
            None,
            None,
            "{}",
            1,
            1,
            0,
            None,
        )
        self.assertEqual(
            rt._classify_failure(TypeError("bad"), item, 100), (False, True, None)
        )
        self.assertEqual(
            rt._classify_failure(ValueError("bad"), item, 100), (False, True, None)
        )
        self.assertEqual(
            rt._classify_failure(RuntimeError("transient"), item, 100),
            (True, True, None),
        )

    def test_gp3_6_coalesced_single_shot_timer_transitions_to_triggered(self) -> None:
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                resp = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    remit="Coalesce check",
                    readable_scope=["shared"],
                    now_ns=1_000,
                )
                create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    source_kind="manual",
                    source_id="open-disc",
                    payload={"responsibility_id": resp.responsibility_id},
                    now_ns=1_100,
                )
                cond = set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    responsibility_id=resp.responsibility_id,
                    trigger_kind="timer",
                    due_at_ns=2_000,
                    now_ns=1_200,
                )
                admitted = evaluate_conditions(db, self.company_id, now_ns=3_000)
                row = db.execute(
                    "SELECT status, fire_count, updated_at_ns FROM work_conditions WHERE condition_id = ?",
                    (cond.condition_id,),
                ).fetchone()
            self.assertEqual(admitted, ())
            self.assertEqual(row, ("triggered", 1, 3_000))

    def test_gp4_2_live_gen2_slot_budget_or_runtime_failure_fails_closed_cleanly(
        self,
    ) -> None:
        class _FailingSlotAdapter(AgentRuntimeAdapter):
            def __init__(self) -> None:
                self.calls = 0

            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ):
                self.calls += 1
                if self.calls == 1:
                    broker.invoke(
                        "c1",
                        "record_work_decision",
                        {
                            "action_kind": "hold",
                            "rationale": "Gen1 ok",
                            "evidence": [],
                            "action_id": f"dec-{request.run_id}",
                        },
                    )
                    return ShiftResult(
                        output_text="ok",
                        usage=TokenUsage(1, 1),
                        provider=profile.provider,
                        model=profile.model,
                        run_id=request.run_id,
                        turn_id=request.turn_id,
                        tool_calls_executed=1,
                        events=(),
                    )
                if self.calls <= 3:
                    raise AgentBudgetExceeded("Simulated budget exceeded")
                raise AgentRuntimeRefused("Simulated refusal")

        reg = AgentAdapterRegistry()
        reg.register(_FailingSlotAdapter())
        eval_dir = self.root / "evals_fail"
        eval_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        eval_store = eval_dir / "evaluator.sqlite"
        from sourdough.trial_registry import TrialRefused, read_sealed_suite_meta

        with self.assertRaises(TrialRefused):
            with hold_company_writer(self.store, self.company_id) as w:
                run_recursive_learning_cycle(
                    w,
                    evaluator_store=eval_store,
                    company_id=self.company_id,
                    meta_agent_id=self.founder_id,
                    promotion_actor_id=self.founder_id,
                    promotion_grant_id=self.grant_id,
                    trial_root=self.root / "trials_fail",
                    min_families=3,
                    adapter_registry=reg,
                    profile_bindings={self.founder_id: self.profile},
                )
        meta = read_sealed_suite_meta(eval_store)
        self.assertTrue(meta["revision"])
        with closing(sqlite3.connect(f"file:{eval_store}?mode=ro", uri=True)) as edb:
            ev_rows = edb.execute("SELECT evidence FROM trial_observation").fetchall()
            grade_rows = edb.execute("SELECT grade FROM trial_grade").fetchall()
        self.assertGreaterEqual(len(ev_rows), 10)
        parsed_ev = [json.loads(str(r[0])) for r in ev_rows]
        parsed_grades = [json.loads(str(r[0])) for r in grade_rows]
        self.assertTrue(all(e.get("outcome") == "failed" for e in parsed_ev))
        self.assertEqual(len(parsed_grades), len(parsed_ev))
        self.assertTrue(all(g.get("authority_violations") == 0 for g in parsed_grades))

    def test_gp4_3_redact_for_shared_scope_broadened_desk_paths(self) -> None:
        _, _, claim = self._open_claim()
        with hold_company_writer(self.store, self.company_id) as w:
            for raw, expected_clean in (
                ("See desks/agent-1", "See [redacted-private-ref]"),
                ("Path desks\\agent-1\\a.md", "Path [redacted-private-ref]"),
                ("URL desks%2Fagent-1%2Fa.md", "URL [redacted-private-ref]"),
                ("URL DESKS%5Cagent-1%5Cb.md", "URL [redacted-private-ref]"),
                ("Log .desk/logs/0001.json", "Log [redacted-private-ref]"),
                ("Clean desks policy", "Clean desks policy"),
            ):
                res = redact_for_shared_scope(
                    w,
                    claim,
                    raw_body=raw,
                    source_refs=[f"work_run:{claim.run_id}"],
                    target_scope="company",
                    now_ns=2_000,
                )
                self.assertEqual(res.redacted_body, expected_clean)
                if raw != "Clean desks policy":
                    self.assertTrue(res.stripped_items)
                else:
                    self.assertEqual(res.stripped_items, ())

    def test_t5_new_1_cross_agent_tool_call_receipt_collision(self) -> None:
        base_ns = time_ns()
        with hold_company_writer(self.store, self.company_id) as w:
            aaa_id, _ = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="aaa-colliding-agent",
                profile_id=self.profile.profile_id(),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, claim_aaa = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=aaa_id,
                    grant_id=f"grant-{aaa_id}",
                    grant_scope="assessment",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=base_ns,
                )
                item_f, _, claim_f = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=base_ns + 100,
                )
                for aid, rid, cid, ts in (
                    (aaa_id, claim_aaa.run_id, "c1", base_ns + 200),
                    (aaa_id, claim_aaa.run_id, "c_other", base_ns + 250),
                    (self.founder_id, claim_f.run_id, "c1", base_ns + 300),
                ):
                    db.execute(
                        """INSERT INTO tool_call_receipt (
                               company_id, run_id, agent_id, call_id, tool_name, arg_digest,
                               status, result_digest, result_json, error_type, error_message,
                               offloaded_log_path, recorded_at_ns
                           ) VALUES (?, ?, ?, ?, 'desk_list_drafts', 'd1', 'ok', 'r1', '{}', NULL, NULL, NULL, ?)""",
                        (self.company_id, rid, aid, cid, ts),
                    )
                dec = record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=item_f.work_item_id,
                    run_id=claim_f.run_id,
                    fencing_token=claim_f.fencing_token,
                    agent_id=self.founder_id,
                    action_kind="hold",
                    rationale="Cited own c1",
                    evidence=(
                        DecisionEvidence(
                            kind="tool_call_receipt",
                            resource_id="c1",
                            revision="r1",
                            observed_at_ns=base_ns + 400,
                            available_at_ns=base_ns + 300,
                        ),
                    ),
                    decision_at_ns=base_ns + 500,
                    action_id="act-own-c1",
                )
                self.assertEqual(dec.action_id, "act-own-c1")
                with self.assertRaises(WorkKernelRefused):
                    record_work_decision(
                        db,
                        company_id=self.company_id,
                        work_item_id=item_f.work_item_id,
                        run_id=claim_f.run_id,
                        fencing_token=claim_f.fencing_token,
                        agent_id=self.founder_id,
                        action_kind="hold",
                        rationale="Foreign c_other",
                        evidence=(
                            DecisionEvidence(
                                kind="tool_call_receipt",
                                resource_id="c_other",
                                revision="r1",
                                observed_at_ns=base_ns + 400,
                                available_at_ns=base_ns + 250,
                            ),
                        ),
                        decision_at_ns=base_ns + 550,
                        action_id="act-foreign-cother",
                    )

            delta = distill_method_delta(
                w,
                claim_f,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-own-c1",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="Cited own receipt c1 cleanly.",
                audience_scope="one_agent",
                source_refs=["tool_call_receipt:c1"],
                now_ns=base_ns + 600,
            )
            self.assertEqual(delta.status, "candidate")
            with self.assertRaises(MethodRefused):
                distill_method_delta(
                    w,
                    claim_f,
                    delta_kind="ADD",
                    target_kind="work_method",
                    method_id="m-foreign-cother",
                    trigger_spec={"duty_kind": "work.assessment_review"},
                    body="Attempted foreign receipt.",
                    audience_scope="one_agent",
                    source_refs=["tool_call_receipt:c_other"],
                    now_ns=base_ns + 700,
                )

    def test_t5_new_2_coalesced_shared_asset_revision_max_fires_1_transitions_triggered(
        self,
    ) -> None:
        with hold_company_writer(self.store, self.company_id) as w:
            peer_id, peer_g = _hire_active_agent(
                w,
                company_id=self.company_id,
                grant_id=self.grant_id,
                agent_id="pub-peer-t5new2",
                profile_id=self.profile.profile_id(),
            )
            proj_rev = assign_project(
                w,
                self.grant_id,
                "proj-t5new2",
                (self.founder_id, peer_id),
                publishers=(peer_id,),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, cp = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=peer_id,
                    grant_id=peer_g,
                    grant_scope="assessment",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
            d = save_draft(
                w,
                cp,
                cp.purpose,
                name="asset-t5new2",
                content="Revision one",
                expected_revision=None,
            )
            asset = publish_selection(
                w,
                cp,
                cp.purpose,
                Selection(
                    "pub-t5new2-1",
                    "asset-t5new2",
                    d.revision,
                    "proj-t5new2",
                    proj_rev,
                    (self.founder_id, peer_id),
                    0,
                    len(d.content),
                ),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                resp = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    remit="Watch asset once",
                    readable_scope=["shared"],
                    now_ns=1_100,
                )
                open_item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    source_kind="manual",
                    source_id="open-disc-t5new2",
                    payload={"responsibility_id": resp.responsibility_id},
                    now_ns=1_200,
                )
                cond = set_condition(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    responsibility_id=resp.responsibility_id,
                    trigger_kind="shared_asset_revision",
                    source_identity=asset.asset_id,
                    max_fires=1,
                    now_ns=1_300,
                )
                admitted1 = evaluate_conditions(db, self.company_id, now_ns=2_000)
                row1 = db.execute(
                    "SELECT status, fire_count FROM work_conditions WHERE condition_id = ?",
                    (cond.condition_id,),
                ).fetchone()
                self.assertEqual(admitted1, ())
                self.assertEqual(row1, ("triggered", 1))
                # Complete the open item and publish a second revision; must NOT fire again!
                db.execute(
                    "UPDATE work_items SET state = 'settled' WHERE work_item_id = ?",
                    (open_item.work_item_id,),
                )
                admitted2 = evaluate_conditions(
                    db,
                    self.company_id,
                    now_ns=3_000,
                    observed_assets={
                        asset.asset_id: sha256(b"Revision two").hexdigest()
                    },
                    triggering_actor_id=peer_id,
                )
                row2 = db.execute(
                    "SELECT status, fire_count FROM work_conditions WHERE condition_id = ?",
                    (cond.condition_id,),
                ).fetchone()
            self.assertEqual(admitted2, ())
            self.assertEqual(row2, ("triggered", 1))

    def test_t5_new_3_candidate_quarantine_blocks_readd_and_add_enforces_active_cas(
        self,
    ) -> None:
        _, _, claim = self._open_claim()
        with hold_company_writer(self.store, self.company_id) as w:
            # 1. Quarantine an unactivated candidate -> blocks subsequent ADD activation
            d_cand = distill_method_delta(
                w,
                claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-q-cand",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="Bad candidate",
                audience_scope="company",
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=1_000,
            )
            quarantine_method_lineage(
                w,
                self.company_id,
                method_id="m-q-cand",
                revision_sha256=d_cand.revision_sha256,
                reason="Unsafe candidate",
                now_ns=1_100,
            )
            d_readd = distill_method_delta(
                w,
                claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-q-cand",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="Retry under same method_id",
                audience_scope="company",
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=1_200,
            )
            qualify_method_delta(
                w,
                d_readd.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=1_300,
            )
            with self.assertRaises(MethodRefused):
                activate_method_delta(
                    w,
                    d_readd.delta_id,
                    actor_id=self.founder_id,
                    grant_id=self.grant_id,
                    now_ns=1_400,
                )

            # 2. ADD over an already active revision in the same scope fails closed
            d_act1 = distill_method_delta(
                w,
                claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-add-cas",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="Active v1",
                audience_scope="company",
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=2_000,
            )
            qualify_method_delta(
                w,
                d_act1.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=2_100,
            )
            activate_method_delta(
                w,
                d_act1.delta_id,
                actor_id=self.founder_id,
                grant_id=self.grant_id,
                now_ns=2_200,
            )
            d_act2_dup = distill_method_delta(
                w,
                claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-add-cas",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="Overwrite without PATCH",
                audience_scope="company",
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=2_300,
            )
            qualify_method_delta(
                w,
                d_act2_dup.delta_id,
                passed_k=3,
                negative_trigger_passed=True,
                safety_passed=True,
                now_ns=2_400,
            )
            with self.assertRaises(MethodRefused):
                activate_method_delta(
                    w,
                    d_act2_dup.delta_id,
                    actor_id=self.founder_id,
                    grant_id=self.grant_id,
                    now_ns=2_500,
                )

    def test_t5_new_4_live_gen2_slot_duty_refused_fails_closed_cleanly(self) -> None:
        class _DutyRefusedSlotAdapter(AgentRuntimeAdapter):
            def __init__(self) -> None:
                self.calls = 0

            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ):
                self.calls += 1
                if self.calls == 1:
                    broker.invoke(
                        "c1",
                        "record_work_decision",
                        {
                            "action_kind": "hold",
                            "rationale": "Gen1 ok",
                            "evidence": [],
                            "action_id": f"dec-{request.run_id}",
                        },
                    )
                    return ShiftResult(
                        output_text="ok",
                        usage=TokenUsage(1, 1),
                        provider=profile.provider,
                        model=profile.model,
                        run_id=request.run_id,
                        turn_id=request.turn_id,
                        tool_calls_executed=1,
                        events=(),
                    )
                if self.calls <= 3:
                    raise DutyRefused("Simulated handler/desk DutyRefused inside slot")
                if self.calls <= 5:
                    raise WorkKernelRefused("Simulated WorkKernelRefused inside slot")
                raise MethodRefused("Simulated MethodRefused inside slot")

        reg = AgentAdapterRegistry()
        reg.register(_DutyRefusedSlotAdapter())
        eval_dir = self.root / "evals_duty_refused"
        eval_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        eval_store = eval_dir / "evaluator.sqlite"
        from sourdough.trial_registry import TrialRefused

        with self.assertRaises(TrialRefused):
            with hold_company_writer(self.store, self.company_id) as w:
                run_recursive_learning_cycle(
                    w,
                    evaluator_store=eval_store,
                    company_id=self.company_id,
                    meta_agent_id=self.founder_id,
                    promotion_actor_id=self.founder_id,
                    promotion_grant_id=self.grant_id,
                    trial_root=self.root / "trials_duty_refused",
                    min_families=3,
                    adapter_registry=reg,
                    profile_bindings={self.founder_id: self.profile},
                )
        with closing(sqlite3.connect(f"file:{eval_store}?mode=ro", uri=True)) as edb:
            ev_rows = edb.execute("SELECT evidence FROM trial_observation").fetchall()
            grade_rows = edb.execute("SELECT grade FROM trial_grade").fetchall()
        self.assertGreaterEqual(len(ev_rows), 10)
        parsed_ev = [json.loads(str(r[0])) for r in ev_rows]
        parsed_grades = [json.loads(str(r[0])) for r in grade_rows]
        self.assertTrue(all(e.get("outcome") == "failed" for e in parsed_ev))
        self.assertEqual(len(parsed_grades), len(parsed_ev))
        self.assertTrue(all(g.get("authority_violations") == 0 for g in parsed_grades))

    def test_t5_new_5_private_desk_path_delimiters_and_safe_preservations(
        self,
    ) -> None:
        _, _, claim = self._open_claim()
        with hold_company_writer(self.store, self.company_id) as w:
            for raw, expected_clean, should_strip in (
                (
                    "path=desks/agent-1/secret.md",
                    "path=[redacted-private-ref]",
                    True,
                ),
                (
                    "url=desks%2Fagent-1%2Fdraft.md",
                    "url=[redacted-private-ref]",
                    True,
                ),
                (
                    "ref:desks/agent-1/secret.md",
                    "ref:[redacted-private-ref]",
                    True,
                ),
                (
                    "[desks/agent-1/secret.md]",
                    "[[redacted-private-ref]]",
                    True,
                ),
                ("Clean desks policy", "Clean desks policy", False),
                ("my_desks/agent-1/x", "my_desks/agent-1/x", False),
            ):
                res = redact_for_shared_scope(
                    w,
                    claim,
                    raw_body=raw,
                    source_refs=[f"work_run:{claim.run_id}"],
                    target_scope="company",
                    now_ns=2_000,
                )
                self.assertEqual(res.redacted_body, expected_clean, raw)
                self.assertEqual(bool(res.stripped_items), should_strip, raw)

            for leaked in (
                "Read path=desks/agent-1/secret.md",
                "Open url=desks%2Fagent-1%2Fdraft.md",
                "Cite ref:desks/agent-1/secret.md",
                "Check [desks/agent-1/secret.md]",
            ):
                with self.subTest(leaked=leaked):
                    with self.assertRaises(MethodRefused):
                        distill_method_delta(
                            w,
                            claim,
                            delta_kind="ADD",
                            target_kind="work_method",
                            method_id="m-leak-delim",
                            trigger_spec={"duty_kind": "work.assessment_review"},
                            body=leaked,
                            audience_scope="company",
                            source_refs=[f"work_run:{claim.run_id}"],
                            now_ns=2_100,
                        )
            clean_delta = distill_method_delta(
                w,
                claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-clean-delim",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="Clean desks policy and my_desks/agent-1/x allowed.",
                audience_scope="company",
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=2_200,
            )
            self.assertEqual(clean_delta.status, "candidate")

    def test_pr191_sandboxed_desk_command_terminates_descendants_on_normal_exit(
        self,
    ) -> None:
        """PR #191 P1: backgrounded child processes must be terminated even when parent exits 0."""
        import signal
        import time
        from unittest import mock

        from sourdough import harness as harness_mod

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
            session = create_agent_session(w, claim, claim.purpose)
            standin = self.root / "fake_sandbox_exec.py"
            standin.write_text(
                f"#!{sys.executable}\n"
                "import subprocess, sys\n"
                "# Skip '-p <profile>' and execute the command\n"
                "cmd = sys.argv[3:]\n"
                "sys.exit(subprocess.call(cmd))\n",
                encoding="utf-8",
            )
            os.chmod(standin, 0o700)
            pid_file = session.desk_path / ".desk" / "scratch" / claim.run_id / "bg.pid"
            spawner = (
                "import pathlib, subprocess, sys; "
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid), encoding='utf-8')"
            )
            with mock.patch.object(harness_mod, "_SANDBOX_EXEC", standin):
                res = run_desk_command_sandboxed(
                    session,
                    [sys.executable, "-c", spawner],
                    store=self.store,
                    sandbox_backend="local-macos",
                    limits=DeskCommandLimits(timeout_seconds=10),
                    granted_capabilities=("desk:exec",),
                )
            self.assertTrue(res["ok"])
            self.assertEqual(res["returncode"], 0)
            bg_pid = int(pid_file.read_text(encoding="utf-8").strip())
            # Background descendant in the command's process group must be terminated.
            alive = True
            for _ in range(40):
                try:
                    os.kill(bg_pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.05)
            if alive:
                try:
                    os.kill(bg_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.assertFalse(
                alive, f"Detached background descendant {bg_pid} survived normal exit"
            )

    def test_pr191_record_work_decision_validates_private_draft_existence_and_revision(
        self,
    ) -> None:
        """PR #191 P2: private_draft citations must resolve to the agent's real draft & revision."""
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                item, run, claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.discovery_review",
                    purpose="discovery_review",
                    now_ns=1_000,
                )
            draft = save_draft(
                w,
                claim,
                claim.purpose,
                name="real-note",
                content="Verified private working note",
                expected_revision=None,
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                # 1. Nonexistent draft refused
                with self.assertRaises(WorkKernelRefused):
                    record_work_decision(
                        db,
                        company_id=self.company_id,
                        work_item_id=item.work_item_id,
                        run_id=run.run_id,
                        fencing_token=claim.fencing_token,
                        agent_id=self.founder_id,
                        action_kind="hold",
                        rationale="citing nonexistent draft",
                        evidence=(
                            DecisionEvidence(
                                "private_draft",
                                "nonexistent",
                                "fake-revision",
                                900,
                                800,
                            ),
                        ),
                        decision_at_ns=1_500,
                    )
                # 2. Stale/wrong revision on existing draft refused
                with self.assertRaises(WorkKernelRefused):
                    record_work_decision(
                        db,
                        company_id=self.company_id,
                        work_item_id=item.work_item_id,
                        run_id=run.run_id,
                        fencing_token=claim.fencing_token,
                        agent_id=self.founder_id,
                        action_kind="hold",
                        rationale="citing stale revision",
                        evidence=(
                            DecisionEvidence(
                                "private_draft",
                                f"{self.founder_id}/real-note",
                                "stale-revision",
                                900,
                                800,
                            ),
                        ),
                        decision_at_ns=1_500,
                    )
                # 3. Valid existing draft & exact revision accepted
                dec = record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=claim.fencing_token,
                    agent_id=self.founder_id,
                    action_kind="hold",
                    rationale="citing verified draft",
                    evidence=(
                        DecisionEvidence(
                            "private_draft",
                            f"{self.founder_id}/real-note",
                            draft.revision,
                            900,
                            800,
                        ),
                    ),
                    decision_at_ns=1_500,
                )
                self.assertEqual(dec.action_kind, "hold")

    def test_pr191_evals_projection_preserves_sealed_analysis_plan_criteria(
        self,
    ) -> None:
        """PR #191 P2: read_system_evals_projection must never weaken sealed analysis criteria."""
        from sourdough.ipc.projections import read_system_evals_projection
        from sourdough.trial_registry import (
            PairwiseJudgment,
            TrialCeiling,
            TrialGrade,
            TrialInputs,
            TrialSlot,
            TrialSuite,
            TrialTerminalEvidence,
            blind_pairwise_packets,
            blind_trial_packet,
            commit_blind_grade,
            commit_position_swapped_pairwise_grade,
            commit_trial_observation,
            reserve_trial_launch,
            seal_trial_suite,
            summarize_rsi_transfer,
        )

        def _populate_three_family_wins(
            eval_path: Path, *, pass_plan_at_seal: bool
        ) -> tuple[str, dict[str, object]]:
            plan: dict[str, object] = {
                "min_pairs": 3,
                "min_net_wins": 1,
                "min_families": 3,
                "alpha": 0.05,
                "min_worthwhile_margin": 0.0,
                "bootstrap_resamples": 1000,
            }
            plan_digest = sha256(
                json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            slots = []
            for idx in (1, 2, 3):
                for arm, cp in (("incumbent", f"s{idx}-c"), ("candidate", f"s{idx}-i")):
                    sid = f"s{idx}-i" if arm == "incumbent" else f"s{idx}-c"
                    slots.append(
                        TrialSlot(
                            slot_id=sid,
                            case_id=f"c{idx}",
                            family_id=f"f{idx}",
                            inputs=TrialInputs(
                                sha256(b"fix").hexdigest(),
                                sha256(f"case-{idx}".encode()).hexdigest(),
                                sha256(arm.encode()).hexdigest(),
                                sha256(b"tools").hexdigest(),
                                "offline-v1",
                            ),
                            limits=TrialCeiling(2, 10, 1000),
                            arm=arm,
                            counterpart_id=cp,
                        )
                    )
            suite = TrialSuite(
                suite_id="strict-3fam-suite",
                purpose="recursive",
                rubric_digest=sha256(b"rubric").hexdigest(),
                evaluator_digest=sha256(b"eval").hexdigest(),
                analysis_plan_digest=plan_digest,
                slots=tuple(slots),
            )
            rev = seal_trial_suite(
                eval_path,
                suite,
                analysis_plan=plan if pass_plan_at_seal else None,
            )
            cand_results: set[str] = set()
            for idx, slot in enumerate(suite.slots, start=1):
                rid = f"r-{idx}"
                reserve_trial_launch(
                    eval_path,
                    revision=rev,
                    slot_id=slot.slot_id,
                    run_id=rid,
                    inputs=slot.inputs,
                    limits=slot.limits,
                )
                rec = commit_trial_observation(
                    eval_path,
                    revision=rev,
                    slot_id=slot.slot_id,
                    run_id=rid,
                    evidence=TrialTerminalEvidence(
                        "completed",
                        sha256(f"o-{slot.slot_id}".encode()).hexdigest(),
                        sha256(f"d-{slot.slot_id}".encode()).hexdigest(),
                        sha256(b"t").hexdigest(),
                        sha256(b"s").hexdigest(),
                    ),
                )
                assert rec.result_id is not None
                if slot.arm == "candidate":
                    cand_results.add(rec.result_id)
                pkt = blind_trial_packet(
                    eval_path, revision=rev, result_id=rec.result_id
                )
                commit_blind_grade(
                    eval_path,
                    revision=rev,
                    packet=pkt,
                    grade=TrialGrade(True, True, True, 0, True),
                )
            for ab, ba in blind_pairwise_packets(eval_path, revision=rev):
                ab_pref = "first" if ab.first.result_id in cand_results else "second"
                ba_pref = "first" if ba.first.result_id in cand_results else "second"
                commit_position_swapped_pairwise_grade(
                    eval_path,
                    revision=rev,
                    ab_packet=ab,
                    ab_judgment=PairwiseJudgment("Candidate won", ab_pref),
                    ba_packet=ba,
                    ba_judgment=PairwiseJudgment("Candidate won", ba_pref),
                )
            return rev, plan

        # Case 1: Sealed with explicit plan (3 families, perm_p = 0.125 > alpha = 0.05 -> INCONCLUSIVE)
        eval_sealed = self.root / "eval_sealed.sqlite"
        rev1, plan1 = _populate_three_family_wins(eval_sealed, pass_plan_at_seal=True)
        direct = summarize_rsi_transfer(eval_sealed, revision=rev1, analysis_plan=plan1)
        self.assertEqual(direct.verdict, "INCONCLUSIVE")
        self.assertAlmostEqual(direct.perm_p, 0.125)
        proj1, _ = read_system_evals_projection(
            self.store, self.company_id, evaluator_store=eval_sealed
        )
        self.assertEqual(len(proj1["rsi_transfer_summaries"]), 1)
        self.assertEqual(proj1["rsi_transfer_summaries"][0]["verdict"], "INCONCLUSIVE")
        self.assertIn(
            "permutation-p-above-alpha", proj1["rsi_transfer_summaries"][0]["reasons"]
        )

        # Case 2: Sealed without embedded plan -> fail-closed UNAVAILABLE (never weaker SUPPORTED!)
        eval_opaque = self.root / "eval_opaque.sqlite"
        _rev2, plan2 = _populate_three_family_wins(eval_opaque, pass_plan_at_seal=False)
        proj2, _ = read_system_evals_projection(
            self.store, self.company_id, evaluator_store=eval_opaque
        )
        self.assertEqual(len(proj2["rsi_transfer_summaries"]), 1)
        self.assertEqual(proj2["rsi_transfer_summaries"][0]["verdict"], "UNAVAILABLE")
        self.assertIn(
            "sealed-analysis-plan-unavailable",
            proj2["rsi_transfer_summaries"][0]["reasons"],
        )
        # Passing the verified plan explicitly restores exact INCONCLUSIVE verdict
        proj2_with_plan, _ = read_system_evals_projection(
            self.store,
            self.company_id,
            evaluator_store=eval_opaque,
            analysis_plan=plan2,
        )
        self.assertEqual(
            proj2_with_plan["rsi_transfer_summaries"][0]["verdict"], "INCONCLUSIVE"
        )


if __name__ == "__main__":
    unittest.main()
