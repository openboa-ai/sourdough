"""Unit tests for Pillar 1 Agent Desk 3-tier layout, limits, WorkRunClaim, sandbox exec, and Checkpoint v2."""

from __future__ import annotations

import os
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from zipfile import ZipFile

from sourdough.agent_desk import (
    LEGACY_DESK_LIMITS,
    PRODUCTION_DESK_LIMITS,
    DeskLimits,
    allocate_run_scratch,
    cleanup_run_scratch,
    clone_desk_snapshot,
    compute_agent_desk_digest,
    delete_draft,
    desk_directory,
    logs_directory,
    resolve_desk,
    save_draft,
)
from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    bind_board_principal,
    execute_cut_checkpoint_command,
)
from sourdough.company import FoundingTerms, _connect, found
from sourdough.company_assets import (
    Selection,
    assign_project,
    discover_project_references,
    publish_selection,
    read_shared_asset,
)
from sourdough.company_checkpoint import checkpoint_company, inspect_company_checkpoint
from sourdough.company_restore import restore_core_checkpoint
from sourdough.duty_run import DutyRefused, WorkRunClaim, resolve_run
from sourdough.harness import (
    DeskCommandLimits,
    ScopedRun,
    ScopedWorkflow,
    create_agent_session,
    run_desk_command_sandboxed,
    tool,
)
from sourdough.runtime.work_kernel import (
    RunFenced,
    claim_work_run,
    complete_work_run,
    create_work_item,
    register_responsibility,
)
from sourdough.storage.checkpoint import CheckpointRefused, checkpoint_store
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.writer_lease import hold_company_writer


class AgentDeskAndCheckpointV2Tests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="sd-desk-")))
        self.store = self.root / "company.sqlite"
        self.leases = self.root / "leases"
        self.founding = found(
            self.store,
            FoundingTerms("setup-desk", "charter-v1", "manage-company", "profile-v1"),
        )
        self.company_id = self.founding.company_id
        self.founder_id = self.founding.founder_id
        self.writer = self.enterContext(
            hold_company_writer(
                self.store, self.company_id, lease_directory=self.leases
            )
        )

    def _start_work_run_claim(
        self,
        agent_id: str,
        grant_id: str,
        grant_scope: str = "manage-company",
        *,
        work_item_id: str | None = None,
        now_ns: int | None = None,
    ) -> WorkRunClaim:
        ts = time_ns() if now_ns is None else now_ns
        with closing(open_writer(self.store, lease=self.writer)) as db, write_tx(db):
            item, _ = create_work_item(
                db,
                company_id=self.company_id,
                owner_agent_id=agent_id,
                duty_kind="work.assessment_review",
                source_kind="manual",
                source_id=work_item_id or f"src-{agent_id}-{ts}",
                work_item_id=work_item_id,
                now_ns=ts,
            )
            run = claim_work_run(
                db,
                company_id=self.company_id,
                work_item_id=item.work_item_id,
                lease_owner="worker-1",
                lease_ttl_ns=60_000_000_000,
                now_ns=ts,
            )
        return WorkRunClaim(
            company_id=self.company_id,
            founder_id=agent_id,
            duty_id=item.work_item_id,
            run_id=run.run_id,
            fencing_token=run.fencing_token,
            grant_id=grant_id,
            grant_scope=grant_scope,
            purpose="work.assessment_review",
        )

    def test_founding_initializes_founder_private_desk_idempotently(self):
        with closing(open_reader(self.store)) as reader, read_tx(reader):
            rows = reader.execute(
                "SELECT agent_id FROM private_desk WHERE agent_id = ?",
                (self.founder_id,),
            ).fetchall()
        self.assertEqual(rows, [(self.founder_id,)])
        # Replay found() remains idempotent
        replay = found(
            self.store,
            FoundingTerms("setup-desk", "charter-v1", "manage-company", "profile-v1"),
        )
        self.assertEqual(replay, self.founding)

    def test_three_tier_desk_limits_delete_and_work_run_claim_bridge(self):
        claim = self._start_work_run_claim(
            self.founder_id, self.founding.grant_id, "manage-company"
        )
        # Legacy limits reject > 800 bytes; production limits accept up to 64 KB
        large_content = "x" * 2048
        with self.assertRaisesRegex(DutyRefused, "Invalid private draft"):
            save_draft(
                self.writer,
                claim,
                claim.purpose,
                name="large-note",
                content=large_content,
                expected_revision=None,
                limits=LEGACY_DESK_LIMITS,
            )
        saved = save_draft(
            self.writer,
            claim,
            claim.purpose,
            name="large-note",
            content=large_content,
            expected_revision=None,
            limits=PRODUCTION_DESK_LIMITS,
        )
        self.assertEqual(saved.content, large_content)

        # Total byte budget cap enforced cleanly
        tight_limits = DeskLimits(
            max_drafts=10,
            max_content_bytes=4096,
            max_total_bytes=2500,
            max_scratch_bytes=4096,
        )
        with self.assertRaisesRegex(DutyRefused, "total draft capacity"):
            save_draft(
                self.writer,
                claim,
                claim.purpose,
                name="second-note",
                content="y" * 1000,
                expected_revision=None,
                limits=tight_limits,
            )

        # Scratch allocation & cleanup + logs directory isolation
        scratch = allocate_run_scratch(
            self.store, self.founder_id, claim.run_id, limits=PRODUCTION_DESK_LIMITS
        )
        self.assertTrue(scratch.is_dir())
        (scratch / "temp.txt").write_text("scratch-data", encoding="utf-8")
        logs = logs_directory(self.store, self.founder_id)
        logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        (logs / "0001-tool.json").write_text("{}", encoding="utf-8")

        digest_before = compute_agent_desk_digest(self.store, self.founder_id)
        cleanup_run_scratch(self.store, self.founder_id, claim.run_id)
        self.assertFalse(scratch.exists())
        self.assertEqual(
            compute_agent_desk_digest(self.store, self.founder_id), digest_before
        )

        # WorkRunClaim works end-to-end with publish_selection (>800 bytes!), discover, read, ScopedRun
        rev = assign_project(
            self.writer,
            self.founding.grant_id,
            "proj-m1",
            (self.founder_id,),
            publishers=(self.founder_id,),
        )
        asset = publish_selection(
            self.writer,
            claim,
            claim.purpose,
            Selection(
                "req-large-1",
                saved.name,
                saved.revision,
                "proj-m1",
                rev,
                (self.founder_id,),
                0,
                1500,
            ),
        )
        self.assertEqual(len(asset.content), 1500)
        refs, receipts, _cursor = discover_project_references(
            self.writer, claim, claim.purpose, ""
        )
        self.assertEqual([r.asset_id for r in refs], [asset.asset_id])
        self.assertEqual([rc.request_id for rc in receipts], ["req-large-1"])
        self.assertEqual(
            read_shared_asset(
                self.writer, claim, claim.purpose, asset.asset_id
            ).content,
            large_content[:1500],
        )

        # ScopedRun with WorkRunClaim
        scoped = ScopedRun(
            self.writer,
            claim,
            ScopedWorkflow(
                claim.purpose,
                "Inspect draft.",
                (tool("ping", "Ping tool.", {}),),
                "ping",
            ),
            lambda name, _args: {"ok": True, "tool": name},
        )
        self.assertEqual(scoped.call("ping", {}), {"ok": True, "tool": "ping"})

        # Delete draft enforces revision & updates digest
        with self.assertRaisesRegex(DutyRefused, "revision changed"):
            delete_draft(
                self.writer,
                claim,
                claim.purpose,
                name="large-note",
                expected_revision="wrong-rev",
            )
        new_digest = delete_draft(
            self.writer,
            claim,
            claim.purpose,
            name="large-note",
            expected_revision=saved.revision,
        )
        self.assertNotEqual(new_digest, digest_before)
        _rev, drafts = resolve_desk(
            self.writer, claim, claim.purpose, include_drafts=True
        )
        self.assertEqual(drafts, ())

        # Fencing the run blocks WorkRunClaim operations
        with closing(open_writer(self.store, lease=self.writer)) as db, write_tx(db):
            complete_work_run(
                db,
                claim.run_id,
                claim.fencing_token,
                outcome={"disposition": "completed"},
                now_ns=20_000,
            )
        with closing(_connect(self.store, "ro")) as db:
            with self.assertRaises(RunFenced):
                resolve_run(db, claim)

    def test_discover_project_references_filters_by_company_id(self):
        claim = self._start_work_run_claim(
            self.founder_id, self.founding.grant_id, "manage-company"
        )
        # Insert a foreign company and shared_project directly in SQLite to verify isolation
        with closing(_connect(self.store, "rw")) as db:
            db.execute("PRAGMA foreign_keys = OFF")
            with db:
                db.execute(
                    "INSERT INTO shared_project VALUES ('foreign-proj', 'other-co', 'rev-1', ?, ?)",
                    (f'["{self.founder_id}"]', f'["{self.founder_id}"]'),
                )
                db.execute(
                    """INSERT INTO shared_publication (
                           asset_id, company_id, project_id, author_id, work_item_id,
                           request_id, request_digest, audience, content
                       ) VALUES ('foreign-asset', 'other-co', 'foreign-proj', ?, ?, 'r-foreign', 'd', ?, 'secret')""",
                    (self.founder_id, claim.duty_id, f'["{self.founder_id}"]'),
                )
        refs, receipts, _ = discover_project_references(
            self.writer, claim, claim.purpose, ""
        )
        self.assertEqual(refs, ())
        self.assertEqual(receipts, ())

    def test_clone_desk_snapshot_excludes_scratch_logs_and_revoked_agents(self):
        with closing(_connect(self.store, "rw")) as db, db:
            db.execute(
                "INSERT INTO agent VALUES ('active-2', ?, 'profile-v1')",
                (self.company_id,),
            )
            db.execute(
                "INSERT INTO standing_grant VALUES ('grant-2', 'active-2', 'assessment')"
            )
            db.execute("INSERT INTO private_desk VALUES ('active-2', 'rev-active-2')")
            db.execute(
                "INSERT INTO agent VALUES ('revoked-3', ?, 'profile-v1')",
                (self.company_id,),
            )
            db.execute(
                "INSERT INTO standing_grant VALUES ('grant-3', 'revoked-3', 'revoked')"
            )
            db.execute("INSERT INTO private_desk VALUES ('revoked-3', 'rev-revoked-3')")

        c1 = self._start_work_run_claim(
            self.founder_id, self.founding.grant_id, "manage-company"
        )
        c2 = self._start_work_run_claim("active-2", "grant-2", "assessment")
        save_draft(
            self.writer,
            c1,
            c1.purpose,
            name="plan",
            content="founder-plan",
            expected_revision=None,
            limits=PRODUCTION_DESK_LIMITS,
        )
        save_draft(
            self.writer,
            c2,
            c2.purpose,
            name="analysis",
            content="agent2-analysis",
            expected_revision=None,
            limits=PRODUCTION_DESK_LIMITS,
        )
        scratch = allocate_run_scratch(self.store, self.founder_id, c1.run_id)
        (scratch / "ephemeral.tmp").write_text("do-not-clone", encoding="utf-8")
        logs = logs_directory(self.store, self.founder_id)
        logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        (logs / "0001-tool.json").write_text("{}", encoding="utf-8")

        # Create an orphan draft directory for revoked-3 to confirm it is NOT cloned
        revoked_drafts = desk_directory(self.store, "revoked-3") / ".desk" / "drafts"
        revoked_drafts.mkdir(mode=0o700, parents=True)
        (revoked_drafts / "secret.json").write_text("{}", encoding="utf-8")

        target_store = self.root / "arm-a" / "company.sqlite"
        target_store.parent.mkdir(mode=0o700)
        with self.assertRaisesRegex(DutyRefused, "revision mismatch"):
            clone_desk_snapshot(
                self.store,
                self.company_id,
                [self.founder_id],
                target_store,
                expected_revisions={self.founder_id: "stale-rev"},
            )

        cloned = clone_desk_snapshot(
            self.store,
            self.company_id,
            [self.founder_id, "active-2", "revoked-3"],
            target_store,
        )
        self.assertEqual(set(cloned.keys()), {self.founder_id, "active-2"})
        self.assertFalse(desk_directory(target_store, "revoked-3").exists())
        self.assertTrue(
            (
                desk_directory(target_store, self.founder_id)
                / ".desk"
                / "drafts"
                / "plan.json"
            ).is_file()
        )
        self.assertEqual(
            list(
                (
                    desk_directory(target_store, self.founder_id) / ".desk" / "scratch"
                ).iterdir()
            ),
            [],
        )
        self.assertEqual(
            list(
                (
                    desk_directory(target_store, self.founder_id) / ".desk" / "logs"
                ).iterdir()
            ),
            [],
        )

    def test_checkpoint_v2_wires_desk_root_excludes_ephemeral_and_rejects_hardlinks(
        self,
    ):
        subroot = self.root / "ckpt-v2"
        subroot.mkdir(mode=0o700)
        sub_store = subroot / "company.sqlite"
        sub_leases = subroot / "leases"
        sub_found = found(
            sub_store,
            FoundingTerms("setup-ckpt", "charter-v1", "manage-company", "profile-v1"),
        )
        with hold_company_writer(
            sub_store, sub_found.company_id, lease_directory=sub_leases
        ) as sub_writer:
            with (
                closing(open_writer(sub_store, lease=sub_writer)) as db,
                write_tx(db),
            ):
                item, _ = create_work_item(
                    db,
                    company_id=sub_found.company_id,
                    owner_agent_id=sub_found.founder_id,
                    duty_kind="work.assessment_review",
                    source_kind="manual",
                    source_id="src-ckpt",
                )
                run = claim_work_run(
                    db,
                    company_id=sub_found.company_id,
                    work_item_id=item.work_item_id,
                    lease_owner="worker-1",
                    lease_ttl_ns=60_000_000_000,
                )
            claim = WorkRunClaim(
                company_id=sub_found.company_id,
                founder_id=sub_found.founder_id,
                duty_id=item.work_item_id,
                run_id=run.run_id,
                fencing_token=run.fencing_token,
                grant_id=sub_found.grant_id,
                grant_scope="manage-company",
                purpose="work.assessment_review",
            )
            save_draft(
                sub_writer,
                claim,
                claim.purpose,
                name="strategy",
                content="checkpointed-draft",
                expected_revision=None,
                limits=PRODUCTION_DESK_LIMITS,
            )
            expected_digest = compute_agent_desk_digest(sub_store, sub_found.founder_id)
            scratch = allocate_run_scratch(
                sub_store, sub_found.founder_id, claim.run_id
            )
            (scratch / "temp.bin").write_text("ephemeral", encoding="utf-8")
            logs = logs_directory(sub_store, sub_found.founder_id)
            logs.mkdir(mode=0o700, parents=True, exist_ok=True)
            (logs / "0001.json").write_text("log", encoding="utf-8")
            mounts = (
                desk_directory(sub_store, sub_found.founder_id) / ".desk" / "mounts"
            )
            mounts.mkdir(mode=0o700, parents=True, exist_ok=True)
            (mounts / "stale.json").write_text("mount", encoding="utf-8")
            (
                desk_directory(sub_store, sub_found.founder_id)
                / ".desk"
                / "drafts"
                / ".tmp-partial.json"
            ).write_text("partial", encoding="utf-8")

            # Board control CUT_CHECKPOINT uses held writer + desks_root without WriterLease collision
            bind_board_principal(
                sub_writer, principal_id="board-1", role_name=BOARD_CONTROL_ROLE
            )
            board_res = execute_cut_checkpoint_command(
                sub_writer,
                command_id="cmd-ckpt-1",
                charter_revision="charter-v1",
                reason="M1 checkpoint verification",
                principal_hint="board-1",
            )
            self.assertEqual(board_res.state, "applied")
            ckpt_files = list((subroot / "checkpoints").glob("checkpoint-*.zip"))
            self.assertEqual(len(ckpt_files), 1)
            board_manifest = inspect_company_checkpoint(
                ckpt_files[0], sub_found.company_id
            )
            self.assertEqual(
                board_manifest.agent_desk_digests,
                {sub_found.founder_id: expected_digest},
            )
            self.assertTrue(
                all(
                    ".desk/scratch/" not in p
                    and ".desk/logs/" not in p
                    and ".desk/mounts/" not in p
                    and ".tmp-" not in p
                    for p in board_manifest.desk_files
                )
            )

        # Writer released: test CLI-facing checkpoint_company + restore_core_checkpoint
        archive = subroot / "company-v2.zip"
        manifest = checkpoint_company(sub_store, sub_found.company_id, archive)
        self.assertEqual(
            manifest.agent_desk_digests, {sub_found.founder_id: expected_digest}
        )
        with ZipFile(archive, "r") as zf:
            names = zf.namelist()
            self.assertIn(
                f"desk/{sub_found.founder_id}/.desk/drafts/strategy.json", names
            )
            self.assertFalse(
                any("scratch" in n or "logs" in n or "mounts" in n for n in names)
            )

        restored_db = subroot / "restored" / "company.sqlite"
        restored_db.parent.mkdir(mode=0o700)
        restored_desks = subroot / "restored" / "desks"
        restore_core_checkpoint(
            sub_store,
            sub_found.company_id,
            archive,
            restored_db,
            desk_destination=restored_desks,
        )
        self.assertTrue(
            (
                restored_desks
                / sub_found.founder_id
                / ".desk"
                / "drafts"
                / "strategy.json"
            ).is_file()
        )

        # Hard-link inside Desk must be rejected; symlink named scratch must be rejected!
        with hold_company_writer(
            restored_db, sub_found.company_id, lease_directory=sub_leases
        ) as rw:
            hardlink_target = (
                restored_desks
                / sub_found.founder_id
                / ".desk"
                / "drafts"
                / "strategy.json"
            )
            alias = (
                restored_desks
                / sub_found.founder_id
                / ".desk"
                / "drafts"
                / "alias.json"
            )
            os.link(hardlink_target, alias)
            with self.assertRaisesRegex(CheckpointRefused, "Hard-link"):
                checkpoint_store(
                    restored_db,
                    sub_found.company_id,
                    subroot / "bad-hardlink.zip",
                    desk_root=restored_desks,
                    writer=rw,
                )
            alias.unlink()
            # Symlink named scratch must still fail closed!
            sym_scratch = restored_desks / sub_found.founder_id / ".desk" / "scratch"
            if sym_scratch.exists():
                sym_scratch.rmdir()
            os.symlink(subroot, sym_scratch)
            with self.assertRaisesRegex(CheckpointRefused, "Symlink"):
                checkpoint_store(
                    restored_db,
                    sub_found.company_id,
                    subroot / "bad-symlink.zip",
                    desk_root=restored_desks,
                    writer=rw,
                )

    def test_desk_run_command_sandboxed_gating_isolation_and_bounds(self):
        claim = self._start_work_run_claim(
            self.founder_id, self.founding.grant_id, "manage-company"
        )
        session = create_agent_session(self.writer, claim, claim.purpose)

        # Fail-closed without desk:exec grant
        with self.assertRaisesRegex(DutyRefused, "desk:exec grant"):
            run_desk_command_sandboxed(
                session,
                [sys.executable, "-c", "print('hi')"],
                store=self.store,
                sandbox_backend="local-macos",
            )

        # Fail-closed when sandbox_backend != local-macos even with desk:exec
        with self.assertRaisesRegex(DutyRefused, "local-macos"):
            run_desk_command_sandboxed(
                session,
                [sys.executable, "-c", "print('hi')"],
                store=self.store,
                sandbox_backend="docker",
                granted_capabilities=("desk:exec",),
            )

        # Authorize via work_responsibilities readable_scope_json["grants"] = ["desk:exec"]
        with closing(open_writer(self.store, lease=self.writer)) as db, write_tx(db):
            register_responsibility(
                db,
                company_id=self.company_id,
                owner_agent_id=self.founder_id,
                duty_kind="work.assessment_review",
                remit="Run local checks",
                readable_scope={"grants": ["desk:exec"]},
            )

        ok_res = run_desk_command_sandboxed(
            session,
            [sys.executable, "-c", "print('hello-sandbox')"],
            store=self.store,
            sandbox_backend="local-macos",
        )
        self.assertTrue(ok_res["ok"], ok_res)
        self.assertEqual(ok_res["returncode"], 0)
        self.assertEqual(ok_res["stdout"].strip(), "hello-sandbox")
        self.assertFalse(ok_res["stdout_truncated"])

        # Cannot read company.sqlite from inside sandboxed command
        escape_res = run_desk_command_sandboxed(
            session,
            [
                sys.executable,
                "-c",
                f"import pathlib; print(pathlib.Path({str(self.store)!r}).read_bytes())",
            ],
            store=self.store,
            sandbox_backend="local-macos",
        )
        self.assertFalse(escape_res["ok"])
        self.assertNotEqual(escape_res["returncode"], 0)

        # Output truncation & timeout enforcement
        cap_res = run_desk_command_sandboxed(
            session,
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('A' * 500); sys.stderr.write('B' * 300)",
            ],
            store=self.store,
            sandbox_backend="local-macos",
            limits=DeskCommandLimits(
                timeout_seconds=5.0,
                max_stdout_bytes=64,
                max_stderr_bytes=32,
            ),
        )
        self.assertTrue(cap_res["ok"])
        self.assertTrue(cap_res["stdout_truncated"])
        self.assertTrue(cap_res["stderr_truncated"])
        self.assertEqual(len(cap_res["stdout"].encode("utf-8")), 64)
        self.assertEqual(len(cap_res["stderr"].encode("utf-8")), 32)

        # Verify RLIMIT_FSIZE stops oversized file writes inside scratch
        fsize_res = run_desk_command_sandboxed(
            session,
            [
                sys.executable,
                "-c",
                "import os, pathlib; p = pathlib.Path(os.environ['TMPDIR']) / 'big.bin'; p.write_bytes(b'x' * 200000)",
            ],
            store=self.store,
            sandbox_backend="local-macos",
            limits=DeskCommandLimits(max_file_bytes=4096),
        )
        self.assertFalse(fsize_res["ok"])
        self.assertNotEqual(fsize_res["returncode"], 0)

        # Verify timeout kills runaway command cleanly
        to_res = run_desk_command_sandboxed(
            session,
            [sys.executable, "-c", "import time; time.sleep(5)"],
            store=self.store,
            sandbox_backend="local-macos",
            limits=DeskCommandLimits(timeout_seconds=0.3),
        )
        self.assertFalse(to_res["ok"])
        self.assertTrue(to_res["timed_out"])

    def test_desk_command_limits_and_tool_reject_non_finite_or_bool_timeout(self):
        import json as _json
        from sourdough.agent_runtime.protocol import OuterToolBroker
        from sourdough.runtime.desk_tools import build_core_desk_tool_contribution

        for bad_limits_kwargs in (
            {"timeout_seconds": float("nan")},
            {"timeout_seconds": float("inf")},
            {"timeout_seconds": float("-inf")},
            {"timeout_seconds": True},
            {"timeout_seconds": 0},
            {"timeout_seconds": -1.0},
            {"cpu_seconds": True},
            {"memory_bytes": False},
            {"max_processes": True},
        ):
            with self.assertRaises(DutyRefused):
                DeskCommandLimits(**bad_limits_kwargs)

        claim = self._start_work_run_claim(
            self.founder_id, self.founding.grant_id, "manage-company"
        )
        with closing(open_writer(self.store, lease=self.writer)) as db, write_tx(db):
            register_responsibility(
                db,
                company_id=self.company_id,
                owner_agent_id=self.founder_id,
                duty_kind="work.exec_nan",
                remit="Verify timeout bounds",
                readable_scope={"grants": ["desk:exec"]},
            )
        contrib = build_core_desk_tool_contribution(
            writer=self.writer,
            claim=claim,
            purpose=claim.purpose,
            sandbox_backend="local-macos",
            command_limits=DeskCommandLimits(timeout_seconds=0.5),
        )
        broker = OuterToolBroker(
            company_id=self.company_id,
            agent_id=self.founder_id,
            run_id=claim.run_id,
            fencing_token=claim.fencing_token,
            handlers=dict(contrib.handlers),
        )
        for idx, bad_timeout in enumerate(
            (
                float("nan"),
                float("inf"),
                float("-inf"),
                True,
                0,
                -0.5,
                10**400,
            )
        ):
            res = broker.invoke(
                f"bad-to-{idx}",
                "desk_run_command",
                {
                    "command": [sys.executable, "-c", "print('should-not-run')"],
                    "timeout_seconds": bad_timeout,
                },
            )
            self.assertEqual(res["ok"], False)
            self.assertEqual(res["error_type"], "DutyRefused")

        # Also test NaN parsed from JSON text just as model JSON payloads arrive.
        parsed = _json.loads('{"command": ["echo", "hi"], "timeout_seconds": NaN}')
        nan_res = broker.invoke("bad-to-json", "desk_run_command", parsed)
        self.assertEqual(nan_res["ok"], False)
        self.assertEqual(nan_res["error_type"], "DutyRefused")


if __name__ == "__main__":
    unittest.main()
