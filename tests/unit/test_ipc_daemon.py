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
from sourdough.paper_risk import (
    PAPER_RISK_COMMAND,
    SUSPEND_NEW_RISK,
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
            missing = client.command_receipt("cmd-1", operation=SUSPEND_NEW_RISK)
            self.assertEqual(missing["status"], "not_found")

            # Unsupported contract version rejected with 422 before any domain write.
            bad_status, _ = client.submit_command(
                company_id=self.company,
                command_id="cmd-bad-ver",
                command_type=SUSPEND_NEW_RISK,
                contract_version="paper-risk-command-v999",
                preconditions={"charter_revision": "charter-1", "expected_revision": 0},
                payload={"reason": "bad version"},
            )
            self.assertEqual(bad_status, 422)

            # Unauthorized board command denied 403 fail-closed.
            self.allow_board = False
            denied_status, _ = client.submit_command(
                company_id=self.company,
                command_id="cmd-denied",
                command_type=SUSPEND_NEW_RISK,
                contract_version=PAPER_RISK_COMMAND,
                preconditions={"charter_revision": "charter-1", "expected_revision": 0},
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

            # Submit suspend_new_risk (including a spoofed peer_pid in payload which MUST be ignored).
            code, receipt = client.submit_command(
                company_id=self.company,
                command_id="cmd-suspend-1",
                command_type=SUSPEND_NEW_RISK,
                contract_version=PAPER_RISK_COMMAND,
                preconditions={"charter_revision": "charter-1", "expected_revision": 0},
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
                command_type=SUSPEND_NEW_RISK,
                contract_version=PAPER_RISK_COMMAND,
                preconditions={"charter_revision": "charter-1", "expected_revision": 0},
                payload={"reason": "private board note", "peer_pid": 999999},
            )
            self.assertEqual(code_replay, 200)
            self.assertEqual(replay, receipt)

            code_conflict, _ = client.submit_command(
                company_id=self.company,
                command_id="cmd-suspend-1",
                command_type=SUSPEND_NEW_RISK,
                contract_version=PAPER_RISK_COMMAND,
                preconditions={"charter_revision": "charter-1", "expected_revision": 0},
                payload={"reason": "mutated reason"},
            )
            self.assertEqual(code_conflict, 409)

            # Stale expected_revision records conflicting_revision cleanly.
            code_stale, stale_receipt = client.submit_command(
                company_id=self.company,
                command_id="cmd-stale",
                command_type=SUSPEND_NEW_RISK,
                contract_version=PAPER_RISK_COMMAND,
                preconditions={"charter_revision": "charter-1", "expected_revision": 0},
                payload={"reason": "stale retry"},
            )
            self.assertEqual(code_stale, 200)
            self.assertEqual(stale_receipt["state"], "denied")
            self.assertEqual(stale_receipt["denial_code"], "conflicting_revision")

            # Verify long-poll waiter received paper.risk_suspended without private reason/actor.
            waiter.join(timeout=4.0)
            self.assertFalse(waiter.is_alive())
            self.assertEqual(len(polled), 1)
            event_types = [ev["event_type"] for ev in polled[0]]
            self.assertIn("paper.risk_suspended", event_types)
            for ev in polled[0]:
                self.assertNotIn("actor_id", ev)
                self.assertNotIn("payload", ev)
                self.assertNotIn("private board note", json.dumps(ev))

            # GET /v1/commands/{id} returns redacted receipt; wrong operation returns 403 error.
            saved = client.command_receipt("cmd-suspend-1", operation=SUSPEND_NEW_RISK)
            self.assertEqual(saved["status"], "found")
            self.assertEqual(saved["state"], "applied")
            self.assertEqual(saved["effective_revision"], 1)
            self.assertNotIn("principal", saved)
            self.assertNotIn("reason", saved)
            with self.assertRaises(IpcRefused):
                client.command_receipt("cmd-suspend-1", operation="resume_new_risk")

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
                    raw.sendall(b"GET /v1/meta HTTP/1.1\r\nHost: localhost\r\n\r\n")
                    self.assertEqual(raw.recv(1024), b"")

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
                command_type=SUSPEND_NEW_RISK,
                contract_version=PAPER_RISK_COMMAND,
                preconditions={
                    "charter_revision": "charter-1",
                    "expected_revision": 0,
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
