"""Shared test fixtures for domain-scoped hardening regression suites."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from typing import Any

from sourdough.agent_runtime import PROVIDER_OPENAI, AgentProfileBinding
from sourdough.company import FoundingTerms, found
from sourdough.duty_run import WorkRunClaim
from sourdough.runtime import create_work_item
from sourdough.runtime.work_kernel import WorkItem, claim_work_run
from sourdough.storage.db import open_writer, write_tx
from sourdough.writer_lease import hold_company_writer


class _LoopbackJsonServer:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                _ = self.rfile.read(length)
                encoded = json.dumps(parent.body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._srv.server_address
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=2.0)


def _hire_active_agent(
    writer: Any,
    *,
    company_id: str,
    grant_id: str,
    agent_id: str,
    profile_id: str,
    scope: str = "assessment",
) -> tuple[str, str]:
    del grant_id
    hire_grant = f"grant-{agent_id}"
    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        db.execute(
            "INSERT INTO agent VALUES (?, ?, ?)",
            (agent_id, company_id, profile_id),
        )
        db.execute(
            "INSERT INTO standing_grant VALUES (?, ?, ?)",
            (hire_grant, agent_id, scope),
        )
        db.execute(
            "INSERT INTO private_desk VALUES (?, ?)",
            (agent_id, f"desk-{agent_id}"),
        )
    return agent_id, hire_grant


def _open_work_run_claim(
    db: sqlite3.Connection,
    *,
    company_id: str,
    agent_id: str,
    grant_id: str,
    grant_scope: str,
    duty_kind: str = "work.assessment_review",
    purpose: str = "assessment_review",
    now_ns: int = 1_000,
) -> tuple[WorkItem, Any, WorkRunClaim]:
    lease_ttl_ns = 3_600_000_000_000
    eff_now = time_ns() if now_ns == 1_000 else now_ns
    item, _ = create_work_item(
        db,
        company_id=company_id,
        owner_agent_id=agent_id,
        duty_kind=duty_kind,
        source_kind="unit",
        source_id=f"src-{agent_id}-{duty_kind}-{eff_now}-{os.urandom(3).hex()}",
        now_ns=eff_now,
    )
    run = claim_work_run(
        db,
        company_id=company_id,
        work_item_id=item.work_item_id,
        lease_owner="unit-worker",
        lease_ttl_ns=lease_ttl_ns,
        now_ns=eff_now,
    )
    claim = WorkRunClaim(
        company_id=company_id,
        founder_id=agent_id,
        duty_id=item.work_item_id,
        run_id=run.run_id,
        fencing_token=run.fencing_token,
        grant_id=grant_id,
        grant_scope=grant_scope,
        purpose=purpose,
    )
    return item, run, claim


class HardeningTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        co_dir = self.root / "co"
        co_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.store = co_dir / "company.sqlite"
        self.profile = AgentProfileBinding(
            provider=PROVIDER_OPENAI,
            model="gpt-mock",
            revision="rev-1",
            credential_handle_ref="envref:OPENAI_API_KEY",
        )
        self.founding = found(
            self.store,
            FoundingTerms(
                "Tier5 Co",
                "charter-v1",
                "manage-company",
                self.profile.profile_id(),
            ),
        )
        self.company_id = self.founding.company_id
        self.founder_id = self.founding.founder_id
        self.grant_id = self.founding.grant_id

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _open_claim(
        self,
        agent_id: str | None = None,
        grant_id: str | None = None,
        scope: str = "manage-company",
    ):
        aid = agent_id or self.founder_id
        gid = grant_id or self.grant_id
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                return _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=aid,
                    grant_id=gid,
                    grant_scope=scope,
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
