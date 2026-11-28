"""Shared opaque-box E2E harness for Core Company OS Agent System tests (Tiers 1-4)."""

from __future__ import annotations

import ast
import importlib
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from typing import Any
from uuid import uuid4

from sourdough.agent_desk import desk_directory, desks_root, ensure_private_desk
from sourdough.agent_runtime import (
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI,
    AgentProfileBinding,
    InMemorySecretResolver,
    OuterToolBroker,
    build_default_adapter_registry,
)
from sourdough.company import FoundingReceipt, FoundingTerms, found
from sourdough.company_assets import assign_project
from sourdough.duty_run import DutyClaim, start_run
from sourdough.runtime.engine import CompanyRuntime
from sourdough.runtime.registry import DutyRegistry
from sourdough.runtime.work_kernel import (
    WorkItem,
    WorkRun,
    claim_work_run,
    create_work_item,
    register_responsibility,
)
from sourdough.storage.checkpoint import LogicalWatermark, compute_logical_watermark
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.storage.kernel import append_audit_event, max_event_seq
from sourdough.storage.schema import installed_domains, verify_v1_schema
from sourdough.writer_lease import hold_company_writer


CORE_FORBIDDEN_TRADING_MODULES: frozenset[str] = frozenset(
    {
        "sourdough.trading",
        "trading",
        "binance_venue",
        "company_run",
        "credentials",
        "execution_tiers",
        "external_venue",
        "first_trade",
        "fund_returns",
        "hired_paper_run",
        "market_runtime",
        "nautilus_paper",
        "nautilus_runtime",
        "paper_runtime",
        "replay_tape",
        "sender_lease",
        "spot_cash",
        "strategy_comparison",
        "trading_engine",
        "venue_coordinator",
        "window",
    }
)


def lazy_module(module_name: str) -> Any:
    """Resolve an M1-M4 module lazily at test execution time without masking ImportError."""
    return importlib.import_module(module_name)


def lazy_attr(module_name: str, attr_name: str) -> Any:
    """Resolve an M1-M4 symbol lazily at test execution time without masking missing contracts."""
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


def sha256_hex(raw: bytes | str) -> str:
    payload = raw.encode("utf-8") if isinstance(raw, str) else raw
    return sha256(payload).hexdigest()


def open_ro(store: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(store.as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA trusted_schema = OFF")
    return conn


def query_ro(
    store: Path, sql: str, params: tuple[Any, ...] = ()
) -> list[tuple[Any, ...]]:
    with closing(open_ro(store)) as conn:
        return conn.execute(sql, params).fetchall()


def run_cli(*args: object, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-IB", "-m", "sourdough.cli", *(str(a) for a in args)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def cli_json(*args: object, timeout: float = 20.0) -> dict[str, Any]:
    proc = run_cli(*args, timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(
            f"CLI {args!r} failed ({proc.returncode}): {proc.stderr or proc.stdout}"
        )
    return json.loads(proc.stdout)


def read_watermark(store: Path, company_id: str) -> LogicalWatermark:
    with closing(open_reader(store)) as db, read_tx(db):
        return compute_logical_watermark(db, company_id)


def assert_core_only_store(testcase: Any, store: Path) -> None:
    """Verify Core Company OS invariants: v1, zero trading tables, zero FTS5, strict FK."""
    with closing(open_reader(store)) as db, read_tx(db):
        testcase.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        testcase.assertEqual(tuple(installed_domains(db)), ("core",))
        testcase.assertEqual(verify_v1_schema(db), frozenset({"core"}))
        trading_tables = db.execute(
            """SELECT name FROM sqlite_schema
               WHERE type = 'table' AND (name LIKE 'paper_%' OR name LIKE 'trading_%')"""
        ).fetchall()
        testcase.assertEqual(trading_tables, [])
        fts_objects = db.execute(
            """SELECT name, sql FROM sqlite_schema
               WHERE lower(COALESCE(sql, '')) LIKE '%fts5%'
                  OR name LIKE '%_fts%'"""
        ).fetchall()
        testcase.assertEqual(fts_objects, [])
        testcase.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        testcase.assertEqual(db.execute("PRAGMA quick_check").fetchall(), [("ok",)])


def assert_strict_tables_only(testcase: Any, store: Path) -> None:
    """Verify every user table in company.sqlite is declared STRICT."""
    with closing(open_reader(store)) as db, read_tx(db):
        rows = db.execute(
            """SELECT name, sql FROM sqlite_schema
               WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"""
        ).fetchall()
        non_strict = [
            name for name, sql in rows if not (sql and "STRICT" in sql.upper())
        ]
        testcase.assertEqual(
            non_strict, [], f"Non-STRICT tables in company.sqlite: {non_strict}"
        )


def assert_no_canary_leak(
    testcase: Any,
    root: Path,
    canaries: Sequence[str],
    *,
    extra_texts: Sequence[str] = (),
) -> None:
    """Verify secret canary strings never appear in SQLite stores, traces, or projections."""
    encoded_canaries = [c.encode("utf-8") for c in canaries if c]
    for candidate_file in root.rglob("*"):
        if not candidate_file.is_file() or candidate_file.is_symlink():
            continue
        if candidate_file.suffix in {".sqlite", ".jsonl", ".json", ".md"}:
            raw = candidate_file.read_bytes()
            for canary, raw_canary in zip(canaries, encoded_canaries, strict=True):
                testcase.assertNotIn(
                    raw_canary,
                    raw,
                    f"Secret canary '{canary}' leaked into {candidate_file}",
                )
    for text in extra_texts:
        for canary in canaries:
            testcase.assertNotIn(
                canary, text, "Secret canary leaked into serialized text"
            )


def assert_no_trading_imports(testcase: Any, directory: Path) -> None:
    """Verify no Python file in directory imports sourdough.trading or forbidden trading roots."""
    for py_file in sorted(directory.glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    testcase.assertFalse(
                        alias.name == "sourdough.trading"
                        or alias.name.startswith("sourdough.trading."),
                        f"Forbidden trading import {alias.name} in {py_file}",
                    )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                testcase.assertFalse(
                    mod == "sourdough.trading" or mod.startswith("sourdough.trading."),
                    f"Forbidden trading import-from {mod} in {py_file}",
                )


def write_codex_shim(path: Path, body: str) -> Path:
    """Create an executable Python shim script for CodexCliAdapter testing."""
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o700)
    return path


class LoopbackMultiProviderServer:
    """Deterministic loopback HTTP server for OpenAI, Anthropic, and Gemini shift turns."""

    def __init__(self) -> None:
        self.openai_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.anthropic_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.gemini_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> LoopbackMultiProviderServer:
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                payload = json.loads(raw) if raw else {}
                headers = {k.lower(): v for k, v in self.headers.items()}
                with parent._lock:
                    parent.requests.append((self.path, headers, payload))
                if self.path == "/v1/responses":
                    spec = parent.openai_queue.get(timeout=5.0)
                elif self.path == "/v1/messages":
                    spec = parent.anthropic_queue.get(timeout=5.0)
                elif self.path.endswith(":generateContent"):
                    spec = parent.gemini_queue.get(timeout=5.0)
                else:
                    self._send_json(404, {"error": f"Unknown path {self.path}"})
                    return
                if spec.get("drop_connection"):
                    self.connection.close()
                    return
                status = int(spec.get("status", 200))
                self._send_json(status, spec["body"])

            def _send_json(self, status: int, body: dict[str, Any]) -> None:
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="sd-fas-mock-llm",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def build_registry(
        self,
        *,
        secret_resolver: InMemorySecretResolver,
        codex_executable: tuple[str, ...] = ("codex", "exec", "--json"),
        codex_use_macos_sandbox: bool = False,
        codex_extra_read_paths: tuple[Path, ...] = (),
        codex_extra_read_files: tuple[Path, ...] = (),
    ) -> Any:
        if self._server is None:
            return build_default_adapter_registry(
                secret_resolver=secret_resolver,
                codex_executable=codex_executable,
                codex_use_macos_sandbox=codex_use_macos_sandbox,
                codex_extra_read_paths=codex_extra_read_paths,
                codex_extra_read_files=codex_extra_read_files,
            )
        return build_default_adapter_registry(
            secret_resolver=secret_resolver,
            openai_base_url=self.base_url,
            anthropic_base_url=self.base_url,
            gemini_base_url=self.base_url,
            allow_loopback_override=True,
            codex_executable=codex_executable,
            codex_use_macos_sandbox=codex_use_macos_sandbox,
            codex_extra_read_paths=codex_extra_read_paths,
            codex_extra_read_files=codex_extra_read_files,
        )


def openai_tool_call_response(
    resp_id: str,
    call_id: str,
    name: str,
    arguments: dict[str, Any],
    *,
    input_tokens: int = 500,
    output_tokens: int = 120,
) -> dict[str, Any]:
    return {
        "status": 200,
        "body": {
            "id": resp_id,
            "output": [
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": json.dumps(arguments),
                }
            ],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        },
    }


def openai_text_response(
    resp_id: str,
    text: str,
    *,
    input_tokens: int = 400,
    output_tokens: int = 80,
) -> dict[str, Any]:
    return {
        "status": 200,
        "body": {
            "id": resp_id,
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        },
    }


def anthropic_tool_call_response(
    msg_id: str,
    call_id: str,
    name: str,
    arguments: dict[str, Any],
    *,
    input_tokens: int = 600,
    output_tokens: int = 140,
) -> dict[str, Any]:
    return {
        "status": 200,
        "body": {
            "id": msg_id,
            "content": [
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": name,
                    "input": arguments,
                }
            ],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        },
    }


def anthropic_text_response(
    msg_id: str,
    text: str,
    *,
    input_tokens: int = 450,
    output_tokens: int = 90,
) -> dict[str, Any]:
    return {
        "status": 200,
        "body": {
            "id": msg_id,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        },
    }


def gemini_tool_call_response(
    call_id: str,
    name: str,
    arguments: dict[str, Any],
    *,
    input_tokens: int = 550,
    output_tokens: int = 110,
) -> dict[str, Any]:
    return {
        "status": 200,
        "body": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "id": call_id,
                                    "name": name,
                                    "args": arguments,
                                }
                            }
                        ],
                    }
                }
            ],
            "usageMetadata": {
                "promptTokenCount": input_tokens,
                "candidatesTokenCount": output_tokens,
            },
        },
    }


def gemini_text_response(
    text: str,
    *,
    input_tokens: int = 380,
    output_tokens: int = 75,
) -> dict[str, Any]:
    return {
        "status": 200,
        "body": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": text}],
                    }
                }
            ],
            "usageMetadata": {
                "promptTokenCount": input_tokens,
                "candidatesTokenCount": output_tokens,
            },
        },
    }


@dataclass
class HiredAgent:
    agent_id: str
    grant_id: str
    grant_scope: str
    profile: AgentProfileBinding


class CoreCompanyHarness:
    """Self-contained isolated Core-only Sourdough company workspace for E2E tests."""

    def __init__(self, *, prefix: str = "sd-fas-") -> None:
        self._root_tmp = TemporaryDirectory(prefix=prefix)
        self._sock_tmp = TemporaryDirectory(prefix="sd-", dir="/tmp")
        self.root = Path(self._root_tmp.name)
        self.sock_dir = Path(self._sock_tmp.name)
        self.store = self.root / "company.sqlite"
        self.leases = self.root / "leases"
        self.evaluator_store = self.root / "evals" / "evaluator.sqlite"
        self.leases.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.leases, 0o700)
        self.evaluator_store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.evaluator_store.parent, 0o700)
        self.receipt: FoundingReceipt | None = None
        self.founder_profile: AgentProfileBinding | None = None

    def close(self) -> None:
        self._sock_tmp.cleanup()
        self._root_tmp.cleanup()

    def __enter__(self) -> CoreCompanyHarness:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @property
    def company_id(self) -> str:
        assert self.receipt is not None
        return self.receipt.company_id

    @property
    def founder_id(self) -> str:
        assert self.receipt is not None
        return self.receipt.founder_id

    @property
    def founder_grant_id(self) -> str:
        assert self.receipt is not None
        return self.receipt.grant_id

    def found_company(
        self,
        *,
        setup_identity: str = "fas-e2e-setup",
        charter_revision: str = "charter-v1",
        founder_scope: str = "manage-company",
        profile: AgentProfileBinding | None = None,
    ) -> FoundingReceipt:
        resolved_profile = profile or AgentProfileBinding(
            provider=PROVIDER_OPENAI,
            model="gpt-5-mini",
            revision="v1",
            credential_handle_ref="envref:SD_OPENAI_KEY",
        )
        self.founder_profile = resolved_profile
        self.receipt = found(
            self.store,
            FoundingTerms(
                setup_identity=setup_identity,
                charter_revision=charter_revision,
                founder_scope=founder_scope,
                execution_profile=resolved_profile.profile_id(),
            ),
        )
        return self.receipt

    def writer(self) -> Any:
        return hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        )

    def ensure_agent_desk(self, agent_id: str, execution_profile: str) -> None:
        with self.writer() as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                ensure_private_desk(db, self.company_id, agent_id, execution_profile)

    def hire_agent(
        self,
        agent_id: str,
        *,
        grant_scope: str = "assessment",
        profile: AgentProfileBinding | None = None,
        allocated_turns: int = 20,
        project_id: str | None = None,
        grant_id: str | None = None,
    ) -> HiredAgent:
        resolved_profile = profile or AgentProfileBinding(
            provider=PROVIDER_ANTHROPIC,
            model="claude-sonnet-4-5",
            revision="v1",
            credential_handle_ref="envref:SD_ANTHROPIC_KEY",
        )
        gid = grant_id or f"grant-{agent_id}-{uuid4().hex[:6]}"
        profile_str = resolved_profile.profile_id()
        now = time_ns()
        with self.writer() as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                row = db.execute(
                    "SELECT id FROM standing_grant WHERE agent_id = ? AND scope = 'manage-company' LIMIT 1",
                    (self.founder_id,),
                ).fetchone()
                manager_grant = str(row[0]) if row else self.founder_grant_id
                db.execute(
                    """INSERT OR IGNORE INTO staffing_authority
                       VALUES (?, ?, ?, 16, ?, 'manage-company')""",
                    (manager_grant, profile_str, grant_scope, allocated_turns),
                )
                db.execute(
                    "INSERT INTO agent (id, company_id, execution_profile) VALUES (?, ?, ?)",
                    (agent_id, self.company_id, profile_str),
                )
                db.execute(
                    "INSERT INTO standing_grant (id, agent_id, scope) VALUES (?, ?, ?)",
                    (gid, agent_id, grant_scope),
                )
                history_id = f"hire-hist-{agent_id}-{uuid4().hex[:6]}"
                hire_run_id = f"hire-run-{agent_id}-{uuid4().hex[:6]}"
                choice_id = f"hire-choice-{agent_id}-{uuid4().hex[:6]}"
                req_id = f"hire-req-{agent_id}-{uuid4().hex[:6]}"
                db.execute(
                    "INSERT INTO duty VALUES (?, ?, 'settled')",
                    (history_id, self.founder_id),
                )
                db.execute(
                    "INSERT INTO duty_run VALUES (?, ?, ?, 'manage-company', 'staffing', 0)",
                    (hire_run_id, history_id, manager_grant),
                )
                evidence = json.dumps({"project_id": project_id} if project_id else {})
                db.execute(
                    "INSERT INTO staffing_assignment VALUES (?, ?, ?, 'fas', ?)",
                    (history_id, manager_grant, evidence, now),
                )
                db.execute(
                    "INSERT INTO staffing_read VALUES (?, ?, 'fas', ?)",
                    (hire_run_id, evidence, now),
                )
                db.execute(
                    "INSERT INTO staffing_choice VALUES (?, ?, ?, ?, ?)",
                    (history_id, hire_run_id, choice_id, evidence, now),
                )
                db.execute(
                    "INSERT INTO hire_receipt VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        req_id,
                        choice_id,
                        manager_grant,
                        agent_id,
                        gid,
                        allocated_turns,
                        "{}",
                        now,
                    ),
                )
                ensure_private_desk(db, self.company_id, agent_id, profile_str)
                has_binding = (
                    db.execute(
                        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='agent_runtime_binding'"
                    ).fetchone()
                    is not None
                )
                if has_binding:
                    db.execute(
                        """INSERT OR REPLACE INTO agent_runtime_binding (
                               agent_id, company_id, provider, model, revision,
                               sandbox_backend, credential_handle_ref, profile_id,
                               created_at_ns, updated_at_ns
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            agent_id,
                            self.company_id,
                            resolved_profile.provider,
                            resolved_profile.model,
                            resolved_profile.revision,
                            resolved_profile.sandbox_backend,
                            resolved_profile.credential_handle_ref,
                            profile_str,
                            now,
                            now,
                        ),
                    )
        return HiredAgent(
            agent_id=agent_id,
            grant_id=gid,
            grant_scope=grant_scope,
            profile=resolved_profile,
        )

    def create_project(
        self,
        project_id: str,
        members: Sequence[str],
        *,
        publishers: Sequence[str],
        expected_revision: str | None = None,
        peer_responders: Sequence[str] | None = None,
    ) -> str:
        with self.writer() as w:
            kwargs: dict[str, Any] = {
                "publishers": tuple(publishers),
                "expected_revision": expected_revision,
            }
            if peer_responders is not None:
                kwargs["peer_responders"] = tuple(peer_responders)
            return assign_project(
                w,
                self.founder_grant_id,
                project_id,
                tuple(members),
                **kwargs,
            )

    def open_work_run_claim(
        self,
        agent_id: str,
        grant_id: str,
        grant_scope: str,
        *,
        duty_kind: str = "work.assessment_review",
        purpose: str = "assessment_review",
        source_id: str | None = None,
        now_ns: int | None = None,
        budget_ceiling_minor: int = 5000,
    ) -> tuple[WorkItem, WorkRun, Any]:
        """Create and claim a canonical WorkItem + WorkRun and return a WorkRunClaim."""
        WorkRunClaim = lazy_attr("sourdough.duty_run", "WorkRunClaim")
        ts = time_ns() if now_ns is None else now_ns
        with self.writer() as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=agent_id,
                    duty_kind=duty_kind,
                    source_kind="e2e",
                    source_id=source_id or f"src-{uuid4().hex[:8]}",
                    now_ns=ts,
                )
                run = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=item.work_item_id,
                    lease_owner="e2e-harness",
                    lease_ttl_ns=60_000_000_000,
                    budget_ceiling_minor=budget_ceiling_minor,
                    now_ns=ts,
                )
        claim = WorkRunClaim(
            company_id=self.company_id,
            founder_id=agent_id,
            duty_id=item.work_item_id,
            run_id=run.run_id,
            fencing_token=run.fencing_token,
            grant_id=grant_id,
            grant_scope=grant_scope,
            purpose=purpose,
        )
        return item, run, claim

    def open_legacy_duty_claim(
        self,
        agent: HiredAgent,
        *,
        purpose: str = "assessment",
        project_id: str | None = None,
    ) -> DutyClaim:
        """Create legacy duty + duty_run + onboarding rows when testing DutyClaim parity."""
        duty_id = f"duty-{uuid4().hex[:8]}"
        with self.writer() as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                db.execute(
                    "INSERT INTO duty VALUES (?, ?, 'due')",
                    (duty_id, agent.agent_id),
                )
                inserted_onboarding = False
                if project_id is not None:
                    cur = db.execute(
                        """INSERT OR IGNORE INTO hire_onboarding (
                               agent_id, purpose, state, attempts, retry_at, reason, first_duty_id
                           ) VALUES (?, ?, 'ready', 1, NULL, NULL, ?)""",
                        (agent.agent_id, purpose, duty_id),
                    )
                    inserted_onboarding = cur.rowcount == 1
                claim = start_run(
                    db,
                    self.company_id,
                    agent.agent_id,
                    agent.grant_id,
                    duty_id,
                    agent.profile.profile_id(),
                    purpose=purpose,
                    grant_scope=agent.grant_scope,
                )
                if inserted_onboarding:
                    db.execute(
                        """INSERT OR IGNORE INTO onboarding_read (run_id, revision)
                           VALUES (?, 'seed')""",
                        (claim.run_id,),
                    )
                    db.execute(
                        """INSERT OR IGNORE INTO onboarding_acceptance (
                               duty_id, run_id, revision, rationale
                           ) VALUES (?, ?, 'seed', 'accepted')""",
                        (duty_id, claim.run_id),
                    )
                return claim

    def make_runtime(
        self,
        registry: DutyRegistry,
        *,
        clock: Callable[[], int] | None = None,
    ) -> CompanyRuntime:
        return CompanyRuntime(
            self.store,
            self.company_id,
            registry,
            lease_directory=self.leases,
            clock=clock,
        )

    def max_seq(self) -> int:
        with closing(open_reader(self.store)) as db, read_tx(db):
            return max_event_seq(db, self.company_id)

    def agent_desk_path(self, agent_id: str) -> Path:
        return desk_directory(self.store, agent_id)

    def company_desks_root(self) -> Path:
        return desks_root(self.store)

    def register_discovery_responsibility(
        self,
        agent_id: str,
        *,
        duty_kind: str = "work.discovery_review",
        remit: str = "Monitor assigned project publications and respond cleanly.",
        readable_scope: Sequence[str] = ("shared",),
        now_ns: int = 10_000,
    ) -> str:
        with self.writer() as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                resp = register_responsibility(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=agent_id,
                    duty_kind=duty_kind,
                    remit=remit,
                    readable_scope=list(readable_scope),
                    now_ns=now_ns,
                )
                return resp.responsibility_id

    def emit_audit(
        self,
        *,
        domain: str,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: dict[str, Any],
        now_ns: int,
    ) -> None:
        with self.writer() as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                append_audit_event(
                    db,
                    company_id=self.company_id,
                    domain=domain,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    event_type=event_type,
                    actor_id=actor_id,
                    payload=payload,
                    recorded_at_ns=now_ns,
                )


def default_secret_resolver(
    *, canary_prefix: str = "sk-canary-"
) -> tuple[InMemorySecretResolver, tuple[str, ...]]:
    """Build an InMemorySecretResolver with unique canary tokens for leak assertions."""
    openai_secret = f"{canary_prefix}openai-{uuid4().hex[:8]}"
    anthropic_secret = f"{canary_prefix}anthropic-{uuid4().hex[:8]}"
    gemini_secret = f"{canary_prefix}gemini-{uuid4().hex[:8]}"
    cli_secret = f"{canary_prefix}codex-{uuid4().hex[:8]}"
    resolver = InMemorySecretResolver(
        {
            "envref:SD_OPENAI_KEY": openai_secret,
            "envref:SD_ANTHROPIC_KEY": anthropic_secret,
            "envref:SD_GEMINI_KEY": gemini_secret,
            "envref:SD_CODEX_KEY": cli_secret,
        }
    )
    return resolver, (openai_secret, anthropic_secret, gemini_secret, cli_secret)


def make_broker_for_run(
    *,
    company_id: str,
    agent_id: str,
    run_id: str,
    fencing_token: int,
    handlers: dict[str, Any],
    max_tool_calls: int = 8,
) -> OuterToolBroker:
    return OuterToolBroker(
        company_id=company_id,
        agent_id=agent_id,
        run_id=run_id,
        fencing_token=fencing_token,
        handlers=handlers,
        max_tool_calls=max_tool_calls,
    )


def has_command_subparser(subcommand: str) -> bool:
    proc = run_cli(subcommand, "--help")
    return proc.returncode == 0


def count_rows(
    store: Path, table: str, where_sql: str = "", params: tuple[Any, ...] = ()
) -> int:
    sql = f'SELECT COUNT(*) FROM "{table}"'
    if where_sql:
        sql += f" WHERE {where_sql}"
    return int(query_ro(store, sql, params)[0][0])


def file_mode_bits(path: Path) -> int:
    return os.lstat(path).st_mode & 0o777


def run_shift_for_claim(
    harness: CoreCompanyHarness,
    *,
    item: WorkItem,
    run: WorkRun,
    profile: AgentProfileBinding,
    writer: Any | None = None,
    adapter_registry: Any | None = None,
    secret_resolver: InMemorySecretResolver | None = None,
    mandate_summary: str = "Execute assigned duty.",
    instruction: str = "Complete the assigned task.",
    default_budget_ceiling_minor: int = 5000,
    turn_id: str = "turn-1",
    purpose: str | None = None,
    base_tools: Sequence[Any] = (),
    base_handlers: Mapping[str, Any] = (),
    history_notes: Sequence[str] = (),
    open_receipts: Sequence[dict[str, Any]] = (),
    include_core_desk_tools: bool = True,
    include_plugin_tools: bool = True,
    extra_recoverable_exceptions: tuple[type[BaseException], ...] = (),
    max_tool_calls: int = 16,
    timeout_seconds: float = 30.0,
    active_methods_provider: Any = None,
) -> Any:
    """Invoke landed ShiftRunner using canonical DutyContext and ToolContribution wiring."""
    ShiftRunner = lazy_attr("sourdough.runtime.shift_runner", "ShiftRunner")
    DutyContext = lazy_attr("sourdough.runtime.registry", "DutyContext")
    ToolContribution = lazy_attr("sourdough.runtime.domains", "ToolContribution")
    _read_agent_state = lazy_attr("sourdough.runtime.handlers", "_read_agent_state")
    _resolve_profile = lazy_attr("sourdough.runtime.handlers", "_resolve_profile")

    if adapter_registry is None:
        resolver = secret_resolver or default_secret_resolver()[0]
        adapter_registry = build_default_adapter_registry(secret_resolver=resolver)

    runner = ShiftRunner(
        adapter_registry=adapter_registry,
        profile_bindings={item.owner_agent_id: profile},
        default_budget_ceiling_minor=default_budget_ceiling_minor,
        read_agent_state=_read_agent_state,
        resolve_profile=_resolve_profile,
        contribute_tools=lambda _qctx: ToolContribution(),
        active_methods_provider=active_methods_provider,
    )

    def _invoke(active_writer: Any) -> Any:
        ctx = DutyContext(
            writer=active_writer,
            company_id=harness.company_id,
            work_item=item,
            run=run,
        )
        return runner.run(
            ctx,
            mandate_summary=mandate_summary,
            instruction=instruction,
            turn_id=turn_id,
            purpose=purpose,
            base_tools=base_tools,
            base_handlers=base_handlers,
            history_notes=history_notes,
            open_receipts=open_receipts,
            include_core_desk_tools=include_core_desk_tools,
            include_plugin_tools=include_plugin_tools,
            extra_recoverable_exceptions=extra_recoverable_exceptions,
            max_tool_calls=max_tool_calls,
            timeout_seconds=timeout_seconds,
        )

    if writer is not None:
        return _invoke(writer)
    with harness.writer() as w:
        return _invoke(w)
