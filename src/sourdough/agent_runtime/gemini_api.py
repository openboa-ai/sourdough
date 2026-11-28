"""Google Gemini generateContent API adapter over Sourdough OuterToolBroker."""

from __future__ import annotations

from time import time_ns
from typing import Any

from ._http import (
    post_json_request,
    render_context_system_prompt,
    resolve_endpoint_base,
)
from .protocol import (
    PROVIDER_GEMINI,
    AdapterEvent,
    AdapterManifest,
    AgentProfileBinding,
    AgentProfileMismatch,
    AgentRuntimeRefused,
    EnvSecretResolver,
    OuterToolBroker,
    SecretHandleResolver,
    ShiftRequest,
    ShiftResult,
    TokenUsage,
)

GEMINI_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"


class GeminiGenerateContentAdapter:
    """Multi-turn function-calling adapter for Gemini v1beta generateContent."""

    def __init__(
        self,
        *,
        secret_resolver: SecretHandleResolver | None = None,
        base_url_override: str | None = None,
        allow_loopback_override: bool = False,
    ) -> None:
        self._secret_resolver = secret_resolver or EnvSecretResolver()
        self._base_url = resolve_endpoint_base(
            GEMINI_DEFAULT_BASE_URL,
            base_url_override,
            allow_loopback_override=allow_loopback_override,
        )
        self._manifest = AdapterManifest(
            provider=PROVIDER_GEMINI,
            adapter_revision="gemini-generate-content-v1",
            transport="https_json",
            supports_tool_mediation=True,
            supports_usage_accounting=True,
            supports_sandbox_desk=False,
        )

    @property
    def manifest(self) -> AdapterManifest:
        return self._manifest

    def run_shift(
        self,
        profile: AgentProfileBinding,
        request: ShiftRequest,
        broker: OuterToolBroker,
    ) -> ShiftResult:
        if profile.provider != PROVIDER_GEMINI:
            raise AgentProfileMismatch(
                f"GeminiGenerateContentAdapter cannot run provider '{profile.provider}'"
            )
        if profile.credential_handle_ref is None:
            raise AgentRuntimeRefused("Gemini profile requires credential_handle_ref")
        secret = self._secret_resolver.resolve_secret(profile.credential_handle_ref)

        events: list[AdapterEvent] = [
            AdapterEvent(
                kind="started",
                recorded_at_ns=time_ns(),
                payload={"provider": PROVIDER_GEMINI, "model": profile.model},
            )
        ]
        system_prompt = render_context_system_prompt(request)
        tool_decls = [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            }
            for spec in request.tools
        ]
        endpoint = f"{self._base_url}/v1beta/models/{profile.model}:generateContent"
        headers = {"x-goog-api-key": secret.value}

        total_usage = TokenUsage(0, 0)
        contents: list[dict[str, Any]] = [
            {"role": "user", "parts": [{"text": request.instruction}]}
        ]
        max_rounds = request.max_tool_calls + 2

        for round_idx in range(max_rounds):
            payload: dict[str, Any] = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": contents,
            }
            if tool_decls:
                payload["tools"] = [{"functionDeclarations": tool_decls}]

            response = post_json_request(
                endpoint,
                headers=headers,
                payload=payload,
                timeout_seconds=request.timeout_seconds,
                provider=PROVIDER_GEMINI,
            )

            usage_obj = response.get("usageMetadata")
            if isinstance(usage_obj, dict):
                round_usage = TokenUsage(
                    input_tokens=int(usage_obj.get("promptTokenCount") or 0),
                    output_tokens=int(usage_obj.get("candidatesTokenCount") or 0),
                )
                total_usage = total_usage + round_usage
                broker.record_usage(round_usage)
                events.append(
                    AdapterEvent(
                        kind="usage",
                        recorded_at_ns=time_ns(),
                        payload={
                            "input_tokens": round_usage.input_tokens,
                            "output_tokens": round_usage.output_tokens,
                        },
                    )
                )

            candidates = response.get("candidates")
            parts: list[dict[str, Any]] = []
            if isinstance(candidates, list) and candidates:
                first = candidates[0]
                if isinstance(first, dict):
                    content = first.get("content")
                    if isinstance(content, dict) and isinstance(
                        content.get("parts"), list
                    ):
                        parts = [p for p in content["parts"] if isinstance(p, dict)]

            fn_responses: list[dict[str, Any]] = []
            text_parts: list[str] = []

            for part_idx, part in enumerate(parts):
                if "functionCall" in part and isinstance(part["functionCall"], dict):
                    fc = part["functionCall"]
                    tool_name = str(fc.get("name") or "")
                    call_id = str(fc.get("id") or f"gemini-{round_idx}-{part_idx}")
                    args = fc.get("args", {})
                    if not isinstance(args, dict):
                        raise AgentRuntimeRefused(
                            f"Gemini functionCall '{tool_name}' args must be a JSON object"
                        )
                    result = broker.invoke(call_id, tool_name, args)
                    fn_responses.append(
                        {
                            "functionResponse": {
                                "name": tool_name,
                                "response": result,
                            }
                        }
                    )
                elif isinstance(part.get("text"), str):
                    text_parts.append(part["text"])

            if fn_responses:
                contents.append({"role": "model", "parts": parts})
                contents.append({"role": "user", "parts": fn_responses})
                continue

            final_text = "\n".join(part for part in text_parts if part).strip()
            events.extend(broker.events)
            events.append(
                AdapterEvent(
                    kind="terminal",
                    recorded_at_ns=time_ns(),
                    payload={"output_chars": len(final_text)},
                )
            )
            return ShiftResult(
                provider=PROVIDER_GEMINI,
                model=profile.model,
                run_id=request.run_id,
                turn_id=request.turn_id,
                output_text=final_text,
                usage=total_usage,
                tool_calls_executed=broker.call_count,
                events=tuple(events),
            )

        raise AgentRuntimeRefused("Gemini shift exceeded maximum tool-call loop rounds")
