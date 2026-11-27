"""Bind a company-owned workflow to a Two-Plane AgentSession, Outer Broker, and private Desk."""

import json
import math
import os
import re
import resource
import signal
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from time import time_ns
from typing import Any
from uuid import uuid4

from .agent_desk import (
    LEGACY_DESK_LIMITS,
    PRODUCTION_DESK_LIMITS,
    DeskLimits,
    allocate_run_scratch,
    desk_directory,
    desks_root,
    resolve_desk,
)
from .company import _connect
from .duty_run import DutyClaim, DutyRefused, WorkRunClaim, employment_cutoff
from .runtime.work_kernel import WorkKernelRefused, assert_run_fencing
from .writer_lease import WriterLease


_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
DEFAULT_OFFLOAD_THRESHOLD_BYTES = 4096


def tool(name: str, description: str, properties: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": sorted(properties),
            "additionalProperties": False,
        },
    }


@dataclass(frozen=True)
class ScopedWorkflow:
    purpose: str
    instruction: str
    tools: tuple[dict, ...]
    terminal_tool: str | tuple[str, ...]


@dataclass(frozen=True)
class AgentSession:
    """Server-bound session identity for Two-Plane execution (never supplied by the Agent)."""

    company_id: str
    agent_id: str
    work_item_id: str
    run_id: str
    fencing_token: int
    capability_scope: str
    budget_ceiling_minor: int
    desk_path: Path
    trace_path: Path


def _safe_segment(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value) or ".." in value:
        raise DutyRefused(f"Invalid {label} for AgentSession")
    return value


def traces_root(store: Path) -> Path:
    """Return the agent-invisible trace root stored outside every Agent Desk."""
    return store.resolve(strict=False).parent / ".sourdough-traces"


def session_trace_path(store: Path, company_id: str, run_id: str) -> Path:
    """Return the append-only JSONL trace path for one fenced run outside all Desks."""
    safe_company = _safe_segment(company_id, "company_id")
    safe_run = _safe_segment(run_id, "run_id")
    return traces_root(store) / safe_company / f"{safe_run}.jsonl"


def _ensure_private_directory(path: Path, stop_at: Path) -> None:
    rel = path.relative_to(stop_at)
    current = stop_at
    for part in rel.parts:
        current = current / part
        if os.path.lexists(current):
            st = os.lstat(current)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise DutyRefused("Private runtime directory must not be a symlink")
        else:
            os.mkdir(current, 0o700)
        os.chmod(current, 0o700)


def _append_trace_event(
    store: Path,
    session: AgentSession,
    *,
    seq: int,
    tool_name: str,
    status: str,
    arg_keys: list[str],
    result_sha256: str | None = None,
    error_kind: str | None = None,
) -> None:
    """Append metadata-only tool call audit line to agent-invisible JSONL trace."""
    root = store.resolve(strict=False).parent
    _ensure_private_directory(session.trace_path.parent, root)
    if os.path.lexists(session.trace_path):
        st = os.lstat(session.trace_path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise DutyRefused("AgentSession trace file must be a regular file")
    record = {
        "seq": seq,
        "recorded_at_ns": time_ns(),
        "company_id": session.company_id,
        "agent_id": session.agent_id,
        "work_item_id": session.work_item_id,
        "run_id": session.run_id,
        "fencing_token": session.fencing_token,
        "tool": tool_name,
        "status": status,
        "arg_keys": arg_keys,
        "result_sha256": result_sha256,
        "error_kind": error_kind,
    }
    line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    fd = os.open(
        session.trace_path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | _O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    os.chmod(session.trace_path, 0o600)


def offload_large_output(
    store: Path,
    session: AgentSession,
    tool_name: str,
    payload: dict,
    *,
    seq: int,
    max_inline_bytes: int = DEFAULT_OFFLOAD_THRESHOLD_BYTES,
) -> dict:
    """Offload tool outputs exceeding max_inline_bytes into <desk_path>/.desk/logs/."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= max_inline_bytes:
        return payload
    root = store.resolve(strict=False).parent
    logs_dir = session.desk_path / ".desk" / "logs"
    _ensure_private_directory(logs_dir, root)
    safe_tool = re.sub(r"[^a-z0-9_]", "_", tool_name)
    filename = f"{seq:04d}-{safe_tool}.json"
    tmp_path = logs_dir / f".tmp-{uuid4().hex}-{filename}"
    fd = os.open(
        tmp_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    os.close(fd)
    target = logs_dir / filename
    if os.path.lexists(target) and stat.S_ISLNK(os.lstat(target).st_mode):
        tmp_path.unlink()
        raise DutyRefused("Symlinks are forbidden inside Desk logs")
    os.replace(tmp_path, target)
    os.chmod(target, 0o600)
    digest = sha256(encoded).hexdigest()
    return {
        "offloaded": True,
        "tool": tool_name,
        "log_path": f".desk/logs/{filename}",
        "bytes": len(encoded),
        "sha256": digest,
        "keys": sorted(payload.keys()),
    }


def create_agent_session(
    writer: WriterLease, claim: DutyClaim | WorkRunClaim, purpose: str
) -> AgentSession:
    """Bind a server-side AgentSession from the authoritative Work kernel and grant state."""
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        run_row = database.execute(
            """SELECT fencing_token, budget_ceiling_minor
               FROM work_runs
               WHERE company_id = ? AND run_id = ? AND work_item_id = ? AND agent_id = ?""",
            (claim.company_id, claim.run_id, claim.duty_id, claim.founder_id),
        ).fetchone()
        grant_row = database.execute(
            "SELECT scope FROM standing_grant WHERE id = ? AND agent_id = ?",
            (claim.grant_id, claim.founder_id),
        ).fetchone()
    if grant_row is None or str(grant_row[0]).strip().lower() == "revoked":
        raise DutyRefused("Standing grant is unavailable for AgentSession")
    if isinstance(claim, WorkRunClaim):
        if run_row is None or int(run_row[0]) != claim.fencing_token:
            raise DutyRefused("Work run fencing token mismatch for AgentSession")
        fencing_token = claim.fencing_token
    else:
        fencing_token = int(run_row[0]) if run_row is not None else 1
    budget_ceiling = int(run_row[1]) if run_row is not None else 0
    return AgentSession(
        company_id=claim.company_id,
        agent_id=claim.founder_id,
        work_item_id=claim.duty_id,
        run_id=claim.run_id,
        fencing_token=fencing_token,
        capability_scope=f"{grant_row[0]}:{purpose}",
        budget_ceiling_minor=budget_ceiling,
        desk_path=desk_directory(writer.store, claim.founder_id),
        trace_path=session_trace_path(writer.store, claim.company_id, claim.run_id),
    )


def _sbpl_quote(path: Path) -> str:
    text = str(path)
    if any(ord(ch) < 32 for ch in text):
        raise DutyRefused("Sandbox path contains control characters")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_desk_sandbox_profile(
    store: Path,
    desk_path: Path,
    *,
    broker_socket: Path | None = None,
    scratch_dir: Path | None = None,
    extra_read_paths: tuple[Path, ...] = (),
    extra_read_files: tuple[Path, ...] = (),
) -> str:
    """Generate a macOS Seatbelt (SBPL) profile isolating an Agent to its own Desk."""
    resolved_store = store.resolve(strict=False)
    resolved_parent = resolved_store.parent
    resolved_desk = desk_path.resolve(strict=False)
    resolved_desks = desks_root(resolved_store).resolve(strict=False)
    resolved_traces = traces_root(resolved_store).resolve(strict=False)
    try:
        resolved_desk.relative_to(resolved_desks)
    except ValueError as error:
        raise DutyRefused(
            "Agent Desk must reside under the company desks root"
        ) from error

    py_roots = sorted(
        {
            str(Path(sys.base_prefix).resolve(strict=False)),
            str(Path(sys.prefix).resolve(strict=False)),
            str(Path(sys.executable).parent.parent),
            str(Path(sys.executable).resolve(strict=False).parent.parent),
            *(str(p.resolve(strict=False)) for p in extra_read_paths),
        }
    )
    read_subpaths = "\n".join(f"    (subpath {_sbpl_quote(Path(r))})" for r in py_roots)
    rw_entries = [f"    (subpath {_sbpl_quote(resolved_desk)})"]
    if scratch_dir is not None:
        resolved_scratch = scratch_dir.resolve(strict=False)
        for forbidden in (resolved_parent, resolved_desks, resolved_traces):
            if resolved_scratch == forbidden:
                raise DutyRefused("Sandbox scratch directory overlaps protected root")
        rw_entries.append(f"    (subpath {_sbpl_quote(resolved_scratch)})")
    rw_block = "\n".join(rw_entries)

    extra_file_lines: list[str] = []
    if extra_read_files:
        literals = "\n".join(
            f"    (literal {_sbpl_quote(p.resolve(strict=False))})"
            for p in extra_read_files
        )
        extra_file_lines.append(f"(allow file-read*\n{literals})")

    network_lines = ["(deny network*)"]
    if broker_socket is not None:
        resolved_sock = broker_socket.resolve(strict=False)
        network_lines.append(
            f"(allow network-outbound (remote unix-socket (path-literal {_sbpl_quote(resolved_sock)})))"
        )
    network_block = "\n".join(network_lines)

    store_wal = Path(f"{resolved_store}-wal")
    store_shm = Path(f"{resolved_store}-shm")
    return "\n".join(
        [
            "(version 1)",
            "(deny default)",
            '(import "system.sb")',
            "(allow process-exec process-fork file-map-executable)",
            "(allow signal (target self))",
            "(allow file-read-metadata)",
            "(allow file-read*",
            '    (subpath "/opt/homebrew")',
            read_subpaths + ")",
            '(allow file-write* (literal "/dev/null"))',
            f"(deny file-read* file-write* (subpath {_sbpl_quote(resolved_parent)}))",
            f"(deny file-read* file-write* (subpath {_sbpl_quote(resolved_desks)}))",
            "(allow file-read* file-write*",
            rw_block + ")",
            *extra_file_lines,
            "(deny file-read* file-write*",
            f"    (literal {_sbpl_quote(resolved_store)})",
            f"    (literal {_sbpl_quote(store_wal)})",
            f"    (literal {_sbpl_quote(store_shm)})",
            f"    (subpath {_sbpl_quote(resolved_traces)}))",
            network_block,
            "",
        ]
    )


def run_in_desk_sandbox(
    session: AgentSession,
    command: list[str],
    *,
    store: Path,
    broker_socket: Path | None = None,
    scratch_dir: Path | None = None,
    extra_read_paths: tuple[Path, ...] = (),
    extra_read_files: tuple[Path, ...] = (),
    input_data: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    """Execute an Inner Plane command confined by macOS sandbox-exec to the Agent's Desk."""
    if sys.platform != "darwin" or not _SANDBOX_EXEC.is_file():
        raise DutyRefused("The local macOS sandbox backend is unavailable")
    root = store.resolve(strict=False).parent
    _ensure_private_directory(session.desk_path, root)
    profile = build_desk_sandbox_profile(
        store,
        session.desk_path,
        broker_socket=broker_socket,
        scratch_dir=scratch_dir,
        extra_read_paths=extra_read_paths,
        extra_read_files=extra_read_files,
    )
    sandbox_env = (
        {"PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.defpath}"}
        if env is None
        else dict(env)
    )
    sandbox_env["HOME"] = str(session.desk_path)
    return subprocess.run(
        [str(_SANDBOX_EXEC), "-p", profile, *command],
        input=input_data,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(session.desk_path),
        env=sandbox_env,
    )


@dataclass(frozen=True)
class DeskCommandLimits:
    timeout_seconds: float = 15.0
    cpu_seconds: int = 10
    memory_bytes: int = 1_073_741_824
    max_processes: int = 16
    max_open_files: int = 64
    max_file_bytes: int = 4_194_304
    max_stdout_bytes: int = 16_384
    max_stderr_bytes: int = 8_192

    def __post_init__(self) -> None:
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise DutyRefused("Invalid DeskCommandLimits configuration")
        try:
            timeout_val = float(self.timeout_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DutyRefused("Invalid DeskCommandLimits configuration") from exc
        if not math.isfinite(timeout_val) or timeout_val <= 0:
            raise DutyRefused("Invalid DeskCommandLimits configuration")
        for int_val in (
            self.cpu_seconds,
            self.memory_bytes,
            self.max_processes,
            self.max_open_files,
            self.max_file_bytes,
            self.max_stdout_bytes,
            self.max_stderr_bytes,
        ):
            if isinstance(int_val, bool) or not isinstance(int_val, int):
                raise DutyRefused("Invalid DeskCommandLimits configuration")
        if (
            self.cpu_seconds <= 0
            or self.memory_bytes <= 0
            or self.max_processes <= 0
            or self.max_open_files < 16
            or self.max_file_bytes <= 0
            or self.max_stdout_bytes <= 0
            or self.max_stderr_bytes <= 0
        ):
            raise DutyRefused("Invalid DeskCommandLimits configuration")


def has_desk_exec_grant(
    connection: Any,
    company_id: str,
    agent_id: str,
) -> bool:
    """Return True iff the active Agent holds an explicit authoritative desk:exec grant."""
    if employment_cutoff(connection, agent_id) is not None:
        return False
    grant_row = connection.execute(
        """SELECT g.scope
           FROM standing_grant g
           JOIN agent a ON a.id = g.agent_id
           WHERE a.company_id = ? AND a.id = ?
             AND trim(g.scope) != '' AND lower(trim(g.scope)) != 'revoked'""",
        (company_id, agent_id),
    ).fetchone()
    if grant_row is None:
        return False
    if str(grant_row[0]).strip() == "desk:exec":
        return True
    rows = connection.execute(
        """SELECT readable_scope_json
           FROM work_responsibilities
           WHERE company_id = ? AND owner_agent_id = ? AND status = 'active'""",
        (company_id, agent_id),
    ).fetchall()
    for (raw_scope,) in rows:
        try:
            scope = json.loads(str(raw_scope))
        except json.JSONDecodeError:
            continue
        if isinstance(scope, dict):
            grants = scope.get("grants")
            if isinstance(grants, list) and "desk:exec" in grants:
                return True
    return False


def _read_stream_bounded(
    stream: Any,
    max_bytes: int,
    sink: dict[str, Any],
) -> None:
    buf = bytearray()
    truncated = False
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            remaining = max_bytes - len(buf)
            if remaining > 0:
                buf.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True
            else:
                truncated = True
    finally:
        try:
            stream.close()
        except OSError:
            pass
    sink["text"] = bytes(buf).decode("utf-8", errors="replace")
    sink["truncated"] = truncated


def run_desk_command_sandboxed(
    session: AgentSession,
    command: Sequence[str],
    *,
    store: Path,
    sandbox_backend: str = "local-macos",
    limits: DeskCommandLimits = DeskCommandLimits(),
    granted_capabilities: Collection[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run a non-shell command inside macOS sandbox-exec with rlimits and output byte caps."""
    if (
        sandbox_backend != "local-macos"
        or sys.platform != "darwin"
        or not _SANDBOX_EXEC.is_file()
    ):
        raise DutyRefused("desk_run_command requires local-macos sandbox backend")
    if (
        not isinstance(command, Sequence)
        or isinstance(command, (str, bytes))
        or not command
        or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command)
    ):
        raise DutyRefused("desk_run_command requires a nonempty argument list")

    with closing(_connect(store, "ro")) as db:
        db.execute("BEGIN")
        if employment_cutoff(db, session.agent_id) is not None:
            raise DutyRefused("Agent employment is fenced")
        grant_ok = (
            "desk:exec" in granted_capabilities
            and db.execute(
                """SELECT 1
                   FROM standing_grant g
                   JOIN agent a ON a.id = g.agent_id
                   WHERE a.company_id = ? AND a.id = ?
                     AND trim(g.scope) != '' AND lower(trim(g.scope)) != 'revoked'""",
                (session.company_id, session.agent_id),
            ).fetchone()
            is not None
            if granted_capabilities is not None
            else has_desk_exec_grant(db, session.company_id, session.agent_id)
        )
    if not grant_ok:
        raise DutyRefused("desk_run_command requires explicit desk:exec grant")

    root = store.resolve(strict=False).parent
    _ensure_private_directory(session.desk_path, root)
    scratch_dir = allocate_run_scratch(store, session.agent_id, session.run_id)
    profile = build_desk_sandbox_profile(
        store,
        session.desk_path,
        broker_socket=None,
        scratch_dir=scratch_dir,
    )
    sandbox_env = (
        {"PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.defpath}"}
        if env is None
        else dict(env)
    )
    sandbox_env["HOME"] = str(session.desk_path)
    sandbox_env["TMPDIR"] = str(scratch_dir)

    def _apply_rlimits() -> None:
        for res, target in (
            (resource.RLIMIT_CPU, limits.cpu_seconds),
            (resource.RLIMIT_FSIZE, limits.max_file_bytes),
            (resource.RLIMIT_NOFILE, limits.max_open_files),
        ):
            _soft, hard = resource.getrlimit(res)
            capped = min(target, hard) if hard != resource.RLIM_INFINITY else target
            resource.setrlimit(res, (capped, capped))
        for mem_res in (resource.RLIMIT_DATA, resource.RLIMIT_AS):
            try:
                _soft, hard = resource.getrlimit(mem_res)
                capped = (
                    min(limits.memory_bytes, hard)
                    if hard != resource.RLIM_INFINITY
                    else limits.memory_bytes
                )
                resource.setrlimit(mem_res, (capped, capped))
            except (ValueError, OSError):
                pass
        if sys.platform != "darwin" and hasattr(resource, "RLIMIT_NPROC"):
            try:
                _soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
                capped = (
                    min(limits.max_processes, hard)
                    if hard != resource.RLIM_INFINITY
                    else limits.max_processes
                )
                resource.setrlimit(resource.RLIMIT_NPROC, (capped, capped))
            except (ValueError, OSError):
                pass

    proc = subprocess.Popen(
        [str(_SANDBOX_EXEC), "-p", profile, *command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(session.desk_path),
        env=sandbox_env,
        start_new_session=True,
        preexec_fn=_apply_rlimits,
    )
    stdout_sink: dict[str, Any] = {"text": "", "truncated": False}
    stderr_sink: dict[str, Any] = {"text": "", "truncated": False}
    t_out = threading.Thread(
        target=_read_stream_bounded,
        args=(proc.stdout, limits.max_stdout_bytes, stdout_sink),
        daemon=True,
    )
    t_err = threading.Thread(
        target=_read_stream_bounded,
        args=(proc.stderr, limits.max_stderr_bytes, stderr_sink),
        daemon=True,
    )
    t_out.start()
    t_err.start()
    timed_out = False
    try:
        returncode: int | None = proc.wait(timeout=limits.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5.0)
        returncode = None
    finally:
        t_out.join(timeout=5.0)
        t_err.join(timeout=5.0)

    return {
        "ok": (not timed_out and returncode == 0),
        "timed_out": timed_out,
        "returncode": returncode,
        "stdout": stdout_sink["text"],
        "stderr": stderr_sink["text"],
        "stdout_truncated": stdout_sink["truncated"],
        "stderr_truncated": stderr_sink["truncated"],
        "scratch_dir": f".desk/scratch/{session.run_id}",
    }


class ScopedRun:
    """Outer Capability Broker exposing only company-selected tools for one AgentSession."""

    def __init__(
        self,
        writer: WriterLease,
        claim: DutyClaim | WorkRunClaim,
        workflow: ScopedWorkflow,
        invoke: Callable[[str, dict], dict],
        *,
        context_current: Callable[[], bool] | None = None,
        offload_threshold_bytes: int | None = None,
        limits: DeskLimits | None = None,
    ):
        definitions = {item["name"]: item["inputSchema"] for item in workflow.tools}
        terminal = workflow.terminal_tool
        terminal_names = (terminal,) if isinstance(terminal, str) else terminal
        if (
            not 1 <= len(definitions) <= 24
            or len(definitions) != len(workflow.tools)
            or not terminal_names
            or any(name not in definitions for name in terminal_names)
            or len(set(terminal_names)) != len(terminal_names)
            or any(
                not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) for name in definitions
            )
            or not workflow.instruction.strip()
            or len(workflow.instruction.encode()) > 16_000
        ):
            raise DutyRefused("Invalid scoped workflow manifest")
        self._writer = writer
        self._claim = claim
        self._purpose = workflow.purpose
        self._invoke = invoke
        self._context_current = context_current
        self._offload_threshold_bytes = offload_threshold_bytes
        self._limits = (
            limits
            if limits is not None
            else (
                PRODUCTION_DESK_LIMITS
                if isinstance(claim, WorkRunClaim)
                else LEGACY_DESK_LIMITS
            )
        )
        self._call_seq = 0
        self._desk_revision, drafts = resolve_desk(
            writer,
            claim,
            workflow.purpose,
            include_drafts=True,
            limits=self._limits,
        )
        self._session = create_agent_session(writer, claim, workflow.purpose)
        manifest = {
            "run_id": claim.run_id,
            "desk_revision": self._desk_revision,
            "drafts": [asdict(draft) for draft in drafts],
            "instruction": workflow.instruction,
            "tools": workflow.tools,
            "terminal_tool": workflow.terminal_tool,
        }
        self._manifest = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        manifest_cap = 262_144 if isinstance(claim, WorkRunClaim) else 32_000
        if len(self._manifest.encode()) > manifest_cap:
            raise DutyRefused("Scoped workflow manifest exceeds the run budget")
        self._definitions = {
            item["name"]: item["inputSchema"]
            for item in json.loads(self._manifest)["tools"]
        }

    def _current_desk(self) -> str:
        revision, _ = resolve_desk(
            self._writer, self._claim, self._purpose, limits=self._limits
        )
        return revision

    def _verify_kernel_fencing(self) -> None:
        with closing(_connect(self._writer.store, "ro")) as database:
            database.execute("BEGIN")
            exists = database.execute(
                "SELECT 1 FROM work_runs WHERE run_id = ?", (self._session.run_id,)
            ).fetchone()
            if exists is None:
                return
            try:
                assert_run_fencing(
                    database,
                    self._session.run_id,
                    self._session.fencing_token,
                )
            except WorkKernelRefused as error:
                raise DutyRefused(
                    f"Run is fenced and no longer current: {error}"
                ) from error

    @property
    def claim(self) -> DutyClaim | WorkRunClaim:
        return self._claim

    @property
    def session(self) -> AgentSession:
        return self._session

    def manifest(self) -> dict:
        if self._current_desk() != self._desk_revision:
            raise DutyRefused("The Agent Desk changed after the context was prepared")
        if self._context_current is not None and not self._context_current():
            raise DutyRefused("Scoped context changed before disclosure")
        return json.loads(self._manifest) | {
            "revision": sha256(self._manifest.encode()).hexdigest()
        }

    def call(self, name: str, arguments: dict) -> dict:
        self._call_seq += 1
        seq = self._call_seq
        arg_keys = sorted(arguments.keys()) if isinstance(arguments, dict) else []
        try:
            if not isinstance(name, str) or name not in self._definitions:
                raise DutyRefused("Tool is not available to this run")
            schema = self._definitions[name]
            if (
                not isinstance(arguments, dict)
                or not set(schema["required"])
                <= arguments.keys()
                <= schema["properties"].keys()
            ):
                raise DutyRefused("Tool request does not match the scoped manifest")
            if self._desk_revision != self._current_desk():
                raise DutyRefused(
                    "The Agent Desk changed after the context was prepared"
                )
            self._verify_kernel_fencing()
            result = self._invoke(name, arguments)
            if self._offload_threshold_bytes is not None:
                result = offload_large_output(
                    self._writer.store,
                    self._session,
                    name,
                    result,
                    seq=seq,
                    max_inline_bytes=self._offload_threshold_bytes,
                )
        except Exception as error:
            _append_trace_event(
                self._writer.store,
                self._session,
                seq=seq,
                tool_name=name if isinstance(name, str) else "<invalid>",
                status="refused",
                arg_keys=arg_keys,
                error_kind=type(error).__name__,
            )
            raise
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        _append_trace_event(
            self._writer.store,
            self._session,
            seq=seq,
            tool_name=name,
            status="ok",
            arg_keys=arg_keys,
            result_sha256=sha256(encoded).hexdigest(),
        )
        return result
