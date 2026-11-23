"""Connected Wave A gate: unified SQLite kernel, WAL restore, 3-entity Work, and runtime parity."""

import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found, upgrade_work_schema
from sourdough.paper_execution import VenueRefused
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.runtime.engine import CompanyRuntime, build_default_registry
from sourdough.runtime.registry import DutyContext, DutyOutcome
from sourdough.runtime.venue_coordinator import (
    BoundedVenueCoordinator,
    VenueSegmentPolicy,
)
from sourdough.runtime.work_kernel import (
    RunFenced,
    assert_run_fencing,
    claim_work_run,
    create_work_item,
    open_peer_request,
    register_responsibility,
    replace_condition,
    resolve_peer_request,
    set_condition,
)
from sourdough.storage.checkpoint import checkpoint_store, restore_store
from sourdough.storage.db import (
    MAX_SCHEMA_VERSION,
    StorageRefused,
    open_reader,
    open_writer,
    read_tx,
    verify_no_wal_sidecars,
    wal_checkpoint,
    write_tx,
)
from sourdough.storage.kernel import (
    CommandUnauthorized,
    admit_command_receipt,
    append_audit_event,
    read_command_receipt,
)
from sourdough.storage.migrations import load_migrations, verify_applied_migrations


def _cli(*args: object) -> dict:
    run = subprocess.run(
        [sys.executable, "-I", "-m", "sourdough.cli", *(str(a) for a in args)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(run.stdout)


def _scripted_peer(directory: Path) -> Path:
    peer = directory / "scripted-peer"
    peer.write_text(
        f"#!{sys.executable}\n"
        "import json, socket, sys\n"
        "request = json.load(sys.stdin)\n"
        "op = request['operation']\n"
        "if op == 'identify':\n"
        "    print(json.dumps({'provider':'test','model':'scripted','revision':'1'}))\n"
        "elif op == 'ready':\n"
        "    print(json.dumps({'ready': True}))\n"
        "elif op == 'first_duty':\n"
        "    print(json.dumps({'tool': 'paper_buy', 'call_id': 'wave-a-call',"
        " 'quantity': 2, 'limit_minor_units': 110}))\n"
        "else:\n"
        "    with socket.socket(socket.AF_UNIX) as client:\n"
        "        client.connect(request['read_endpoint'])\n"
        "        with client.makefile() as stream:\n"
        "            observation = json.load(stream)\n"
        "    print(json.dumps({'tool': 'record_review_decision', 'call_id': 'wave-a-rev',"
        " 'observation_revision': observation['revision'],"
        " 'action': 'hold', 'rationale': 'Verified position', 'next_check_seconds': 60}))\n",
        encoding="utf-8",
    )
    peer.chmod(0o700)
    return peer


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        leases = root / "leases"

        # 1. Legacy v5 and v7 upgrades + future user_version fail-closed (API & CLI)
        legacy_terms = FoundingTerms("legacy-setup", "charter-v1", "paper", "profile-1")
        v5_store = root / "legacy-v5.sqlite"
        with closing(sqlite3.connect(v5_store)) as raw, raw:
            raw.executescript(
                """
                CREATE TABLE company (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    id TEXT NOT NULL UNIQUE,
                    schema_revision INTEGER NOT NULL,
                    charter_revision TEXT NOT NULL
                );
                CREATE TABLE founder (
                    id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL REFERENCES company(id),
                    execution_profile TEXT NOT NULL
                );
                CREATE TABLE standing_grant (
                    id TEXT PRIMARY KEY,
                    founder_id TEXT NOT NULL REFERENCES founder(id),
                    scope TEXT NOT NULL
                );
                CREATE TABLE duty (
                    id TEXT PRIMARY KEY,
                    founder_id TEXT NOT NULL REFERENCES founder(id),
                    state TEXT NOT NULL
                );
                CREATE TABLE founding_receipt (
                    setup_identity TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL REFERENCES company(id),
                    founder_id TEXT NOT NULL REFERENCES founder(id),
                    grant_id TEXT NOT NULL REFERENCES standing_grant(id),
                    due_work_id TEXT NOT NULL REFERENCES duty(id)
                );
                INSERT INTO company VALUES (1, 'co-v5', 5, 'charter-v1');
                INSERT INTO founder VALUES ('ag-v5', 'co-v5', 'profile-1');
                INSERT INTO standing_grant VALUES ('gr-v5', 'ag-v5', 'paper');
                INSERT INTO duty VALUES ('du-v5', 'ag-v5', 'due');
                INSERT INTO founding_receipt VALUES ('setup-v5', 'co-v5', 'ag-v5', 'gr-v5', 'du-v5');
                """
            )
        upgrade_work_schema(v5_store, "co-v5")
        with closing(open_reader(v5_store)) as reader, read_tx(reader):
            self.assertEqual(
                reader.execute("PRAGMA user_version").fetchone()[0], MAX_SCHEMA_VERSION
            )
            self.assertEqual(
                reader.execute(
                    "SELECT id FROM agent WHERE company_id = 'co-v5'"
                ).fetchone(),
                ("ag-v5",),
            )
            self.assertEqual(reader.execute("PRAGMA foreign_key_check").fetchall(), [])

        legacy_store = root / "legacy-v7.sqlite"
        legacy_receipt = found(legacy_store, legacy_terms)
        with closing(sqlite3.connect(legacy_store)) as raw, raw:
            raw.execute("PRAGMA journal_mode = DELETE")
            for table in (
                "schema_migrations",
                "audit_event",
                "command_receipt",
                "external_actions",
                "company_restore_receipt",
                "company_store_retired",
                "work_peer_requests",
                "work_condition_occurrences",
                "work_conditions",
                "work_responsibilities",
                "work_runs",
                "work_items",
            ):
                raw.execute(f"DROP TABLE IF EXISTS {table}")
            raw.execute("UPDATE company SET schema_revision = 7 WHERE singleton = 1")
            raw.execute("PRAGMA user_version = 0")

        upgrade_work_schema(legacy_store, legacy_receipt.company_id)
        with closing(open_reader(legacy_store)) as reader, read_tx(reader):
            self.assertEqual(
                reader.execute("PRAGMA user_version").fetchone()[0], MAX_SCHEMA_VERSION
            )
            self.assertEqual(
                verify_applied_migrations(reader),
                [spec.version for spec in load_migrations()],
            )
            self.assertEqual(reader.execute("PRAGMA foreign_key_check").fetchall(), [])

        future_store = root / "future.sqlite"
        future_receipt = found(future_store, legacy_terms)
        with closing(sqlite3.connect(future_store)) as raw, raw:
            raw.execute(f"PRAGMA user_version = {MAX_SCHEMA_VERSION + 5}")
        with self.assertRaises(StorageRefused):
            open_reader(future_store)
        with self.assertRaises(StorageRefused):
            open_writer(future_store)
        future_cli = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "sourdough.cli",
                "inspect-work",
                str(future_store),
                "--company-id",
                future_receipt.company_id,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(future_cli.returncode, 1)
        self.assertIn("Company command refused", future_cli.stderr)

        # 2. Installed CLI paper trade + continuation through unified CompanyRuntime & WAL store
        store = root / "company.sqlite"
        venue_db = root / "venue.sqlite"
        peer = _scripted_peer(root)
        profile = _cli("employee-profile", peer)["execution_profile"]
        company = _cli(
            "found",
            store,
            "--setup-identity",
            "wave-a-setup",
            "--charter-revision",
            "charter-v1",
            "--founder-scope",
            "paper",
            "--execution-profile",
            profile,
        )
        company_id = company["company_id"]
        founder_id = company["founder_id"]

        _cli(
            "bind-paper",
            store,
            venue_db,
            "--company-id",
            company_id,
            "--charter-revision",
            "charter-v1",
            "--revision",
            "mandate-v1",
            "--account-id",
            "acct-1",
            "--rules",
            "rules-v1",
            "--starting-minor-units",
            5000,
            "--exposure-minor-units",
            1000,
            "--quote-minor-units",
            100,
        )
        trade = _cli(
            "first-duty",
            store,
            venue_db,
            "--company-id",
            company_id,
            "--employee",
            peer,
        )
        self.assertEqual(trade["position_quantity"], 2)

        operated = _cli(
            "operate-paper",
            store,
            venue_db,
            "--company-id",
            company_id,
            "--employee",
            peer,
            "--turn-budget",
            2,
            "--idle-seconds",
            0,
        )
        self.assertIsNotNone(operated["review"])
        self.assertEqual(operated["review"]["decision"]["action"], "hold")

        timeline = _cli("inspect-work", store, "--company-id", company_id)
        self.assertEqual(timeline["paper"]["booked_position"], 2)

        # 3. Three-entity Work kernel: standing responsibility, condition replacement,
        # anti-self-wake, zero-compute wait, and lease fencing
        colleague_id = "colleague-reviewer"
        with closing(open_writer(store)) as conn, write_tx(conn):
            conn.execute(
                "INSERT INTO agent VALUES (?, ?, ?)",
                (colleague_id, company_id, "profile-colleague"),
            )
            conn.execute(
                "INSERT INTO standing_grant VALUES ('grant-colleague-reviewer', ?, 'manage')",
                (colleague_id,),
            )
            resp = register_responsibility(
                conn,
                company_id=company_id,
                owner_agent_id=founder_id,
                duty_kind="work.discovery_review",
                remit="Review shared policy changes",
                readable_scope=["shared"],
                now_ns=10_000,
            )
            old_timer = set_condition(
                conn,
                company_id=company_id,
                owner_agent_id=founder_id,
                responsibility_id=resp.responsibility_id,
                trigger_kind="timer",
                due_at_ns=15_000,
                now_ns=10_000,
            )
            replace_condition(
                conn,
                old_timer.condition_id,
                owner_agent_id=founder_id,
                trigger_kind="shared_asset_revision",
                source_identity="asset:policy-1",
                expected_revision=None,
                now_ns=11_000,
            )
            stale_item, _ = create_work_item(
                conn,
                company_id=company_id,
                owner_agent_id=colleague_id,
                duty_kind="work.cleanup",
                source_kind="system",
                source_id="stale-job",
                now_ns=10_000,
            )
            stale_run = claim_work_run(
                conn,
                company_id=company_id,
                work_item_id=stale_item.work_item_id,
                lease_owner="crashed-worker",
                lease_ttl_ns=1_000,
                now_ns=10_000,
            )

        observed_runs: list[tuple[str, int]] = []

        def handle_discovery(ctx: DutyContext) -> DutyOutcome:
            observed_runs.append(("discovery", ctx.run.fencing_token))
            if ctx.run.fencing_token == 1:
                return DutyOutcome(
                    disposition="waiting",
                    wait_until_ns=90_000,
                    wait_event_key="asset:policy-approved",
                    wait_reason="waiting_for_approval",
                )
            return DutyOutcome(disposition="completed", result={"final": "adopted"})

        def handle_cleanup(ctx: DutyContext) -> DutyOutcome:
            observed_runs.append(("cleanup", ctx.run.fencing_token))
            return DutyOutcome(disposition="completed", result={"cleaned": True})

        registry = build_default_registry(
            {
                "work.discovery_review": handle_discovery,
                "work.cleanup": handle_cleanup,
            }
        )
        runtime = CompanyRuntime(store, company_id, registry, lease_directory=leases)

        # Anti-self-wake: self-authored rev-1 does not admit discovery (even on later actor-less sweep)
        s0 = runtime.step(
            max_dispatches=0,
            now_ns=20_000,
            observed_assets={"asset:policy-1": "rev-1"},
            triggering_actor_id=founder_id,
        )
        self.assertEqual(s0.fenced_runs, (stale_run.run_id,))
        self.assertEqual(s0.admitted_items, ())
        s0_later = runtime.step(
            max_dispatches=0,
            now_ns=20_500,
            observed_assets={"asset:policy-1": "rev-1"},
            triggering_actor_id=None,
        )
        self.assertEqual(s0_later.admitted_items, ())
        with closing(open_writer(store)) as conn:
            with self.assertRaises(RunFenced):
                assert_run_fencing(conn, stale_run.run_id, stale_run.fencing_token)

        # Peer-authored rev-2 admits discovery once; superseded timer never fires
        s1 = runtime.step(
            max_dispatches=4,
            now_ns=21_000,
            observed_assets={"asset:policy-1": "rev-2"},
            triggering_actor_id=colleague_id,
        )
        self.assertEqual(len(s1.admitted_items), 1)
        self.assertIn(("cleanup", 2), observed_runs)
        self.assertIn(("discovery", 1), observed_runs)

        # Wake waiting discovery via peer event -> completes on fencing_token=2
        s2 = runtime.step(
            max_dispatches=4,
            now_ns=25_000,
            event_key="asset:policy-approved",
            triggering_actor_id=colleague_id,
            truncate_wal_on_exit=True,
        )
        self.assertEqual(s2.woken_items, s1.admitted_items)
        self.assertIn(("discovery", 2), observed_runs)

        # Verify two-process Paper Venue independence + segment planning
        with self.assertRaises(VenueRefused):
            BoundedVenueCoordinator(store, FixedQuoteVenue(store))
        coordinator = BoundedVenueCoordinator(
            store,
            FixedQuoteVenue(venue_db),
            policy=VenueSegmentPolicy(segment_seconds=60, drain_margin_seconds=5),
        )
        self.assertEqual(
            [seg.active_seconds for seg in coordinator.plan_segments(125)], [60, 57, 8]
        )

        # 4. Command receipt scoping + WAL-normalized checkpoint/restore with Desk bundle
        payload_hash = sha256(b'{"control":"suspend"}').hexdigest()
        with closing(open_writer(store)) as conn, write_tx(conn):
            admit_command_receipt(
                conn,
                company_id=company_id,
                command_id="board-cmd-1",
                actor_id="board-owner",
                peer_pid=4242,
                command_type="risk.suspend",
                contract_version="v1",
                payload_sha256=payload_hash,
                preconditions={
                    "charter_revision": "charter-v1",
                    "expected_control_revision": 0,
                },
                state="applied",
                result={"control_revision": 1},
            )
            with self.assertRaises(CommandUnauthorized):
                read_command_receipt(
                    conn,
                    company_id,
                    "board-cmd-1",
                    principal="other-actor",
                    operation="risk.suspend",
                )

        desk_src = root / "desk-src"
        (desk_src / founder_id).mkdir(parents=True)
        (desk_src / founder_id / "state.md").write_text(
            "shift-ready\n", encoding="utf-8"
        )

        archive = root / "checkpoint.zip"
        manifest = checkpoint_store(
            store, company_id, archive, desk_root=desk_src, lease_directory=leases
        )

        # Physical WAL checkpoint + PRAGMA optimize must not break logical watermark cut check
        with closing(open_writer(store)) as conn:
            wal_checkpoint(conn, "TRUNCATE")
            conn.execute("PRAGMA optimize")

        def reconcile_shadow(shadow_db: Path) -> None:
            with closing(open_writer(shadow_db)) as conn, write_tx(conn):
                append_audit_event(
                    conn,
                    company_id=company_id,
                    domain="ops",
                    entity_type="restore",
                    entity_id="cut-1",
                    event_type="restore.reconciled",
                    actor_id="reconciler",
                    payload={"verified": True},
                )

        restored_db = root / "restored.sqlite"
        restored_desk = root / "desk-restored"
        restore_receipt = restore_store(
            store,
            company_id,
            archive,
            restored_db,
            desk_destination=restored_desk,
            reconcile=reconcile_shadow,
            lease_directory=leases,
        )
        verify_no_wal_sidecars(restored_db)

        with closing(open_reader(restored_db)) as reader, read_tx(reader):
            self.assertEqual(
                reader.execute("PRAGMA journal_mode").fetchone()[0].lower(), "delete"
            )
            self.assertEqual(
                reader.execute(
                    "SELECT event_type FROM audit_event WHERE event_type = 'restore.reconciled'"
                ).fetchall(),
                [("restore.reconciled",)],
            )
            self.assertEqual(
                reader.execute(
                    "SELECT checkpoint_sha256 FROM company_restore_receipt WHERE request_revision = ?",
                    (restore_receipt.request_revision,),
                ).fetchone(),
                (manifest.sha256,),
            )

        self.assertEqual(
            (restored_desk / founder_id / "state.md").read_text(encoding="utf-8"),
            "shift-ready\n",
        )
        restored_timeline = _cli(
            "inspect-work", restored_db, "--company-id", company_id
        )
        self.assertEqual(restored_timeline["paper"]["booked_position"], 2)
        with self.assertRaisesRegex(StorageRefused, "retired"):
            open_writer(store)

    def test_peer_request_yield_response_and_resume_end_to_end(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        leases = root / "leases"
        store = root / "company.sqlite"
        receipt = found(
            store, FoundingTerms("peer-setup", "charter-v1", "manage", "profile-1")
        )
        company_id = receipt.company_id
        founder_id = receipt.founder_id
        colleague_id = "peer-approver"
        with closing(open_writer(store)) as conn, write_tx(conn):
            conn.execute(
                "INSERT INTO agent VALUES (?, ?, ?)",
                (colleague_id, company_id, "profile-2"),
            )
            conn.execute(
                "INSERT INTO standing_grant VALUES ('grant-peer-approver', ?, 'manage')",
                (colleague_id,),
            )
            register_responsibility(
                conn,
                company_id=company_id,
                owner_agent_id=colleague_id,
                duty_kind="work.peer_response",
                remit="Approve founder peer requests",
                readable_scope={
                    "allowed_requesters": [founder_id],
                    "evidence_prefixes": ["asset:"],
                },
                now_ns=500,
            )
            item, _ = create_work_item(
                conn,
                company_id=company_id,
                owner_agent_id=founder_id,
                duty_kind="work.assessment",
                source_kind="project",
                source_id="proj-a",
                now_ns=1_000,
            )

        trace: list[tuple[str, int]] = []

        def handle_assessment(ctx: DutyContext) -> DutyOutcome:
            trace.append(("assessment", ctx.run.fencing_token))
            if ctx.run.fencing_token == 1:
                with closing(open_writer(store, lease=ctx.writer)) as conn:
                    with write_tx(conn):
                        open_peer_request(
                            conn,
                            company_id=ctx.company_id,
                            requester_work_item_id=ctx.work_item.work_item_id,
                            requester_run_id=ctx.run.run_id,
                            fencing_token=ctx.run.fencing_token,
                            recipient_agent_id=colleague_id,
                            question="Approve approach?",
                            expected_decision="yes_or_no",
                            evidence_refs=["asset:memo-1"],
                            deadline_ns=90_000,
                            request_id="req-e2e",
                            now_ns=ctx.now_ns,
                        )
                return DutyOutcome(disposition="waiting", wait_reason="peer")
            return DutyOutcome(disposition="completed", result={"approved": True})

        def handle_peer(ctx: DutyContext) -> DutyOutcome:
            trace.append(("peer", ctx.run.fencing_token))
            with closing(open_writer(store, lease=ctx.writer)) as conn:
                with write_tx(conn):
                    resolve_peer_request(
                        conn,
                        company_id=ctx.company_id,
                        request_id="req-e2e",
                        actor_id=colleague_id,
                        resolution_state="answered",
                        response_payload={"answer": "yes"},
                        response_run_id=ctx.run.run_id,
                        response_fencing_token=ctx.run.fencing_token,
                        now_ns=ctx.now_ns,
                    )
            return DutyOutcome(disposition="completed", result={"answered": True})

        runtime = CompanyRuntime(
            store,
            company_id,
            build_default_registry(
                {
                    "work.assessment": handle_assessment,
                    "work.peer_response": handle_peer,
                }
            ),
            lease_directory=leases,
        )
        summary = runtime.step(max_dispatches=4, now_ns=2_000)
        self.assertEqual(len(summary.executed_runs), 3)
        self.assertEqual(trace, [("assessment", 1), ("peer", 1), ("assessment", 2)])
        with closing(open_reader(store)) as reader, read_tx(reader):
            row = reader.execute(
                "SELECT state, owner_agent_id FROM work_items WHERE work_item_id = ?",
                (item.work_item_id,),
            ).fetchone()
            peer_state = reader.execute(
                "SELECT state FROM work_peer_requests WHERE request_id = 'req-e2e'"
            ).fetchone()
        self.assertEqual(row, ("settled", founder_id))
        self.assertEqual(peer_state, ("answered",))
