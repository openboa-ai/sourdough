"""Unit and integration tests for M2 Feature #11: Collab OuterToolBroker, ShiftRunner waiting, and DutyRegistry."""

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
from sourdough.duty_run import DutyRefused, WorkRunClaim
from sourdough.runtime import (
    DUTY_BOARD_APPLY_DECISION,
    DUTY_CEO_RESPOND_MESSAGE,
    DUTY_WORK_ASSESSMENT_REVIEW,
    DUTY_WORK_DISCOVERY_REVIEW,
    DUTY_WORK_DREAMING,
    DUTY_WORK_HANDOFF_FOLLOWUP,
    DUTY_WORK_PEER_RESPONSE,
    CompanyRuntime,
    DutyOutcome,
    RunFenced,
    WorkKernelRefused,
    build_production_duty_registry,
    create_work_item,
)
from sourdough.runtime.collab_tools import build_core_collab_tool_contribution
from sourdough.runtime.work_kernel import (
    claim_work_run,
    complete_work_run,
    interrupt_work_run,
)
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.writer_lease import hold_company_writer


class _QueuedOpenAiServer:
    def __init__(self) -> None:
        self.responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                with parent._lock:
                    parent.requests.append(payload)
                reply = parent.responses.get(timeout=5.0)
                encoded = json.dumps(reply).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._server.server_port)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _QueuedOpenAiServer:
        self._thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)

    def push_tool(
        self, resp_id: str, call_id: str, name: str, arguments: dict[str, Any]
    ) -> None:
        self.responses.put(
            {
                "id": resp_id,
                "output": [
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": json.dumps(arguments),
                    }
                ],
                "usage": {"input_tokens": 100, "output_tokens": 30},
            }
        )

    def push_text(self, resp_id: str, text: str) -> None:
        self.responses.put(
            {
                "id": resp_id,
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ],
                "usage": {"input_tokens": 120, "output_tokens": 25},
            }
        )

    def build_registry(self, secret_resolver: InMemorySecretResolver):
        return build_default_adapter_registry(
            secret_resolver=secret_resolver,
            openai_base_url=f"http://127.0.0.1:{self.port}",
            allow_loopback_override=True,
        )


def _hire_agent(
    store: Path,
    company_id: str,
    founder_id: str,
    founder_grant_id: str,
    leases: Path,
    name: str,
    *,
    profile: str = "profile-v1",
    grant_scope: str = "assessment",
    idx: int = 1,
) -> tuple[str, str, str]:
    del founder_id, founder_grant_id
    agent_id = f"agent-{name}-{idx}"
    grant_id = f"grant-{name}-{idx}"
    with hold_company_writer(store, company_id, lease_directory=leases) as w:
        with closing(open_writer(store, lease=w)) as db, write_tx(db):
            db.execute(
                "INSERT INTO agent VALUES (?, ?, ?)",
                (agent_id, company_id, profile),
            )
            db.execute(
                "INSERT INTO standing_grant VALUES (?, ?, ?)",
                (grant_id, agent_id, grant_scope),
            )
            ensure_private_desk(db, company_id, agent_id, profile)
    return agent_id, grant_id, grant_scope


class M2CollabRuntimeTests(unittest.TestCase):
    def test_collab_tool_schemas_and_extra_handlers_override(self) -> None:
        with TemporaryDirectory(prefix="sd-m2-schemas-") as tmp:
            root = Path(tmp)
            store = root / "company.sqlite"
            leases = root / "leases"
            receipt = found(
                store,
                FoundingTerms(
                    "schema-co", "charter-v1", "manage-company", "profile-v1"
                ),
            )
            with hold_company_writer(
                store, receipt.company_id, lease_directory=leases
            ) as w:
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    item, _ = create_work_item(
                        db,
                        company_id=receipt.company_id,
                        owner_agent_id=receipt.founder_id,
                        duty_kind=DUTY_WORK_ASSESSMENT_REVIEW,
                        source_kind="unit",
                        source_id="s-1",
                        now_ns=1_000,
                    )
                    run = claim_work_run(
                        db,
                        company_id=receipt.company_id,
                        work_item_id=item.work_item_id,
                        lease_owner="unit",
                        lease_ttl_ns=60_000_000_000,
                        now_ns=1_000,
                    )
                claim = WorkRunClaim(
                    company_id=receipt.company_id,
                    founder_id=receipt.founder_id,
                    duty_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    grant_id=receipt.grant_id,
                    grant_scope="manage-company",
                    purpose="assessment_review",
                    _clock=1_000,
                )
                contrib = build_core_collab_tool_contribution(
                    writer=w,
                    claim=claim,
                    purpose=claim.purpose,
                    include_peer_respond=True,
                )
            expected_names = {
                "append_message",
                "read_messages",
                "read_inbox",
                "acknowledge_delivery",
                "request_peer_response",
                "respond_peer_request",
                "offer_handoff",
                "discover_handoffs",
                "accept_handoff",
                "decline_handoff",
                "org_list_roster",
            }
            self.assertEqual({s.name for s in contrib.specs}, expected_names)
            self.assertEqual(
                contrib.recoverable_exceptions, (DutyRefused, WorkKernelRefused)
            )
            for spec in contrib.specs:
                self.assertEqual(spec.parameters.get("additionalProperties"), False)
                props = set(spec.parameters.get("properties", {}).keys())
                self.assertEqual(props & _FORBIDDEN_TOOL_ARG_KEYS, set())

            # extra_handlers override works cleanly without duplicate registration error
            overridden = build_production_duty_registry(
                adapter_registry=build_default_adapter_registry(
                    secret_resolver=InMemorySecretResolver({})
                ),
                profile_bindings={},
                extra_handlers={
                    DUTY_WORK_ASSESSMENT_REVIEW: lambda _ctx: DutyOutcome(
                        disposition="completed", result={"override": True}
                    )
                },
            )
            self.assertEqual(
                set(overridden.registered_kinds()),
                {
                    DUTY_CEO_RESPOND_MESSAGE,
                    DUTY_BOARD_APPLY_DECISION,
                    DUTY_WORK_PEER_RESPONSE,
                    DUTY_WORK_ASSESSMENT_REVIEW,
                    DUTY_WORK_HANDOFF_FOLLOWUP,
                    DUTY_WORK_DISCOVERY_REVIEW,
                    DUTY_WORK_DREAMING,
                },
            )

    def test_peer_request_wait_resume_privacy_and_blocked_without_disposition(
        self,
    ) -> None:
        with (
            TemporaryDirectory(prefix="sd-m2-rt-peer-") as tmp,
            _QueuedOpenAiServer() as srv,
        ):
            root = Path(tmp)
            store = root / "company.sqlite"
            leases = root / "leases"
            resolver = InMemorySecretResolver({"envref:KEY": "sk-test-123"})
            p_bind = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:KEY",
            )
            receipt = found(
                store,
                FoundingTerms(
                    "peer-rt", "charter-v1", "manage-company", p_bind.profile_id()
                ),
            )
            a, ag, as_ = _hire_agent(
                store,
                receipt.company_id,
                receipt.founder_id,
                receipt.grant_id,
                leases,
                "analyst",
                profile=p_bind.profile_id(),
                idx=1,
            )
            b, bg, bs = _hire_agent(
                store,
                receipt.company_id,
                receipt.founder_id,
                receipt.grant_id,
                leases,
                "reviewer",
                profile=p_bind.profile_id(),
                idx=2,
            )
            with hold_company_writer(
                store, receipt.company_id, lease_directory=leases
            ) as w:
                rev = assign_project(
                    w,
                    receipt.grant_id,
                    "proj-rt",
                    (receipt.founder_id, a, b),
                    publishers=(a, b),
                    peer_responders=(b,),
                )
                # Seed private draft for A + shared asset for A & B + review item for A
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    prep_item, _ = create_work_item(
                        db,
                        company_id=receipt.company_id,
                        owner_agent_id=a,
                        duty_kind=DUTY_WORK_ASSESSMENT_REVIEW,
                        source_kind="unit",
                        source_id="prep-a",
                        now_ns=1_000,
                    )
                    prep_run = claim_work_run(
                        db,
                        company_id=receipt.company_id,
                        work_item_id=prep_item.work_item_id,
                        lease_owner="seed",
                        lease_ttl_ns=60_000_000_000,
                        now_ns=1_000,
                    )
                prep_claim = WorkRunClaim(
                    company_id=receipt.company_id,
                    founder_id=a,
                    duty_id=prep_item.work_item_id,
                    run_id=prep_run.run_id,
                    fencing_token=prep_run.fencing_token,
                    grant_id=ag,
                    grant_scope=as_,
                    purpose="assessment_review",
                    _clock=1_000,
                )
                a_secret_draft = save_draft(
                    w,
                    prep_claim,
                    prep_claim.purpose,
                    name="requester-secret-note",
                    content="PRIVATE-A-SECRET Shared clause 7.",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    prep_claim,
                    prep_claim.purpose,
                    Selection(
                        "pub-rt-1",
                        "requester-secret-note",
                        a_secret_draft.revision,
                        "proj-rt",
                        rev,
                        (a, b),
                        17,
                        32,
                    ),
                )
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    complete_work_run(
                        db,
                        prep_run.run_id,
                        prep_run.fencing_token,
                        outcome={"prep": True},
                        now_ns=1_500,
                    )
                    review_item, _ = create_work_item(
                        db,
                        company_id=receipt.company_id,
                        owner_agent_id=a,
                        duty_kind=DUTY_WORK_ASSESSMENT_REVIEW,
                        source_kind="unit",
                        source_id="rev-a",
                        now_ns=2_000,
                    )

            registry = build_production_duty_registry(
                adapter_registry=srv.build_registry(secret_resolver=resolver),
                profile_bindings={
                    receipt.founder_id: p_bind,
                    a: p_bind,
                    b: p_bind,
                },
            )
            rt = CompanyRuntime(
                store, receipt.company_id, registry, lease_directory=leases
            )

            # Step 1: A runs assessment_review, calls org_list_roster + request_peer_response -> waiting
            srv.push_tool("r1", "c1", "org_list_roster", {})
            srv.push_tool(
                "r2",
                "c2",
                "request_peer_response",
                {
                    "request_key": "rt-q1",
                    "recipient_id": b,
                    "question": "Approve clause 7?",
                    "expected_response": "answered/declined",
                    "expires_at": 4_102_444_800,
                    "project_id": "proj-rt",
                    "asset_id": asset.asset_id,
                    "asset_revision": sha256(asset.content.encode("utf-8")).hexdigest(),
                },
            )
            srv.push_text("r3", "Yielded to wait for B.")
            step1 = rt.step(max_dispatches=1, now_ns=3_000)
            self.assertEqual(len(step1.executed_runs), 1)
            with closing(open_reader(store)) as db, read_tx(db):
                st1 = db.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (review_item.work_item_id,),
                ).fetchone()[0]
                ep1 = db.execute(
                    "SELECT outcome_status FROM agent_episode WHERE run_id = ?",
                    (step1.executed_runs[0],),
                ).fetchone()[0]
                # Check org_list_roster tool receipt did not expose private_desk / tokens
                roster_rec = db.execute(
                    """SELECT result_json FROM tool_call_receipt
                       WHERE run_id = ? AND tool_name = 'org_list_roster'""",
                    (step1.executed_runs[0],),
                ).fetchone()[0]
            self.assertEqual(st1, "waiting")
            self.assertEqual(ep1, "waiting")
            self.assertNotIn("private_desk", roster_rec)
            self.assertNotIn("fencing_token", roster_rec)
            self.assertNotIn("grant_id", roster_rec)

            # Step 2: B runs work.peer_response -> calls respond_peer_request
            srv.push_tool(
                "r4",
                "c4",
                "respond_peer_request",
                {"outcome": "answered", "answer": "Clause 7 approved."},
            )
            srv.push_text("r5", "Sent answer to A.")
            step2 = rt.step(max_dispatches=1, now_ns=4_000)
            self.assertEqual(len(step2.executed_runs), 1)
            # Verify B's HTTP request context did NOT include A's private draft name or secret text
            b_req_serialized = json.dumps(srv.requests[-2])
            self.assertNotIn("requester-secret-note", b_req_serialized)
            self.assertNotIn("PRIVATE-A-SECRET", b_req_serialized)
            self.assertIn("Shared clause 7", b_req_serialized)

            # Step 3: A resumes with hydrated peer_outcome and completes
            srv.push_text("r6", "Finalized with B approval.")
            step3 = rt.step(max_dispatches=1, now_ns=5_000)
            self.assertEqual(len(step3.executed_runs), 1)
            a_resume_req = json.dumps(srv.requests[-1])
            self.assertIn("Clause 7 approved.", a_resume_req)
            with closing(open_reader(store)) as db, read_tx(db):
                st_final = db.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (review_item.work_item_id,),
                ).fetchone()[0]
            self.assertEqual(st_final, "settled")

            # Also verify work.peer_response that finishes without calling respond_peer_request -> blocked
            with hold_company_writer(
                store, receipt.company_id, lease_directory=leases
            ) as w:
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    item2, _ = create_work_item(
                        db,
                        company_id=receipt.company_id,
                        owner_agent_id=a,
                        duty_kind=DUTY_WORK_ASSESSMENT_REVIEW,
                        source_kind="unit",
                        source_id="rev-a-2",
                        now_ns=6_000,
                    )
            srv.push_tool(
                "r7",
                "c7",
                "request_peer_response",
                {
                    "request_key": "rt-q2",
                    "recipient_id": b,
                    "question": "Check again?",
                    "expected_response": "yes/no",
                    "expires_at": 4_102_444_800,
                },
            )
            srv.push_text("r8", "Waiting again.")
            rt.step(max_dispatches=1, now_ns=7_000)
            # B returns text without respond_peer_request
            srv.push_text("r9", "Forgot to call tool.")
            step_blk = rt.step(max_dispatches=1, now_ns=8_000)
            self.assertEqual(len(step_blk.blocked_items), 1)
            with closing(open_reader(store)) as db, read_tx(db):
                blk_state = db.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (step_blk.blocked_items[0],),
                ).fetchone()[0]
            self.assertEqual(blk_state, "blocked")
            _ = (bg, bs, item2)

    def test_handoff_tools_and_followup_handler_end_to_end(self) -> None:
        with (
            TemporaryDirectory(prefix="sd-m2-rt-ho-") as tmp,
            _QueuedOpenAiServer() as srv,
        ):
            root = Path(tmp)
            store = root / "company.sqlite"
            leases = root / "leases"
            resolver = InMemorySecretResolver({"envref:KEY": "sk-test-456"})
            p_bind = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:KEY",
            )
            receipt = found(
                store,
                FoundingTerms(
                    "ho-rt", "charter-v1", "manage-company", p_bind.profile_id()
                ),
            )
            a, ag, as_ = _hire_agent(
                store,
                receipt.company_id,
                receipt.founder_id,
                receipt.grant_id,
                leases,
                "sender",
                profile=p_bind.profile_id(),
                idx=1,
            )
            b, bg, bs = _hire_agent(
                store,
                receipt.company_id,
                receipt.founder_id,
                receipt.grant_id,
                leases,
                "acceptor",
                profile=p_bind.profile_id(),
                idx=2,
            )
            with hold_company_writer(
                store, receipt.company_id, lease_directory=leases
            ) as w:
                rev = assign_project(
                    w,
                    receipt.grant_id,
                    "proj-ho-rt",
                    (receipt.founder_id, a, b),
                    publishers=(a, b),
                )
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    item_a, _ = create_work_item(
                        db,
                        company_id=receipt.company_id,
                        owner_agent_id=a,
                        duty_kind=DUTY_WORK_ASSESSMENT_REVIEW,
                        source_kind="unit",
                        source_id="ho-a",
                        now_ns=1_000,
                    )
                    run_a = claim_work_run(
                        db,
                        company_id=receipt.company_id,
                        work_item_id=item_a.work_item_id,
                        lease_owner="seed",
                        lease_ttl_ns=60_000_000_000,
                        now_ns=1_000,
                    )
                claim_a = WorkRunClaim(
                    company_id=receipt.company_id,
                    founder_id=a,
                    duty_id=item_a.work_item_id,
                    run_id=run_a.run_id,
                    fencing_token=run_a.fencing_token,
                    grant_id=ag,
                    grant_scope=as_,
                    purpose="assessment_review",
                    _clock=1_000,
                )
                d = save_draft(
                    w,
                    claim_a,
                    claim_a.purpose,
                    name="a-private-ho",
                    content="SECRET-SENDER-ONLY Shared handoff body.",
                    expected_revision=None,
                )
                asset = publish_selection(
                    w,
                    claim_a,
                    claim_a.purpose,
                    Selection(
                        "pub-ho-rt",
                        "a-private-ho",
                        d.revision,
                        "proj-ho-rt",
                        rev,
                        (a, b),
                        19,
                        39,
                    ),
                )
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    complete_work_run(
                        db,
                        run_a.run_id,
                        run_a.fencing_token,
                        outcome={"pub": True},
                        now_ns=1_500,
                    )
                    item_a2, _ = create_work_item(
                        db,
                        company_id=receipt.company_id,
                        owner_agent_id=a,
                        duty_kind=DUTY_WORK_ASSESSMENT_REVIEW,
                        source_kind="unit",
                        source_id="ho-a2",
                        now_ns=2_000,
                    )
                    # Publish an asset from item_a2 during runtime via desk_publish_selection or offer directly:
                    run_a2 = claim_work_run(
                        db,
                        company_id=receipt.company_id,
                        work_item_id=item_a2.work_item_id,
                        lease_owner="seed",
                        lease_ttl_ns=60_000_000_000,
                        now_ns=2_000,
                    )
                claim_a2 = WorkRunClaim(
                    company_id=receipt.company_id,
                    founder_id=a,
                    duty_id=item_a2.work_item_id,
                    run_id=run_a2.run_id,
                    fencing_token=run_a2.fencing_token,
                    grant_id=ag,
                    grant_scope=as_,
                    purpose="assessment_review",
                    _clock=2_000,
                )
                asset2 = publish_selection(
                    w,
                    claim_a2,
                    claim_a2.purpose,
                    Selection(
                        "pub-ho-rt-2",
                        "a-private-ho",
                        d.revision,
                        "proj-ho-rt",
                        rev,
                        (a, b),
                        19,
                        39,
                    ),
                )
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    complete_work_run(
                        db,
                        run_a2.run_id,
                        run_a2.fencing_token,
                        outcome={"pub": True},
                        now_ns=2_500,
                    )
                    # Give A a fresh item & B a review item to discover/accept
                    item_b, _ = create_work_item(
                        db,
                        company_id=receipt.company_id,
                        owner_agent_id=b,
                        duty_kind=DUTY_WORK_HANDOFF_FOLLOWUP,
                        source_kind="unit",
                        source_id="b-check",
                        now_ns=3_000,
                    )
                    # Also test tool messaging + recoverable duplicate-body refusal in B's shift
                    item_a_offer, _ = create_work_item(
                        db,
                        company_id=receipt.company_id,
                        owner_agent_id=a,
                        duty_kind=DUTY_WORK_ASSESSMENT_REVIEW,
                        source_kind="unit",
                        source_id="a-offer-item",
                        now_ns=2_600,
                    )

            registry = build_production_duty_registry(
                adapter_registry=srv.build_registry(secret_resolver=resolver),
                profile_bindings={receipt.founder_id: p_bind, a: p_bind, b: p_bind},
            )
            rt = CompanyRuntime(
                store, receipt.company_id, registry, lease_directory=leases
            )
            # First run B's handoff_followup to exercise messaging + recoverable duplicate-body refusal
            srv.push_tool(
                "m1",
                "cm1",
                "append_message",
                {
                    "project_id": "proj-ho-rt",
                    "project_revision": rev,
                    "request_id": "msg-1",
                    "body": "Starting review",
                },
            )
            srv.push_tool(
                "m2",
                "cm2",
                "append_message",
                {
                    "project_id": "proj-ho-rt",
                    "project_revision": rev,
                    "request_id": "msg-2-dup",
                    "body": "Starting review",
                },
            )
            srv.push_text("m3", "Message check done.")
            rt.step(max_dispatches=1, now_ns=3_500)
            with closing(open_reader(store)) as db, read_tx(db):
                dup_refusal = db.execute(
                    "SELECT status, error_type FROM tool_call_receipt WHERE call_id = 'cm2'"
                ).fetchone()
            self.assertEqual(dup_refusal, ("refused", "DutyRefused"))

            # Now A publishes & offers handoff inside shift -> B accepts -> successor executes
            srv.push_tool(
                "h1",
                "ch1",
                "desk_save_draft",
                {"name": "handoff-note", "content": "Verified ledger excerpt."},
            )
            # Let A read its own draft revision? Even simpler: A uses known draft via desk_publish_selection?
            # Or A sends a message + offers handoff? Since publish requires exact draft revision, let's test
            # offer_handoff via recoverable refusal on wrong asset, then finish A, seed direct offer, and have B accept via tool!
            srv.push_tool(
                "h2",
                "ch2",
                "offer_handoff",
                {
                    "request_id": "ho-bad",
                    "recipient_id": b,
                    "asset_id": asset2.asset_id,
                    "expires_at": 4_102_444_800,
                    "expected_result": "Not from this duty",
                },
            )
            srv.push_text("h3", "Done with A.")
            rt.step(max_dispatches=1, now_ns=4_000)
            with closing(open_reader(store)) as db, read_tx(db):
                ho_refusal = db.execute(
                    "SELECT status, error_type FROM tool_call_receipt WHERE call_id = 'ch2'"
                ).fetchone()
            self.assertEqual(ho_refusal, ("refused", "DutyRefused"))
            _ = (asset, item_b, item_a_offer, bg, bs)

            # Now seed an offer from a fresh Claim A3 and run B's discover_handoffs + accept_handoff + successor execution
            with hold_company_writer(
                store, receipt.company_id, lease_directory=leases
            ) as w:
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    item_a3, _ = create_work_item(
                        db,
                        company_id=receipt.company_id,
                        owner_agent_id=a,
                        duty_kind=DUTY_WORK_ASSESSMENT_REVIEW,
                        source_kind="unit",
                        source_id="a-3",
                        now_ns=5_000,
                    )
                    run_a3 = claim_work_run(
                        db,
                        company_id=receipt.company_id,
                        work_item_id=item_a3.work_item_id,
                        lease_owner="seed",
                        lease_ttl_ns=60_000_000_000,
                        now_ns=5_000,
                    )
                claim_a3 = WorkRunClaim(
                    company_id=receipt.company_id,
                    founder_id=a,
                    duty_id=item_a3.work_item_id,
                    run_id=run_a3.run_id,
                    fencing_token=run_a3.fencing_token,
                    grant_id=ag,
                    grant_scope=as_,
                    purpose="assessment_review",
                    _clock=5_000,
                )
                asset3 = publish_selection(
                    w,
                    claim_a3,
                    claim_a3.purpose,
                    Selection(
                        "pub-ho-rt-3",
                        "a-private-ho",
                        d.revision,
                        "proj-ho-rt",
                        rev,
                        (a, b),
                        19,
                        39,
                    ),
                )
                from sourdough.work_handoff import HandoffOffer, offer_handoff

                offer_id = offer_handoff(
                    w,
                    claim_a3,
                    HandoffOffer(
                        "ho-rt-ok", b, asset3.asset_id, 4_102_444_800, "Sign off"
                    ),
                    now=5,
                )
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    item_b2, _ = create_work_item(
                        db,
                        company_id=receipt.company_id,
                        owner_agent_id=b,
                        duty_kind=DUTY_WORK_HANDOFF_FOLLOWUP,
                        source_kind="unit",
                        source_id="b-accept",
                        now_ns=6_000,
                    )
            srv.push_tool("b1", "cb1", "discover_handoffs", {})
            srv.push_tool(
                "b2",
                "cb2",
                "accept_handoff",
                {"offer_id": offer_id, "expected_state_version": 1},
            )
            srv.push_text("b3", "Accepted handoff.")
            # Successor item runs next!
            srv.push_text("b4", "Completed successor handoff work.")
            rt.step(max_dispatches=2, now_ns=7_000)
            succ_req_serialized = json.dumps(srv.requests[-1])
            self.assertIn("Shared handoff body.", succ_req_serialized)
            self.assertNotIn("SECRET-SENDER-ONLY", succ_req_serialized)
            self.assertNotIn("a-private-ho", succ_req_serialized)
            with closing(open_reader(store)) as db, read_tx(db):
                ho_row = db.execute(
                    "SELECT state, state_version, successor_work_item_id FROM work_handoff WHERE offer_id = ?",
                    (offer_id,),
                ).fetchone()
                succ_state = db.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (ho_row[2],),
                ).fetchone()[0]
                a3_state = db.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (item_a3.work_item_id,),
                ).fetchone()[0]
            self.assertEqual(ho_row[:2], ("accepted", 2))
            self.assertEqual(succ_state, "settled")
            self.assertEqual(a3_state, "cancelled")
            _ = item_b2

    def test_multi_hop_peer_response_shift_inbox_decline_and_fatal_fencing(
        self,
    ) -> None:
        with (
            TemporaryDirectory(prefix="sd-m2-rt-multihop-") as tmp,
            _QueuedOpenAiServer() as srv,
        ):
            root = Path(tmp)
            store = root / "company.sqlite"
            leases = root / "leases"
            resolver = InMemorySecretResolver({"envref:KEY": "sk-test-789"})
            p_bind = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:KEY",
            )
            receipt = found(
                store,
                FoundingTerms(
                    "mh-rt", "charter-v1", "manage-company", p_bind.profile_id()
                ),
            )
            a, ag, as_ = _hire_agent(
                store,
                receipt.company_id,
                receipt.founder_id,
                receipt.grant_id,
                leases,
                "a",
                profile=p_bind.profile_id(),
                idx=1,
            )
            b, bg, bs = _hire_agent(
                store,
                receipt.company_id,
                receipt.founder_id,
                receipt.grant_id,
                leases,
                "b",
                profile=p_bind.profile_id(),
                idx=2,
            )
            c, cg, cs = _hire_agent(
                store,
                receipt.company_id,
                receipt.founder_id,
                receipt.grant_id,
                leases,
                "c",
                profile=p_bind.profile_id(),
                idx=3,
            )
            with hold_company_writer(
                store, receipt.company_id, lease_directory=leases
            ) as w:
                rev = assign_project(
                    w,
                    receipt.grant_id,
                    "proj-mh",
                    (receipt.founder_id, a, b, c),
                    publishers=(a, b, c),
                    peer_responders=(b, c),
                )
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    item_a, _ = create_work_item(
                        db,
                        company_id=receipt.company_id,
                        owner_agent_id=a,
                        duty_kind=DUTY_WORK_ASSESSMENT_REVIEW,
                        source_kind="unit",
                        source_id="mh-a",
                        now_ns=1_000,
                    )

            registry = build_production_duty_registry(
                adapter_registry=srv.build_registry(secret_resolver=resolver),
                profile_bindings={
                    receipt.founder_id: p_bind,
                    a: p_bind,
                    b: p_bind,
                    c: p_bind,
                },
            )
            rt = CompanyRuntime(
                store, receipt.company_id, registry, lease_directory=leases
            )
            # 1. A sends message + asks B
            srv.push_tool(
                "a1",
                "ca1",
                "append_message",
                {
                    "project_id": "proj-mh",
                    "project_revision": rev,
                    "request_id": "mh-msg-1",
                    "body": "Check project thread.",
                },
            )
            srv.push_tool(
                "a2",
                "ca2",
                "request_peer_response",
                {
                    "request_key": "mh-ab",
                    "recipient_id": b,
                    "question": "Check with C?",
                    "expected_response": "answered/declined",
                    "expires_at": 4_102_444_800,
                    "project_id": "proj-mh",
                },
            )
            srv.push_text("a3", "A waiting on B.")
            rt.step(max_dispatches=1, now_ns=2_000)

            # 2. B reads inbox/messages, delegates to C (multi-hop waiting)
            srv.push_tool("b1", "cb1", "read_inbox", {})
            srv.push_tool("b2", "cb2", "read_messages", {"project_id": "proj-mh"})
            srv.push_tool(
                "b3",
                "cb3",
                "request_peer_response",
                {
                    "request_key": "mh-bc",
                    "recipient_id": c,
                    "question": "Confirm depth-2 detail?",
                    "expected_response": "answered/declined",
                    "expires_at": 4_102_444_800,
                },
            )
            srv.push_text("b4", "B waiting on C.")
            rt.step(max_dispatches=1, now_ns=3_000)

            # 3. C answers B
            srv.push_tool(
                "c1",
                "cc1",
                "respond_peer_request",
                {"outcome": "answered", "answer": "C confirmed."},
            )
            srv.push_text("c2", "C answered.")
            rt.step(max_dispatches=1, now_ns=4_000)

            # 4. B wakes/resumes (advance_peer_read_on_resume not needed if B calls respond_peer_request? Wait:
            # _run_work_peer_response calls record_peer_request_read during hydration!), then B answers A!
            srv.push_tool(
                "b5",
                "cb5",
                "respond_peer_request",
                {"outcome": "answered", "answer": "B+C confirmed."},
            )
            srv.push_text("b6", "B answered A.")
            rt.step(max_dispatches=1, now_ns=5_000)

            # 5. A wakes and settles
            srv.push_text("a4", "A settled.")
            rt.step(max_dispatches=1, now_ns=6_000)
            with closing(open_reader(store)) as db, read_tx(db):
                a_state = db.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (item_a.work_item_id,),
                ).fetchone()[0]
                depths = [
                    r[0]
                    for r in db.execute(
                        "SELECT chain_depth FROM work_peer_requests WHERE company_id = ? ORDER BY chain_depth",
                        (receipt.company_id,),
                    ).fetchall()
                ]
                ab_row = db.execute(
                    """SELECT read_run_id, answered_run_id
                       FROM work_peer_requests
                       WHERE company_id = ? AND request_key = 'mh-ab'""",
                    (receipt.company_id,),
                ).fetchone()
                bc_row = db.execute(
                    """SELECT resumed_run_id
                       FROM work_peer_requests
                       WHERE company_id = ? AND request_key = 'mh-bc'""",
                    (receipt.company_id,),
                ).fetchone()
            self.assertEqual(a_state, "settled")
            self.assertEqual(depths, [1, 2])
            self.assertIsNotNone(ab_row)
            self.assertIsNotNone(bc_row)
            self.assertIsNotNone(ab_row[1])
            self.assertEqual(ab_row[0], ab_row[1])
            self.assertIsNotNone(bc_row[0])
            self.assertEqual(bc_row[0], ab_row[1])
            # B's resumed shift (req index 9) must see C's answer in its payload
            b_resumed_payload = json.dumps(srv.requests[9])
            self.assertIn("C confirmed.", b_resumed_payload)

            # Verify RunFenced propagates fatally through OuterToolBroker
            with hold_company_writer(
                store, receipt.company_id, lease_directory=leases
            ) as w:
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    fenced_item, _ = create_work_item(
                        db,
                        company_id=receipt.company_id,
                        owner_agent_id=c,
                        duty_kind=DUTY_WORK_ASSESSMENT_REVIEW,
                        source_kind="unit",
                        source_id="fenced-item",
                        now_ns=7_000,
                    )
                    fenced_run = claim_work_run(
                        db,
                        company_id=receipt.company_id,
                        work_item_id=fenced_item.work_item_id,
                        lease_owner="unit",
                        lease_ttl_ns=60_000_000_000,
                        now_ns=7_000,
                    )
                    interrupt_work_run(
                        db,
                        company_id=receipt.company_id,
                        run_id=fenced_run.run_id,
                        expected_fencing_token=fenced_run.fencing_token,
                        actor_id=receipt.founder_id,
                        reason="fenced_test",
                        now_ns=7_100,
                    )
                fenced_claim = WorkRunClaim(
                    company_id=receipt.company_id,
                    founder_id=c,
                    duty_id=fenced_item.work_item_id,
                    run_id=fenced_run.run_id,
                    fencing_token=fenced_run.fencing_token,
                    grant_id=cg,
                    grant_scope=cs,
                    purpose="assessment_review",
                    _clock=7_200,
                )
                contrib = build_core_collab_tool_contribution(
                    writer=w, claim=fenced_claim, purpose=fenced_claim.purpose
                )
                with self.assertRaises(RunFenced):
                    contrib.handlers["org_list_roster"]({})
            _ = (ag, as_, bg, bs)

    def test_nanosecond_expires_at_returns_recoverable_refusal_in_shift_and_tool(
        self,
    ) -> None:
        with (
            TemporaryDirectory(prefix="sd-m2-rt-nano-") as tmp,
            _QueuedOpenAiServer() as srv,
        ):
            root = Path(tmp)
            store = root / "company.sqlite"
            leases = root / "leases"
            resolver = InMemorySecretResolver({"envref:KEY": "sk-test-nano"})
            p_bind = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:KEY",
            )
            receipt = found(
                store,
                FoundingTerms(
                    "nano-rt", "charter-v1", "manage-company", p_bind.profile_id()
                ),
            )
            a, ag, as_ = _hire_agent(
                store,
                receipt.company_id,
                receipt.founder_id,
                receipt.grant_id,
                leases,
                "a",
                profile=p_bind.profile_id(),
                idx=1,
            )
            b, _, _ = _hire_agent(
                store,
                receipt.company_id,
                receipt.founder_id,
                receipt.grant_id,
                leases,
                "b",
                profile=p_bind.profile_id(),
                idx=2,
            )
            with hold_company_writer(
                store, receipt.company_id, lease_directory=leases
            ) as w:
                assign_project(
                    w,
                    receipt.grant_id,
                    "proj-nano",
                    (receipt.founder_id, a, b),
                    publishers=(a,),
                    peer_responders=(b,),
                )
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    item, _ = create_work_item(
                        db,
                        company_id=receipt.company_id,
                        owner_agent_id=a,
                        duty_kind=DUTY_WORK_ASSESSMENT_REVIEW,
                        source_kind="unit",
                        source_id="nano-item",
                        now_ns=1_000,
                    )
                    run = claim_work_run(
                        db,
                        company_id=receipt.company_id,
                        work_item_id=item.work_item_id,
                        lease_owner="unit",
                        lease_ttl_ns=60_000_000_000,
                        now_ns=1_000,
                    )
                claim = WorkRunClaim(
                    company_id=receipt.company_id,
                    founder_id=a,
                    duty_id=item.work_item_id,
                    run_id=run.run_id,
                    fencing_token=run.fencing_token,
                    grant_id=ag,
                    grant_scope=as_,
                    purpose="assessment_review",
                    _clock=1_000,
                )
                contrib = build_core_collab_tool_contribution(
                    writer=w, claim=claim, purpose=claim.purpose
                )
                specs_by_name = {s.name: s for s in contrib.specs}
                self.assertIn(
                    "Unix epoch seconds",
                    specs_by_name["request_peer_response"].parameters["properties"][
                        "expires_at"
                    ]["description"],
                )
                self.assertIn(
                    "Unix epoch seconds",
                    specs_by_name["offer_handoff"].parameters["properties"][
                        "expires_at"
                    ]["description"],
                )
                nano_ts = 1_764_220_000_000_000_000
                with self.assertRaises(DutyRefused):
                    contrib.handlers["request_peer_response"](
                        {
                            "request_key": "nano-k",
                            "recipient_id": b,
                            "question": "Check?",
                            "expected_response": "yes/no",
                            "expires_at": nano_ts,
                        }
                    )
                with self.assertRaises(DutyRefused):
                    contrib.handlers["offer_handoff"](
                        {
                            "request_id": "nano-ho",
                            "recipient_id": b,
                            "asset_id": "missing-asset",
                            "expires_at": nano_ts,
                            "expected_result": "Review",
                        }
                    )
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    complete_work_run(
                        db,
                        run.run_id,
                        run.fencing_token,
                        outcome={"done": True},
                        now_ns=1_100,
                    )
                    item2, _ = create_work_item(
                        db,
                        company_id=receipt.company_id,
                        owner_agent_id=a,
                        duty_kind=DUTY_WORK_ASSESSMENT_REVIEW,
                        source_kind="unit",
                        source_id="nano-item-2",
                        now_ns=1_200,
                    )

            registry = build_production_duty_registry(
                adapter_registry=srv.build_registry(secret_resolver=resolver),
                profile_bindings={receipt.founder_id: p_bind, a: p_bind, b: p_bind},
            )
            rt = CompanyRuntime(
                store, receipt.company_id, registry, lease_directory=leases
            )
            srv.push_tool(
                "n1",
                "cn1",
                "request_peer_response",
                {
                    "request_key": "nano-shift",
                    "recipient_id": b,
                    "question": "Check?",
                    "expected_response": "yes/no",
                    "expires_at": 1_764_220_000_000_000_000,
                },
            )
            srv.push_text("n2", "Recovered after invalid expires_at.")
            rt.step(max_dispatches=1, now_ns=2_000)

            with closing(open_reader(store)) as db, read_tx(db):
                state = db.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (item2.work_item_id,),
                ).fetchone()[0]
                receipt_row = db.execute(
                    """SELECT status, error_message, result_json
                       FROM tool_call_receipt
                       WHERE company_id = ? AND tool_name = 'request_peer_response'""",
                    (receipt.company_id,),
                ).fetchone()
            self.assertEqual(state, "settled")
            self.assertEqual(receipt_row[0], "refused")
            self.assertIn("Unix epoch seconds", str(receipt_row[1]))
            self.assertIn("Unix epoch seconds", str(receipt_row[2]))

    def test_peer_response_entry_and_post_shift_expiry_settle_without_run_fenced(
        self,
    ) -> None:
        ns = 1_000_000_000
        with (
            TemporaryDirectory(prefix="sd-m2-rt-exp-") as tmp,
            _QueuedOpenAiServer() as srv,
        ):
            root = Path(tmp)
            store = root / "company.sqlite"
            leases = root / "leases"
            resolver = InMemorySecretResolver({"envref:KEY": "sk-test-exp"})
            p_bind = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-5-mini",
                revision="v1",
                credential_handle_ref="envref:KEY",
            )
            receipt = found(
                store,
                FoundingTerms(
                    "exp-rt", "charter-v1", "manage-company", p_bind.profile_id()
                ),
            )
            a, _, _ = _hire_agent(
                store,
                receipt.company_id,
                receipt.founder_id,
                receipt.grant_id,
                leases,
                "a",
                profile=p_bind.profile_id(),
                idx=1,
            )
            b, _, _ = _hire_agent(
                store,
                receipt.company_id,
                receipt.founder_id,
                receipt.grant_id,
                leases,
                "b",
                profile=p_bind.profile_id(),
                idx=2,
            )
            with hold_company_writer(
                store, receipt.company_id, lease_directory=leases
            ) as w:
                assign_project(
                    w,
                    receipt.grant_id,
                    "proj-exp",
                    (receipt.founder_id, a, b),
                    publishers=(a,),
                    peer_responders=(b,),
                )
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    item_case1, _ = create_work_item(
                        db,
                        company_id=receipt.company_id,
                        owner_agent_id=a,
                        duty_kind=DUTY_WORK_ASSESSMENT_REVIEW,
                        source_kind="unit",
                        source_id="exp-case1-a",
                        now_ns=1_000 * ns,
                    )

            registry = build_production_duty_registry(
                adapter_registry=srv.build_registry(secret_resolver=resolver),
                profile_bindings={receipt.founder_id: p_bind, a: p_bind, b: p_bind},
            )

            # Case 1: Entry expiry — claimed at 1049s < 1050s, clock reaches 1050s at _run_work_peer_response entry
            clock_state = {"now_ns": 1_000 * ns}
            entry_tick_calls = {"count": 0, "active": False}

            def _clock() -> int:
                if entry_tick_calls["active"]:
                    entry_tick_calls["count"] += 1
                    # Call 1 = sweep_now, Call 2 = claim_now; Call 3+ = inside _run_work_peer_response
                    return 1_049 * ns if entry_tick_calls["count"] <= 2 else 1_050 * ns
                return clock_state["now_ns"]

            rt = CompanyRuntime(
                store,
                receipt.company_id,
                registry,
                lease_directory=leases,
                clock=_clock,
            )
            srv.push_tool(
                "e1",
                "ce1",
                "request_peer_response",
                {
                    "request_key": "exp-q1",
                    "recipient_id": b,
                    "question": "Approve before 1050s?",
                    "expected_response": "answered/declined",
                    "expires_at": 1_050,
                    "project_id": "proj-exp",
                },
            )
            srv.push_text("e2", "A waiting on B for case 1.")
            step1 = rt.step(max_dispatches=1)
            self.assertEqual(len(step1.executed_runs), 1)

            entry_tick_calls["active"] = True
            step_b_entry = rt.step(max_dispatches=1)
            entry_tick_calls["active"] = False
            self.assertEqual(len(step_b_entry.executed_runs), 1)
            self.assertEqual(step_b_entry.blocked_items, ())

            with closing(open_reader(store)) as db, read_tx(db):
                req1 = db.execute(
                    """SELECT state, response_work_item_id
                       FROM work_peer_requests
                       WHERE company_id = ? AND request_key = 'exp-q1'""",
                    (receipt.company_id,),
                ).fetchone()
                a1_state = db.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (item_case1.work_item_id,),
                ).fetchone()[0]
                b1_item_state = db.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (req1[1],),
                ).fetchone()[0]
                b1_run_status = db.execute(
                    "SELECT status FROM work_runs WHERE work_item_id = ?",
                    (req1[1],),
                ).fetchone()[0]
            self.assertEqual(req1[0], "expired")
            self.assertEqual(a1_state, "due")
            self.assertEqual(b1_item_state, "settled")
            self.assertEqual(b1_run_status, "completed")

            # Requester A resumes cleanly with the expired outcome
            clock_state["now_ns"] = 1_051 * ns
            srv.push_text("e3", "A observed expired peer request.")
            step_a_resume1 = rt.step(max_dispatches=1)
            self.assertEqual(len(step_a_resume1.executed_runs), 1)
            self.assertIn("expired", json.dumps(srv.requests[-1]))
            with closing(open_reader(store)) as db, read_tx(db):
                a1_final = db.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (item_case1.work_item_id,),
                ).fetchone()[0]
            self.assertEqual(a1_final, "settled")

            # Case 2: Post-shift expiry — B runs LLM shift without calling respond_peer_request,
            # clock reaches 2050s before post-shift check, settles cleanly, wakes A, and frees B.
            with hold_company_writer(
                store, receipt.company_id, lease_directory=leases
            ) as w:
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    item_case2, _ = create_work_item(
                        db,
                        company_id=receipt.company_id,
                        owner_agent_id=a,
                        duty_kind=DUTY_WORK_ASSESSMENT_REVIEW,
                        source_kind="unit",
                        source_id="exp-case2-a",
                        now_ns=2_000 * ns,
                    )

            clock_state["now_ns"] = 2_001 * ns
            srv.push_tool(
                "p1",
                "cp1",
                "request_peer_response",
                {
                    "request_key": "exp-q2",
                    "recipient_id": b,
                    "question": "Approve before 2050s?",
                    "expected_response": "answered/declined",
                    "expires_at": 2_050,
                    "project_id": "proj-exp",
                },
            )
            srv.push_text("p2", "A waiting on B for case 2.")
            rt.step(max_dispatches=1)

            pre_shift_reqs = len(srv.requests)
            post_shift_watch = {"active": True}

            def _post_shift_clock() -> int:
                if post_shift_watch["active"]:
                    return (
                        2_050 * ns if len(srv.requests) > pre_shift_reqs else 2_049 * ns
                    )
                return clock_state["now_ns"]

            rt_post = CompanyRuntime(
                store,
                receipt.company_id,
                registry,
                lease_directory=leases,
                clock=_post_shift_clock,
            )
            srv.push_text("p3", "B thought about it without responding.")
            step_b_post = rt_post.step(max_dispatches=1)
            post_shift_watch["active"] = False
            self.assertEqual(len(step_b_post.executed_runs), 1)
            self.assertEqual(step_b_post.blocked_items, ())

            with closing(open_reader(store)) as db, read_tx(db):
                req2 = db.execute(
                    """SELECT state, response_work_item_id
                       FROM work_peer_requests
                       WHERE company_id = ? AND request_key = 'exp-q2'""",
                    (receipt.company_id,),
                ).fetchone()
                a2_state = db.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (item_case2.work_item_id,),
                ).fetchone()[0]
                b2_item_state = db.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (req2[1],),
                ).fetchone()[0]
                b2_run_status = db.execute(
                    "SELECT status FROM work_runs WHERE work_item_id = ?",
                    (req2[1],),
                ).fetchone()[0]
            self.assertEqual(req2[0], "expired")
            self.assertEqual(a2_state, "due")
            self.assertEqual(b2_item_state, "settled")
            self.assertEqual(b2_run_status, "completed")

            # Verify B is free to claim new work and A resumes with expired outcome
            with hold_company_writer(
                store, receipt.company_id, lease_directory=leases
            ) as w:
                with closing(open_writer(store, lease=w)) as db, write_tx(db):
                    b_extra, _ = create_work_item(
                        db,
                        company_id=receipt.company_id,
                        owner_agent_id=b,
                        duty_kind=DUTY_WORK_HANDOFF_FOLLOWUP,
                        source_kind="unit",
                        source_id="exp-b-extra",
                        now_ns=2_051 * ns,
                    )
            clock_state["now_ns"] = 2_052 * ns
            srv.push_text("p4", "A resumed after post-shift expiry.")
            srv.push_text("p5", "B completed new work.")
            step_followup = rt_post.step(max_dispatches=2)
            self.assertEqual(len(step_followup.executed_runs), 2)
            self.assertEqual(step_followup.blocked_items, ())
            with closing(open_reader(store)) as db, read_tx(db):
                a2_final = db.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (item_case2.work_item_id,),
                ).fetchone()[0]
                b_extra_final = db.execute(
                    "SELECT state FROM work_items WHERE work_item_id = ?",
                    (b_extra.work_item_id,),
                ).fetchone()[0]
            self.assertEqual(a2_final, "settled")
            self.assertEqual(b_extra_final, "settled")


if __name__ == "__main__":
    unittest.main()
