"""Unit and integration tests for M3 Feature #13: work.discovery_review, Proactive Tools & work_decision."""

from __future__ import annotations

import json
import queue
import threading
import unittest
from contextlib import closing
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from sourdough.agent_desk import ensure_private_desk, save_draft
from sourdough.agent_runtime import (
    PROVIDER_OPENAI,
    AgentProfileBinding,
    InMemorySecretResolver,
    build_default_adapter_registry,
)
from sourdough.agent_runtime.protocol import _FORBIDDEN_TOOL_ARG_KEYS
from sourdough.company import FoundingTerms, found
from sourdough.company_assets import Selection, assign_project, publish_selection
from sourdough.duty_run import WorkRunClaim
from sourdough.runtime import (
    DUTY_WORK_DISCOVERY_REVIEW,
    BaseDomainPlugin,
    CompanyRuntime,
    DecisionEvidence,
    RunFenced,
    WorkKernelRefused,
    arm_result_continuation,
    build_proactive_tool_handlers,
    build_production_duty_registry,
    claim_work_run,
    complete_work_run,
    create_work_item,
    evaluate_conditions,
    record_work_decision,
    register_responsibility,
)
from sourdough.runtime.proactive_tools import build_core_proactive_tool_contribution
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.storage.kernel import append_audit_event
from sourdough.writer_lease import hold_company_writer


class _QueuedOpenAiServer:
    def __init__(self) -> None:
        self.responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self.requests: list[dict[str, Any]] = []
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                parent.requests.append(payload)
                body = parent.responses.get(timeout=5.0)
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.base_url = f"http://127.0.0.1:{self._httpd.server_port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def __enter__(self) -> _QueuedOpenAiServer:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2.0)


def _tool_resp(
    resp_id: str, call_id: str, name: str, args: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": resp_id,
        "output": [
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(args),
            }
        ],
        "usage": {"input_tokens": 120, "output_tokens": 40},
    }


def _text_resp(resp_id: str, text: str) -> dict[str, Any]:
    return {
        "id": resp_id,
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {"input_tokens": 80, "output_tokens": 20},
    }


class DiscoveryDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "company.sqlite"
        self.leases = self.root / "leases"
        self.profile = AgentProfileBinding(
            provider=PROVIDER_OPENAI,
            model="gpt-5-mini",
            revision="v1",
            credential_handle_ref="envref:SD_TEST_KEY",
        )
        self.receipt = found(
            self.store,
            FoundingTerms(
                "m3-f13", "charter-v1", "manage-company", self.profile.profile_id()
            ),
        )
        self.company_id = self.receipt.company_id
        self.founder_id = self.receipt.founder_id

    def _hire(self, agent_id: str, scope: str = "desk:write") -> tuple[str, str, str]:
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                grant_id = f"grant-{agent_id}"
                db.execute(
                    "INSERT INTO agent VALUES (?, ?, ?)",
                    (agent_id, self.company_id, self.profile.profile_id()),
                )
                db.execute(
                    "INSERT INTO standing_grant VALUES (?, ?, ?)",
                    (grant_id, agent_id, scope),
                )
                ensure_private_desk(
                    db, self.company_id, agent_id, self.profile.profile_id()
                )
                return agent_id, grant_id, scope

    def _open_claim(
        self,
        agent_id: str,
        grant_id: str,
        scope: str,
        *,
        duty_kind: str = DUTY_WORK_DISCOVERY_REVIEW,
    ) -> tuple[str, str, WorkRunClaim]:
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=agent_id,
                    duty_kind=duty_kind,
                    source_kind="test",
                    source_id=f"src-{agent_id}",
                    now_ns=1_000,
                )
                run = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    lease_owner="test",
                    lease_ttl_ns=60_000_000_000,
                    now_ns=1_000,
                )
                claim = WorkRunClaim(
                    company_id=self.company_id,
                    founder_id=agent_id,
                    duty_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    grant_id=grant_id,
                    grant_scope=scope,
                    purpose=duty_kind,
                    _clock=1_000,
                )
                return item.work_item_id, run.run_id, claim

    def test_evidence_future_leak_cross_agent_draft_and_digest_checks(self) -> None:
        a, ga, sa = self._hire("a")
        b, gb, sb = self._hire("b")
        c, _, _ = self._hire("c")
        _, _, ca = self._open_claim(a, ga, sa)
        item_b, run_b, cb = self._open_claim(b, gb, sb)
        item_c, run_c, cc = self._open_claim(c, *self._get_grant(c))
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            rev = assign_project(
                w,
                self.receipt.grant_id,
                "proj-ev",
                (self.founder_id, a, b),
                publishers=(a,),
            )
            draft = save_draft(
                w,
                ca,
                ca.purpose,
                name="sec",
                content="Authorized asset",
                expected_revision=None,
            )
            asset = publish_selection(
                w,
                ca,
                ca.purpose,
                Selection("p1", "sec", draft.revision, "proj-ev", rev, (a, b), 0, 16),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                # 1. Future-leak cases
                for bad in (
                    DecisionEvidence(
                        "shared_publication",
                        asset.asset_id,
                        sha256(b"Authorized asset").hexdigest(),
                        2_000,
                        3_001,
                    ),
                    DecisionEvidence(
                        "shared_publication",
                        asset.asset_id,
                        sha256(b"Authorized asset").hexdigest(),
                        3_001,
                        1_000,
                    ),
                    DecisionEvidence(
                        "shared_publication",
                        asset.asset_id,
                        sha256(b"Authorized asset").hexdigest(),
                        1_500,
                        2_000,
                    ),
                ):
                    with self.assertRaises(WorkKernelRefused):
                        record_work_decision(
                            db,
                            company_id=self.company_id,
                            work_item_id=item_b,
                            run_id=run_b,
                            fencing_token=cb.fencing_token,
                            agent_id=b,
                            action_kind="hold",
                            rationale="bad timestamp",
                            evidence=(bad,),
                            decision_at_ns=3_000,
                        )
                # 2. Cross-agent private draft
                with self.assertRaises(WorkKernelRefused):
                    record_work_decision(
                        db,
                        company_id=self.company_id,
                        work_item_id=item_b,
                        run_id=run_b,
                        fencing_token=cb.fencing_token,
                        agent_id=b,
                        action_kind="hold",
                        rationale="leak",
                        evidence=(
                            DecisionEvidence(
                                "private_draft", f"{a}/sec", "r1", 1_500, 1_000
                            ),
                        ),
                        decision_at_ns=3_000,
                    )
                # 3. Wrong digest on shared_publication
                with self.assertRaises(WorkKernelRefused):
                    record_work_decision(
                        db,
                        company_id=self.company_id,
                        work_item_id=item_b,
                        run_id=run_b,
                        fencing_token=cb.fencing_token,
                        agent_id=b,
                        action_kind="hold",
                        rationale="bad digest",
                        evidence=(
                            DecisionEvidence(
                                "shared_publication",
                                asset.asset_id,
                                "wrong-sha",
                                1_500,
                                1_000,
                            ),
                        ),
                        decision_at_ns=3_000,
                    )
                # 4. Unauthorized non-member citing shared_publication
                with self.assertRaises(WorkKernelRefused):
                    record_work_decision(
                        db,
                        company_id=self.company_id,
                        work_item_id=item_c,
                        run_id=run_c,
                        fencing_token=cc.fencing_token,
                        agent_id=c,
                        action_kind="hold",
                        rationale="unauthorized",
                        evidence=(
                            DecisionEvidence(
                                "shared_publication",
                                asset.asset_id,
                                sha256(b"Authorized asset").hexdigest(),
                                1_500,
                                1_000,
                            ),
                        ),
                        decision_at_ns=3_000,
                    )

    def _get_grant(self, agent_id: str) -> tuple[str, str]:
        with closing(open_reader(self.store)) as db, read_tx(db):
            row = db.execute(
                "SELECT id, scope FROM standing_grant WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            return str(row[0]), str(row[1])

    def test_domain_plugin_hook_replay_conflict_and_atomic_next_condition(self) -> None:
        class _CustomPlugin(BaseDomainPlugin):
            domain_name = "custom_ev"

            def validate_decision_evidence(
                self, _conn, _company_id, _agent_id, _decision_at_ns, evidence
            ) -> bool:
                if evidence.kind == "custom_metric":
                    if evidence.revision == "bad":
                        raise WorkKernelRefused("Domain plugin rejected custom_metric")
                    return True
                return False

        a, ga, sa = self._hire("dec-a")
        item_a, run_a, ca = self._open_claim(a, ga, sa)
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                resp_id = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    duty_kind=DUTY_WORK_DISCOVERY_REVIEW,
                    remit="Review",
                    readable_scope=["shared"],
                    now_ns=1_000,
                ).responsibility_id
                with self.assertRaises(WorkKernelRefused):
                    record_work_decision(
                        db,
                        company_id=self.company_id,
                        work_item_id=item_a,
                        run_id=run_a,
                        fencing_token=ca.fencing_token,
                        agent_id=a,
                        action_kind="hold",
                        rationale="bad plugin evidence",
                        evidence=(
                            DecisionEvidence(
                                "custom_metric", "m1", "bad", 1_500, 1_000
                            ),
                        ),
                        decision_at_ns=2_000,
                        domain_plugins=(_CustomPlugin(),),
                    )
                ev = (DecisionEvidence("custom_metric", "m1", "good", 1_500, 1_000),)
                nxt = {
                    "responsibility_id": resp_id,
                    "trigger_kind": "timer",
                    "due_at_ns": 10_000,
                }
                d1 = record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=item_a,
                    run_id=run_a,
                    fencing_token=ca.fencing_token,
                    agent_id=a,
                    action_kind="hold",
                    rationale="Valid plugin evidence",
                    evidence=ev,
                    decision_at_ns=2_000,
                    action_id="act-1",
                    next_condition=nxt,
                    domain_plugins=(_CustomPlugin(),),
                )
                self.assertIsNotNone(d1.next_condition_id)
                d2 = record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=item_a,
                    run_id=run_a,
                    fencing_token=ca.fencing_token,
                    agent_id=a,
                    action_kind="hold",
                    rationale="Valid plugin evidence",
                    evidence=ev,
                    decision_at_ns=2_000,
                    action_id="act-1",
                    next_condition=nxt,
                    domain_plugins=(_CustomPlugin(),),
                )
                self.assertEqual(d1.decision_id, d2.decision_id)
                with self.assertRaises(WorkKernelRefused):
                    record_work_decision(
                        db,
                        company_id=self.company_id,
                        work_item_id=item_a,
                        run_id=run_a,
                        fencing_token=ca.fencing_token,
                        agent_id=a,
                        action_kind="hold",
                        rationale="Conflicting rationale",
                        evidence=ev,
                        decision_at_ns=2_000,
                        action_id="act-1",
                        next_condition=nxt,
                    )

    def test_arm_result_continuation_both_orders_and_proactive_tools_fencing(
        self,
    ) -> None:
        a, ga, sa = self._hire("cont-a")
        for order in ("result_first", "timer_first"):
            with self.subTest(order=order):
                with hold_company_writer(
                    self.store, self.company_id, lease_directory=self.leases
                ) as w:
                    dep_id = f"dep-{order}"
                    with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                        cond = arm_result_continuation(
                            db,
                            company_id=self.company_id,
                            owner_agent_id=a,
                            dependency_asset_id=dep_id,
                            due_at_ns=2_000,
                            now_ns=1_000,
                        )
                        if order == "result_first":
                            append_audit_event(
                                db,
                                company_id=self.company_id,
                                domain="assets",
                                entity_type="shared_publication",
                                entity_id=dep_id,
                                event_type="asset.published",
                                actor_id="other",
                                payload={"asset_id": dep_id},
                                recorded_at_ns=1_500,
                            )
                            admitted = evaluate_conditions(
                                db, self.company_id, now_ns=2_500
                            )
                            self.assertEqual(len(admitted), 1)
                        else:
                            early = evaluate_conditions(
                                db, self.company_id, now_ns=2_500
                            )
                            self.assertEqual(early, ())
                            append_audit_event(
                                db,
                                company_id=self.company_id,
                                domain="assets",
                                entity_type="shared_publication",
                                entity_id=dep_id,
                                event_type="asset.published",
                                actor_id="other",
                                payload={"asset_id": dep_id},
                                recorded_at_ns=3_000,
                            )
                            late = evaluate_conditions(
                                db, self.company_id, now_ns=3_500
                            )
                            self.assertEqual(len(late), 1)
                        db.execute(
                            "UPDATE work_conditions SET status = 'cancelled' WHERE condition_id = ?",
                            (cond.condition_id,),
                        )

        item_a, run_a, ca = self._open_claim(a, ga, sa)
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            contrib = build_core_proactive_tool_contribution(writer=w, claim=ca)
            for spec in contrib.specs:
                self.assertFalse(spec.parameters.get("additionalProperties", True))
                props = set(spec.parameters.get("properties", {}).keys())
                self.assertEqual(props & _FORBIDDEN_TOOL_ARG_KEYS, set())
            handlers = build_proactive_tool_handlers(w, ca)
            c1 = handlers["set_condition"](
                {"trigger_kind": "timer", "due_at_ns": 5_000}
            )
            self.assertTrue(c1["ok"])
            c2 = handlers["replace_condition"](
                {
                    "condition_id": c1["condition_id"],
                    "trigger_kind": "shared_asset_revision",
                    "source_identity": "asset-x",
                }
            )
            self.assertEqual(c2["condition_revision"], 2)
            listed = handlers["list_conditions"]({"status": "armed"})
            self.assertTrue(
                any(
                    c["condition_id"] == c2["condition_id"]
                    for c in listed["conditions"]
                )
            )
            handlers["cancel_condition"]({"condition_id": c2["condition_id"]})
            cont = handlers["arm_result_continuation"](
                {"dependency_asset_id": "dep-tool", "due_at_ns": 8_000}
            )
            self.assertEqual(cont["trigger_kind"], "timer")
            dec = handlers["record_work_decision"](
                {
                    "action_kind": "hold",
                    "rationale": "Checked via tool",
                    "decision_at_ns": 9_000,
                }
            )
            self.assertTrue(dec["ok"])
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                complete_work_run(
                    db, run_a, ca.fencing_token, outcome={"ok": True}, now_ns=9_500
                )
            stale_handlers = build_proactive_tool_handlers(w, ca)
            for name, payload in (
                ("list_conditions", {}),
                ("set_condition", {"trigger_kind": "timer", "due_at_ns": 10_000}),
                (
                    "replace_condition",
                    {
                        "condition_id": c2["condition_id"],
                        "trigger_kind": "timer",
                        "due_at_ns": 10_000,
                    },
                ),
                ("cancel_condition", {"condition_id": c2["condition_id"]}),
                ("record_work_decision", {"action_kind": "hold", "rationale": "stale"}),
                (
                    "arm_result_continuation",
                    {"dependency_asset_id": "d", "due_at_ns": 10_000},
                ),
            ):
                with self.assertRaises(RunFenced):
                    stale_handlers[name](payload)

    def test_work_discovery_review_settled_blocked_recoverable_and_peer_wait_resume(
        self,
    ) -> None:
        a, ga, sa = self._hire("disc-a")
        b, gb, sb = self._hire("peer-b")
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            assign_project(
                w,
                self.receipt.grant_id,
                "proj-rev",
                (self.founder_id, a, b),
                publishers=(a,),
                peer_responders=(b,),
            )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                # 1. Text-only response -> blocked (0 decisions)
                item_prose, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    duty_kind=DUTY_WORK_DISCOVERY_REVIEW,
                    source_kind="test",
                    source_id="prose-1",
                    now_ns=1_000,
                )
                # 2. Valid discovery review with recoverable tool error + decision -> settled
                item_ok, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    duty_kind=DUTY_WORK_DISCOVERY_REVIEW,
                    source_kind="test",
                    source_id="ok-1",
                    now_ns=2_000,
                )
                # 3. Peer wait -> resume -> decision -> settled
                item_wait, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    duty_kind=DUTY_WORK_DISCOVERY_REVIEW,
                    source_kind="test",
                    source_id="wait-1",
                    now_ns=3_000,
                )

        with _QueuedOpenAiServer() as srv:
            resolver = InMemorySecretResolver({"envref:SD_TEST_KEY": "sk-test"})
            reg = build_production_duty_registry(
                adapter_registry=build_default_adapter_registry(
                    secret_resolver=resolver,
                    openai_base_url=srv.base_url,
                    allow_loopback_override=True,
                ),
                profile_bindings={
                    self.founder_id: self.profile,
                    a: self.profile,
                    b: self.profile,
                },
                domain_plugins=(),
            )
            rt = CompanyRuntime(
                self.store, self.company_id, reg, lease_directory=self.leases
            )

            # Dispatch 1: prose-only -> blocked
            srv.responses.put(
                _text_resp("r-prose", "I decided to hold without calling tool.")
            )
            rt.step(max_dispatches=1, now_ns=1_500)

            # Dispatch 2: recoverable cancel_condition error -> record_work_decision -> settled
            srv.responses.put(
                _tool_resp(
                    "r-bad", "c-bad", "cancel_condition", {"condition_id": "missing"}
                )
            )
            srv.responses.put(
                _tool_resp(
                    "r-dec",
                    "c-dec",
                    "record_work_decision",
                    {
                        "action_kind": "hold",
                        "rationale": "Recovered after tool error.",
                        "decision_at_ns": 2_500,
                    },
                )
            )
            srv.responses.put(_text_resp("r-ok", "Decision recorded."))
            rt.step(max_dispatches=1, now_ns=2_500)

            # Dispatch 3: request_peer_response -> wait -> B responds -> cursor wakes A -> A records decision
            srv.responses.put(
                _tool_resp(
                    "r-req",
                    "c-req",
                    "request_peer_response",
                    {
                        "request_key": "k-1",
                        "recipient_id": b,
                        "question": "Confirm?",
                        "expected_response": "yes/no",
                        "expires_at": 4_102_444_800,
                    },
                )
            )
            srv.responses.put(_text_resp("r-w", "Waiting for B."))
            rt.step(max_dispatches=1, now_ns=3_500)

            srv.responses.put(
                _tool_resp(
                    "r-b",
                    "c-b",
                    "respond_peer_request",
                    {"outcome": "answered", "answer": "Confirmed."},
                )
            )
            srv.responses.put(_text_resp("r-b2", "Done."))
            rt.step(max_dispatches=1, now_ns=4_000)

            srv.responses.put(
                _tool_resp(
                    "r-res",
                    "c-res",
                    "record_work_decision",
                    {
                        "action_kind": "investigate",
                        "rationale": "Peer confirmed.",
                        "decision_at_ns": 4_500,
                    },
                )
            )
            srv.responses.put(_text_resp("r-res2", "Settled."))
            rt.step(max_dispatches=1, now_ns=4_500)

        with closing(open_reader(self.store)) as db, read_tx(db):
            states = dict(
                db.execute(
                    "SELECT work_item_id, state FROM work_items WHERE company_id = ?",
                    (self.company_id,),
                ).fetchall()
            )
            self.assertEqual(states[item_prose.work_item_id], "blocked")
            self.assertEqual(states[item_ok.work_item_id], "settled")
            self.assertEqual(states[item_wait.work_item_id], "settled")
            decisions = db.execute(
                "SELECT COUNT(*) FROM work_decision WHERE company_id = ?",
                (self.company_id,),
            ).fetchone()[0]
            self.assertEqual(decisions, 2)

    def test_crash_recovery_settled_without_llm_and_replay_source_identity_conflict(
        self,
    ) -> None:
        a, ga, sa = self._hire("rec-a")
        item_a, run_a, ca = self._open_claim(a, ga, sa)
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                resp_id = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=a,
                    duty_kind=DUTY_WORK_DISCOVERY_REVIEW,
                    remit="Recover committed decision",
                    readable_scope=["shared"],
                    now_ns=1_000,
                ).responsibility_id
                d1 = record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=item_a,
                    run_id=run_a,
                    fencing_token=ca.fencing_token,
                    agent_id=a,
                    action_kind="hold",
                    rationale="Decision committed before crash",
                    decision_at_ns=1_500,
                    action_id="act-rec-1",
                    next_condition={
                        "responsibility_id": resp_id,
                        "trigger_kind": "timer",
                        "source_identity": "custom-src",
                        "due_at_ns": 50_000,
                    },
                )
                # Omitting non-timer source_identity on replay must raise WorkKernelRefused
                with self.assertRaises(WorkKernelRefused):
                    record_work_decision(
                        db,
                        company_id=self.company_id,
                        work_item_id=item_a,
                        run_id=run_a,
                        fencing_token=ca.fencing_token,
                        agent_id=a,
                        action_kind="hold",
                        rationale="Decision committed before crash",
                        decision_at_ns=1_500,
                        action_id="act-rec-1",
                        next_condition={
                            "responsibility_id": resp_id,
                            "trigger_kind": "timer",
                            "due_at_ns": 50_000,
                        },
                    )
                # Expire run_a lease so it becomes fenced and item_a returns to 'due'
                db.execute(
                    "UPDATE work_runs SET lease_expires_at_ns = 2_000 WHERE run_id = ?",
                    (run_a,),
                )

                # Also verify cross-run idempotent replay when prior run is fenced
                item_r, run_r1, cr1 = (
                    create_work_item(
                        db,
                        company_id=self.company_id,
                        owner_agent_id=a,
                        duty_kind=DUTY_WORK_DISCOVERY_REVIEW,
                        source_kind="test",
                        source_id="replay-item",
                        now_ns=1_000,
                    )[0],
                    None,
                    None,
                )
                del run_r1, cr1
                # Complete item_r setup in its own clean state after run_a is reclaimed
                db.execute(
                    "UPDATE work_items SET state = 'scheduled' WHERE work_item_id = ?",
                    (item_r.work_item_id,),
                )

        # Empty OpenAI server: recovery of item_a must settle with ZERO LLM calls!
        with _QueuedOpenAiServer() as srv:
            resolver = InMemorySecretResolver({"envref:SD_TEST_KEY": "sk-test"})
            reg = build_production_duty_registry(
                adapter_registry=build_default_adapter_registry(
                    secret_resolver=resolver,
                    openai_base_url=srv.base_url,
                    allow_loopback_override=True,
                ),
                profile_bindings={self.founder_id: self.profile, a: self.profile},
                domain_plugins=(),
            )
            rt = CompanyRuntime(
                self.store, self.company_id, reg, lease_directory=self.leases
            )
            summary = rt.step(max_dispatches=1, now_ns=3_000)
            self.assertEqual(summary.fenced_runs, (run_a,))
            self.assertEqual(len(summary.executed_runs), 1)
            self.assertEqual(len(srv.requests), 0)

        with closing(open_reader(self.store)) as db, read_tx(db):
            item_state = db.execute(
                "SELECT state FROM work_items WHERE work_item_id = ?", (item_a,)
            ).fetchone()[0]
            self.assertEqual(item_state, "settled")
            run2_outcome = json.loads(
                db.execute(
                    "SELECT outcome_json FROM work_runs WHERE run_id = ?",
                    (summary.executed_runs[0],),
                ).fetchone()[0]
            )
            self.assertTrue(run2_outcome["recovered_committed_decision"])
            self.assertEqual(run2_outcome["decision_id"], d1.decision_id)

        # Verify direct cross-run record_work_decision replay after run fencing
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                db.execute(
                    "UPDATE work_items SET state = 'due' WHERE work_item_id = ?",
                    (item_r.work_item_id,),
                )
                r1 = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=item_r.work_item_id,
                    lease_owner="test",
                    lease_ttl_ns=1_000,
                    now_ns=10_000,
                )
                dec1 = record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=item_r.work_item_id,
                    run_id=r1.run_id,
                    fencing_token=r1.fencing_token,
                    agent_id=a,
                    action_kind="hold",
                    rationale="Crash-replay test",
                    decision_at_ns=10_500,
                    action_id="act-cross-run",
                )
                from sourdough.runtime import reclaim_expired_runs

                reclaim_expired_runs(db, self.company_id, now_ns=12_000)
                r2 = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=item_r.work_item_id,
                    lease_owner="test",
                    lease_ttl_ns=60_000,
                    now_ns=12_100,
                )
                dec2 = record_work_decision(
                    db,
                    company_id=self.company_id,
                    work_item_id=item_r.work_item_id,
                    run_id=r2.run_id,
                    fencing_token=r2.fencing_token,
                    agent_id=a,
                    action_kind="hold",
                    rationale="Crash-replay test",
                    decision_at_ns=10_500,
                    action_id="act-cross-run",
                )
                self.assertEqual(dec1.decision_id, dec2.decision_id)


if __name__ == "__main__":
    unittest.main()
