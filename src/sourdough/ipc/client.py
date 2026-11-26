"""UDS control plane client used by client-bridge without reading SQLite directly."""

from __future__ import annotations

import json
import os
import socket
from contextlib import closing
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import quote, urlencode

from .protocol import (
    WORK_TIMELINE_V1,
    HttpResponse,
    IpcRefused,
    extract_peer_credentials,
    send_http_request,
    validate_timeout_seconds,
    verify_socket_path,
)


class ControlPlaneClient:
    """Typed HTTP/1.1 client talking exclusively to a verified local CompanyDaemon UDS."""

    def __init__(self, socket_path: Path, *, timeout_seconds: float = 10.0) -> None:
        self._socket_path = socket_path.absolute()
        self._timeout_seconds = validate_timeout_seconds(timeout_seconds)

    def _request(
        self,
        method: str,
        target: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> HttpResponse:
        verify_socket_path(self._socket_path)
        deadline = monotonic() + self._timeout_seconds
        try:
            with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as conn:
                conn.settimeout(self._timeout_seconds)
                conn.connect(str(self._socket_path))
                # Mutual kernel peer authentication: verify daemon UID matches our euid.
                peer = extract_peer_credentials(conn)
                if peer.uid != os.geteuid():
                    raise IpcRefused(
                        "HTTP 403: Control plane daemon is owned by another user"
                    )
                return send_http_request(
                    conn,
                    method,
                    target,
                    headers=headers,
                    body=body,
                    deadline=deadline,
                )
        except OSError as error:
            raise IpcRefused(f"Control plane transport failed: {error}") from error

    def _decode_json(self, response: HttpResponse) -> dict[str, Any]:
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IpcRefused(
                f"HTTP {response.status}: Control plane returned invalid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise IpcRefused(
                f"HTTP {response.status}: Control plane returned non-object JSON"
            )
        return payload

    def _http_error(
        self, response: HttpResponse, payload: dict[str, Any]
    ) -> IpcRefused:
        detail = payload.get("error")
        if isinstance(detail, str) and detail.strip():
            return IpcRefused(f"HTTP {response.status}: {detail}")
        return IpcRefused(f"HTTP {response.status}")

    def raw_request(
        self,
        method: str,
        target: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> HttpResponse:
        """Forward a bounded HTTP/1.1 request over the verified private UDS."""
        return self._request(method, target, headers=headers, body=body)

    def projection(
        self,
        name: str,
        *,
        query: dict[str, str] | None = None,
        if_none_match: str | None = None,
        expected_company_id: str | None = None,
        principal: str | None = None,
    ) -> tuple[int, str | None, dict[str, Any] | None]:
        target = f"/v1/projections/{name}"
        if query:
            target = f"{target}?{urlencode(query)}"
        headers: dict[str, str] = {}
        if if_none_match:
            headers["If-None-Match"] = if_none_match
        if principal:
            headers["X-Sourdough-Principal"] = principal
        response = self._request("GET", target, headers=headers or None)
        etag = response.headers.get("etag")
        header_company = response.headers.get("x-sourdough-company-id")
        if response.status == 304:
            if (
                expected_company_id is not None
                and header_company != expected_company_id
            ):
                raise IpcRefused(
                    "HTTP 403: Control plane company identity mismatch on 304 response"
                )
            return 304, etag, None
        payload = self._decode_json(response)
        if response.status != 200:
            raise self._http_error(response, payload)
        if expected_company_id is not None and (
            payload.get("company_id") != expected_company_id
            or (header_company is not None and header_company != expected_company_id)
        ):
            raise IpcRefused("HTTP 403: Control plane company identity mismatch")
        return 200, etag, payload

    def meta(self, *, principal: str | None = None) -> dict[str, Any]:
        headers = {"X-Sourdough-Principal": principal} if principal else None
        response = self._request("GET", "/v1/meta", headers=headers)
        payload = self._decode_json(response)
        if response.status != 200:
            raise self._http_error(response, payload)
        return payload

    def work_timeline(
        self,
        *,
        contract_revision: str = WORK_TIMELINE_V1,
        if_none_match: str | None = None,
        expected_company_id: str | None = None,
    ) -> tuple[int, str | None, dict[str, Any] | None]:
        query = urlencode({"contract_revision": contract_revision})
        headers = {"If-None-Match": if_none_match} if if_none_match else None
        response = self._request(
            "GET", f"/v1/projections/work-timeline?{query}", headers=headers
        )
        etag = response.headers.get("etag")
        header_company = response.headers.get("x-sourdough-company-id")
        if response.status == 304:
            if (
                expected_company_id is not None
                and header_company != expected_company_id
            ):
                raise IpcRefused(
                    "HTTP 403: Control plane company identity mismatch on 304 response"
                )
            return 304, etag, None
        payload = self._decode_json(response)
        if response.status != 200:
            raise self._http_error(response, payload)
        if expected_company_id is not None and (
            payload.get("company_id") != expected_company_id
            or (header_company is not None and header_company != expected_company_id)
        ):
            raise IpcRefused("HTTP 403: Control plane company identity mismatch")
        return 200, etag, payload

    def command_receipt(
        self,
        command_id: str,
        *,
        operation: str | None = None,
        principal: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(command_id, str) or not command_id.strip():
            raise IpcRefused("Command identifier is required")
        target = f"/v1/commands/{quote(command_id, safe='')}"
        if operation is not None:
            target = f"{target}?{urlencode({'operation': operation})}"
        headers = {"X-Sourdough-Principal": principal} if principal else None
        response = self._request("GET", target, headers=headers)
        payload = self._decode_json(response)
        if response.status in (200, 404):
            return payload
        raise self._http_error(response, payload)

    def submit_command(
        self,
        *,
        company_id: str,
        command_id: str,
        command_type: str,
        contract_version: str,
        preconditions: dict[str, Any],
        payload: dict[str, Any],
        principal: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        body = json.dumps(
            {
                "company_id": company_id,
                "command_id": command_id,
                "command_type": command_type,
                "contract_version": contract_version,
                "preconditions": preconditions,
                "payload": payload,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": command_id,
        }
        if principal:
            headers["X-Sourdough-Principal"] = principal
        response = self._request(
            "POST",
            "/v1/commands",
            headers=headers,
            body=body,
        )
        decoded = self._decode_json(response)
        return response.status, decoded

    def events(
        self,
        *,
        after_seq: int = 0,
        limit: int = 100,
        wait_ms: int = 0,
        expected_company_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = urlencode({"after_seq": after_seq, "limit": limit, "wait_ms": wait_ms})
        response = self._request("GET", f"/v1/events?{query}")
        if response.status != 200:
            payload = self._decode_json(response)
            raise self._http_error(response, payload)
        header_company = response.headers.get("x-sourdough-company-id")
        if (
            expected_company_id is not None
            and header_company is not None
            and header_company != expected_company_id
        ):
            raise IpcRefused("HTTP 403: Control plane company identity mismatch")
        result: list[dict[str, Any]] = []
        for raw_line in response.body.splitlines():
            if not raw_line.strip():
                continue
            try:
                entry = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise IpcRefused(
                    "Control plane returned invalid NDJSON event"
                ) from error
            if not isinstance(entry, dict):
                raise IpcRefused("Control plane event line must be a JSON object")
            if (
                expected_company_id is not None
                and entry.get("company_id") != expected_company_id
            ):
                raise IpcRefused(
                    "HTTP 403: Control plane event company identity mismatch"
                )
            result.append(entry)
        return result
