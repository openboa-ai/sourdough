"""Unit and integration tests for M3 Feature #14: Supervision, Bindings & Shift Recovery."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from typing import Any

from sourdough.agent_desk import ensure_private_desk, save_draft
from sourdough.agent_runtime import (
    PROVIDER_OPENAI,
    AgentProfileBinding,
    AgentProfileMismatch,
    AgentTransportInterrupted,
    InMemorySecretResolver,
    ShiftRequest,
    ShiftResult,
    TokenUsage,
    build_default_adapter_registry,
)
from sourdough.agent_runtime.protocol import AdapterManifest
from sourdough.company import FoundingTerms, found
from sourdough.duty_run import WorkRunClaim
from sourdough.runtime import (
    DUTY_WORK_DISCOVERY_REVIEW,
    CompanyRuntime,
    DutyOutcome,
    ResidentCompanyService,
    RunFenced,
    RuntimeRefused,
    build_default_registry,
    build_proactive_tool_handlers,
    build_production_duty_registry,
    build_store_profile_lookup,
    claim_work_run,
    complete_work_run,
    create_work_item,
    record_work_decision,
    yield_run_to_wait,
)
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.supervisor import SupervisionRefused, supervise_worker
from sourdough.writer_lease import hold_company_writer


class _FlakyAdapter:
    def __init__(self, *, fail_times: int) -> None:
        self._fail_times = fail_times
        self.calls = 0

    @property
    def manifest(self) -> AdapterManifest:
        return AdapterManifest(
            provider=PROVIDER_OPENAI,
            adapter_revision="v1",
            transport="https_json",
            supports_tool_mediation=True,
            supports_usage_accounting=True,
            supports_sandbox_desk=False,
        )

    def run_shift(
        self, profile: AgentProfileBinding, request: ShiftRequest, broker: Any
    ) -> ShiftResult:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise AgentTransportInterrupted("transient network blip")
        return ShiftResult(
            provider=PROVIDER_OPENAI,
            model=profile.model,
            run_id=request.context.run_id,
            turn_id=request.turn_id,
            output_text="recovered",
            usage=TokenUsage(10, 5),
            tool_calls_executed=0,
            events=(),
        )


class SupervisionRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.sock_root = Path(
            self.enterContext(TemporaryDirectory(prefix="sd-m3-", dir="/tmp"))
        )
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
                "m3-f14", "charter-v1", "manage-company", self.profile.profile_id()
            ),
        )
        self.company_id = self.receipt.company_id
        self.founder_id = self.receipt.founder_id

    def test_supervise_company_cli_cold_launch_and_run_company_stdout_mode(
        self,
    ) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "sourdough.cli",
                "supervise-company",
                str(self.store),
                "--company-id",
                self.company_id,
                "--socket-dir",
                str(self.sock_root),
                "--lease-dir",
                str(self.leases),
                "--tick-interval",
                "0.1",
                "--max-restarts",
                "0",
                "--startup-seconds",
                "10",
                "--max-seconds",
                "10",
                "--once",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = json.loads(proc.stdout)
        self.assertTrue(res["completed"])
        self.assertEqual(len(res["attempts"]), 1)
        self.assertIsNotNone(res["attempts"][0]["ready_at_ns"])

        # run-company without --ready-fd preserves DaemonInfo JSON on stdout
        child = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-m",
                "sourdough.cli",
                "run-company",
                str(self.store),
                "--company-id",
                self.company_id,
                "--socket-dir",
                str(self.sock_root),
                "--lease-dir",
                str(self.leases),
                "--tick-interval",
                "0.1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout is not None
            line = child.stdout.readline()
            info = json.loads(line)
            self.assertEqual(info["company_id"], self.company_id)
            self.assertIn("socket_path", info)
        finally:
            child.send_signal(signal.SIGTERM)
            child.wait(timeout=5)
            if child.stdout is not None:
                child.stdout.close()
            if child.stderr is not None:
                child.stderr.close()

        # Invalid --ready-fd < 3 fails closed before acquiring lease
        bad_fd = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "sourdough.cli",
                "run-company",
                str(self.store),
                "--company-id",
                self.company_id,
                "--ready-fd",
                "2",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(bad_fd.returncode, 1)

    def test_supervise_worker_rejects_hardlink_symlink_mismatch_and_exhaustion(
        self,
    ) -> None:
        alias = self.root / "alias.sqlite"
        os.link(self.store, alias)
        try:
            with self.assertRaises(SupervisionRefused):
                supervise_worker(
                    ["true"],
                    alias,
                    self.company_id,
                    max_restarts=0,
                    startup_seconds=5,
                    max_seconds=5,
                    emit=lambda _e: None,
                )
        finally:
            alias.unlink(missing_ok=True)

        symlink = self.root / "sym.sqlite"
        symlink.symlink_to(self.store)
        with self.assertRaises(SupervisionRefused):
            supervise_worker(
                ["true"],
                symlink,
                self.company_id,
                max_restarts=0,
                startup_seconds=5,
                max_seconds=5,
                emit=lambda _e: None,
            )

        st = self.store.lstat()
        with self.assertRaises(SupervisionRefused):
            supervise_worker(
                ["true"],
                self.store,
                self.company_id,
                max_restarts=0,
                startup_seconds=5,
                max_seconds=5,
                expected_store_key=(st.st_dev, st.st_ino + 1),
                emit=lambda _e: None,
            )

        events: list[dict] = []
        exhausted = supervise_worker(
            [sys.executable, "-c", "import sys; sys.exit(1)"],
            self.store,
            self.company_id,
            max_restarts=1,
            startup_seconds=2,
            max_seconds=6,
            emit=events.append,
        )
        self.assertFalse(exhausted.completed)
        self.assertEqual(len(exhausted.attempts), 2)
        self.assertEqual(events[-1], {"event": "supervision_exhausted", "attempts": 2})

    def test_resident_startup_sweep_reclaims_and_wakes_before_readiness_and_sigkill_recovery(
        self,
    ) -> None:
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                db.execute(
                    "INSERT INTO agent VALUES (?, ?, ?)",
                    ("worker-a", self.company_id, self.profile.profile_id()),
                )
                grant_id = "grant-worker-a"
                db.execute(
                    "INSERT INTO standing_grant VALUES (?, ?, ?)",
                    (grant_id, "worker-a", "desk:write"),
                )
                ensure_private_desk(
                    db, self.company_id, "worker-a", self.profile.profile_id()
                )
                db.execute(
                    "INSERT INTO agent VALUES (?, ?, ?)",
                    ("worker-b", self.company_id, self.profile.profile_id()),
                )
                db.execute(
                    "INSERT INTO standing_grant VALUES (?, ?, ?)",
                    ("grant-worker-b", "worker-b", "desk:write"),
                )
                # Expired running run for worker-a
                item_a, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id="worker-a",
                    duty_kind=DUTY_WORK_DISCOVERY_REVIEW,
                    source_kind="test",
                    source_id="exp-1",
                    now_ns=1_000,
                )
                stale_run = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=item_a.work_item_id,
                    lease_owner="dead-child",
                    lease_ttl_ns=1_000,
                    now_ns=1_000,
                )
                # Due waiting run for worker-b
                item_b, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id="worker-b",
                    duty_kind=DUTY_WORK_DISCOVERY_REVIEW,
                    source_kind="test",
                    source_id="wait-1",
                    now_ns=1_000,
                )
                wait_run = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=item_b.work_item_id,
                    lease_owner="old-child",
                    lease_ttl_ns=60_000_000_000,
                    now_ns=1_000,
                )
                yield_run_to_wait(
                    db,
                    wait_run.run_id,
                    wait_run.fencing_token,
                    wait_until_ns=2_000,
                    reason="cooldown",
                    now_ns=1_500,
                )

        # ResidentCompanyService.start() executes initial max_dispatches=0 sweep synchronously
        # before returning info (and before announcing readiness in run-company).
        seen: list[str] = []
        reg = build_default_registry(
            {
                DUTY_WORK_DISCOVERY_REVIEW: lambda ctx: (
                    seen.append(ctx.work_item.work_item_id),
                    DutyOutcome("completed", result={"ok": True}),
                )[1]
            }
        )
        # Keep scheduler idle by checking DB immediately after start() with long tick_interval
        # Wait: start() wakes scheduler thread immediately, so test via a fresh store with
        # blocking/no-op registry or assert post-start DB state directly for fenced/woken!
        with ResidentCompanyService(
            self.store,
            self.company_id,
            reg,
            socket_dir=self.sock_root,
            lease_directory=self.leases,
            tick_interval_seconds=10.0,
            enable_board_trigger_intake=False,
        ) as svc:
            svc.wait_for(lambda h: h.ticks_completed >= 1, timeout_seconds=5.0)
        with closing(open_reader(self.store)) as db, read_tx(db):
            stale_status = db.execute(
                "SELECT status FROM work_runs WHERE run_id = ?", (stale_run.run_id,)
            ).fetchone()[0]
            self.assertEqual(stale_status, "fenced")
            fenced_audits = db.execute(
                "SELECT COUNT(*) FROM audit_event WHERE company_id = ? AND event_type = 'work.run_fenced'",
                (self.company_id,),
            ).fetchone()[0]
            self.assertEqual(fenced_audits, 1)
            woken_audits = db.execute(
                "SELECT COUNT(*) FROM audit_event WHERE company_id = ? AND event_type = 'work.wait_woken'",
                (self.company_id,),
            ).fetchone()[0]
            self.assertEqual(woken_audits, 1)

        # Verify stale fencing_token=1 is refused across complete_work_run, record_work_decision, save_draft, set_condition
        stale_claim = WorkRunClaim(
            company_id=self.company_id,
            founder_id="worker-a",
            duty_id=item_a.work_item_id,
            run_id=stale_run.run_id,
            fencing_token=1,
            grant_id=grant_id,
            grant_scope="desk:write",
            purpose=DUTY_WORK_DISCOVERY_REVIEW,
        )
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                with self.assertRaises(RunFenced):
                    complete_work_run(
                        db,
                        stale_run.run_id,
                        1,
                        outcome={"stale": True},
                        now_ns=time_ns(),
                    )
                with self.assertRaises(RunFenced):
                    record_work_decision(
                        db,
                        company_id=self.company_id,
                        work_item_id=item_a.work_item_id,
                        run_id=stale_run.run_id,
                        fencing_token=1,
                        agent_id="worker-a",
                        action_kind="hold",
                        rationale="stale",
                        decision_at_ns=time_ns(),
                    )
            with self.assertRaises(RunFenced):
                save_draft(
                    w,
                    stale_claim,
                    stale_claim.purpose,
                    name="s",
                    content="x",
                    expected_revision=None,
                )
            with self.assertRaises(RunFenced):
                build_proactive_tool_handlers(w, stale_claim)["set_condition"](
                    {"trigger_kind": "timer", "due_at_ns": 99_999}
                )

        # SIGKILL on attempt 1 -> supervisor relaunches run-company --once cleanly on same inode
        marker = self.root / "killed.once"
        worker_script = self.root / "kill_worker.py"
        worker_script.write_text(
            f"""#!{sys.executable}
import os, signal
from pathlib import Path
f = Path({str(marker)!r})
if not f.exists():
    f.write_text("1")
    os.kill(os.getpid(), signal.SIGKILL)
from sourdough.cli import main
raise SystemExit(main())
""",
            encoding="utf-8",
        )
        worker_script.chmod(0o700)
        sup = supervise_worker(
            [
                sys.executable,
                "-IB",
                str(worker_script),
                "run-company",
                str(self.store),
                "--company-id",
                self.company_id,
                "--socket-dir",
                str(self.sock_root),
                "--lease-dir",
                str(self.leases),
                "--tick-interval",
                "0.1",
                "--once",
            ],
            self.store,
            self.company_id,
            max_restarts=1,
            startup_seconds=10,
            max_seconds=20,
            emit=lambda _e: None,
        )
        self.assertTrue(sup.completed)
        self.assertEqual(len(sup.attempts), 2)
        self.assertEqual(sup.attempts[0].returncode, -signal.SIGKILL)
        self.assertIsNone(sup.attempts[0].ready_at_ns)
        self.assertIsNotNone(sup.attempts[1].ready_at_ns)

    def test_store_profile_lookup_db_binding_overrides_and_tamper_blocks(self) -> None:
        alt_profile = AgentProfileBinding(
            provider=PROVIDER_OPENAI,
            model="gpt-5",
            revision="v2",
            credential_handle_ref="envref:SD_ALT_KEY",
        )
        lookup = build_store_profile_lookup(self.store, self.company_id)
        with self.assertRaises(RuntimeRefused):
            lookup("missing-agent")

        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                db.execute(
                    "INSERT INTO agent VALUES (?, ?, ?)",
                    ("bound-a", self.company_id, self.profile.profile_id()),
                )
                db.execute(
                    "INSERT INTO standing_grant VALUES (?, ?, ?)",
                    ("grant-bound-a", "bound-a", "desk:write"),
                )
                db.execute(
                    """INSERT INTO agent_runtime_binding (
                           agent_id, company_id, provider, model, revision,
                           sandbox_backend, credential_handle_ref, profile_id,
                           created_at_ns, updated_at_ns
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "bound-a",
                        self.company_id,
                        self.profile.provider,
                        self.profile.model,
                        self.profile.revision,
                        self.profile.sandbox_backend,
                        self.profile.credential_handle_ref,
                        self.profile.profile_id(),
                        1_000,
                        1_000,
                    ),
                )
        resolved = lookup("bound-a")
        self.assertEqual(resolved.profile_id(), self.profile.profile_id())

        overridden = build_store_profile_lookup(
            self.store, self.company_id, overrides={"bound-a": alt_profile}
        )
        self.assertEqual(overridden("bound-a").model, "gpt-5")

        # Tamper persisted profile_id -> AgentProfileMismatch
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                db.execute(
                    "UPDATE agent_runtime_binding SET profile_id = 'tampered' WHERE agent_id = 'bound-a'"
                )
        with self.assertRaises(AgentProfileMismatch):
            lookup("bound-a")

    def test_transport_interrupted_exponential_backoff_and_blocked_transition(
        self,
    ) -> None:
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                db.execute(
                    "INSERT INTO agent VALUES (?, ?, ?)",
                    ("flaky-a", self.company_id, self.profile.profile_id()),
                )
                db.execute(
                    "INSERT INTO standing_grant VALUES (?, ?, ?)",
                    ("grant-flaky-a", "flaky-a", "desk:write"),
                )
                item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id="flaky-a",
                    duty_kind=DUTY_WORK_DISCOVERY_REVIEW,
                    source_kind="test",
                    source_id="flaky-1",
                    now_ns=1_000,
                )

        adapter_reg = build_default_adapter_registry(
            secret_resolver=InMemorySecretResolver({"envref:SD_TEST_KEY": "sk-test"})
        )
        adapter_reg.register(_FlakyAdapter(fail_times=5))
        reg = build_production_duty_registry(
            adapter_registry=adapter_reg,
            profile_bindings={self.founder_id: self.profile, "flaky-a": self.profile},
        )
        rt = CompanyRuntime(
            self.store,
            self.company_id,
            reg,
            lease_directory=self.leases,
            max_transient_attempts=3,
            retry_base_backoff_ns=1_000,
            retry_max_backoff_ns=10_000,
        )
        # Attempt 1 fails at 2_000 -> next_retry_at_ns = 3_000
        with self.assertRaises(AgentTransportInterrupted):
            rt.step(max_dispatches=1, now_ns=2_000)
        # Skipped at 2_500 before backoff elapses
        s_skip = rt.step(max_dispatches=1, now_ns=2_500)
        self.assertEqual(s_skip.executed_runs, ())
        # Attempt 2 fails at 3_000 -> next_retry_at_ns = 5_000
        with self.assertRaises(AgentTransportInterrupted):
            rt.step(max_dispatches=1, now_ns=3_000)
        # Attempt 3 fails at 5_000 -> hits max_transient_attempts=3 -> blocked
        with self.assertRaises(AgentTransportInterrupted):
            rt.step(max_dispatches=1, now_ns=5_000)
        with closing(open_reader(self.store)) as db, read_tx(db):
            row = db.execute(
                "SELECT state, attempt_count FROM work_items WHERE work_item_id = ?",
                (item.work_item_id,),
            ).fetchone()
            self.assertEqual(row, ("blocked", 3))

    def test_run_company_rejects_malformed_profiles_json_cleanly(self) -> None:
        for idx, raw_text in enumerate(
            (
                "{not-valid-json",
                json.dumps(["not-an-object"]),
                json.dumps({"agent-a": {"unknown_key": 1}}),
            )
        ):
            bad_path = self.root / f"bad_profiles_{idx}.json"
            bad_path.write_text(raw_text, encoding="utf-8")
            proc = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "sourdough.cli",
                    "run-company",
                    str(self.store),
                    "--company-id",
                    self.company_id,
                    "--profiles-json",
                    str(bad_path),
                    "--once",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("Company command refused:", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main()
