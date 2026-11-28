"""Strict fail-closed registry dispatching each Agent shift to its pinned provider adapter."""

from __future__ import annotations

from pathlib import Path

from .anthropic_api import AnthropicMessagesAdapter
from .codex_cli import CodexCliAdapter
from .gemini_api import GeminiGenerateContentAdapter
from .openai_api import OpenAiResponsesAdapter
from .protocol import (
    VALID_EXECUTION_PLANES,
    AdapterManifest,
    AgentProfileBinding,
    AgentRuntimeAdapter,
    AgentRuntimeRefused,
    EnvSecretResolver,
    ExecutionPlane,
    OuterToolBroker,
    SecretHandleResolver,
    ShiftRequest,
    ShiftResult,
)


class AgentAdapterRegistry:
    """Provider registry that enforces immutable profile pinning with zero silent fallback."""

    def __init__(self) -> None:
        self._adapters: dict[str, AgentRuntimeAdapter] = {}

    def register(self, adapter: AgentRuntimeAdapter) -> None:
        manifest = adapter.manifest
        if not isinstance(manifest, AdapterManifest):
            raise AgentRuntimeRefused(
                "Registered adapter must expose an AdapterManifest instance"
            )
        if manifest.execution_plane not in VALID_EXECUTION_PLANES:
            raise AgentRuntimeRefused(
                f"Unsupported execution plane '{manifest.execution_plane}'"
            )
        self._adapters[manifest.provider] = adapter

    def registered_providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters.keys()))

    def get(self, provider: str) -> AgentRuntimeAdapter:
        adapter = self._adapters.get(provider)
        if adapter is None:
            raise AgentRuntimeRefused(
                f"No runtime adapter is registered for provider '{provider}'"
            )
        return adapter

    def get_manifest(self, provider: str) -> AdapterManifest:
        return self.get(provider).manifest

    def adapters_for_plane(
        self, execution_plane: ExecutionPlane
    ) -> tuple[AgentRuntimeAdapter, ...]:
        if execution_plane not in VALID_EXECUTION_PLANES:
            raise AgentRuntimeRefused(
                f"Unsupported execution plane '{execution_plane}'"
            )
        return tuple(
            self._adapters[name]
            for name in sorted(self._adapters.keys())
            if self._adapters[name].manifest.execution_plane == execution_plane
        )

    def execute_shift(
        self,
        *,
        persisted_profile_id: str,
        profile: AgentProfileBinding,
        request: ShiftRequest,
        broker: OuterToolBroker,
    ) -> ShiftResult:
        """Verify profile binding matches persisted Agent profile, then dispatch strictly."""
        profile.verify_matches_agent(persisted_profile_id)
        adapter = self.get(profile.provider)
        if (
            adapter.manifest.transport == "https_json"
            and profile.credential_handle_ref is None
        ):
            raise AgentRuntimeRefused(
                f"HTTPS runtime provider '{profile.provider}' requires an opaque credential_handle_ref"
            )
        return adapter.run_shift(profile, request, broker)


def build_default_adapter_registry(
    *,
    secret_resolver: SecretHandleResolver | None = None,
    openai_base_url: str | None = None,
    anthropic_base_url: str | None = None,
    gemini_base_url: str | None = None,
    allow_loopback_override: bool = False,
    codex_executable: tuple[str, ...] = ("codex", "exec", "--json"),
    codex_use_macos_sandbox: bool = False,
    codex_extra_read_paths: tuple[Path, ...] = (),
    codex_extra_read_files: tuple[Path, ...] = (),
) -> AgentAdapterRegistry:
    """Construct the canonical multi-provider registry for codex_cli, openai, anthropic, and gemini."""
    resolver = secret_resolver or EnvSecretResolver()
    registry = AgentAdapterRegistry()
    registry.register(
        CodexCliAdapter(
            executable=codex_executable,
            use_macos_sandbox=codex_use_macos_sandbox,
            extra_read_paths=codex_extra_read_paths,
            extra_read_files=codex_extra_read_files,
            secret_resolver=resolver,
        )
    )
    registry.register(
        OpenAiResponsesAdapter(
            secret_resolver=resolver,
            base_url_override=openai_base_url,
            allow_loopback_override=allow_loopback_override,
        )
    )
    registry.register(
        AnthropicMessagesAdapter(
            secret_resolver=resolver,
            base_url_override=anthropic_base_url,
            allow_loopback_override=allow_loopback_override,
        )
    )
    registry.register(
        GeminiGenerateContentAdapter(
            secret_resolver=resolver,
            base_url_override=gemini_base_url,
            allow_loopback_override=allow_loopback_override,
        )
    )
    return registry
