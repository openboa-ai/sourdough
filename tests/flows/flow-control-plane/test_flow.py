import json
import os
import signal
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.board_control import PAUSE_COMPANY, RESTRICTION_COMMAND
from sourdough.company import FoundingTerms, found
from sourdough.ipc import CompanyDaemon, ControlPlaneClient
from sourdough.writer_lease import WriterRefused, hold_company_writer


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self):
        with TemporaryDirectory(prefix="sd-cp-") as scratch:
            root = Path(scratch)
            store = root / "c.sqlite"
            leases = root / "l"
            sock_dir = root / "s"
            company_id = found(
                store, FoundingTerms("cp-flow", "charter-1", "paper", "profile")
            ).company_id

            # 1. Launch real serve-company-daemon subprocess with ready-fd pipe.
            read_fd, write_fd = os.pipe()
            daemon_proc = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "sourdough.cli",
                    "serve-company-daemon",
                    str(store),
                    "--company-id",
                    company_id,
                    "--socket-dir",
                    str(sock_dir),
                    "--lease-dir",
                    str(leases),
                    "--ready-fd",
                    str(write_fd),
                ],
                pass_fds=(write_fd,),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            os.close(write_fd)
            try:
                with os.fdopen(read_fd, "r", encoding="utf-8") as ready_pipe:
                    ready_line = ready_pipe.readline()
                self.assertTrue(ready_line, "Daemon failed to announce readiness")
                announcement = json.loads(ready_line)
                sock_path = Path(announcement["socket_path"])
                self.assertEqual(announcement["company_id"], company_id)
                self.assertEqual(announcement["daemon_pid"], daemon_proc.pid)
                self.assertTrue(sock_path.exists())

                # 2. WriterLease mutual exclusion: concurrent direct writer is refused.
                with self.assertRaises(WriterRefused):
                    with hold_company_writer(store, company_id, lease_directory=leases):
                        pass

                # 3. Subprocess client-bridge inspects meta, timeline, receipt, and events
                #    WITHOUT ever receiving the SQLite store path!
                def run_bridge(*bridge_args: str) -> subprocess.CompletedProcess[str]:
                    return subprocess.run(
                        [
                            sys.executable,
                            "-I",
                            "-m",
                            "sourdough.cli",
                            "client-bridge",
                            "--socket",
                            str(sock_path),
                            *bridge_args,
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=10,
                    )

                meta_proc = run_bridge("meta")
                self.assertEqual(meta_proc.returncode, 0, meta_proc.stderr)
                meta = json.loads(meta_proc.stdout)
                self.assertEqual(meta["company_id"], company_id)
                self.assertEqual(meta["daemon_pid"], daemon_proc.pid)

                work_proc = run_bridge(
                    "inspect-work",
                    "--company-id",
                    company_id,
                    "--contract-revision",
                    "work-timeline-v1",
                )
                self.assertEqual(work_proc.returncode, 0, work_proc.stderr)
                timeline = json.loads(work_proc.stdout)
                self.assertEqual(timeline["company_id"], company_id)
                self.assertEqual(timeline["contract_revision"], "work-timeline-v1")

                receipt_proc = run_bridge(
                    "read-command-receipt",
                    "--company-id",
                    company_id,
                    "--command-id",
                    "missing-cmd",
                    "--operation",
                    PAUSE_COMPANY,
                )
                self.assertEqual(receipt_proc.returncode, 0, receipt_proc.stderr)
                self.assertEqual(json.loads(receipt_proc.stdout)["status"], "not_found")

                # Unconfigured CLI daemon refuses mutating commands fail-closed (403).
                client = ControlPlaneClient(sock_path)
                denied_code, _ = client.submit_command(
                    company_id=company_id,
                    command_id="unauth-1",
                    command_type=PAUSE_COMPANY,
                    contract_version=RESTRICTION_COMMAND,
                    preconditions={
                        "charter_revision": "charter-1",
                        "restriction_id": "stop-unauth",
                        "expected_restriction_revision": 0,
                    },
                    payload={
                        "scope_ref": "company:all",
                        "cause_category": "board_pause",
                        "reason": "must be refused",
                    },
                )
                self.assertEqual(denied_code, 403)
            finally:
                daemon_proc.send_signal(signal.SIGTERM)
                _, stderr = daemon_proc.communicate(timeout=10)
                self.assertEqual(daemon_proc.returncode, 0, stderr)

            # 4. Socket unlinked and WriterLease released cleanly on SIGTERM.
            self.assertFalse(sock_path.exists())

            # 5. Cross-process LOCAL_PEERPID proof: authorized daemon accepts command from
            #    a separate child Python process and persists that child's kernel PID.
            def authorize_board(db, cid, op):
                return (
                    "board-lead" if cid == company_id and op == PAUSE_COMPANY else None
                )

            with CompanyDaemon(
                store,
                company_id,
                socket_dir=sock_dir,
                lease_directory=leases,
                authorize_board=authorize_board,
            ) as daemon:
                retired_code, _ = ControlPlaneClient(daemon.socket_path).submit_command(
                    company_id=company_id,
                    command_id="retired-risk-cmd",
                    command_type="suspend_new_risk",
                    contract_version="paper-risk-command-v1",
                    preconditions={
                        "charter_revision": "charter-1",
                        "expected_revision": 0,
                    },
                    payload={"reason": "retired"},
                )
                self.assertEqual(retired_code, 422)
                child_script = (
                    "import json, os, sys; from pathlib import Path; "
                    "from sourdough.ipc import ControlPlaneClient; "
                    "c = ControlPlaneClient(Path(sys.argv[1])); "
                    "code, body = c.submit_command("
                    f"company_id=sys.argv[2], command_id='child-suspend-1', "
                    f"command_type='{PAUSE_COMPANY}', contract_version='{RESTRICTION_COMMAND}', "
                    "preconditions={'charter_revision': 'charter-1', 'restriction_id': 'stop-1', 'expected_restriction_revision': 0}, "
                    "payload={'scope_ref': 'company:all', 'cause_category': 'board_pause', 'reason': 'cross-process board stop'}); "
                    "print(json.dumps({'pid': os.getpid(), 'code': code, 'body': body}))"
                )
                child = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        child_script,
                        str(daemon.socket_path),
                        company_id,
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=10,
                )
                child_out = json.loads(child.stdout)
                self.assertEqual(child_out["code"], 200)
                self.assertNotEqual(child_out["pid"], os.getpid())
                self.assertEqual(child_out["body"]["peer_pid"], child_out["pid"])
                self.assertEqual(child_out["body"]["state"], "applied")

                # Verify events & timeline reflect the committed suspension via client-bridge.
                events_proc = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-m",
                        "sourdough.cli",
                        "client-bridge",
                        "--socket",
                        str(daemon.socket_path),
                        "events",
                        "--after-seq",
                        "0",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=10,
                )
                events = [
                    json.loads(line)
                    for line in events_proc.stdout.splitlines()
                    if line.strip()
                ]
                self.assertTrue(
                    any(
                        e["event_type"] == "governance.restriction_applied"
                        for e in events
                    )
                )

            with closing(sqlite3.connect(store)) as db:
                (stored_pid,) = db.execute(
                    "SELECT peer_pid FROM command_receipt WHERE command_id = ?",
                    ("child-suspend-1",),
                ).fetchone()
            self.assertEqual(stored_pid, child_out["pid"])
