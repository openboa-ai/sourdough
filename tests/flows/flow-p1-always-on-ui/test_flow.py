"""Connected flow verification for flow-p1-always-on-ui."""

from __future__ import annotations

import http.client
import json
import socket
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns

from sourdough.agent_desk import save_draft
from sourdough.agent_runtime import PROVIDER_OPENAI, AgentProfileBinding
from sourdough.board_control import bind_board_principal
from sourdough.company import FoundingTerms, found
from sourdough.duty_run import WorkRunClaim
from sourdough.learning import (
    activate_method_delta,
    distill_method_delta,
    qualify_method_delta,
)
from sourdough.memory import record_episode
from sourdough.runtime.kernel import (
    DecisionEvidence,
    claim_work_run,
    complete_work_run,
    create_work_item,
    open_peer_request,
    record_discovery_hysteresis,
    record_work_decision,
    register_responsibility,
    set_condition,
    upsert_project_peer_responsibility,
    work_run_evidence_revision,
)
from sourdough.runtime.registry import DutyRegistry
from sourdough.runtime.resident import ResidentCompanyService
from sourdough.storage.db import write_uow
from sourdough.writer_lease import hold_company_writer

_CANARY_METHOD_ID = "canary-private-secret-method-id"
_CANARY_METHOD_BODY = "CANARY_ONE_AGENT_BODY_TOP_SECRET_999"
_CANARY_RATIONALE = "CANARY_RAW_DECISION_RATIONALE_888"
_CANARY_EPISODE_TEXT = "CANARY_RAW_EPISODE_SUMMARY_777"
_CANARY_DRAFT_FILENAME = "canary-secret-desk-draft"
_CANARY_CRED_HANDLE = "envref:OPENAI_CANARY_KEY_666"


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float = 5.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = str(socket_path)

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def _get_json(sock_path: Path, path: str) -> dict:
    conn = _UnixHTTPConnection(sock_path)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        assert resp.status == 200, f"GET {path} returned {resp.status}: {body}"
        return json.loads(body)
    finally:
        conn.close()


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self) -> None:
        with TemporaryDirectory(prefix="sd-p1-") as tmp:
            root = Path(tmp)
            co_dir = root / "co"
            co_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            store = co_dir / "c.sqlite"
            leases = root / "l"
            sock_dir = root / "s"

            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-6-luna",
                revision="rev-p1",
                sandbox_backend="seatbelt",
                credential_handle_ref=_CANARY_CRED_HANDLE,
            )
            founding = found(
                store,
                FoundingTerms(
                    "AlwaysOn UI Co",
                    "charter-v1",
                    "manage-company",
                    profile.profile_id(),
                ),
            )
            cid = founding.company_id
            fid = founding.founder_id
            peer_id = "agent-peer-1"
            now = time_ns()

            with hold_company_writer(store, cid, lease_directory=leases) as w:
                bind_board_principal(
                    w, principal_id="board-owner", role_name="board_control"
                )
                with write_uow(w) as db:
                    # Seed runtime binding (credential handle must NEVER project to Board)
                    db.execute(
                        """INSERT INTO agent_runtime_binding (
                               agent_id, company_id, provider, model, revision,
                               sandbox_backend, credential_handle_ref, profile_id,
                               created_at_ns, updated_at_ns
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            fid,
                            cid,
                            profile.provider,
                            profile.model,
                            profile.revision,
                            profile.sandbox_backend,
                            profile.credential_handle_ref,
                            profile.profile_id(),
                            now,
                            now,
                        ),
                    )
                    # Hire peer agent for peer request chain depth check
                    db.execute(
                        "INSERT INTO agent VALUES (?, ?, ?)",
                        (peer_id, cid, profile.profile_id()),
                    )
                    db.execute(
                        "INSERT INTO standing_grant VALUES (?, ?, ?)",
                        ("grant-peer-1", peer_id, "assessment"),
                    )
                    db.execute(
                        "INSERT INTO private_desk VALUES (?, ?)",
                        (peer_id, "desk-peer-1"),
                    )
                    # Standing responsibility + discovery hysteresis + armed condition with cooldown/max_fires
                    resp = register_responsibility(
                        db,
                        company_id=cid,
                        owner_agent_id=fid,
                        duty_kind="work.discovery_review",
                        remit="Monitor portfolio posture",
                        readable_scope=["company"],
                        now_ns=now,
                    )
                    record_discovery_hysteresis(
                        db,
                        company_id=cid,
                        responsibility_id=resp.responsibility_id,
                        action_kind="noop",
                        now_ns=now + 10,
                    )
                    set_condition(
                        db,
                        company_id=cid,
                        owner_agent_id=fid,
                        responsibility_id=resp.responsibility_id,
                        trigger_kind="timer",
                        due_at_ns=now + 600_000_000_000,
                        cooldown_ns=300_000_000_000,
                        max_fires=10,
                        now_ns=now + 20,
                    )
                    # Seed a completed work.dreaming run first (single active run per agent)
                    d_item, _ = create_work_item(
                        db,
                        company_id=cid,
                        owner_agent_id=fid,
                        duty_kind="work.dreaming",
                        source_kind="timer",
                        source_id="dream-1",
                        now_ns=now + 22,
                    )
                    d_run = claim_work_run(
                        db,
                        company_id=cid,
                        work_item_id=d_item.work_item_id,
                        lease_owner="p1-dream",
                        lease_ttl_ns=3_600_000_000_000,
                        now_ns=now + 24,
                    )
                    complete_work_run(
                        db,
                        d_run.run_id,
                        d_run.fencing_token,
                        outcome={
                            "summary": "Dreaming consolidated 1 episode",
                            "proposals": [_CANARY_METHOD_ID],
                        },
                        now_ns=now + 26,
                    )
                    # Create & claim a work item, record work_decision + agent_episode + peer_request
                    item, _ = create_work_item(
                        db,
                        company_id=cid,
                        owner_agent_id=fid,
                        duty_kind="work.assessment_review",
                        source_kind="unit",
                        source_id="src-p1-1",
                        now_ns=now + 30,
                    )
                    run = claim_work_run(
                        db,
                        company_id=cid,
                        work_item_id=item.work_item_id,
                        lease_owner="p1-worker",
                        lease_ttl_ns=3_600_000_000_000,
                        now_ns=now + 40,
                    )
                    claim = WorkRunClaim(
                        company_id=cid,
                        founder_id=fid,
                        duty_id=item.work_item_id,
                        run_id=run.run_id,
                        fencing_token=run.fencing_token,
                        grant_id=founding.grant_id,
                        grant_scope="manage-company",
                        purpose="manage-company",
                    )
                    run_rev = work_run_evidence_revision(
                        run.run_id,
                        company_id=cid,
                        work_item_id=item.work_item_id,
                        agent_id=fid,
                        duty_kind=item.duty_kind,
                        fencing_token=run.fencing_token,
                        created_at_ns=run.created_at_ns,
                    )
                    record_work_decision(
                        db,
                        company_id=cid,
                        work_item_id=item.work_item_id,
                        run_id=run.run_id,
                        fencing_token=run.fencing_token,
                        agent_id=fid,
                        action_id="p1-action-1",
                        action_kind="proceed",
                        evidence=(
                            DecisionEvidence(
                                kind="work_run",
                                resource_id=run.run_id,
                                revision=run_rev,
                                observed_at_ns=now + 45,
                                available_at_ns=now + 45,
                            ),
                        ),
                        rationale=_CANARY_RATIONALE,
                        now_ns=now + 50,
                    )
                    record_episode(
                        db,
                        company_id=cid,
                        agent_id=fid,
                        work_item_id=item.work_item_id,
                        run_id=run.run_id,
                        duty_kind="work.assessment_review",
                        context_digest="a" * 64,
                        outcome_status="completed",
                        summary_text=_CANARY_EPISODE_TEXT,
                        tool_calls_count=3,
                        input_tokens=500,
                        output_tokens=200,
                        cost_minor_units=45,
                        recorded_at_ns=now + 60,
                    )
                    db.execute(
                        """INSERT INTO shared_project (
                               project_id, company_id, revision, members, publishers
                           ) VALUES (?, ?, ?, ?, ?)""",
                        (
                            "proj-p1",
                            cid,
                            "1",
                            json.dumps([fid, peer_id]),
                            json.dumps([fid, peer_id]),
                        ),
                    )
                    upsert_project_peer_responsibility(
                        db,
                        company_id=cid,
                        owner_agent_id=peer_id,
                        project_id="proj-p1",
                        allowed_requesters=[fid],
                        now_ns=now + 65,
                    )

                # Save a private draft (its filename must NEVER leak) and distill + qualify + activate a one_agent method
                save_draft(
                    w,
                    claim,
                    "manage-company",
                    name=_CANARY_DRAFT_FILENAME,
                    content="Top secret draft body",
                    expected_revision=None,
                )
                delta = distill_method_delta(
                    w,
                    claim,
                    delta_kind="ADD",
                    item_kind="work_method",
                    method_id=_CANARY_METHOD_ID,
                    trigger_spec={"duty_kind": "work.assessment_review"},
                    body=_CANARY_METHOD_BODY,
                    audience_scope="one_agent",
                    source_refs=[f"work_run:{run.run_id}"],
                    now_ns=now + 100,
                )
                qual = qualify_method_delta(
                    w,
                    delta.delta_id,
                    passed_k=3,
                    total_k=3,
                    negative_trigger_passed=True,
                    safety_passed=True,
                    holdout_margin=0.05,
                    metrics={"pass_rate": 1.0},
                    now_ns=now + 110,
                )
                activate_method_delta(
                    w,
                    delta.delta_id,
                    actor_id=fid,
                    grant_id=founding.grant_id,
                    qualification_id=qual.qualification_id,
                    now_ns=now + 120,
                )
                with write_uow(w) as db:
                    open_peer_request(
                        db,
                        company_id=cid,
                        requester_work_item_id=item.work_item_id,
                        requester_run_id=run.run_id,
                        fencing_token=run.fencing_token,
                        recipient_agent_id=peer_id,
                        project_id="proj-p1",
                        question="Verify posture?",
                        expected_decision="approve",
                        evidence_refs=[],
                        deadline_ns=now + 3_600_000_000_000,
                        request_id="pr-p1-1",
                        now_ns=now + 130,
                    )

            # Boot live ResidentCompanyService (Always-On) and verify all P1 endpoints + zero canary leaks!
            with ResidentCompanyService(
                store,
                cid,
                DutyRegistry(),
                socket_dir=sock_dir,
                lease_directory=leases,
                authorize_board=lambda _db, _cid, _op: "board-owner",
                tick_interval_seconds=0.05,
                enable_board_trigger_intake=False,
            ) as svc:
                svc.wait_for(lambda h: h.ticks_completed >= 1, timeout_seconds=5.0)
                sock = svc.socket_path

                meta = _get_json(sock, "/v1/meta")
                self.assertIn("resident_health", meta)
                rh = meta["resident_health"]
                self.assertTrue(rh["running"])
                self.assertIn(rh["sleep_state"], ("awake", "sleeping"))
                self.assertGreaterEqual(rh["ticks_completed"], 1)
                self.assertGreaterEqual(rh["wake_count"], 1)

                detail = _get_json(sock, f"/v1/projections/agents/{fid}")
                # Enriched P1 fields present
                self.assertEqual(detail["runtime_binding"]["provider"], "openai")
                self.assertEqual(detail["runtime_binding"]["model"], "gpt-6-luna")
                self.assertNotIn("credential_handle_ref", detail["runtime_binding"])
                self.assertGreaterEqual(len(detail["responsibilities"]), 1)
                self.assertEqual(detail["responsibilities"][0]["consecutive_noops"], 1)
                self.assertEqual(detail["dreaming_summary"]["completed_runs"], 1)
                self.assertTrue(detail["active_snapshot_id"])
                self.assertEqual(len(detail["active_methods"]), 1)
                self.assertTrue(detail["active_methods"][0]["body_redacted"])
                self.assertEqual(len(detail["tabs"]["work_decisions"]), 1)
                self.assertTrue(
                    detail["tabs"]["work_decisions"][0]["rationale_redacted"]
                )
                self.assertEqual(len(detail["tabs"]["agent_episodes"]), 1)
                self.assertTrue(detail["tabs"]["agent_episodes"][0]["summary_redacted"])
                self.assertIn("chain_depth", detail["tabs"]["peer_requests"][0])
                self.assertIn("hop_count", detail["tabs"]["peer_requests"][0])
                self.assertIn("attempt_count", detail["tabs"]["work_items"][0])

                evals = _get_json(sock, "/v1/projections/system-evals")
                self.assertEqual(len(evals["method_deltas"]), 1)
                self.assertEqual(len(evals["method_qualifications"]), 1)
                self.assertEqual(len(evals["method_activations"]), 1)
                self.assertTrue(evals["method_deltas"][0]["method_id_redacted"])
                self.assertIn("rsi_transfer_summaries", evals)

                audit = _get_json(sock, "/v1/projections/system-audit")
                self.assertTrue(
                    any(e["domain"] == "learning" for e in audit["events"]),
                    "Expected learning domain audit events",
                )

                # Strict zero-leakage check across ALL JSON projections!
                combined = json.dumps(
                    {"meta": meta, "detail": detail, "evals": evals, "audit": audit}
                )
                for canary in (
                    _CANARY_METHOD_ID,
                    _CANARY_METHOD_BODY,
                    _CANARY_RATIONALE,
                    _CANARY_EPISODE_TEXT,
                    _CANARY_DRAFT_FILENAME,
                    _CANARY_CRED_HANDLE,
                ):
                    self.assertNotIn(
                        canary,
                        combined,
                        f"Private canary leaked into Board IPC projections: {canary}",
                    )


if __name__ == "__main__":
    unittest.main()
