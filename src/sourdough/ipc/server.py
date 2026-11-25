"""Resident CompanyDaemon serving the versioned local UDS control plane."""

from __future__ import annotations

import errno
import json
import os
import socket
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ..company import FoundingRefused, _identity
from ..paper_account import BindingRefused
from ..paper_execution import VenueRefused
from ..paper_resume import resume_managed_paper_risk
from ..paper_risk import (
    PAPER_RISK_COMMAND,
    RESUME_NEW_RISK,
    SUSPEND_NEW_RISK,
    AuthorizeBoard,
    PaperRiskRefused,
    PaperSuspension,
    read_local_command_receipt_status,
    suspend_paper_risk,
)
from ..storage.db import StorageRefused, open_reader, read_tx
from ..storage.kernel import max_event_seq
from ..storage.schema import installed_domains
from ..trading_engine import EngineRefused
from ..work_projection import read_local_board_timeline_with_seq
from ..writer_lease import WriterLease, WriterRefused, hold_company_writer
from .protocol import (
    CONTROL_PLANE_VERSION,
    MAX_EVENTS_BATCH,
    WORK_TIMELINE_V1,
    WORK_TIMELINE_V2,
    HttpRequest,
    IpcRefused,
    PeerCredentials,
    default_socket_path,
    extract_peer_credentials,
    read_http_request,
    validate_socket_path_length,
    validate_timeout_seconds,
    verify_private_socket_dir,
    write_http_response,
)

_UMASK_LOCK = threading.Lock()
_MAX_WORKERS = 16
_MAX_IN_FLIGHT = 32
_MAX_EVENT_WAITERS = 8
_MAX_WAIT_MS = 5000
_ALLOWED_PRECONDITION_KEYS = frozenset({"charter_revision", "expected_revision"})


@dataclass(frozen=True)
class DaemonInfo:
    control_plane_version: str
    company_id: str
    store_key: str
    socket_path: str
    daemon_pid: int


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _etag_matches(header_value: str | None, etag: str) -> bool:
    if not header_value:
        return False
    for raw_part in header_value.split(","):
        candidate = raw_part.strip()
        if candidate == etag or candidate == f"W/{etag}":
            return True
    return False


def _probe_socket_is_active(target: Path) -> bool:
    """Return True if another process is actively listening on target Unix socket."""
    with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as probe:
        probe.setblocking(False)
        try:
            probe.connect(str(target))
            return True
        except ConnectionRefusedError:
            return False
        except FileNotFoundError:
            return False
        except OSError as error:
            if error.errno in (errno.ECONNREFUSED, errno.ENOENT):
                return False
            # EAGAIN / EINPROGRESS / EACCES / etc. -> fail closed as active.
            return True


def _unlink_owned_socket(dir_fd: int, name: str, expected_key: tuple[int, int]) -> None:
    try:
        current = os.lstat(name, dir_fd=dir_fd)
        if (
            stat.S_ISSOCK(current.st_mode)
            and current.st_uid == os.geteuid()
            and (current.st_dev, current.st_ino) == expected_key
        ):
            os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


class CompanyDaemon:
    """Single-writer resident daemon exposing HTTP/1.1 + NDJSON over a private UDS."""

    def __init__(
        self,
        store: Path,
        company_id: str,
        *,
        socket_dir: Path | None = None,
        socket_path: Path | None = None,
        lease_directory: Path | None = None,
        expected_store_key: tuple[int, int] | None = None,
        authorize_board: AuthorizeBoard | None = None,
        engine_directory: Path | None = None,
        request_timeout: float = 10.0,
    ) -> None:
        self._store = store.absolute()
        self._company_id = company_id
        self._socket_dir = socket_dir.absolute() if socket_dir is not None else None
        self._explicit_socket_path = (
            socket_path.absolute() if socket_path is not None else None
        )
        self._lease_directory = (
            lease_directory.absolute() if lease_directory is not None else None
        )
        self._expected_store_key = expected_store_key
        self._authorize_board = authorize_board
        self._engine_directory = (
            engine_directory.absolute() if engine_directory is not None else None
        )
        self._request_timeout = validate_timeout_seconds(request_timeout)

        self._stack: ExitStack | None = None
        self._writer: WriterLease | None = None
        self._socket_path: Path | None = None
        self._socket_dir_fd: int | None = None
        self._socket_key: tuple[int, int] | None = None
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._stop_event = threading.Event()
        self._write_lock = threading.RLock()
        self._event_cond = threading.Condition()
        self._active_lock = threading.Lock()
        self._active_sockets: set[socket.socket] = set()
        self._in_flight_sem = threading.BoundedSemaphore(_MAX_IN_FLIGHT)
        self._event_waiters_sem = threading.BoundedSemaphore(_MAX_EVENT_WAITERS)
        self._started = False

    @property
    def socket_path(self) -> Path:
        if self._socket_path is None:
            raise IpcRefused("CompanyDaemon is not running")
        return self._socket_path

    @property
    def info(self) -> DaemonInfo:
        if self._writer is None or self._socket_path is None:
            raise IpcRefused("CompanyDaemon is not running")
        dev, ino = self._writer.store_key
        return DaemonInfo(
            control_plane_version=CONTROL_PLANE_VERSION,
            company_id=self._company_id,
            store_key=f"{dev}:{ino}",
            socket_path=str(self._socket_path),
            daemon_pid=os.getpid(),
        )

    def start(self) -> DaemonInfo:
        if self._started:
            return self.info
        stack = ExitStack()
        try:
            # 1. Acquire WriterLease BEFORE touching or unlinking any socket path.
            writer = stack.enter_context(
                hold_company_writer(
                    self._store,
                    self._company_id,
                    lease_directory=self._lease_directory,
                    expected_store_key=self._expected_store_key,
                )
            )
            if self._explicit_socket_path is not None:
                target = self._explicit_socket_path
                validate_socket_path_length(target)
            else:
                target = default_socket_path(
                    self._company_id,
                    writer.store_key,
                    socket_dir=self._socket_dir,
                )
            # 2. Verify private 0700 parent directory without symlink traversal.
            dir_fd = verify_private_socket_dir(target.parent)
            stack.callback(os.close, dir_fd)

            # 3. Safely remove stale socket ONLY after holding WriterLease and confirming
            #    no live daemon is actively listening on it.
            try:
                existing = os.lstat(target.name, dir_fd=dir_fd)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if (
                    not stat.S_ISSOCK(existing.st_mode)
                    or existing.st_uid != os.geteuid()
                ):
                    raise IpcRefused(
                        "Refusing to remove non-socket or foreign file at socket path"
                    )
                if _probe_socket_is_active(target):
                    raise IpcRefused(
                        "Another daemon is actively listening on this socket path"
                    )
                os.unlink(target.name, dir_fd=dir_fd)

            # 4. Bind Unix socket with restrictive umask (0600 inside 0700 dir).
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stack.callback(listener.close)
            with _UMASK_LOCK:
                previous_umask = os.umask(0o177)
                try:
                    listener.bind(str(target))
                finally:
                    os.umask(previous_umask)
            bound_info = os.lstat(target.name, dir_fd=dir_fd)
            bound_key = (bound_info.st_dev, bound_info.st_ino)
            # Ensure partial startup failure removes only our newly bound socket inode
            # BEFORE dir_fd is closed and BEFORE WriterLease is released.
            stack.callback(_unlink_owned_socket, dir_fd, target.name, bound_key)
            if (
                not stat.S_ISSOCK(bound_info.st_mode)
                or bound_info.st_uid != os.geteuid()
                or (bound_info.st_mode & 0o077) != 0
            ):
                raise IpcRefused(
                    "Bound control plane socket failed privacy verification"
                )
            listener.listen(64)
            listener.settimeout(0.25)

            self._stack = stack
            self._writer = writer
            self._socket_path = target
            self._socket_dir_fd = dir_fd
            self._socket_key = bound_key
            self._listener = listener
            self._stop_event.clear()
            self._pool = ThreadPoolExecutor(
                max_workers=_MAX_WORKERS, thread_name_prefix="sourdough-ipc"
            )
            self._accept_thread = threading.Thread(
                target=self._accept_loop, name="sourdough-ipc-accept", daemon=True
            )
            self._started = True
            self._accept_thread.start()
            return self.info
        except BaseException:
            self._started = False
            stack.close()
            raise

    def stop(self) -> None:
        if not self._started:
            return
        self._stop_event.set()
        with self._event_cond:
            self._event_cond.notify_all()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        with self._active_lock:
            active = list(self._active_sockets)
        for conn in active:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=5.0)
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
        # ExitStack closes in LIFO order: unlinks bound socket via pinned dir_fd
        # (verifying inode), closes listener, closes dir_fd, then releases WriterLease.
        if self._stack is not None:
            self._stack.close()
        self._stack = None
        self._writer = None
        self._socket_path = None
        self._socket_dir_fd = None
        self._socket_key = None
        self._listener = None
        self._accept_thread = None
        self._pool = None
        self._started = False

    def __enter__(self) -> CompanyDaemon:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def _accept_loop(self) -> None:
        listener = self._listener
        pool = self._pool
        if listener is None or pool is None:
            return
        while not self._stop_event.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop_event.is_set():
                    break
                continue
            if not self._in_flight_sem.acquire(blocking=False):
                try:
                    conn.settimeout(0.5)
                    write_http_response(
                        conn,
                        503,
                        body=_json_bytes({"error": "Control plane server is busy"}),
                    )
                except OSError:
                    pass
                finally:
                    conn.close()
                continue
            try:
                pool.submit(self._handle_connection, conn)
            except RuntimeError:
                self._in_flight_sem.release()
                conn.close()

    def _handle_connection(self, conn: socket.socket) -> None:
        with self._active_lock:
            self._active_sockets.add(conn)
        try:
            with closing(conn):
                deadline = monotonic() + self._request_timeout
                conn.settimeout(self._request_timeout)
                try:
                    peer = extract_peer_credentials(conn)
                except IpcRefused:
                    return
                if peer.uid != os.geteuid():
                    return
                try:
                    request = read_http_request(conn, deadline=deadline)
                except IpcRefused as error:
                    status = 413 if "exceeds maximum size" in str(error) else 400
                    self._safe_error(conn, status, str(error), deadline=deadline)
                    return
                except OSError:
                    return

                try:
                    self._dispatch(conn, request, peer, deadline=deadline)
                except (WriterRefused, StorageRefused, EngineRefused) as error:
                    self._safe_error(conn, 503, str(error), deadline=deadline)
                except (
                    FoundingRefused,
                    BindingRefused,
                    VenueRefused,
                    IpcRefused,
                ) as error:
                    self._safe_error(conn, 400, str(error), deadline=deadline)
                except sqlite3.Error as error:
                    self._safe_error(conn, 503, str(error), deadline=deadline)
                except OSError:
                    return
                except Exception:
                    self._safe_error(
                        conn, 500, "Internal control plane error", deadline=deadline
                    )
        finally:
            with self._active_lock:
                self._active_sockets.discard(conn)
            self._in_flight_sem.release()

    def _safe_error(
        self,
        conn: socket.socket,
        status: int,
        message: str,
        *,
        deadline: float | None = None,
    ) -> None:
        try:
            write_http_response(
                conn,
                status,
                body=_json_bytes({"error": message}),
                deadline=deadline,
            )
        except (OSError, IpcRefused):
            pass

    def _supported_commands(self) -> dict[str, list[str]]:
        supported: dict[str, list[str]] = {
            SUSPEND_NEW_RISK: [PAPER_RISK_COMMAND],
        }
        if self._engine_directory is not None:
            supported[RESUME_NEW_RISK] = [PAPER_RISK_COMMAND]
        return supported

    def _dispatch(
        self,
        conn: socket.socket,
        request: HttpRequest,
        peer: PeerCredentials,
        *,
        deadline: float,
    ) -> None:
        writer = self._writer
        if writer is None:
            self._safe_error(
                conn,
                503,
                "CompanyDaemon writer lease is unavailable",
                deadline=deadline,
            )
            return
        writer.verify()

        parsed = urlsplit(request.target)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=False)

        if path == "/v1/meta":
            if request.method != "GET":
                self._safe_error(conn, 405, "Method not allowed", deadline=deadline)
                return
            self._handle_meta(conn, writer, deadline=deadline)
            return

        if path == "/v1/projections/work-timeline":
            if request.method != "GET":
                self._safe_error(conn, 405, "Method not allowed", deadline=deadline)
                return
            self._handle_work_timeline(conn, writer, request, query, deadline=deadline)
            return

        if path == "/v1/events":
            if request.method != "GET":
                self._safe_error(conn, 405, "Method not allowed", deadline=deadline)
                return
            self._handle_events(conn, query, deadline=deadline)
            return

        if path == "/v1/commands":
            if request.method != "POST":
                self._safe_error(conn, 405, "Method not allowed", deadline=deadline)
                return
            self._handle_submit_command(conn, writer, request, peer, deadline=deadline)
            return

        if path.startswith("/v1/commands/"):
            if request.method != "GET":
                self._safe_error(conn, 405, "Method not allowed", deadline=deadline)
                return
            raw_command_id = path[len("/v1/commands/") :]
            if "/" in raw_command_id or not raw_command_id:
                self._safe_error(
                    conn, 400, "Invalid command identifier path", deadline=deadline
                )
                return
            command_id = unquote(raw_command_id)
            if (
                "/" in command_id
                or "\\" in command_id
                or "\x00" in command_id
                or command_id in (".", "..")
                or not command_id.strip()
            ):
                self._safe_error(
                    conn, 400, "Invalid command identifier", deadline=deadline
                )
                return
            self._handle_read_command(conn, command_id, query, deadline=deadline)
            return

        self._safe_error(conn, 404, "Endpoint not found", deadline=deadline)

    def _handle_meta(
        self, conn: socket.socket, writer: WriterLease, *, deadline: float
    ) -> None:
        with closing(open_reader(self._store)) as db, read_tx(db):
            if _identity(db) != self._company_id:
                raise StorageRefused("Company identity mismatch")
            domains = sorted(installed_domains(db))
            seq = max_event_seq(db, self._company_id)
        dev, ino = writer.store_key
        body = {
            "control_plane_version": CONTROL_PLANE_VERSION,
            "company_id": self._company_id,
            "store_key": f"{dev}:{ino}",
            "schema_version": 1,
            "active_domains": domains,
            "daemon_pid": os.getpid(),
            "event_seq": seq,
            "supported_projections": [WORK_TIMELINE_V1, WORK_TIMELINE_V2],
            "supported_commands": self._supported_commands(),
        }
        write_http_response(conn, 200, body=_json_bytes(body), deadline=deadline)

    def _handle_work_timeline(
        self,
        conn: socket.socket,
        writer: WriterLease,
        request: HttpRequest,
        query: dict[str, list[str]],
        *,
        deadline: float,
    ) -> None:
        revisions = query.get("contract_revision")
        contract_revision = revisions[0] if revisions else WORK_TIMELINE_V1
        if contract_revision not in (WORK_TIMELINE_V1, WORK_TIMELINE_V2):
            self._safe_error(
                conn,
                400,
                "Unsupported work timeline contract revision",
                deadline=deadline,
            )
            return
        # Single SQLite read_tx snapshot guarantees timeline and event_seq cannot tear.
        timeline, seq = read_local_board_timeline_with_seq(
            self._store, self._company_id
        )
        if contract_revision == WORK_TIMELINE_V2:
            dev, ino = writer.store_key
            timeline = timeline | {
                "contract_revision": WORK_TIMELINE_V2,
                "event_seq": seq,
                "store_key": f"{dev}:{ino}",
            }
            etag = f'"{timeline["revision"]}:{seq}"'
        else:
            etag = f'"{timeline["revision"]}"'
        extra = {
            "ETag": etag,
            "X-Sourdough-Event-Seq": str(seq),
            "X-Sourdough-Company-Id": self._company_id,
        }
        if _etag_matches(request.headers.get("if-none-match"), etag):
            write_http_response(
                conn, 304, body=b"", extra_headers=extra, deadline=deadline
            )
            return
        write_http_response(
            conn,
            200,
            body=_json_bytes(timeline),
            extra_headers=extra,
            deadline=deadline,
        )

    def _read_events_after(self, after_seq: int, limit: int) -> list[dict[str, object]]:
        with closing(open_reader(self._store)) as db, read_tx(db):
            if _identity(db) != self._company_id:
                raise StorageRefused("Company identity mismatch")
            rows = db.execute(
                """SELECT event_seq, company_id, domain, entity_type, entity_id,
                          event_type, recorded_at_ns
                   FROM audit_event
                   WHERE company_id = ? AND event_seq > ?
                   ORDER BY event_seq ASC
                   LIMIT ?""",
                (self._company_id, after_seq, limit),
            ).fetchall()
        return [
            {
                "event_seq": int(row[0]),
                "company_id": row[1],
                "domain": row[2],
                "entity_type": row[3],
                "entity_id": row[4],
                "event_type": row[5],
                "recorded_at_ns": int(row[6]),
            }
            for row in rows
        ]

    def _handle_events(
        self, conn: socket.socket, query: dict[str, list[str]], *, deadline: float
    ) -> None:
        try:
            after_seq = int(query.get("after_seq", ["0"])[0])
            limit = int(query.get("limit", ["100"])[0])
            wait_ms = int(query.get("wait_ms", ["0"])[0])
        except ValueError:
            self._safe_error(
                conn, 400, "Invalid event query parameters", deadline=deadline
            )
            return
        if (
            after_seq < 0
            or not 1 <= limit <= MAX_EVENTS_BATCH
            or not 0 <= wait_ms <= _MAX_WAIT_MS
        ):
            self._safe_error(
                conn, 400, "Event query parameters out of bounds", deadline=deadline
            )
            return
        events = self._read_events_after(after_seq, limit)
        if not events and wait_ms > 0:
            if not self._event_waiters_sem.acquire(blocking=False):
                self._safe_error(
                    conn,
                    503,
                    "Too many concurrent event stream waiters",
                    deadline=deadline,
                )
                return
            try:
                wait_until = min(deadline - 0.1, monotonic() + (wait_ms / 1000.0))
                while not self._stop_event.is_set():
                    remaining = wait_until - monotonic()
                    if remaining <= 0:
                        break
                    with self._event_cond:
                        self._event_cond.wait(timeout=min(remaining, 0.25))
                    events = self._read_events_after(after_seq, limit)
                    if events:
                        break
            finally:
                self._event_waiters_sem.release()
        ndjson = b"".join(_json_bytes(event) + b"\n" for event in events)
        write_http_response(
            conn,
            200,
            body=ndjson,
            content_type="application/x-ndjson",
            extra_headers={"X-Sourdough-Company-Id": self._company_id},
            deadline=deadline,
        )

    def _handle_read_command(
        self,
        conn: socket.socket,
        command_id: str,
        query: dict[str, list[str]],
        *,
        deadline: float,
    ) -> None:
        operations = query.get("operation")
        operation = operations[0] if operations else None
        try:
            status_payload = read_local_command_receipt_status(
                self._store,
                self._company_id,
                command_id,
                operation=operation,
            )
        except PaperRiskRefused as error:
            message = str(error)
            if "Invalid" in message or "Unsupported" in message:
                code = 400
            elif "unavailable" in message or "changed while" in message:
                code = 503
            elif "Corrupted" in message:
                code = 500
            else:
                code = 403
            self._safe_error(conn, code, message, deadline=deadline)
            return
        http_status = 404 if status_payload["status"] == "not_found" else 200
        write_http_response(
            conn, http_status, body=_json_bytes(status_payload), deadline=deadline
        )

    def _persisted_peer_pid(self, command_id: str, fallback: int) -> int:
        with closing(open_reader(self._store)) as db, read_tx(db):
            row = db.execute(
                """SELECT peer_pid FROM command_receipt
                   WHERE company_id = ? AND command_id = ?""",
                (self._company_id, command_id),
            ).fetchone()
        if row is not None and type(row[0]) is int and row[0] > 0:
            return row[0]
        return fallback

    def _handle_submit_command(
        self,
        conn: socket.socket,
        writer: WriterLease,
        request: HttpRequest,
        peer: PeerCredentials,
        *,
        deadline: float,
    ) -> None:
        idem_key = request.headers.get("idempotency-key", "").strip()
        if not idem_key or len(idem_key) > 128:
            self._safe_error(
                conn, 400, "Idempotency-Key header is required", deadline=deadline
            )
            return
        try:
            body = json.loads(request.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._safe_error(conn, 400, "Expected JSON command body", deadline=deadline)
            return
        if not isinstance(body, dict):
            self._safe_error(
                conn, 400, "Command body must be a JSON object", deadline=deadline
            )
            return

        body_command_id = body.get("command_id")
        if body_command_id is not None and body_command_id != idem_key:
            self._safe_error(
                conn,
                400,
                "Body command_id must match Idempotency-Key header",
                deadline=deadline,
            )
            return

        company_id = body.get("company_id")
        if company_id != self._company_id:
            self._safe_error(
                conn,
                403,
                "Command company_id does not match daemon scope",
                deadline=deadline,
            )
            return

        command_type = body.get("command_type")
        contract_version = body.get("contract_version")
        supported = self._supported_commands()
        if (
            not isinstance(command_type, str)
            or not isinstance(contract_version, str)
            or contract_version not in supported.get(command_type, ())
        ):
            self._safe_error(
                conn,
                422,
                "Unsupported command type or contract version",
                deadline=deadline,
            )
            return

        preconditions = body.get("preconditions")
        payload = body.get("payload")
        if not isinstance(preconditions, dict) or not isinstance(payload, dict):
            self._safe_error(
                conn,
                400,
                "Command requires object preconditions and payload",
                deadline=deadline,
            )
            return
        if set(preconditions.keys()) != _ALLOWED_PRECONDITION_KEYS:
            self._safe_error(
                conn,
                400,
                "Command preconditions contain unsupported or missing fields",
                deadline=deadline,
            )
            return

        charter_revision = preconditions.get("charter_revision")
        expected_revision = preconditions.get("expected_revision")
        reason = payload.get("reason")
        try:
            command = PaperSuspension(
                request_id=idem_key,
                charter_revision=charter_revision,
                expected_revision=expected_revision,
                reason=reason,
                contract_revision=contract_version,
            )
        except PaperRiskRefused as error:
            self._safe_error(conn, 400, str(error), deadline=deadline)
            return

        try:
            if command_type == SUSPEND_NEW_RISK:
                with self._write_lock:
                    writer.verify()
                    receipt = suspend_paper_risk(
                        writer,
                        command,
                        self._authorize_board,
                        peer_pid=peer.pid,
                    )
            else:
                # resume_managed_paper_risk runs external venue observation lock-free
                # and uses BEGIN IMMEDIATE + busy_timeout inside SQLite for serialization.
                assert self._engine_directory is not None
                writer.verify()
                receipt = resume_managed_paper_risk(
                    writer,
                    command,
                    self._engine_directory,
                    self._authorize_board,
                    peer_pid=peer.pid,
                )
        except PaperRiskRefused as error:
            message = str(error)
            if "already in use" in message:
                self._safe_error(conn, 409, message, deadline=deadline)
            elif (
                "grant is required" in message
                or "not authorized" in message
                or "authorization changed" in message
                or "identity mismatch" in message
            ):
                self._safe_error(conn, 403, message, deadline=deadline)
            else:
                self._safe_error(conn, 400, message, deadline=deadline)
            return

        with self._event_cond:
            self._event_cond.notify_all()

        persisted_pid = self._persisted_peer_pid(receipt.request_id, peer.pid)
        applied = receipt.disposition == "applied"
        response_payload = {
            "status": "found",
            "company_id": self._company_id,
            "command_id": receipt.request_id,
            "command_type": receipt.operation,
            "state": "applied" if applied else "denied",
            "disposition": receipt.disposition,
            "denial_code": None if applied else "conflicting_revision",
            "effective_revision": receipt.effective_revision,
            "recorded_at_ns": receipt.recorded_at_ns,
            "peer_pid": persisted_pid,
        }
        write_http_response(
            conn, 200, body=_json_bytes(response_payload), deadline=deadline
        )
