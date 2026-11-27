import json
import os
import socket
import sqlite3
import stat
import threading
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sourdough.board_control import (
    PAUSE_COMPANY,
    RESTRICTION_COMMAND,
    RESUME_COMPANY,
)
from sourdough.company import FoundingTerms, found
from sourdough.ipc import (
    CONTROL_PLANE_VERSION,
    WORK_TIMELINE_V1,
    WORK_TIMELINE_V2,
    CompanyDaemon,
    ControlPlaneClient,
    IpcRefused,
    PeerCredentials,
    default_socket_path,
)
from sourdough.trading import TradingDomainPlugin
from sourdough.writer_lease import WriterRefused, hold_company_writer


class CompanyDaemonControlPlaneTests(unittest.TestCase):
    def setUp(self):
        # Keep temporary paths short so UDS paths stay well below macOS's 104-byte sun_path limit.
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="sd-ipc-")))
        self.store = self.root / "c.sqlite"
        self.leases = self.root / "l"
        self.sock_dir = self.root / "s"
        self.company = found(
            self.store, FoundingTerms("ipc-co", "charter-1", "paper", "profile")
        ).company_id
        self.allow_board = True

    def _authorize(self, connection, company_id, operation):
        self.assertEqual(
            connection.execute("SELECT id FROM company").fetchone(), (company_id,)
        )
        return "board-principal-1" if self.allow_board else None

    def _daemon(self, *, authorize=None, socket_path=None):
        return CompanyDaemon(
            self.store,
            self.company,
            socket_dir=self.sock_dir,
            socket_path=socket_path,
            lease_directory=self.leases,
            authorize_board=authorize,
            domain_plugins=(TradingDomainPlugin(),),
        )

    def test_writer_lease_exclusivity_socket_permissions_and_stale_recovery(self):
        # First hold WriterLease externally -> daemon MUST fail closed without touching sockets.
        with hold_company_writer(
            self.store, self.company, lease_directory=self.leases
        ) as lease:
            expected_sock = default_socket_path(
                self.company, lease.store_key, socket_dir=self.sock_dir
            )
            with self.assertRaises(WriterRefused):
                with self._daemon():
                    pass
            self.assertFalse(expected_sock.exists())

        # Create a stale orphaned Unix socket at the exact target path (simulating SIGKILL).
        self.sock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        stale_sock = self.sock_dir / "stale.sock"
        with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as orphan:
            orphan.bind(str(stale_sock))
        self.assertTrue(stat.S_ISSOCK(os.lstat(stale_sock).st_mode))

        # Daemon acquires WriterLease first, removes stale socket, binds 0600, blocks concurrent writers.
        with self._daemon(socket_path=stale_sock) as daemon:
            info = os.lstat(daemon.socket_path)
            self.assertTrue(stat.S_ISSOCK(info.st_mode))
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.lstat(self.sock_dir).st_mode), 0o700)
            with self.assertRaises(WriterRefused):
                with hold_company_writer(
                    self.store, self.company, lease_directory=self.leases
                ):
                    pass
            client = ControlPlaneClient(daemon.socket_path)
            meta = client.meta()
            self.assertEqual(meta["control_plane_version"], CONTROL_PLANE_VERSION)
            self.assertEqual(meta["company_id"], self.company)
            self.assertEqual(meta["schema_version"], 1)
            self.assertEqual(meta["active_domains"], ["core"])
            self.assertEqual(meta["daemon_pid"], os.getpid())

        # Clean shutdown unlinks socket and releases WriterLease.
        self.assertFalse(stale_sock.exists())
        with hold_company_writer(self.store, self.company, lease_directory=self.leases):
            pass

    def test_refuses_non_socket_file_and_symlink_client_target(self):
        self.sock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        regular = self.sock_dir / "not-a-sock.sock"
        regular.write_text("keep-me", encoding="utf-8")
        with self.assertRaisesRegex(IpcRefused, "non-socket"):
            with self._daemon(socket_path=regular):
                pass
        self.assertEqual(regular.read_text(encoding="utf-8"), "keep-me")

        with self._daemon() as daemon:
            link = self.sock_dir / "alias.sock"
            link.symlink_to(daemon.socket_path)
            with self.assertRaisesRegex(IpcRefused, "Unix socket"):
                ControlPlaneClient(link).meta()

    def test_meta_projections_etag_events_commands_and_kernel_peer_pid(self):
        from sourdough.storage.db import open_writer, write_tx
        from sourdough.storage.schema import enable_domain_v1

        with hold_company_writer(
            self.store, self.company, lease_directory=self.leases
        ) as writer:
            with closing(open_writer(self.store, lease=writer)) as db, write_tx(db):
                enable_domain_v1(
                    db, "trading", company_id=self.company, actor_id="setup"
                )

        with self._daemon(authorize=self._authorize) as daemon:
            client = ControlPlaneClient(daemon.socket_path)
            status, etag_v1, timeline_v1 = client.work_timeline(
                contract_revision=WORK_TIMELINE_V1
            )
            self.assertEqual(status, 200)
            self.assertIsNotNone(etag_v1)
            assert timeline_v1 is not None
            self.assertEqual(timeline_v1["contract_revision"], WORK_TIMELINE_V1)
            self.assertEqual(timeline_v1["company_id"], self.company)
            self.assertEqual(timeline_v1["domains"], {})

            # Matching If-None-Match yields 304 Not Modified without body.
            status_304, etag_again, body_304 = client.work_timeline(
                contract_revision=WORK_TIMELINE_V1, if_none_match=etag_v1
            )
            self.assertEqual(status_304, 304)
            self.assertEqual(etag_again, etag_v1)
            self.assertIsNone(body_304)

            # v2 timeline includes event_seq and store_key.
            status_v2, _, timeline_v2 = client.work_timeline(
                contract_revision=WORK_TIMELINE_V2
            )
            self.assertEqual(status_v2, 200)
            assert timeline_v2 is not None
            self.assertEqual(timeline_v2["contract_revision"], WORK_TIMELINE_V2)
            self.assertIn("event_seq", timeline_v2)
            self.assertIn("store_key", timeline_v2)
            before_seq = timeline_v2["event_seq"]

            # Unknown receipt returns 404 status='not_found'.
            missing = client.command_receipt("cmd-1", operation=PAUSE_COMPANY)
            self.assertEqual(missing["status"], "not_found")

            # Unsupported contract version rejected with 422 before any domain write.
            bad_status, _ = client.submit_command(
                company_id=self.company,
                command_id="cmd-bad-ver",
                command_type=PAUSE_COMPANY,
                contract_version="restriction-command-v999",
                preconditions={
                    "charter_revision": "charter-1",
                    "restriction_id": "rest-bad",
                    "expected_restriction_revision": 0,
                },
                payload={"reason": "bad version"},
            )
            self.assertEqual(bad_status, 422)

            # Unauthorized board command denied 403 fail-closed.
            self.allow_board = False
            denied_status, _ = client.submit_command(
                company_id=self.company,
                command_id="cmd-denied",
                command_type=PAUSE_COMPANY,
                contract_version=RESTRICTION_COMMAND,
                preconditions={
                    "charter_revision": "charter-1",
                    "restriction_id": "rest-denied",
                    "expected_restriction_revision": 0,
                },
                payload={"reason": "unauthorized"},
            )
            self.assertEqual(denied_status, 403)
            self.allow_board = True

            # Start long-poll /v1/events waiter before submitting command.
            polled: list[list[dict]] = []

            def wait_for_events():
                polled.append(
                    client.events(after_seq=before_seq, limit=20, wait_ms=3000)
                )

            waiter = threading.Thread(target=wait_for_events)
            waiter.start()

            # Submit pause_company (including a spoofed peer_pid in payload which MUST be ignored).
            code, receipt = client.submit_command(
                company_id=self.company,
                command_id="cmd-suspend-1",
                command_type=PAUSE_COMPANY,
                contract_version=RESTRICTION_COMMAND,
                preconditions={
                    "charter_revision": "charter-1",
                    "restriction_id": "rest-1",
                    "expected_restriction_revision": 0,
                },
                payload={"reason": "private board note", "peer_pid": 999999},
            )
            self.assertEqual(code, 200)
            self.assertEqual(receipt["state"], "applied")
            self.assertEqual(receipt["effective_revision"], 1)
            self.assertEqual(receipt["peer_pid"], os.getpid())
            self.assertNotIn("principal", receipt)
            self.assertNotIn("reason", receipt)

            # Idempotent replay returns exact same receipt; conflicting payload yields 409.
            code_replay, replay = client.submit_command(
                company_id=self.company,
                command_id="cmd-suspend-1",
                command_type=PAUSE_COMPANY,
                contract_version=RESTRICTION_COMMAND,
                preconditions={
                    "charter_revision": "charter-1",
                    "restriction_id": "rest-1",
                    "expected_restriction_revision": 0,
                },
                payload={"reason": "private board note", "peer_pid": 999999},
            )
            self.assertEqual(code_replay, 200)
            self.assertEqual(replay, receipt)

            code_conflict, _ = client.submit_command(
                company_id=self.company,
                command_id="cmd-suspend-1",
                command_type=PAUSE_COMPANY,
                contract_version=RESTRICTION_COMMAND,
                preconditions={
                    "charter_revision": "charter-1",
                    "restriction_id": "rest-1",
                    "expected_restriction_revision": 0,
                },
                payload={"reason": "mutated reason"},
            )
            self.assertEqual(code_conflict, 409)

            # Stale expected_restriction_revision records conflicting_revision cleanly.
            code_stale, stale_receipt = client.submit_command(
                company_id=self.company,
                command_id="cmd-stale",
                command_type=PAUSE_COMPANY,
                contract_version=RESTRICTION_COMMAND,
                preconditions={
                    "charter_revision": "charter-1",
                    "restriction_id": "rest-1",
                    "expected_restriction_revision": 0,
                },
                payload={"reason": "stale retry"},
            )
            self.assertEqual(code_stale, 200)
            self.assertEqual(stale_receipt["state"], "denied")
            self.assertEqual(stale_receipt["denial_code"], "conflicting_revision")

            # Verify long-poll waiter received restriction.applied without private reason/actor.
            waiter.join(timeout=4.0)
            self.assertFalse(waiter.is_alive())
            self.assertEqual(len(polled), 1)
            event_types = [ev["event_type"] for ev in polled[0]]
            self.assertIn("governance.restriction_applied", event_types)
            for ev in polled[0]:
                self.assertNotIn("actor_id", ev)
                self.assertNotIn("payload", ev)
                self.assertNotIn("private board note", json.dumps(ev))

            # GET /v1/commands/{id} returns redacted receipt; wrong operation returns 403 error.
            saved = client.command_receipt("cmd-suspend-1", operation=PAUSE_COMPANY)
            self.assertEqual(saved["status"], "found")
            self.assertEqual(saved["state"], "applied")
            self.assertEqual(saved["effective_revision"], 1)
            self.assertNotIn("principal", saved)
            self.assertNotIn("reason", saved)
            with self.assertRaises(IpcRefused):
                client.command_receipt("cmd-suspend-1", operation=RESUME_COMPANY)

            # Projection ETag updated after mutation (no longer 304 for old ETag).
            status_after, etag_after, timeline_after = client.work_timeline(
                contract_revision=WORK_TIMELINE_V1, if_none_match=etag_v1
            )
            self.assertEqual(status_after, 200)
            self.assertNotEqual(etag_after, etag_v1)
            assert timeline_after is not None
            self.assertEqual(
                timeline_after["domains"]["trading"]["risk_control"]["state"],
                "suspended",
            )
            self.assertEqual(
                timeline_after["domains"]["trading"]["risk_control"][
                    "effective_revision"
                ],
                1,
            )

        # Verify DB persisted kernel peer_pid (not spoofed 999999).
        with closing(sqlite3.connect(self.store)) as db:
            (stored_pid,) = db.execute(
                "SELECT peer_pid FROM command_receipt WHERE command_id = ?",
                ("cmd-suspend-1",),
            ).fetchone()
        self.assertEqual(stored_pid, os.getpid())

    def test_mismatched_peer_uid_and_smuggled_http_requests_are_rejected(self):
        with self._daemon() as daemon:
            # Foreign peer UID is dropped immediately without response bytes.
            with patch(
                "sourdough.ipc.server.extract_peer_credentials",
                return_value=PeerCredentials(
                    uid=os.geteuid() + 1, gid=os.getegid(), pid=os.getpid()
                ),
            ):
                with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as raw:
                    raw.settimeout(2.0)
                    raw.connect(str(daemon.socket_path))
                    try:
                        raw.sendall(b"GET /v1/meta HTTP/1.1\r\nHost: localhost\r\n\r\n")
                        self.assertEqual(raw.recv(1024), b"")
                    except BrokenPipeError:
                        pass

            # Transfer-Encoding smuggling attempt rejected with 400.
            with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as raw:
                raw.settimeout(2.0)
                raw.connect(str(daemon.socket_path))
                raw.sendall(
                    b"POST /v1/commands HTTP/1.1\r\n"
                    b"Host: localhost\r\n"
                    b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
                )
                response = raw.recv(4096)
                self.assertTrue(response.startswith(b"HTTP/1.1 400 "))

            # Duplicate Content-Length, whitespace in header name, scheme-relative target,
            # duplicate Idempotency-Key, and oversized body (413) are all rejected.
            bad_frames = (
                (
                    b"POST /v1/commands HTTP/1.1\r\nHost: localhost\r\n"
                    b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
                    b"HTTP/1.1 400 ",
                ),
                (
                    b"GET /v1/meta HTTP/1.1\r\nHost : localhost\r\n\r\n",
                    b"HTTP/1.1 400 ",
                ),
                (
                    b"GET //evil.local/v1/meta HTTP/1.1\r\nHost: localhost\r\n\r\n",
                    b"HTTP/1.1 400 ",
                ),
                (
                    b"GET /v1/commands/%2f.. HTTP/1.1\r\nHost: localhost\r\n\r\n",
                    b"HTTP/1.1 400 ",
                ),
                (
                    b"POST /v1/commands HTTP/1.1\r\nHost: localhost\r\n"
                    b"Idempotency-Key: a\r\nIdempotency-Key: b\r\nContent-Length: 2\r\n\r\n{}",
                    b"HTTP/1.1 400 ",
                ),
                (
                    b"POST /v1/commands HTTP/1.1\r\nHost: localhost\r\n"
                    b"Content-Length: 999999\r\n\r\n",
                    b"HTTP/1.1 413 ",
                ),
            )
            for frame, expected_prefix in bad_frames:
                with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as raw:
                    raw.settimeout(2.0)
                    raw.connect(str(daemon.socket_path))
                    raw.sendall(frame)
                    self.assertTrue(raw.recv(4096).startswith(expected_prefix))

    def test_active_listener_protection_privacy_limits_and_304_company_check(self):
        # Socket path exceeding 103 bytes is refused cleanly.
        too_long = self.root / ("a" * 110 + ".sock")
        with self.assertRaisesRegex(IpcRefused, "103-byte"):
            with self._daemon(socket_path=too_long):
                pass

        # Group-readable socket directory is refused.
        public_dir = self.root / "pub"
        public_dir.mkdir(mode=0o755)
        os.chmod(public_dir, 0o755)
        with self.assertRaisesRegex(IpcRefused, "0700"):
            with CompanyDaemon(
                self.store,
                self.company,
                socket_dir=public_dir,
                lease_directory=self.leases,
            ):
                pass

        # Explicit --socket refuses to hijack an actively listening socket from another company.
        other_store = self.root / "other.sqlite"
        other_company = found(
            other_store, FoundingTerms("other", "charter-2", "paper", "profile")
        ).company_id
        shared_sock = self.sock_dir / "shared.sock"
        with self._daemon(socket_path=shared_sock) as live_daemon:
            with self.assertRaisesRegex(IpcRefused, "actively listening"):
                with CompanyDaemon(
                    other_store,
                    other_company,
                    socket_path=shared_sock,
                    lease_directory=self.leases,
                ):
                    pass
            self.assertTrue(shared_sock.exists())

            # Client preserves HTTP 403 status on company mismatch (both 200 and 304)
            # and rejects wildcard If-None-Match='*' as non-matching.
            client = ControlPlaneClient(live_daemon.socket_path)
            status_star, etag, _ = client.work_timeline(if_none_match="*")
            self.assertEqual(status_star, 200)
            with self.assertRaisesRegex(IpcRefused, "HTTP 403"):
                client.work_timeline(
                    if_none_match=etag, expected_company_id="wrong-company"
                )

            # Mutual client peer UID verification rejects foreign daemon UID with HTTP 403.
            with patch(
                "sourdough.ipc.client.extract_peer_credentials",
                return_value=PeerCredentials(
                    uid=os.geteuid() + 1, gid=os.getegid(), pid=os.getpid()
                ),
            ):
                with self.assertRaisesRegex(IpcRefused, "HTTP 403"):
                    client.meta()

            # Invalid timeout_seconds is refused.
            with self.assertRaises(IpcRefused):
                ControlPlaneClient(live_daemon.socket_path, timeout_seconds=0)

            # Extra preconditions keys and mismatched body.command_id are rejected with 400.
            code_extra, _ = client.submit_command(
                company_id=self.company,
                command_id="extra-pre",
                command_type=PAUSE_COMPANY,
                contract_version=RESTRICTION_COMMAND,
                preconditions={
                    "charter_revision": "charter-1",
                    "restriction_id": "rest-extra",
                    "expected_restriction_revision": 0,
                    "unexpected": 1,
                },
                payload={"reason": "extra key"},
            )
            self.assertEqual(code_extra, 400)

    def test_invalid_ready_fd_is_refused_before_acquiring_writer_lease(self):
        from sourdough.cli import main

        with patch(
            "sys.argv",
            [
                "sourdough",
                "serve-company-daemon",
                str(self.store),
                "--company-id",
                self.company,
                "--socket-dir",
                str(self.sock_dir),
                "--lease-dir",
                str(self.leases),
                "--ready-fd",
                "1",
            ],
        ):
            exit_code = main()
        self.assertEqual(exit_code, 1)
        # WriterLease must still be unheld.
        with hold_company_writer(self.store, self.company, lease_directory=self.leases):
            pass

    def test_concurrent_readers_and_long_poll_survive_rapid_daemon_restart_cycles(self):
        import threading
        from sourdough.agent_runtime import build_default_adapter_registry
        from sourdough.runtime import (
            ResidentCompanyService,
            build_production_duty_registry,
            load_domain_plugins,
        )
        from sourdough.storage.db import open_reader, read_tx

        plugins = load_domain_plugins()
        adapters = build_default_adapter_registry()
        for cycle in range(6):
            duties = build_production_duty_registry(
                adapter_registry=adapters,
                profile_bindings={},
                domain_plugins=plugins,
            )
            with ResidentCompanyService(
                self.store,
                self.company,
                duties,
                socket_dir=self.sock_dir,
                lease_directory=self.leases,
                tick_interval_seconds=0.02,
                domain_plugins=plugins,
            ) as service:
                client = ControlPlaneClient(service.socket_path, timeout_seconds=3.0)
                errors: list[str] = []

                def reader_worker() -> None:
                    for _ in range(10):
                        try:
                            client.meta()
                            with closing(open_reader(self.store)) as db, read_tx(db):
                                db.execute("SELECT COUNT(*) FROM company").fetchone()
                        except Exception as exc:
                            errors.append(repr(exc))

                def waiter_worker() -> None:
                    try:
                        client.events(
                            after_seq=9999,
                            wait_ms=1500,
                            expected_company_id=self.company,
                        )
                    except IpcRefused:
                        pass

                readers = [threading.Thread(target=reader_worker) for _ in range(3)]
                waiter = threading.Thread(target=waiter_worker)
                waiter.start()
                for t in readers:
                    t.start()
                for t in readers:
                    t.join(timeout=5.0)
                    self.assertFalse(t.is_alive(), f"cycle {cycle}: reader hung")
                # Stop service while event waiter may still be blocking -> must not hang.
            waiter.join(timeout=5.0)
            self.assertFalse(waiter.is_alive(), f"cycle {cycle}: waiter hung")
            self.assertEqual(errors, [], f"cycle {cycle}: {errors}")

    def test_continuous_open_reader_threads_survive_company_daemon_restart_cycles(
        self,
    ):
        import threading
        from time import sleep
        from sourdough.storage.db import open_reader, read_tx

        stop_readers = threading.Event()
        reader_errors: list[str] = []

        def continuous_reader() -> None:
            while not stop_readers.is_set():
                try:
                    with closing(open_reader(self.store)) as db, read_tx(db):
                        db.execute("SELECT COUNT(*) FROM company").fetchone()
                except Exception as exc:
                    reader_errors.append(repr(exc))
                sleep(0.002)

        readers = [threading.Thread(target=continuous_reader) for _ in range(3)]
        for t in readers:
            t.start()

        try:
            for cycle in range(12):
                with CompanyDaemon(
                    self.store,
                    self.company,
                    socket_dir=self.sock_dir,
                    lease_directory=self.leases,
                ) as daemon:
                    client = ControlPlaneClient(daemon.socket_path, timeout_seconds=3.0)
                    client_errors: list[str] = []

                    def ipc_worker() -> None:
                        for _ in range(5):
                            try:
                                client.meta()
                            except Exception as exc:
                                client_errors.append(repr(exc))

                    def waiter_worker() -> None:
                        try:
                            client.events(
                                after_seq=9999,
                                wait_ms=150,
                                expected_company_id=self.company,
                            )
                        except IpcRefused:
                            pass

                    ipc_threads = [
                        threading.Thread(target=ipc_worker) for _ in range(4)
                    ]
                    waiter = threading.Thread(target=waiter_worker)
                    waiter.start()
                    for t in ipc_threads:
                        t.start()
                    for t in ipc_threads:
                        t.join(timeout=5.0)
                        self.assertFalse(t.is_alive(), f"cycle {cycle}: ipc hung")
                    # Stop daemon while continuous_reader threads remain actively running.
                waiter.join(timeout=5.0)
                self.assertFalse(waiter.is_alive(), f"cycle {cycle}: waiter hung")
                self.assertEqual(client_errors, [], f"cycle {cycle}: {client_errors}")
        finally:
            stop_readers.set()
            for t in readers:
                t.join(timeout=5.0)
                self.assertFalse(t.is_alive(), "continuous reader hung")
        self.assertEqual(reader_errors, [])

    def test_strategy_commands_ipc_roundtrip_conflict_and_path_segment_ctl_rejection(
        self,
    ):
        from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
        from sourdough.storage.schema import enable_domain_v1
        from sourdough.trading.accounts import open_company_paper_book
        from sourdough.trading.commands import (
            ACTIVATE_STRATEGY_RELEASE,
            ROLLBACK_STRATEGY_DEPLOYMENT,
            STRATEGY_COMMAND,
        )
        from sourdough.trading.strategy_comparison import (
            build_pit_file_entries,
            register_pit_catalog_manifest,
            register_strategy_release,
        )

        catalog_dir = self.root / "catalog"
        catalog_dir.mkdir(exist_ok=True)
        (catalog_dir / "btc_bars.parquet").write_bytes(b"immutable-pit-bars-v1")
        entries = build_pit_file_entries(
            catalog_dir,
            (("btc_bars.parquet", 1_000, 2_000, 2_000),),
        )

        with hold_company_writer(
            self.store, self.company, lease_directory=self.leases
        ) as writer:
            with closing(open_writer(self.store, lease=writer)) as db, write_tx(db):
                enable_domain_v1(db, "trading", company_id=self.company, actor_id="b1")
                db.execute(
                    "INSERT INTO agent (id, company_id, execution_profile) VALUES (?, ?, ?)",
                    ("author-1", self.company, "profile"),
                )
                db.execute(
                    "INSERT INTO standing_grant (id, agent_id, scope) VALUES (?, ?, ?)",
                    ("grant-author-1", "author-1", "trading"),
                )
            paper = open_company_paper_book(
                writer,
                account_id="paper-strat-ipc",
                book_name="Strategy IPC Paper",
                starting_minor_units=50_000_000,
                exposure_limit_minor_units=25_000_000,
            )
            pit = register_pit_catalog_manifest(
                writer,
                manifest_id="pit-ipc-1",
                dataset_name="btc-spot",
                files=entries,
            )
            rel_inc = register_strategy_release(
                writer,
                release_id="rel-ipc-inc",
                owner_agent_id="author-1",
                strategy_source_sha256="1" * 64,
                parameters={"k": 1},
                instrument_rules_sha256="2" * 64,
                risk_config_sha256="3" * 64,
                fill_policy_sha256="4" * 64,
                pit_manifest_id=pit.manifest_id,
                pit_manifest_sha256=pit.manifest_sha256,
            )
            rel_chal = register_strategy_release(
                writer,
                release_id="rel-ipc-chal",
                owner_agent_id="author-1",
                parent_release_id=rel_inc.release_id,
                strategy_source_sha256="5" * 64,
                parameters={"k": 2},
                instrument_rules_sha256="2" * 64,
                risk_config_sha256="3" * 64,
                fill_policy_sha256="4" * 64,
                pit_manifest_id=pit.manifest_id,
                pit_manifest_sha256=pit.manifest_sha256,
            )
            with closing(open_writer(self.store, lease=writer)) as db, write_tx(db):
                db.execute(
                    """INSERT INTO trading_challenger_trial (
                           comparison_id, company_id, incumbent_release_id, challenger_release_id,
                           baseline_statement_revision, analysis_plan_json, analysis_plan_sha256,
                           incumbent_arm_json, challenger_arm_json, status, verdict, verdict_json,
                           invalidation_reason, registered_at_ns, evaluated_at_ns
                       ) VALUES (
                           'cmp-ipc-1', ?, ?, ?, 'stmt-rev-1', '{}', 'plan-sha', '{}', '{}',
                           'completed', 'PROMOTE', '{}', NULL, 100, 200
                       )""",
                    (self.company, rel_inc.release_id, rel_chal.release_id),
                )

        with self._daemon(authorize=self._authorize) as daemon:
            client = ControlPlaneClient(daemon.socket_path)

            # 1. Path segments with encoded newline or space return 400 (not 404).
            for bad_path in (
                "agents/agt%0a1",
                "agents/agt%201",
                "orders/ord%0a1",
                "orders/ord%201",
            ):
                with self.assertRaisesRegex(IpcRefused, "HTTP 400"):
                    client.projection(bad_path)

            # 2. Stale expected_active_deployment_id returns 409 Conflict.
            code_stale, body_stale = client.submit_command(
                company_id=self.company,
                command_id="ipc-act-stale-1",
                command_type=ACTIVATE_STRATEGY_RELEASE,
                contract_version=STRATEGY_COMMAND,
                preconditions={
                    "binding_revision": paper.binding_revision,
                    "expected_active_deployment_id": "wrong-dep",
                },
                payload={"release_id": rel_inc.release_id, "comparison_id": None},
            )
            self.assertEqual(code_stale, 409, body_stale)

            # 3. Bad binding_revision continues returning 400 Bad Request.
            code_bad_rev, body_bad_rev = client.submit_command(
                company_id=self.company,
                command_id="ipc-act-bad-rev",
                command_type=ACTIVATE_STRATEGY_RELEASE,
                contract_version=STRATEGY_COMMAND,
                preconditions={
                    "binding_revision": "stale-binding-rev",
                    "expected_active_deployment_id": None,
                },
                payload={"release_id": rel_inc.release_id, "comparison_id": None},
            )
            self.assertEqual(code_bad_rev, 400, body_bad_rev)

            # 4. Valid activation & rollback persist canonical contract_version, peer_pid, preconditions.
            code_act1, act1 = client.submit_command(
                company_id=self.company,
                command_id="ipc-act-ok-1",
                command_type=ACTIVATE_STRATEGY_RELEASE,
                contract_version=STRATEGY_COMMAND,
                preconditions={
                    "binding_revision": paper.binding_revision,
                    "expected_active_deployment_id": None,
                },
                payload={"release_id": rel_inc.release_id, "comparison_id": None},
            )
            self.assertEqual(code_act1, 200, act1)
            dep1_id = act1["result"]["deployment_id"]

            code_act2, act2 = client.submit_command(
                company_id=self.company,
                command_id="ipc-act-ok-2",
                command_type=ACTIVATE_STRATEGY_RELEASE,
                contract_version=STRATEGY_COMMAND,
                preconditions={
                    "binding_revision": paper.binding_revision,
                    "expected_active_deployment_id": dep1_id,
                },
                payload={
                    "release_id": rel_chal.release_id,
                    "comparison_id": "cmp-ipc-1",
                },
            )
            self.assertEqual(code_act2, 200, act2)
            dep2_id = act2["result"]["deployment_id"]

            # Stale rollback returns 409 Conflict.
            code_rb_stale, rb_stale = client.submit_command(
                company_id=self.company,
                command_id="ipc-rb-stale-1",
                command_type=ROLLBACK_STRATEGY_DEPLOYMENT,
                contract_version=STRATEGY_COMMAND,
                preconditions={"expected_active_deployment_id": dep1_id},
                payload={"target_release_id": rel_inc.release_id},
            )
            self.assertEqual(code_rb_stale, 409, rb_stale)

            code_rb, rb = client.submit_command(
                company_id=self.company,
                command_id="ipc-rb-ok-1",
                command_type=ROLLBACK_STRATEGY_DEPLOYMENT,
                contract_version=STRATEGY_COMMAND,
                preconditions={"expected_active_deployment_id": dep2_id},
                payload={"target_release_id": rel_inc.release_id},
            )
            self.assertEqual(code_rb, 200, rb)

        with closing(open_reader(self.store)) as db, read_tx(db):
            rec = db.execute(
                """SELECT contract_version, peer_pid, preconditions_json, state
                   FROM command_receipt
                   WHERE company_id = ? AND command_id = 'ipc-act-ok-1'""",
                (self.company,),
            ).fetchone()
            self.assertIsNotNone(rec)
            self.assertEqual(rec[0], STRATEGY_COMMAND)
            self.assertEqual(rec[1], os.getpid())
            self.assertEqual(
                json.loads(rec[2]),
                {
                    "binding_revision": paper.binding_revision,
                    "expected_active_deployment_id": None,
                },
            )
            self.assertEqual(rec[3], "applied")

            rb_rec = db.execute(
                """SELECT contract_version, peer_pid, preconditions_json, state
                   FROM command_receipt
                   WHERE company_id = ? AND command_id = 'ipc-rb-ok-1'""",
                (self.company,),
            ).fetchone()
            self.assertIsNotNone(rb_rec)
            self.assertEqual(rb_rec[0], STRATEGY_COMMAND)
            self.assertEqual(rb_rec[1], os.getpid())
            self.assertEqual(
                json.loads(rb_rec[2]),
                {"expected_active_deployment_id": dep2_id},
            )
            self.assertEqual(rb_rec[3], "applied")
