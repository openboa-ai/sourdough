import hashlib
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.board_control import (
    CEO_CHAT_COMMAND,
    CHECKPOINT_COMMAND,
    CUT_CHECKPOINT,
    DECISION_COMMAND,
    INTERRUPT_RUN,
    PAUSE_COMPANY,
    RBAC_COMMAND,
    REGISTER_OWNER,
    RESTRICTION_COMMAND,
    RESUME_COMPANY,
    SEND_CEO_MESSAGE,
    SUBMIT_DECISION_ANSWER,
    WORK_COMMAND,
    BoardControlRefused,
    bind_board_principal,
    record_ceo_reply,
    record_decision_application_result,
    record_decision_question,
    upsert_agent_profile,
)
from sourdough.company import FoundingTerms, found
from sourdough.ipc import CompanyDaemon, ControlPlaneClient, IpcRefused
from sourdough.runtime.work_kernel import claim_work_run, create_work_item
from sourdough.storage.db import open_writer, write_tx
from sourdough.writer_lease import hold_company_writer


class BoardControlPlaneTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="sd-bc-")))
        self.store = self.root / "c.sqlite"
        self.leases = self.root / "l"
        self.sock_dir = self.root / "s"
        receipt = found(
            self.store, FoundingTerms("bc-co", "charter-v1", "paper", "profile-v1")
        )
        self.company_id = receipt.company_id
        self.founder_id = receipt.founder_id

    def _daemon(self):
        return CompanyDaemon(
            self.store,
            self.company_id,
            socket_dir=self.sock_dir,
            lease_directory=self.leases,
        )

    def test_bootstrap_lock_register_owner_and_board_viewer_enforcement(self):
        with self._daemon() as daemon:
            client = ControlPlaneClient(daemon.socket_path)
            meta = client.meta()
            self.assertTrue(meta["board_access"]["bootstrap_locked"])
            challenge = meta["board_access"]["bootstrap_challenge"]
            self.assertTrue(challenge)

            # Wrong proof fails closed 403.
            code_bad, _ = client.submit_command(
                company_id=self.company_id,
                command_id="owner-bad",
                command_type=REGISTER_OWNER,
                contract_version=RBAC_COMMAND,
                preconditions={"bootstrap_state": "locked"},
                payload={
                    "principal_id": "owner-1",
                    "challenge": challenge,
                    "proof": "0" * 64,
                },
            )
            self.assertEqual(code_bad, 403)

            # Valid proof claims initial board_control and closes bootstrap permanently.
            proof = hashlib.sha256(
                f"{challenge}:{self.company_id}".encode("utf-8")
            ).hexdigest()
            code_ok, receipt = client.submit_command(
                company_id=self.company_id,
                command_id="owner-1-cmd",
                command_type=REGISTER_OWNER,
                contract_version=RBAC_COMMAND,
                preconditions={"bootstrap_state": "locked"},
                payload={
                    "principal_id": "owner-1",
                    "challenge": challenge,
                    "proof": proof,
                },
            )
            self.assertEqual(code_ok, 200)
            self.assertEqual(receipt["state"], "applied")

            meta_after = client.meta()
            self.assertFalse(meta_after["board_access"]["bootstrap_locked"])
            self.assertNotIn("bootstrap_challenge", meta_after["board_access"])
            self.assertEqual(meta_after["board_access"]["role"], "board_control")

            # Second bootstrap attempt fails closed 403.
            code_second, _ = client.submit_command(
                company_id=self.company_id,
                command_id="owner-2-cmd",
                command_type=REGISTER_OWNER,
                contract_version=RBAC_COMMAND,
                preconditions={"bootstrap_state": "locked"},
                payload={
                    "principal_id": "owner-2",
                    "challenge": challenge,
                    "proof": proof,
                },
            )
            self.assertEqual(code_second, 403)

        # Bind a board_viewer principal and verify writes fail closed 403 while reads work.
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as writer:
            bind_board_principal(
                writer, principal_id="viewer-1", role_name="board_viewer"
            )

        with self._daemon() as daemon:
            client = ControlPlaneClient(daemon.socket_path)
            viewer_meta = client.meta(principal="viewer-1")
            self.assertEqual(viewer_meta["board_access"]["role"], "board_viewer")

            code_denied, _ = client.submit_command(
                company_id=self.company_id,
                command_id="viewer-msg",
                command_type=SEND_CEO_MESSAGE,
                contract_version=CEO_CHAT_COMMAND,
                preconditions={
                    "ceo_agent_id": self.founder_id,
                    "charter_revision": "charter-v1",
                },
                payload={"target_agent_id": self.founder_id, "body": "should fail"},
                principal="viewer-1",
            )
            self.assertEqual(code_denied, 403)

    def test_ceo_direct_line_approvals_competing_stops_and_no_retire_employee(self):
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as writer:
            bind_board_principal(
                writer, principal_id="board-1", role_name="board_control"
            )
            upsert_agent_profile(
                writer,
                agent_id=self.founder_id,
                display_name="Founder · CEO",
                role_label="CEO",
                monthly_budget_minor=50000,
            )
            record_ceo_reply(writer, body="Ready for Q1 paper operations.")
            q_hash = record_decision_question(
                writer,
                question_id="q-promote-v4",
                kind="strategy_promotion",
                title="Promote Strategy v4",
                summary="Paired trial achieved PROMOTE.",
                impact={
                    "what_changes": [{"label": "Active strategy", "value": "v4"}],
                    "what_stays_untouched": [
                        {"label": "Environment", "value": "Paper"}
                    ],
                    "confirm_verb": "Promote Strategy v4",
                },
                created_by_agent_id=self.founder_id,
            )
            with closing(open_writer(self.store)) as db, write_tx(db):
                item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="research.review",
                    source_kind="manual",
                    source_id="src-1",
                    payload={"summary": "Review fee model"},
                )
                run = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    lease_owner="worker-1",
                    lease_ttl_ns=60_000_000_000,
                )

        with self._daemon() as daemon:
            client = ControlPlaneClient(daemon.socket_path)

            # 1. Non-CEO advisory message target MUST fail closed 403.
            code_non_ceo, _ = client.submit_command(
                company_id=self.company_id,
                command_id="msg-bad-target",
                command_type=SEND_CEO_MESSAGE,
                contract_version=CEO_CHAT_COMMAND,
                preconditions={
                    "ceo_agent_id": "not-the-ceo",
                    "charter_revision": "charter-v1",
                },
                payload={"target_agent_id": "not-the-ceo", "body": "bypass"},
                principal="board-1",
            )
            self.assertEqual(code_non_ceo, 403)

            # 2. CEO direct message succeeds, replays idempotently, rejects 409 conflict.
            code_msg, rec_msg = client.submit_command(
                company_id=self.company_id,
                command_id="msg-ceo-1",
                command_type=SEND_CEO_MESSAGE,
                contract_version=CEO_CHAT_COMMAND,
                preconditions={
                    "ceo_agent_id": self.founder_id,
                    "charter_revision": "charter-v1",
                },
                payload={
                    "target_agent_id": self.founder_id,
                    "body": "Please double-check v4 fee variance.",
                    "context_ref": {
                        "kind": "decision_question",
                        "id": "q-promote-v4",
                        "label": "Promote Strategy v4",
                    },
                },
                principal="board-1",
            )
            self.assertEqual(code_msg, 200)
            self.assertEqual(rec_msg["state"], "applied")

            code_replay, rec_replay = client.submit_command(
                company_id=self.company_id,
                command_id="msg-ceo-1",
                command_type=SEND_CEO_MESSAGE,
                contract_version=CEO_CHAT_COMMAND,
                preconditions={
                    "ceo_agent_id": self.founder_id,
                    "charter_revision": "charter-v1",
                },
                payload={
                    "target_agent_id": self.founder_id,
                    "body": "Please double-check v4 fee variance.",
                    "context_ref": {
                        "kind": "decision_question",
                        "id": "q-promote-v4",
                        "label": "Promote Strategy v4",
                    },
                },
                principal="board-1",
            )
            self.assertEqual(code_replay, 200)
            self.assertEqual(rec_replay["command_id"], rec_msg["command_id"])

            code_conflict, _ = client.submit_command(
                company_id=self.company_id,
                command_id="msg-ceo-1",
                command_type=SEND_CEO_MESSAGE,
                contract_version=CEO_CHAT_COMMAND,
                preconditions={
                    "ceo_agent_id": self.founder_id,
                    "charter_revision": "charter-v1",
                },
                payload={"target_agent_id": self.founder_id, "body": "mutated"},
                principal="board-1",
            )
            self.assertEqual(code_conflict, 409)

            # 3. Scoped receipt lookup rejects mismatched operation, omitted operation,
            #    or unknown/revoked principal with 403 fail-closed.
            with self.assertRaisesRegex(IpcRefused, "HTTP 403"):
                client.command_receipt(
                    "msg-ceo-1", operation=PAUSE_COMPANY, principal="board-1"
                )
            with self.assertRaisesRegex(IpcRefused, "HTTP 403"):
                client.command_receipt("msg-ceo-1", operation=None, principal="board-1")
            with self.assertRaisesRegex(IpcRefused, "HTTP 403"):
                client.command_receipt(
                    "msg-ceo-1", operation=SEND_CEO_MESSAGE, principal="unknown-actor"
                )

            # Precondition ceo_agent_id mismatch MUST fail closed 403.
            code_mismatch, _ = client.submit_command(
                company_id=self.company_id,
                command_id="msg-pre-mismatch",
                command_type=SEND_CEO_MESSAGE,
                contract_version=CEO_CHAT_COMMAND,
                preconditions={
                    "ceo_agent_id": "someone-else",
                    "charter_revision": "charter-v1",
                },
                payload={"target_agent_id": self.founder_id, "body": "mismatch"},
                principal="board-1",
            )
            self.assertEqual(code_mismatch, 403)

            # 4. retire_employee is excluded from Board commands (422).
            code_retire, _ = client.submit_command(
                company_id=self.company_id,
                command_id="retire-1",
                command_type="retire_employee",
                contract_version="hr-command-v1",
                preconditions={"charter_revision": "charter-v1"},
                payload={"agent_id": self.founder_id, "reason": "disallowed"},
                principal="board-1",
            )
            self.assertEqual(code_retire, 422)

            # 5. Board approval resolves cleanly and rejects stale follow-up with conflicting_revision.
            code_app, rec_app = client.submit_command(
                company_id=self.company_id,
                command_id="app-1",
                command_type=SUBMIT_DECISION_ANSWER,
                contract_version=DECISION_COMMAND,
                preconditions={
                    "question_id": "q-promote-v4",
                    "expected_revision": 1,
                    "expected_payload_hash": q_hash,
                },
                payload={"choice": "approve", "reason": "Metrics verified"},
                principal="board-1",
            )
            self.assertEqual(code_app, 200)
            self.assertEqual(rec_app["state"], "applied")

            code_stale, rec_stale = client.submit_command(
                company_id=self.company_id,
                command_id="app-stale",
                command_type=SUBMIT_DECISION_ANSWER,
                contract_version=DECISION_COMMAND,
                preconditions={
                    "question_id": "q-promote-v4",
                    "expected_revision": 1,
                    "expected_payload_hash": q_hash,
                },
                payload={"choice": "decline", "reason": "Too late"},
                principal="board-1",
            )
            self.assertEqual(code_stale, 200)
            self.assertEqual(rec_stale["state"], "denied")
            self.assertEqual(rec_stale["denial_code"], "conflicting_revision")

            # 6. Competing stops: clearing rest-p1 NEVER clears active rest-p2.
            for rid in ("rest-p1", "rest-p2"):
                code_p, rec_p = client.submit_command(
                    company_id=self.company_id,
                    command_id=f"cmd-{rid}",
                    command_type=PAUSE_COMPANY,
                    contract_version=RESTRICTION_COMMAND,
                    preconditions={
                        "charter_revision": "charter-v1",
                        "restriction_id": rid,
                        "expected_restriction_revision": 0,
                    },
                    payload={
                        "cause_category": "board_pause",
                        "scope_ref": "company:all",
                        "reason": f"Stop {rid}",
                    },
                    principal="board-1",
                )
                self.assertEqual(code_p, 200)
                self.assertEqual(rec_p["state"], "applied")

            code_clr, rec_clr = client.submit_command(
                company_id=self.company_id,
                command_id="cmd-clear-p1",
                command_type=RESUME_COMPANY,
                contract_version=RESTRICTION_COMMAND,
                preconditions={
                    "charter_revision": "charter-v1",
                    "restriction_id": "rest-p1",
                    "expected_restriction_revision": 1,
                },
                payload={"reason": "Clear P1 only"},
                principal="board-1",
            )
            self.assertEqual(code_clr, 200)
            self.assertEqual(rec_clr["state"], "applied")
            self.assertEqual(rec_clr["result"]["remaining_active_causes"], 1)

            status_r, _, rest_proj = client.projection("restrictions")
            self.assertEqual(status_r, 200)
            assert rest_proj is not None
            active_ids = [c["restriction_id"] for c in rest_proj["active_causes"]]
            self.assertEqual(active_ids, ["rest-p2"])

            # 7. Interrupt active work run via optimistic fencing_token + cut verified checkpoint.
            code_int, rec_int = client.submit_command(
                company_id=self.company_id,
                command_id="cmd-int-1",
                command_type=INTERRUPT_RUN,
                contract_version=WORK_COMMAND,
                preconditions={
                    "run_id": run.run_id,
                    "expected_fencing_token": run.fencing_token,
                },
                payload={"reason": "Stop task"},
                principal="board-1",
            )
            self.assertEqual(code_int, 200)
            self.assertEqual(rec_int["state"], "applied")

            code_ckpt, rec_ckpt = client.submit_command(
                company_id=self.company_id,
                command_id="cmd-ckpt-1",
                command_type=CUT_CHECKPOINT,
                contract_version=CHECKPOINT_COMMAND,
                preconditions={"charter_revision": "charter-v1"},
                payload={"reason": "Pre-release backup"},
                principal="board-1",
            )
            self.assertEqual(code_ckpt, 200)
            self.assertEqual(rec_ckpt["state"], "applied")
            self.assertTrue(
                (
                    self.root / "checkpoints" / rec_ckpt["result"]["archive_name"]
                ).is_file()
            )

    def test_record_decision_application_result_validates_statuses_and_allows_failed(
        self,
    ):
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as writer:
            record_decision_question(
                writer,
                question_id="q-status-check",
                kind="advisory",
                title="Check status validation",
                summary="Confirm",
                impact={"action": "advisory"},
                options=("approve",),
                created_by_agent_id=self.founder_id,
                now_ns=1_000,
            )
            with self.assertRaisesRegex(BoardControlRefused, "application_status"):
                record_decision_application_result(
                    writer,
                    question_id="q-status-check",
                    application_status="bogus",
                    delivery_status="delivered",
                    now_ns=2_000,
                )
            with self.assertRaisesRegex(BoardControlRefused, "delivery_status"):
                record_decision_application_result(
                    writer,
                    question_id="q-status-check",
                    application_status="failed",
                    delivery_status="bogus",
                    now_ns=2_000,
                )
            with self.assertRaisesRegex(BoardControlRefused, "effect_summary"):
                record_decision_application_result(
                    writer,
                    question_id="q-status-check",
                    application_status="failed",
                    delivery_status="delivered",
                    effect_summary=None,  # type: ignore[arg-type]
                    now_ns=2_000,
                )
            with self.assertRaisesRegex(BoardControlRefused, "effect_summary"):
                record_decision_application_result(
                    writer,
                    question_id="q-status-check",
                    application_status="failed",
                    delivery_status="delivered",
                    effect_summary=123,  # type: ignore[arg-type]
                    now_ns=2_000,
                )
            record_decision_application_result(
                writer,
                question_id="q-status-check",
                application_status="failed",
                delivery_status="delivered",
                effect_summary="",
                now_ns=1_500,
            )
            record_decision_application_result(
                writer,
                question_id="q-status-check",
                application_status="failed",
                delivery_status="delivered",
                effect_summary="action_refused: validation check",
                now_ns=2_000,
            )
        from sourdough.storage.db import open_reader, read_tx

        with closing(open_reader(self.store)) as db, read_tx(db):
            row = db.execute(
                "SELECT application_status, delivery_status FROM decision_question WHERE question_id = ?",
                ("q-status-check",),
            ).fetchone()
            self.assertEqual(row, ("failed", "delivered"))
