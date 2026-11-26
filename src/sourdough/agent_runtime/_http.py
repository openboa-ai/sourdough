"""Shared HTTPS transport, loopback guard, and ContextPack prompt rendering for LLM adapters."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .protocol import (
    AgentRuntimeRefused,
    AgentTransportInterrupted,
    ShiftRequest,
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def resolve_endpoint_base(
    default_base: str,
    override: str | None,
    *,
    allow_loopback_override: bool,
) -> str:
    """Return pinned provider HTTPS base URL, allowing loopback override only when explicitly opted in."""
    if override is None:
        return default_base.rstrip("/")
    if not allow_loopback_override:
        raise AgentRuntimeRefused(
            "Provider base_url_override requires allow_loopback_override=True"
        )
    parsed = urllib.parse.urlparse(override)
    if parsed.scheme not in ("http", "https"):
        raise AgentRuntimeRefused("base_url_override must use http or https scheme")
    if parsed.hostname not in _LOOPBACK_HOSTS:
        raise AgentRuntimeRefused(
            "base_url_override is restricted to loopback hosts (127.0.0.1 / localhost)"
        )
    return override.rstrip("/")


def render_context_system_prompt(request: ShiftRequest) -> str:
    """Serialize the versioned ContextPack into a deterministic system prompt."""
    ctx = request.context
    envelope = {
        "company_id": ctx.company_id,
        "agent_id": ctx.agent_id,
        "run_id": ctx.run_id,
        "fencing_token": ctx.fencing_token,
        "grant_scope": ctx.grant_scope,
        "mandate_summary": ctx.mandate_summary,
        "open_receipts": list(ctx.open_receipts),
        "history_notes": list(ctx.history_notes),
        "context_digest": ctx.digest,
        "compacted": ctx.compacted,
    }
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    return (
        "You are operating inside a Sourdough fenced Agent shift. "
        "Respect your grant_scope and open_receipts, never spoof server identity arguments "
        "(agent_id, company_id, run_id, fencing_token, tier), and invoke only declared tools.\n"
        f"SHIFT_CONTEXT_PACK={encoded}"
    )


def post_json_request(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
    provider: str,
) -> dict[str, Any]:
    """POST JSON payload and classify HTTP/transport failures cleanly without leaking secrets."""
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        **headers,
    }
    req = urllib.request.Request(
        url,
        data=raw_body,
        headers=req_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            response_bytes = resp.read()
    except urllib.error.HTTPError as error:
        try:
            err_excerpt = error.read().decode("utf-8", errors="replace")[:200]
        finally:
            error.close()
        if error.code in (408, 429, 500, 502, 503, 504):
            raise AgentTransportInterrupted(
                f"{provider} HTTP {error.code} transport error"
            ) from error
        raise AgentRuntimeRefused(
            f"{provider} rejected request (HTTP {error.code}): {err_excerpt}"
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise AgentTransportInterrupted(
            f"{provider} transport interrupted ({type(error).__name__})"
        ) from error

    try:
        parsed = json.loads(response_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AgentTransportInterrupted(
            f"{provider} returned malformed JSON payload"
        ) from error
    if not isinstance(parsed, dict):
        raise AgentTransportInterrupted(
            f"{provider} response root must be a JSON object"
        )
    return parsed
