"""OpenAI Responses API adapter (/v1/responses) over Sourdough OuterToolBroker."""

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
    PROVIDER_OPENAI,
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

OPENAI_DEFAULT_BASE_URL = "https://api.openai.com"


class OpenAiResponsesAdapter:
    """Multi-turn tool-calling adapter for the OpenAI Responses API."""

    def __init__(
        self,
        *,
        secret_resolver: SecretHandleResolver | None = None,
        base_url_override: str | None = None,
        allow_loopback_override: bool = False,
    ) -> None:
        self._secret_resolver = secret_resolver or EnvSecretResolver()
        self._base_url = resolve_endpoint_base(
            OPENAI_DEFAULT_BASE_URL,
            base_url_override,
            allow_loopback_override=allow_loopback_override,
        )
        self._manifest = AdapterManifest(
            provider=PROVIDER_OPENAI,
            adapter_revision="openai-responses-v1",
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
        if profile.provider != PROVIDER_OPENAI:
            raise AgentProfileMismatch(
                f"OpenAiResponsesAdapter cannot run provider '{profile.provider}'"
            )
        if profile.credential_handle_ref is None:
            raise AgentRuntimeRefused("OpenAI profile requires credential_handle_ref")
        secret = self._secret_resolver.resolve_secret(profile.credential_handle_ref)

        events: list[AdapterEvent] = [
            AdapterEvent(
                kind="started",
                recorded_at_ns=time_ns(),
                payload={"provider": PROVIDER_OPENAI, "model": profile.model},
            )
        ]
        instructions = render_context_system_prompt(request)
        tool_defs = [
            {
                "type": "function",
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            }
            for spec in request.tools
        ]
        endpoint = f"{self._base_url}/v1/responses"
        headers = {"Authorization": f"Bearer {secret.value}"}

        total_usage = TokenUsage(0, 0)
        input_items: list[dict[str, Any]] = [
            {"role": "user", "content": request.instruction}
        ]
        previous_response_id: str | None = None
        max_rounds = request.max_tool_calls + 2

        for _ in range(max_rounds):
            payload: dict[str, Any] = {
                "model": profile.model,
                "instructions": instructions,
                "input": input_items,
            }
            if tool_defs:
                payload["tools"] = tool_defs
            if previous_response_id is not None:
                payload["previous_response_id"] = previous_response_id

            response = post_json_request(
                endpoint,
                headers=headers,
                payload=payload,
                timeout_seconds=request.timeout_seconds,
                provider=PROVIDER_OPENAI,
            )

            usage_obj = response.get("usage")
            if isinstance(usage_obj, dict):
                round_usage = TokenUsage(
                    input_tokens=int(usage_obj.get("input_tokens") or 0),
                    output_tokens=int(usage_obj.get("output_tokens") or 0),
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

            output_items = response.get("output")
            if not isinstance(output_items, list):
                output_items = []

            tool_outputs: list[dict[str, Any]] = []
            text_parts: list[str] = []

            for item in output_items:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type == "function_call":
                    call_id = str(item.get("call_id") or item.get("id") or "")
                    tool_name = str(item.get("name") or "")
                    raw_args = item.get("arguments", "{}")
                    if isinstance(raw_args, str):
                        try:
                            parsed_args = (
                                json.loads(raw_args) if raw_args.strip() else {}
                            )
                        except json.JSONDecodeError as error:
                            raise AgentRuntimeRefused(
                                f"OpenAI function_call '{tool_name}' supplied invalid JSON arguments"
                            ) from error
                    elif isinstance(raw_args, dict):
                        parsed_args = raw_args
                    else:
                        raise AgentRuntimeRefused(
                            f"OpenAI function_call '{tool_name}' arguments must be JSON object"
                        )
                    result = broker.invoke(call_id, tool_name, parsed_args)
                    tool_outputs.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps(result, sort_keys=True),
                        }
                    )
                elif item_type == "message":
                    content = item.get("content")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") in (
                                "output_text",
                                "text",
                            ):
                                text = block.get("text")
                                if isinstance(text, str):
                                    text_parts.append(text)
                    elif isinstance(content, str):
                        text_parts.append(content)

            if tool_outputs:
                resp_id = response.get("id")
                if isinstance(resp_id, str) and resp_id:
                    previous_response_id = resp_id
                    input_items = tool_outputs
                else:
                    input_items = [*input_items, *output_items, *tool_outputs]
                continue

            if not text_parts and isinstance(response.get("output_text"), str):
                text_parts.append(response["output_text"])

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
                provider=PROVIDER_OPENAI,
                model=profile.model,
                run_id=request.run_id,
                turn_id=request.turn_id,
                output_text=final_text,
                usage=total_usage,
                tool_calls_executed=broker.call_count,
                events=tuple(events),
            )

        raise AgentRuntimeRefused("OpenAI shift exceeded maximum tool-call loop rounds")
