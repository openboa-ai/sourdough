"""Authenticated loopback HTTP-to-UDS bridge and static SPA host for Board Client.

Binds strictly on 127.0.0.1, enforces Host/Origin/Sec-Fetch-Site DNS-rebinding
and CSRF defense, separates browser HttpOnly SameSite=Strict cookie secrets from
Bearer API tokens, rejects ?token= URLs and Transfer-Encoding smuggling, proxies
/api/v1/* strictly over ControlPlaneClient UDS, and manages dir_fd-pinned atomic
(company_id, principal_id)-scoped board-pending files.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import pwd
import secrets
import stat
import threading
from dataclasses import dataclass
from hashlib import sha256
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

from .client import ControlPlaneClient
from .protocol import MAX_BODY_BYTES, IpcRefused

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _csp_header(style_nonce: str | None = None) -> str:
    style_src = (
        f"style-src 'self' 'nonce-{style_nonce}'" if style_nonce else "style-src 'self'"
    )
    return (
        "default-src 'self'; "
        "connect-src 'self'; "
        "font-src 'self'; "
        f"{style_src}; "
        "style-src-attr 'unsafe-inline'; "
        "script-src 'self'; "
        "img-src 'self' data:; "
        "base-uri 'none'; "
        "frame-ancestors 'none'; "
        "object-src 'none'"
    )


@dataclass(frozen=True)
class WebBridgeInfo:
    host: str
    port: int
    url: str
    cookie_name: str
    session_token: str
    socket_path: str


def default_pending_dir() -> Path:
    home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    return home / "Library/Caches/Sourdough/board-pending"


def default_static_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "apps" / "client" / "dist"


def _open_existing_private_dir(path: Path) -> int | None:
    """Open an existing 0o700 user-owned directory without following symlinks."""
    path = path.absolute()
    try:
        dir_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise IpcRefused("board-pending directory failed verification") from error
    try:
        info = os.fstat(dir_fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or (info.st_mode & 0o077) != 0
        ):
            raise IpcRefused("board-pending directory must be 0700 user-owned")
        return dir_fd
    except BaseException:
        os.close(dir_fd)
        raise


def _ensure_private_dir(path: Path) -> int:
    """Create (if absent) and open a private 0o700 directory without symlink traversal."""
    path = path.absolute()
    existing = _open_existing_private_dir(path)
    if existing is not None:
        return existing
    if os.path.lexists(path):
        raise IpcRefused("board-pending path is not a private directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    dir_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _O_NOFOLLOW)
    try:
        info = os.fstat(dir_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise IpcRefused("board-pending directory must be user-owned")
        if (info.st_mode & 0o077) != 0:
            os.fchmod(dir_fd, 0o700)
        return dir_fd
    except BaseException:
        os.close(dir_fd)
        raise


def _pending_target(
    base_dir: Path, company_id: str, principal_id: str
) -> tuple[Path, str]:
    co_hash = sha256(company_id.encode("utf-8")).hexdigest()[:24]
    pr_hash = sha256(principal_id.encode("utf-8")).hexdigest()[:24]
    return base_dir / co_hash, f"{pr_hash}.json"


def write_pending_intent(
    base_dir: Path,
    *,
    company_id: str,
    principal_id: str,
    intent: dict[str, Any],
) -> None:
    """Atomically write (company_id, principal_id) board-pending file via dir_fd + fsync."""
    base_fd = _ensure_private_dir(base_dir)
    os.close(base_fd)
    parent, filename = _pending_target(base_dir, company_id, principal_id)
    dir_fd = _ensure_private_dir(parent)
    try:
        tmp_name = f".tmp-{uuid4().hex}-{filename}"
        data = json.dumps(intent, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_BODY_BYTES:
            raise IpcRefused("Pending intent exceeds maximum allowed size")
        fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
            0o600,
            dir_fd=dir_fd,
        )
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            written = 0
            while written < len(view):
                written += os.write(fd, view[written:])
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except OSError:
                pass
            raise
        os.close(fd)
        try:
            existing = os.lstat(filename, dir_fd=dir_fd)
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                os.unlink(tmp_name, dir_fd=dir_fd)
                raise IpcRefused("Symlinks are forbidden inside board-pending storage")
        except FileNotFoundError:
            pass
        os.replace(tmp_name, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def read_pending_intent(
    base_dir: Path,
    *,
    company_id: str,
    principal_id: str,
) -> dict[str, Any] | None:
    """Read (company_id, principal_id) board-pending JSON safely via dir_fd + O_NOFOLLOW."""
    parent, filename = _pending_target(base_dir, company_id, principal_id)
    dir_fd = _open_existing_private_dir(parent)
    if dir_fd is None:
        return None
    try:
        try:
            info = os.lstat(filename, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or (info.st_mode & 0o077) != 0
        ):
            raise IpcRefused("board-pending file failed privacy verification")
        fd = os.open(filename, os.O_RDONLY | _O_NOFOLLOW, dir_fd=dir_fd)
        try:
            chunks: list[bytes] = []
            total = 0
            while True:
                piece = os.read(fd, 16384)
                if not piece:
                    break
                total += len(piece)
                if total > MAX_BODY_BYTES:
                    raise IpcRefused("board-pending file exceeds maximum size")
                chunks.append(piece)
            raw = b"".join(chunks)
        finally:
            os.close(fd)
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise IpcRefused("board-pending file is corrupted")
        return parsed
    finally:
        os.close(dir_fd)


def delete_pending_intent(
    base_dir: Path,
    *,
    company_id: str,
    principal_id: str,
) -> bool:
    """Delete (company_id, principal_id) board-pending file via dir_fd and fsync."""
    parent, filename = _pending_target(base_dir, company_id, principal_id)
    dir_fd = _open_existing_private_dir(parent)
    if dir_fd is None:
        return False
    try:
        try:
            info = os.lstat(filename, dir_fd=dir_fd)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise IpcRefused("Refusing to remove non-regular board-pending path")
        os.unlink(filename, dir_fd=dir_fd)
        os.fsync(dir_fd)
        return True
    finally:
        os.close(dir_fd)


class _LoopbackHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class ClientWebBridge:
    """Loopback HTTP-to-UDS bridge serving the React 19 Yogurt Client UI."""

    def __init__(
        self,
        socket_path: Path,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        static_dir: Path | None = None,
        pending_dir: Path | None = None,
        principal: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if host != "127.0.0.1":
            raise IpcRefused("client-web bridge must bind strictly to 127.0.0.1")
        if type(port) is not int or not 0 <= port <= 65535:
            raise IpcRefused("Invalid loopback TCP port")
        self._socket_path = socket_path.absolute()
        self._host = host
        self._requested_port = port
        self._static_dir = (static_dir or default_static_dir()).resolve(strict=False)
        self._pending_dir = (pending_dir or default_pending_dir()).absolute()
        self._principal = principal
        self._timeout_seconds = timeout_seconds
        self._client = ControlPlaneClient(
            self._socket_path, timeout_seconds=timeout_seconds
        )
        # Keep Bearer API secret strictly distinct from browser session cookie secret
        # so a leaked host-scoped cookie can never authenticate Bearer API calls.
        self._bearer_token = secrets.token_urlsafe(32)
        self._cookie_secret = secrets.token_urlsafe(32)
        self._instance_id = secrets.token_hex(6)
        self._server: _LoopbackHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._bound_port: int | None = None
        self._cookie_name: str | None = None

    @property
    def info(self) -> WebBridgeInfo:
        if self._bound_port is None or self._cookie_name is None:
            raise IpcRefused("ClientWebBridge is not running")
        return WebBridgeInfo(
            host=self._host,
            port=self._bound_port,
            url=f"http://{self._host}:{self._bound_port}/",
            cookie_name=self._cookie_name,
            session_token=self._bearer_token,
            socket_path=str(self._socket_path),
        )

    def start(self) -> WebBridgeInfo:
        if self._server is not None:
            return self.info
        bridge = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send_bytes(
                self,
                status: int,
                body: bytes,
                *,
                content_type: str = "application/json",
                extra_headers: dict[str, str] | None = None,
                no_store: bool = True,
                style_nonce: str | None = None,
            ) -> None:
                self.close_connection = True
                self.send_response(status)
                self.send_header("Connection", "close")
                self.send_header("Content-Length", str(len(body)))
                if status != 304:
                    self.send_header("Content-Type", content_type)
                self.send_header("Content-Security-Policy", _csp_header(style_nonce))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                if no_store:
                    self.send_header("Cache-Control", "no-store")
                if extra_headers:
                    for key, value in extra_headers.items():
                        self.send_header(key, value)
                self.end_headers()
                if body and status != 304:
                    self.wfile.write(body)

            def _send_json(
                self,
                status: int,
                payload: Any,
                *,
                extra_headers: dict[str, str] | None = None,
            ) -> None:
                body = json.dumps(
                    payload, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                self._send_bytes(
                    status,
                    body,
                    content_type="application/json",
                    extra_headers=extra_headers,
                )

            def _check_transport_guards(self) -> tuple[bool, str]:
                if self.headers.get("Transfer-Encoding") is not None:
                    self._send_json(
                        400, {"error": "Transfer-Encoding is not supported"}
                    )
                    return False, ""
                # 1. Reject query-string tokens case-insensitively everywhere.
                parsed = urlsplit(self.path)
                qs = parse_qs(parsed.query, keep_blank_values=True)
                if any(k.lower() == "token" for k in qs):
                    self._send_json(400, {"error": "Query-string tokens are forbidden"})
                    return False, ""
                # 2. Host header pinning against DNS rebinding.
                host = (self.headers.get("Host") or "").strip().lower()
                port = bridge._bound_port
                allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
                if host not in allowed_hosts:
                    self._send_json(403, {"error": "Host header rejected"})
                    return False, ""
                # 3. Origin check pinned strictly to matching scheme + Host when present.
                origin = (self.headers.get("Origin") or "").strip().lower()
                if origin and origin != f"http://{host}":
                    self._send_json(403, {"error": "Cross-origin request rejected"})
                    return False, ""
                # 4. Sec-Fetch-Site check when present.
                fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
                if fetch_site and fetch_site not in ("same-origin", "none"):
                    self._send_json(403, {"error": "Cross-site fetch rejected"})
                    return False, ""
                return True, host

            def _auth_kind(self) -> str | None:
                auth = (self.headers.get("Authorization") or "").strip()
                if auth.startswith("Bearer "):
                    candidate = auth[len("Bearer ") :].strip()
                    if hmac.compare_digest(candidate, bridge._bearer_token):
                        return "bearer"
                cookie_header = self.headers.get("Cookie")
                if cookie_header and bridge._cookie_name:
                    jar = SimpleCookie()
                    try:
                        jar.load(cookie_header)
                    except Exception:
                        return None
                    morsel = jar.get(bridge._cookie_name)
                    if morsel is not None and hmac.compare_digest(
                        morsel.value, bridge._cookie_secret
                    ):
                        return "cookie"
                return None

            def _read_body(self) -> bytes | None:
                raw_len = self.headers.get("Content-Length")
                if raw_len is None:
                    return b""
                if not raw_len.isdigit():
                    self._send_json(400, {"error": "Invalid Content-Length"})
                    return None
                length = int(raw_len)
                if length > MAX_BODY_BYTES:
                    self._send_json(413, {"error": "Request body too large"})
                    return None
                return self.rfile.read(length) if length > 0 else b""

            def _resolve_scope(self) -> tuple[str, str]:
                meta = bridge._client.meta(principal=bridge._principal)
                company_id = str(meta["company_id"])
                access = meta.get("board_access") or {}
                principal_id = (
                    bridge._principal or access.get("principal_id") or "unbound"
                )
                return company_id, str(principal_id)

            def _handle_pending(self, method: str) -> None:
                try:
                    company_id, principal_id = self._resolve_scope()
                except IpcRefused as error:
                    self._send_json(503, {"error": str(error)})
                    return

                if method == "GET":
                    try:
                        intent = read_pending_intent(
                            bridge._pending_dir,
                            company_id=company_id,
                            principal_id=principal_id,
                        )
                    except IpcRefused as error:
                        self._send_json(500, {"error": str(error)})
                        return
                    if intent is None:
                        self._send_json(200, {"pending": None})
                        return
                    cmd_id = intent.get("command_id")
                    cmd_type = intent.get("command_type")
                    receipt: dict[str, Any] | None = None
                    if isinstance(cmd_id, str) and cmd_id:
                        try:
                            receipt = bridge._client.command_receipt(
                                cmd_id,
                                operation=cmd_type
                                if isinstance(cmd_type, str)
                                else None,
                                principal=bridge._principal,
                            )
                        except IpcRefused as error:
                            if "HTTP 403" in str(error):
                                delete_pending_intent(
                                    bridge._pending_dir,
                                    company_id=company_id,
                                    principal_id=principal_id,
                                )
                                self._send_json(
                                    200,
                                    {
                                        "pending": None,
                                        "quarantined_stale": True,
                                        "notice": "Stale or unowned pending request was quarantined after 403.",
                                    },
                                )
                                return
                            receipt = None
                    self._send_json(
                        200,
                        {
                            "pending": intent,
                            "receipt": receipt,
                        },
                    )
                    return

                if method in ("POST", "PUT"):
                    body = self._read_body()
                    if body is None:
                        return
                    try:
                        parsed = json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        self._send_json(400, {"error": "Expected JSON pending intent"})
                        return
                    raw_cid = (
                        parsed.get("command_id") if isinstance(parsed, dict) else None
                    )
                    if (
                        not isinstance(parsed, dict)
                        or not isinstance(raw_cid, str)
                        or not raw_cid.strip()
                        or len(raw_cid) > 128
                    ):
                        self._send_json(
                            400,
                            {
                                "error": "Pending intent requires valid string command_id"
                            },
                        )
                        return
                    parsed = parsed | {
                        "company_id": company_id,
                        "principal_id": principal_id,
                    }
                    try:
                        write_pending_intent(
                            bridge._pending_dir,
                            company_id=company_id,
                            principal_id=principal_id,
                            intent=parsed,
                        )
                    except IpcRefused as error:
                        self._send_json(500, {"error": str(error)})
                        return
                    self._send_json(200, {"stored": True, "pending": parsed})
                    return

                if method == "DELETE":
                    try:
                        cleared = delete_pending_intent(
                            bridge._pending_dir,
                            company_id=company_id,
                            principal_id=principal_id,
                        )
                    except IpcRefused as error:
                        self._send_json(500, {"error": str(error)})
                        return
                    self._send_json(200, {"cleared": cleared})
                    return

                self._send_json(405, {"error": "Method not allowed"})

            def _proxy_uds(self, method: str) -> None:
                parsed = urlsplit(self.path)
                uds_path = parsed.path[len("/api") :]
                if not uds_path.startswith("/v1/"):
                    self._send_json(404, {"error": "API endpoint not found"})
                    return
                target = f"{uds_path}?{parsed.query}" if parsed.query else uds_path
                body = b""
                if method == "POST":
                    raw = self._read_body()
                    if raw is None:
                        return
                    body = raw
                fwd_headers: dict[str, str] = {}
                for h_in, h_out in (
                    ("If-None-Match", "If-None-Match"),
                    ("Idempotency-Key", "Idempotency-Key"),
                    ("Content-Type", "Content-Type"),
                ):
                    val = self.headers.get(h_in)
                    if val:
                        fwd_headers[h_out] = val
                if bridge._principal:
                    fwd_headers["X-Sourdough-Principal"] = bridge._principal
                try:
                    resp = bridge._client.raw_request(
                        method, target, headers=fwd_headers or None, body=body
                    )
                except IpcRefused as error:
                    msg = str(error)
                    status = 403 if "HTTP 403" in msg else 503
                    self._send_json(status, {"error": msg})
                    return

                out_headers: dict[str, str] = {}
                for h in ("etag", "x-sourdough-event-seq", "x-sourdough-company-id"):
                    if h in resp.headers:
                        out_headers[h] = resp.headers[h]
                ctype = resp.headers.get("content-type", "application/json")
                self._send_bytes(
                    resp.status,
                    resp.body,
                    content_type=ctype,
                    extra_headers=out_headers,
                )

            def _safe_static_file(self, static_root: Path, rel: str) -> Path | None:
                if "\x00" in rel:
                    return None
                parts = [p for p in Path(rel).parts if p not in ("", ".")]
                if any(p == ".." or "/" in p or "\\" in p for p in parts):
                    return None
                cur = static_root
                for part in parts:
                    cur = cur / part
                    if not os.path.lexists(cur):
                        return None
                    if stat.S_ISLNK(os.lstat(cur).st_mode):
                        return None
                if not cur.is_file():
                    return None
                return cur

            def _serve_static(self) -> None:
                parsed = urlsplit(self.path)
                raw_decoded = unquote(parsed.path)
                if "\x00" in raw_decoded:
                    self._send_json(400, {"error": "Invalid path"})
                    return
                rel = raw_decoded.lstrip("/")
                static_root = bridge._static_dir
                set_cookie = (
                    f"{bridge._cookie_name}={bridge._cookie_secret}; "
                    "HttpOnly; SameSite=Strict; Path=/"
                )
                if not static_root.is_dir():
                    html = (
                        "<!doctype html><html><head><meta charset='utf-8'>"
                        "<title>Sourdough Board Client</title></head>"
                        "<body><h1>Sourdough Board Client</h1>"
                        "<p>Build apps/client first (cd apps/client && npm ci && npm run build).</p>"
                        "</body></html>"
                    ).encode("utf-8")
                    self._send_bytes(
                        200,
                        html,
                        content_type="text/html; charset=utf-8",
                        extra_headers={"Set-Cookie": set_cookie},
                    )
                    return

                if ".." in Path(rel).parts:
                    self._send_json(404, {"error": "Not found"})
                    return

                candidate = self._safe_static_file(static_root, rel or "index.html")
                if candidate is None:
                    if "." in Path(rel).name:
                        self._send_json(404, {"error": "Asset not found"})
                        return
                    candidate = self._safe_static_file(static_root, "index.html")
                    if candidate is None:
                        self._send_json(404, {"error": "Client bundle not found"})
                        return

                try:
                    st = os.lstat(candidate)
                    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                        self._send_json(404, {"error": "Not found"})
                        return
                    fd = os.open(candidate, os.O_RDONLY | _O_NOFOLLOW)
                    try:
                        chunks: list[bytes] = []
                        total = 0
                        while True:
                            piece = os.read(fd, 65536)
                            if not piece:
                                break
                            total += len(piece)
                            if total > 16 * 1024 * 1024:
                                self._send_json(
                                    413, {"error": "Static asset too large"}
                                )
                                return
                            chunks.append(piece)
                        data = b"".join(chunks)
                    finally:
                        os.close(fd)
                except OSError:
                    self._send_json(404, {"error": "Not found"})
                    return

                guessed, _ = mimetypes.guess_type(str(candidate))
                ctype = guessed or "application/octet-stream"
                if ctype.startswith("text/") or ctype == "application/javascript":
                    ctype = f"{ctype}; charset=utf-8"
                is_html = candidate.name == "index.html"
                nonce: str | None = None
                if is_html:
                    nonce = secrets.token_urlsafe(16)
                    html_text = data.decode("utf-8", errors="replace")
                    meta_tag = f'<meta property="csp-nonce" content="{nonce}" />'
                    if "</head>" in html_text:
                        html_text = html_text.replace(
                            "</head>", f"  {meta_tag}\n  </head>", 1
                        )
                    data = html_text.encode("utf-8")
                extra = {"Set-Cookie": set_cookie} if is_html else None
                self._send_bytes(
                    200,
                    data,
                    content_type=ctype,
                    extra_headers=extra,
                    no_store=is_html,
                    style_nonce=nonce,
                )

            def _route(self, method: str) -> None:
                ok, host = self._check_transport_guards()
                if not ok:
                    return
                parsed = urlsplit(self.path)
                if parsed.path.startswith("/api/"):
                    auth_kind = self._auth_kind()
                    if auth_kind is None:
                        self._send_json(
                            401, {"error": "Session authentication required"}
                        )
                        return
                    # Cookie-authenticated mutating requests MUST carry matching Origin
                    # or Sec-Fetch-Site: same-origin to defeat headerless CSRF / cross-port replay.
                    if auth_kind == "cookie" and method in ("POST", "PUT", "DELETE"):
                        origin = (self.headers.get("Origin") or "").strip().lower()
                        fetch_site = (
                            (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
                        )
                        if origin != f"http://{host}" and fetch_site != "same-origin":
                            self._send_json(
                                403,
                                {
                                    "error": "Cookie-authenticated mutation requires matching same-origin header"
                                },
                            )
                            return
                    if parsed.path == "/api/local/pending":
                        self._handle_pending(method)
                        return
                    if method not in ("GET", "POST"):
                        self._send_json(405, {"error": "Method not allowed"})
                        return
                    self._proxy_uds(method)
                    return
                if method != "GET":
                    self._send_json(405, {"error": "Method not allowed"})
                    return
                self._serve_static()

            def do_GET(self) -> None:
                self._route("GET")

            def do_POST(self) -> None:
                self._route("POST")

            def do_PUT(self) -> None:
                self._route("PUT")

            def do_DELETE(self) -> None:
                self._route("DELETE")

        server = _LoopbackHTTPServer((self._host, self._requested_port), _Handler)
        self._bound_port = int(server.server_address[1])
        self._cookie_name = f"sd_sess_{self._bound_port}_{self._instance_id}"
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="sourdough-client-web",
            daemon=True,
        )
        self._thread.start()
        return self.info

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._bound_port = None
        self._cookie_name = None

    def __enter__(self) -> ClientWebBridge:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()
