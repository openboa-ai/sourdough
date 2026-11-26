"""Anthropic Messages API adapter (/v1/messages) over Sourdough OuterToolBroker."""

from __future__ import annotations

import json
from time import time_ns
from typing import Any

from ._http import (
    post_json_request,
    render_context_system_prompt,
    resolve_endpoint_base,
)
from .protocol import (
    PROVIDER_ANTHROPIC,
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

ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_API_VERSION = "2023-06-01"


class AnthropicMessagesAdapter:
    """Multi-turn tool-calling adapter for the Anthropic Messages API."""

    def __init__(
        self,
        *,
        secret_resolver: SecretHandleResolver | None = None,
        base_url_override: str | None = None,
        allow_loopback_override: bool = False,
        max_tokens: int = 4096,
    ) -> None:
        self._secret_resolver = secret_resolver or EnvSecretResolver()
        self._base_url = resolve_endpoint_base(
            ANTHROPIC_DEFAULT_BASE_URL,
            base_url_override,
            allow_loopback_override=allow_loopback_override,
        )
        self._max_tokens = max_tokens
        self._manifest = AdapterManifest(
            provider=PROVIDER_ANTHROPIC,
            adapter_revision="anthropic-messages-v1",
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
        if profile.provider != PROVIDER_ANTHROPIC:
            raise AgentProfileMismatch(
                f"AnthropicMessagesAdapter cannot run provider '{profile.provider}'"
            )
        if profile.credential_handle_ref is None:
            raise AgentRuntimeRefused(
                "Anthropic profile requires credential_handle_ref"
            )
        secret = self._secret_resolver.resolve_secret(profile.credential_handle_ref)

        events: list[AdapterEvent] = [
            AdapterEvent(
                kind="started",
                recorded_at_ns=time_ns(),
                payload={"provider": PROVIDER_ANTHROPIC, "model": profile.model},
            )
        ]
        system_prompt = render_context_system_prompt(request)
        tool_defs = [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.parameters,
            }
            for spec in request.tools
        ]
        endpoint = f"{self._base_url}/v1/messages"
        headers = {
            "x-api-key": secret.value,
            "anthropic-version": ANTHROPIC_API_VERSION,
        }

        total_usage = TokenUsage(0, 0)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": request.instruction}
        ]
        max_rounds = request.max_tool_calls + 2

        for _ in range(max_rounds):
            payload: dict[str, Any] = {
                "model": profile.model,
                "max_tokens": self._max_tokens,
                "system": system_prompt,
                "messages": messages,
            }
            if tool_defs:
                payload["tools"] = tool_defs

            response = post_json_request(
                endpoint,
                headers=headers,
                payload=payload,
                timeout_seconds=request.timeout_seconds,
                provider=PROVIDER_ANTHROPIC,
            )

            usage_obj = response.get("usage")
            if isinstance(usage_obj, dict):
                input_count = (
                    int(usage_obj.get("input_tokens", 0))
                    + int(usage_obj.get("cache_creation_input_tokens", 0))
                    + int(usage_obj.get("cache_read_input_tokens", 0))
                )
                output_count = int(usage_obj.get("output_tokens", 0))
                round_usage = TokenUsage(
                    input_tokens=input_count,
                    output_tokens=output_count,
                )
                total_usage = total_usage + round_usage
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

            content_blocks = response.get("content")
            if not isinstance(content_blocks, list):
                content_blocks = []

            tool_results: list[dict[str, Any]] = []
            text_parts: list[str] = []

            for block in content_blocks:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "tool_use":
                    call_id = str(block.get("id") or "")
                    tool_name = str(block.get("name") or "")
                    tool_input = block.get("input", {})
                    if not isinstance(tool_input, dict):
                        raise AgentRuntimeRefused(
                            f"Anthropic tool_use '{tool_name}' input must be a JSON object"
                        )
                    result = broker.invoke(call_id, tool_name, tool_input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call_id,
                            "content": json.dumps(result, sort_keys=True),
                        }
                    )
                elif block_type == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)

            if tool_results:
                messages.append({"role": "assistant", "content": content_blocks})
                messages.append({"role": "user", "content": tool_results})
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
                provider=PROVIDER_ANTHROPIC,
                model=profile.model,
                run_id=request.run_id,
                turn_id=request.turn_id,
                output_text=final_text,
                usage=total_usage,
                tool_calls_executed=broker.call_count,
                events=tuple(events),
            )

        raise AgentRuntimeRefused(
            "Anthropic shift exceeded maximum tool-call loop rounds"
        )
