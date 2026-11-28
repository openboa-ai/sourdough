"""Built-in scoped external LLM inference connector (connector_kind='llm_inference').

Allows Agent-authored programs or tools to invoke company-approved external LLM
endpoints by binding_id without ever seeing raw API keys. Secrets are injected
strictly in memory at dispatch time, HTTP redirects are blocked, and token usage
is metered deterministically into integer minor units for company GL settlement.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..agent_runtime.cost_accounting import (
    DEFAULT_MODEL_PRICING,
    ModelPricing,
    compute_usage_cost_minor_units,
)
from ..agent_runtime.protocol import PROVIDER_OPENAI, TokenUsage
from ..secret_vault import (
    MaskedSecret,
    MaskedSecretBundle,
    redact_secret_values,
)
from .protocol import (
    ConnectorIndeterminate,
    ConnectorManifest,
    ConnectorRefused,
    ConnectorRequest,
    ConnectorResponse,
    OperationSpec,
)

CONNECTOR_KIND_LLM_INFERENCE = "llm_inference"

LLM_COMPLETE_OP = OperationSpec(
    name="complete",
    mode="write",
    request_schema={
        "type": "object",
        "properties": {
            "model": {"type": "string"},
            "input": {"type": "string"},
            "instructions": {"type": "string"},
            "max_output_tokens": {"type": "integer", "minimum": 1},
            "api_style": {"type": "string", "enum": ["responses", "chat_completions"]},
        },
        "required": ["model", "input"],
    },
    supports_lookup=False,
    idempotent=False,
)

LLM_EMBED_OP = OperationSpec(
    name="embed",
    mode="read",
    request_schema={
        "type": "object",
        "properties": {
            "model": {"type": "string"},
            "input": {"type": "string"},
        },
        "required": ["model", "input"],
    },
    supports_lookup=False,
    idempotent=True,
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Block all HTTP 3xx redirects so Bearer credentials can never bounce to unpinned hosts."""

    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        raise ConnectorRefused(
            f"Connector HTTP redirect (status {code}) is prohibited for credential safety"
        )


def _assert_safe_header_pair(name: str, value: str) -> None:
    if (
        not isinstance(name, str)
        or not name.strip()
        or any(ord(ch) < 33 or ord(ch) > 126 or ch == ":" for ch in name)
    ):
        raise ConnectorRefused("Connector HTTP header name is invalid")
    if (
        not isinstance(value, str)
        or any(ch in value for ch in ("\r", "\n", "\x00"))
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        raise ConnectorRefused(
            "Connector credential header contains prohibited control characters"
        )


DEFAULT_MAX_OUTPUT_TOKENS = 16_384
MAX_LLM_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_TEXT_CHARS = 512_000
MAX_EMBEDDING_DIMENSIONS = 16_384


def _post_pinned_json(
    url: str,
    *,
    allowed_hosts: tuple[str, ...],
    headers: dict[str, str],
    body: dict[str, Any],
    timeout_seconds: float,
    secret_values: tuple[str, ...] = (),
    max_response_bytes: int = MAX_LLM_RESPONSE_BYTES,
) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if hostname not in allowed_hosts:
        raise ConnectorRefused(
            f"Refused outbound LLM connector request to unpinned host '{hostname}'"
        )
    for h_name, h_val in headers.items():
        _assert_safe_header_pair(h_name, h_val)
    raw_body = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw_body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **headers,
        },
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(req, timeout=timeout_seconds) as resp:
            response_bytes = resp.read(max_response_bytes + 1)
    except urllib.error.HTTPError as error:
        try:
            # Read complete bounded body and scrub secrets BEFORE truncating so
            # secrets straddling the excerpt boundary can never leak fragments.
            full_err = error.read(65_536).decode("utf-8", errors="replace")
            scrubbed_err = redact_secret_values(full_err, secret_values)
            err_excerpt = scrubbed_err[:240]
        finally:
            error.close()
        if error.code in (408, 429, 500, 502, 503, 504):
            raise ConnectorIndeterminate(
                f"LLM connector HTTP {error.code} indeterminate transport failure"
            ) from error
        raise ConnectorRefused(
            f"LLM connector rejected request (HTTP {error.code}): {err_excerpt}"
        ) from error
    except ValueError as error:
        raise ConnectorRefused(
            "LLM connector HTTP request construction failed due to malformed header or URL"
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ConnectorIndeterminate(
            f"LLM connector transport interrupted ({type(error).__name__})"
        ) from error

    if len(response_bytes) > max_response_bytes:
        raise ConnectorIndeterminate(
            f"LLM connector response exceeded maximum allowed size ({max_response_bytes} bytes)"
        )

    try:
        decoded = json.loads(response_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConnectorIndeterminate(
            "LLM connector returned malformed JSON response"
        ) from error
    if not isinstance(decoded, dict):
        raise ConnectorIndeterminate("LLM connector JSON root must be an object")
    return decoded


def _extract_usage(parsed: dict[str, Any]) -> TokenUsage:
    usage = parsed.get("usage")
    if not isinstance(usage, dict):
        return TokenUsage(0, 0)
    raw_in = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    raw_out = usage.get("output_tokens", usage.get("completion_tokens", 0))
    in_tok = int(raw_in) if isinstance(raw_in, int) and raw_in >= 0 else 0
    out_tok = int(raw_out) if isinstance(raw_out, int) and raw_out >= 0 else 0
    return TokenUsage(input_tokens=in_tok, output_tokens=out_tok)


def _extract_output_text(parsed: dict[str, Any]) -> str:
    if isinstance(parsed.get("output_text"), str):
        return str(parsed["output_text"])
    output_items = parsed.get("output")
    if isinstance(output_items, list):
        chunks: list[str] = []
        for item in output_items:
            if not isinstance(item, dict):
                continue
            for part in item.get("content") or ():
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
        if chunks:
            return "\n".join(chunks)
    choices = parsed.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            msg = first.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                return msg["content"]
    return ""


class ScopedLlmInferenceConnector:
    """Credential-proxied external LLM connector metered through company GL & policy gates."""

    def __init__(
        self,
        *,
        pricing: ModelPricing | None = None,
        auth_header_name: str = "Authorization",
        auth_prefix: str = "Bearer ",
        max_response_bytes: int = MAX_LLM_RESPONSE_BYTES,
        max_output_text_chars: int = MAX_OUTPUT_TEXT_CHARS,
        max_embedding_dimensions: int = MAX_EMBEDDING_DIMENSIONS,
    ) -> None:
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ConnectorRefused("max_response_bytes must be a positive integer")
        if type(max_output_text_chars) is not int or max_output_text_chars <= 0:
            raise ConnectorRefused("max_output_text_chars must be a positive integer")
        if type(max_embedding_dimensions) is not int or max_embedding_dimensions <= 0:
            raise ConnectorRefused(
                "max_embedding_dimensions must be a positive integer"
            )
        self._pricing = pricing or DEFAULT_MODEL_PRICING[PROVIDER_OPENAI]
        self._auth_header_name = auth_header_name
        self._auth_prefix = auth_prefix
        self._max_response_bytes = max_response_bytes
        self._max_output_text_chars = max_output_text_chars
        self._max_embedding_dimensions = max_embedding_dimensions
        self._manifest = ConnectorManifest(
            connector_kind=CONNECTOR_KIND_LLM_INFERENCE,
            adapter_revision="llm-inference-connector-v1",
            operations=(LLM_COMPLETE_OP, LLM_EMBED_OP),
            supports_lookup=False,
            seals_absent=False,
            requires_credential=True,
            credential_fields=(),
        )

    @property
    def manifest(self) -> ConnectorManifest:
        return self._manifest

    def estimate_max_cost_minor_units(
        self,
        operation: str,
        payload: dict[str, Any],
    ) -> int:
        """Compute a conservative upper-bound integer cost for pre-dispatch budget reservation."""
        raw_input = str(payload.get("input") or "")
        raw_instructions = str(payload.get("instructions") or "")
        # Conservative upper bound: at most 1 token per UTF-8 byte + framing overhead.
        in_tokens_upper = (
            len(raw_input.encode("utf-8")) + len(raw_instructions.encode("utf-8")) + 256
        )
        if operation == "complete":
            max_tok = payload.get("max_output_tokens")
            # If max_output_tokens is omitted, enforce & reserve the default cap.
            out_tokens_upper = (
                int(max_tok)
                if type(max_tok) is int and max_tok > 0
                else DEFAULT_MAX_OUTPUT_TOKENS
            )
            return compute_usage_cost_minor_units(
                TokenUsage(in_tokens_upper, out_tokens_upper), self._pricing
            )
        if operation == "embed":
            return compute_usage_cost_minor_units(
                TokenUsage(in_tokens_upper, 0), self._pricing
            )
        return 0

    def execute(
        self,
        request: ConnectorRequest,
        *,
        secret: MaskedSecret | MaskedSecretBundle | None,
        timeout_seconds: float,
    ) -> ConnectorResponse:
        if not isinstance(secret, MaskedSecret):
            raise ConnectorRefused(
                "llm_inference connector requires a resolved MaskedSecret"
            )
        headers = {self._auth_header_name: f"{self._auth_prefix}{secret.value}"}
        base = request.endpoint_url.rstrip("/")

        if request.operation == "complete":
            model = request.payload.get("model")
            prompt_input = request.payload.get("input")
            if not isinstance(model, str) or not model.strip():
                raise ConnectorRefused("complete requires a nonempty string 'model'")
            if not isinstance(prompt_input, str) or not prompt_input.strip():
                raise ConnectorRefused("complete requires a nonempty string 'input'")
            max_tok_raw = request.payload.get("max_output_tokens")
            max_tok: int | None = None
            if "max_output_tokens" in request.payload and max_tok_raw is not None:
                if type(max_tok_raw) is not int or max_tok_raw <= 0:
                    raise ConnectorRefused(
                        "max_output_tokens must be a positive integer"
                    )
                max_tok = max_tok_raw
            effective_max_tok = (
                max_tok if max_tok is not None else DEFAULT_MAX_OUTPUT_TOKENS
            )
            api_style = str(request.payload.get("api_style") or "responses")
            if api_style == "chat_completions":
                url = f"{base}/v1/chat/completions"
                messages: list[dict[str, str]] = []
                instructions = request.payload.get("instructions")
                if isinstance(instructions, str) and instructions.strip():
                    messages.append({"role": "system", "content": instructions})
                messages.append({"role": "user", "content": prompt_input})
                body: dict[str, Any] = {
                    "model": model.strip(),
                    "messages": messages,
                    "max_tokens": effective_max_tok,
                }
            elif api_style == "responses":
                url = f"{base}/v1/responses"
                body = {
                    "model": model.strip(),
                    "input": prompt_input,
                    "max_output_tokens": effective_max_tok,
                }
                instructions = request.payload.get("instructions")
                if isinstance(instructions, str) and instructions.strip():
                    body["instructions"] = instructions
            else:
                raise ConnectorRefused(
                    f"Unsupported llm_inference api_style: {api_style}"
                )

            parsed = _post_pinned_json(
                url,
                allowed_hosts=request.allowed_hosts,
                headers=headers,
                body=body,
                timeout_seconds=timeout_seconds,
                secret_values=secret.raw_values(),
                max_response_bytes=self._max_response_bytes,
            )
            usage = _extract_usage(parsed)
            cost_minor = compute_usage_cost_minor_units(usage, self._pricing)
            response_id = parsed.get("id")
            output_text = _extract_output_text(parsed)
            if len(output_text) > self._max_output_text_chars:
                raise ConnectorIndeterminate(
                    f"LLM connector output_text exceeded maximum length ({self._max_output_text_chars} chars)"
                )
            return ConnectorResponse(
                status="observed",
                payload={
                    "response_id": str(response_id)
                    if response_id is not None
                    else None,
                    "model": str(parsed.get("model") or model.strip()),
                    "output_text": output_text,
                },
                source_ref=str(response_id) if response_id is not None else None,
                indeterminate=False,
                cost_minor_units=cost_minor,
                usage={
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                },
            )

        if request.operation == "embed":
            model = request.payload.get("model")
            text_input = request.payload.get("input")
            if not isinstance(model, str) or not model.strip():
                raise ConnectorRefused("embed requires a nonempty string 'model'")
            if not isinstance(text_input, str) or not text_input.strip():
                raise ConnectorRefused("embed requires a nonempty string 'input'")
            url = f"{base}/v1/embeddings"
            parsed = _post_pinned_json(
                url,
                allowed_hosts=request.allowed_hosts,
                headers=headers,
                body={"model": model.strip(), "input": text_input},
                timeout_seconds=timeout_seconds,
                secret_values=secret.raw_values(),
                max_response_bytes=self._max_response_bytes,
            )
            usage = _extract_usage(parsed)
            cost_minor = compute_usage_cost_minor_units(usage, self._pricing)
            data_list = parsed.get("data")
            vector: list[float] = []
            if isinstance(data_list, list) and data_list:
                first = data_list[0]
                if isinstance(first, dict) and isinstance(first.get("embedding"), list):
                    raw_emb = first["embedding"]
                    if len(raw_emb) > self._max_embedding_dimensions:
                        raise ConnectorIndeterminate(
                            f"LLM connector embedding exceeded maximum dimensions ({self._max_embedding_dimensions})"
                        )
                    for x in raw_emb:
                        if isinstance(x, bool) or not isinstance(x, (int, float)):
                            raise ConnectorIndeterminate(
                                "LLM connector embedding contains non-numeric element"
                            )
                        fx = float(x)
                        if not math.isfinite(fx):
                            raise ConnectorIndeterminate(
                                "LLM connector embedding contains non-finite float"
                            )
                        vector.append(fx)
            return ConnectorResponse(
                status="observed",
                payload={
                    "model": str(parsed.get("model") or model.strip()),
                    "embedding": vector,
                    "dimensions": len(vector),
                },
                source_ref=None,
                indeterminate=False,
                cost_minor_units=cost_minor,
                usage={
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                },
            )

        raise ConnectorRefused(
            f"Unsupported llm_inference operation: {request.operation}"
        )

    def lookup(
        self,
        request: ConnectorRequest,
        *,
        secret: MaskedSecret | MaskedSecretBundle | None,
        timeout_seconds: float,
    ) -> ConnectorResponse:
        del secret, timeout_seconds
        return ConnectorResponse(
            status="unknown",
            payload={
                "idempotency_key": request.idempotency_key,
                "reason": "llm_inference_lookup_unsupported",
            },
            indeterminate=True,
        )


__all__ = [
    "CONNECTOR_KIND_LLM_INFERENCE",
    "LLM_COMPLETE_OP",
    "LLM_EMBED_OP",
    "ScopedLlmInferenceConnector",
]
