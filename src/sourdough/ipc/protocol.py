"""Bounded HTTP/1.1 framing and macOS kernel peer authentication over UDS."""

from __future__ import annotations

import math
import os
import pwd
import socket
import stat
import struct
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import monotonic

CONTROL_PLANE_VERSION = "control-plane-v1"
WORK_TIMELINE_V1 = "work-timeline-v1"
WORK_TIMELINE_V2 = "work-timeline-v2"

MAX_REQUEST_LINE_BYTES = 4096
MAX_HEADER_COUNT = 64
MAX_HEADER_BYTES = 16 * 1024
MAX_BODY_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_EVENTS_BATCH = 500
SOCKET_PATH_LIMIT = 103

_SOL_LOCAL = 0
_LOCAL_PEERCRED = 0x001
_LOCAL_PEERPID = 0x002
_XUCRED_SIZE = 76
_XUCRED_MAX_NGROUPS = 16
_SO_PEERCRED = 17

_STATUS_PHRASES: dict[int, str] = {
    200: "OK",
    304: "Not Modified",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Payload Too Large",
    422: "Unprocessable Entity",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


class IpcRefused(Exception):
    """The local UDS control plane rejected a socket, peer, frame, or request."""


@dataclass(frozen=True)
class PeerCredentials:
    uid: int
    gid: int
    pid: int


@dataclass(frozen=True)
class HttpRequest:
    method: str
    target: str
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


def _is_private(info: os.stat_result) -> bool:
    return info.st_uid == os.geteuid() and (info.st_mode & 0o077) == 0


def _has_ctl_or_newline(text: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)


def _valid_header_name(name: str) -> bool:
    if not name:
        return False
    for ch in name:
        code = ord(ch)
        if code <= 0x20 or code >= 0x7F or ch in '()<>@,;:\\"/[]?={}':
            return False
    return True


def default_socket_dir() -> Path:
    home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    return home / "Library/Caches/Sourdough/s"


def default_socket_path(
    company_id: str,
    store_key: tuple[int, int],
    *,
    socket_dir: Path | None = None,
) -> Path:
    if not isinstance(company_id, str) or not company_id.strip():
        raise IpcRefused("Company identity is required to derive socket path")
    directory = (socket_dir or default_socket_dir()).absolute()
    digest = sha256(
        f"{company_id}:{store_key[0]}:{store_key[1]}".encode("utf-8")
    ).hexdigest()[:16]
    candidate = directory / f"{digest}.sock"
    validate_socket_path_length(candidate)
    return candidate


def validate_socket_path_length(socket_path: Path) -> None:
    try:
        encoded = os.fsencode(socket_path)
    except (TypeError, ValueError) as error:
        raise IpcRefused("Invalid Unix socket path") from error
    if b"\x00" in encoded:
        raise IpcRefused("Unix socket path cannot contain NUL bytes")
    if len(encoded) == 0 or len(encoded) > SOCKET_PATH_LIMIT:
        raise IpcRefused(
            f"Unix socket path exceeds {SOCKET_PATH_LIMIT}-byte platform limit"
        )


def verify_private_socket_dir(directory: Path) -> int:
    """Create (if absent) and open a private 0o700 socket directory without symlink traversal."""
    directory = directory.absolute()
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise IpcRefused("Control plane socket directory is unavailable") from error
    try:
        info = os.fstat(dir_fd)
        if not stat.S_ISDIR(info.st_mode) or not _is_private(info):
            raise IpcRefused("Control plane socket directory must be 0700 user-owned")
        return dir_fd
    except BaseException:
        os.close(dir_fd)
        raise


def verify_socket_path(socket_path: Path) -> os.stat_result:
    """Validate that a client target socket path is a private 0600 user-owned Unix socket."""
    socket_path = socket_path.absolute()
    validate_socket_path_length(socket_path)
    try:
        dir_fd = os.open(
            socket_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
    except OSError as error:
        raise IpcRefused("Control plane socket directory is unavailable") from error
    try:
        parent_info = os.fstat(dir_fd)
        if not stat.S_ISDIR(parent_info.st_mode) or not _is_private(parent_info):
            raise IpcRefused("Control plane socket directory is not private")
        info = os.lstat(socket_path.name, dir_fd=dir_fd)
    except OSError as error:
        raise IpcRefused("Control plane socket is unavailable") from error
    finally:
        os.close(dir_fd)
    if not stat.S_ISSOCK(info.st_mode) or not _is_private(info):
        raise IpcRefused("Control plane target is not a private user-owned Unix socket")
    return info


def extract_peer_credentials(connection: socket.socket) -> PeerCredentials:
    """Read kernel-authenticated peer UID, GID, and PID from a connected Unix socket."""
    try:
        if sys.platform == "darwin":
            raw_cred = connection.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, _XUCRED_SIZE)
            if len(raw_cred) < 16:
                raise IpcRefused("Incomplete Darwin peer credential structure")
            version, uid, ngroups = struct.unpack_from("=IIh", raw_cred, 0)
            if version != 0 or not 1 <= ngroups <= _XUCRED_MAX_NGROUPS:
                raise IpcRefused("Unsupported Darwin peer credential version")
            (gid,) = struct.unpack_from("=I", raw_cred, 12)
            raw_pid = connection.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, 4)
            if len(raw_pid) != 4:
                raise IpcRefused("Incomplete Darwin peer PID structure")
            (pid,) = struct.unpack("=i", raw_pid)
        elif sys.platform.startswith("linux"):
            raw = connection.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, 12)
            if len(raw) != 12:
                raise IpcRefused("Incomplete peer credential structure")
            pid, uid, gid = struct.unpack("=iII", raw)
        else:
            raise IpcRefused(
                f"Kernel Unix socket peer credentials unsupported on {sys.platform}"
            )
    except OSError as error:
        raise IpcRefused("Failed to read kernel peer credentials") from error
    if pid <= 0 or uid < 0 or gid < 0:
        raise IpcRefused("Kernel reported invalid peer credentials")
    return PeerCredentials(uid=uid, gid=gid, pid=pid)


def _apply_deadline(connection: socket.socket, deadline: float | None) -> None:
    if deadline is None:
        return
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise IpcRefused("Control plane request deadline exceeded")
    connection.settimeout(remaining)


def _recv_exact(
    connection: socket.socket,
    count: int,
    initial: bytes,
    *,
    deadline: float | None = None,
) -> bytes:
    chunks = [initial] if initial else []
    received = len(initial)
    while received < count:
        _apply_deadline(connection, deadline)
        chunk = connection.recv(min(16384, count - received))
        if not chunk:
            raise IpcRefused("Unexpected EOF while reading HTTP body")
        chunks.append(chunk)
        received += len(chunk)
    return b"".join(chunks)


def _read_head(
    connection: socket.socket, *, deadline: float | None = None
) -> tuple[bytes, bytes]:
    buffer = bytearray()
    max_head = MAX_REQUEST_LINE_BYTES + MAX_HEADER_BYTES
    while True:
        marker = buffer.find(b"\r\n\r\n")
        if marker != -1:
            if marker > max_head:
                raise IpcRefused("HTTP headers exceed maximum allowed size")
            return bytes(buffer[:marker]), bytes(buffer[marker + 4 :])
        if len(buffer) > max_head + 4:
            raise IpcRefused("HTTP headers exceed maximum allowed size")
        _apply_deadline(connection, deadline)
        chunk = connection.recv(4096)
        if not chunk:
            raise IpcRefused("Connection closed before HTTP headers completed")
        buffer.extend(chunk)


def read_http_request(
    connection: socket.socket, *, deadline: float | None = None
) -> HttpRequest:
    head, rest = _read_head(connection, deadline=deadline)
    lines = head.split(b"\r\n")
    if not lines or len(lines[0]) > MAX_REQUEST_LINE_BYTES:
        raise IpcRefused("Malformed HTTP request line")
    header_bytes = sum(len(line) + 2 for line in lines[1:])
    if header_bytes > MAX_HEADER_BYTES:
        raise IpcRefused("HTTP headers exceed maximum allowed size")
    try:
        request_line = lines[0].decode("ascii")
    except UnicodeDecodeError as error:
        raise IpcRefused("Non-ASCII HTTP request line") from error
    if _has_ctl_or_newline(request_line):
        raise IpcRefused("Control characters are not allowed in HTTP request line")
    parts = request_line.split(" ")
    if len(parts) != 3 or parts[2] != "HTTP/1.1":
        raise IpcRefused("Expected HTTP/1.1 request line")
    method, target, _ = parts
    if (
        method not in ("GET", "POST")
        or not target.startswith("/")
        or target.startswith("//")
        or "#" in target
    ):
        raise IpcRefused("Unsupported HTTP method or target")
    if len(lines) - 1 > MAX_HEADER_COUNT:
        raise IpcRefused("Too many HTTP headers")

    headers: dict[str, str] = {}
    content_length: int | None = None
    for raw_line in lines[1:]:
        if b":" not in raw_line:
            raise IpcRefused("Malformed HTTP header line")
        raw_name, raw_value = raw_line.split(b":", 1)
        try:
            name_raw = raw_name.decode("ascii")
            value = raw_value.decode("ascii").strip(" \t")
        except UnicodeDecodeError as error:
            raise IpcRefused("Non-ASCII HTTP header") from error
        if not _valid_header_name(name_raw) or _has_ctl_or_newline(value):
            raise IpcRefused("Invalid HTTP header name or value")
        name = name_raw.lower()
        if name == "transfer-encoding":
            raise IpcRefused("Chunked transfer encoding is not permitted")
        if name == "content-length":
            if content_length is not None:
                raise IpcRefused("Duplicate Content-Length header")
            if not value.isdigit():
                raise IpcRefused("Invalid Content-Length header")
            content_length = int(value)
            if content_length > MAX_BODY_BYTES:
                raise IpcRefused("HTTP request body exceeds maximum size")
        if name == "idempotency-key" and name in headers:
            raise IpcRefused("Duplicate Idempotency-Key header")
        if name in headers:
            headers[name] = f"{headers[name]}, {value}"
        else:
            headers[name] = value

    if content_length is None:
        if rest:
            raise IpcRefused("Unexpected body bytes without Content-Length")
        body = b""
    else:
        if len(rest) > content_length:
            raise IpcRefused("Request body exceeds declared Content-Length")
        body = _recv_exact(connection, content_length, rest, deadline=deadline)
    return HttpRequest(method=method, target=target, headers=headers, body=body)


def write_http_response(
    connection: socket.socket,
    status: int,
    *,
    body: bytes = b"",
    content_type: str = "application/json",
    extra_headers: dict[str, str] | None = None,
    deadline: float | None = None,
) -> None:
    phrase = _STATUS_PHRASES.get(status, "Error")
    header_lines = [
        f"HTTP/1.1 {status} {phrase}",
        f"Content-Length: {len(body)}",
        "Connection: close",
        "Cache-Control: no-store",
    ]
    if body or status != 304:
        header_lines.append(f"Content-Type: {content_type}")
    if extra_headers:
        for key, value in extra_headers.items():
            if not _valid_header_name(key) or _has_ctl_or_newline(value):
                raise IpcRefused("Invalid response header characters")
            header_lines.append(f"{key}: {value}")
    try:
        head_bytes = ("\r\n".join(header_lines) + "\r\n\r\n").encode("ascii")
    except UnicodeEncodeError as error:
        raise IpcRefused("Non-ASCII response header") from error
    _apply_deadline(connection, deadline)
    connection.sendall(head_bytes + body)


def validate_timeout_seconds(timeout_seconds: float) -> float:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise IpcRefused("Control plane timeout must be a finite positive number")
    return float(timeout_seconds)


def send_http_request(
    connection: socket.socket,
    method: str,
    target: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    deadline: float | None = None,
) -> HttpResponse:
    if (
        method not in ("GET", "POST")
        or not target.startswith("/")
        or target.startswith("//")
        or "#" in target
        or any(ch.isspace() for ch in target)
        or _has_ctl_or_newline(target)
    ):
        raise IpcRefused("Invalid client HTTP request")
    header_lines = [
        f"{method} {target} HTTP/1.1",
        "Host: localhost",
        "Connection: close",
    ]
    if body or method == "POST":
        header_lines.append(f"Content-Length: {len(body)}")
    if headers:
        for key, value in headers.items():
            if not _valid_header_name(key) or _has_ctl_or_newline(value):
                raise IpcRefused("Invalid request header characters")
            header_lines.append(f"{key}: {value}")
    try:
        head_bytes = ("\r\n".join(header_lines) + "\r\n\r\n").encode("ascii")
    except UnicodeEncodeError as error:
        raise IpcRefused("Non-ASCII HTTP request header or target") from error
    _apply_deadline(connection, deadline)
    connection.sendall(head_bytes + body)

    head, rest = _read_head(connection, deadline=deadline)
    lines = head.split(b"\r\n")
    if not lines:
        raise IpcRefused("Empty HTTP response")
    if len(lines) - 1 > MAX_HEADER_COUNT:
        raise IpcRefused("Too many HTTP response headers")
    try:
        status_line = lines[0].decode("ascii")
    except UnicodeDecodeError as error:
        raise IpcRefused("Non-ASCII HTTP status line") from error
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or parts[0] != "HTTP/1.1" or not parts[1].isdigit():
        raise IpcRefused("Malformed HTTP response status line")
    status = int(parts[1])

    resp_headers: dict[str, str] = {}
    content_length: int | None = None
    for raw_line in lines[1:]:
        if b":" not in raw_line:
            raise IpcRefused("Malformed HTTP response header")
        raw_name, raw_value = raw_line.split(b":", 1)
        try:
            name_raw = raw_name.decode("ascii")
            value = raw_value.decode("ascii").strip(" \t")
        except UnicodeDecodeError as error:
            raise IpcRefused("Non-ASCII HTTP response header") from error
        if not _valid_header_name(name_raw) or _has_ctl_or_newline(value):
            raise IpcRefused("Invalid HTTP response header")
        name = name_raw.lower()
        if name == "transfer-encoding":
            raise IpcRefused("Chunked transfer encoding is not supported")
        if name == "content-length":
            if content_length is not None or not value.isdigit():
                raise IpcRefused("Invalid response Content-Length")
            content_length = int(value)
            if content_length > MAX_RESPONSE_BYTES:
                raise IpcRefused("HTTP response exceeds maximum allowed size")
        resp_headers[name] = value

    if status == 304:
        resp_body = b""
    elif content_length is not None:
        if len(rest) > content_length:
            raise IpcRefused("Response exceeded declared Content-Length")
        resp_body = _recv_exact(connection, content_length, rest, deadline=deadline)
    else:
        chunks = [rest] if rest else []
        total = len(rest)
        while True:
            _apply_deadline(connection, deadline)
            chunk = connection.recv(16384)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise IpcRefused("HTTP response exceeds maximum allowed size")
            chunks.append(chunk)
        resp_body = b"".join(chunks)
    return HttpResponse(status=status, headers=resp_headers, body=resp_body)
