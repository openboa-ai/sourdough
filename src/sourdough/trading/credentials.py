"""Opaque host credential handles and sign-time secret resolution.

Secrets are resolved strictly in memory at request-signing time from opaque
`keychain:...` or `envref:...` handles. Raw API keys and secrets must never be
stored in SQLite, written to Agent Desks, logged, or echoed in exceptions.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Protocol

from .domain import ENV_EXTERNAL_TEST, ENV_LIVE, ENV_PAPER

_HANDLE_RE = re.compile(
    r"^(?P<scheme>keychain|envref):(?P<target>[A-Za-z0-9][A-Za-z0-9._:/-]{2,120})$"
)
_NON_LIVE_MARKERS = ("paper", "testnet", "sandbox", "mock", "demo")


class CredentialRefused(Exception):
    """A credential handle was invalid, environment-mismatched, or unresolvable."""


@dataclass(frozen=True)
class ParsedCredentialHandle:
    scheme: str
    target: str
    raw: str


@dataclass(frozen=True)
class ApiCredentialPair:
    """In-memory API key/secret pair with masked string representations."""

    api_key: str
    api_secret: str
    handle_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise CredentialRefused("Resolved API key is empty")
        if not isinstance(self.api_secret, str) or not self.api_secret.strip():
            raise CredentialRefused("Resolved API secret is empty")

    def __repr__(self) -> str:
        return f"ApiCredentialPair(handle_ref={self.handle_ref!r}, api_key='***', api_secret='***')"

    def __str__(self) -> str:
        return self.__repr__()


class CredentialProvider(Protocol):
    def resolve(
        self, handle_ref: str, *, expected_environment: str
    ) -> ApiCredentialPair: ...


def parse_credential_handle(handle_ref: str) -> ParsedCredentialHandle:
    if not isinstance(handle_ref, str):
        raise CredentialRefused("Credential handle must be a string")
    match = _HANDLE_RE.fullmatch(handle_ref)
    if match is None:
        raise CredentialRefused(
            "Credential handle must be an opaque 'keychain:...' or 'envref:...' reference"
        )
    return ParsedCredentialHandle(
        scheme=match.group("scheme"),
        target=match.group("target"),
        raw=handle_ref,
    )


def assert_handle_matches_environment(
    handle_ref: str, expected_environment: str
) -> ParsedCredentialHandle:
    """Fail closed if a credential handle violates environment isolation rules."""
    parsed = parse_credential_handle(handle_ref)
    if expected_environment == ENV_PAPER:
        raise CredentialRefused(
            "Paper execution bindings must not resolve external venue credentials"
        )
    if expected_environment not in (ENV_EXTERNAL_TEST, ENV_LIVE):
        raise CredentialRefused(
            f"Unsupported credential environment: {expected_environment}"
        )
    lowered = parsed.target.lower()
    if expected_environment == ENV_LIVE and any(
        marker in lowered for marker in _NON_LIVE_MARKERS
    ):
        raise CredentialRefused(
            "Live execution cannot resolve a paper, testnet, or sandbox credential handle"
        )
    if expected_environment == ENV_EXTERNAL_TEST and "live" in lowered:
        raise CredentialRefused(
            "External testnet execution cannot resolve a live credential handle"
        )
    return parsed


class EnvRefCredentialProvider:
    """Resolve `envref:<PREFIX>` handles from host `<PREFIX>_API_KEY` and `<PREFIX>_API_SECRET`."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def resolve(
        self, handle_ref: str, *, expected_environment: str
    ) -> ApiCredentialPair:
        parsed = assert_handle_matches_environment(handle_ref, expected_environment)
        if parsed.scheme != "envref":
            raise CredentialRefused(
                f"EnvRefCredentialProvider cannot resolve scheme '{parsed.scheme}'"
            )
        prefix = re.sub(r"[^A-Za-z0-9_]", "_", parsed.target).upper()
        api_key = self._environ.get(f"{prefix}_API_KEY", "").strip()
        api_secret = self._environ.get(f"{prefix}_API_SECRET", "").strip()
        if not api_key or not api_secret:
            raise CredentialRefused(
                f"Host environment variables for handle '{handle_ref}' are unavailable"
            )
        return ApiCredentialPair(
            api_key=api_key,
            api_secret=api_secret,
            handle_ref=handle_ref,
        )


class MacOsKeychainCredentialProvider:
    """Resolve `keychain:<service>/<account>` handles via macOS Keychain CLI."""

    def resolve(
        self, handle_ref: str, *, expected_environment: str
    ) -> ApiCredentialPair:
        parsed = assert_handle_matches_environment(handle_ref, expected_environment)
        if parsed.scheme != "keychain":
            raise CredentialRefused(
                f"MacOsKeychainCredentialProvider cannot resolve scheme '{parsed.scheme}'"
            )
        if "/" not in parsed.target:
            raise CredentialRefused(
                "Keychain handle must use 'keychain:<service>/<account>' format"
            )
        service, account = parsed.target.split("/", 1)
        api_key = self._read_item(f"{service}.api_key", account)
        api_secret = self._read_item(f"{service}.api_secret", account)
        return ApiCredentialPair(
            api_key=api_key,
            api_secret=api_secret,
            handle_ref=handle_ref,
        )

    @staticmethod
    def _read_item(service: str, account: str) -> str:
        try:
            proc = subprocess.run(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s",
                    service,
                    "-a",
                    account,
                    "-w",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CredentialRefused("macOS Keychain lookup unavailable") from error
        if proc.returncode != 0 or not proc.stdout.strip():
            raise CredentialRefused(
                f"macOS Keychain item not found for service '{service}'"
            )
        return proc.stdout.strip()


class CompositeCredentialProvider:
    """Route `envref:` and `keychain:` handles to their respective host resolvers."""

    def __init__(
        self,
        *,
        env_provider: CredentialProvider | None = None,
        keychain_provider: CredentialProvider | None = None,
    ) -> None:
        self._env = env_provider or EnvRefCredentialProvider()
        self._keychain = keychain_provider or MacOsKeychainCredentialProvider()

    def resolve(
        self, handle_ref: str, *, expected_environment: str
    ) -> ApiCredentialPair:
        parsed = assert_handle_matches_environment(handle_ref, expected_environment)
        if parsed.scheme == "envref":
            return self._env.resolve(
                handle_ref, expected_environment=expected_environment
            )
        return self._keychain.resolve(
            handle_ref, expected_environment=expected_environment
        )


class InMemoryTestCredentialProvider:
    """In-memory credential provider strictly for isolated emulator & unit tests."""

    def __init__(self, mapping: dict[str, tuple[str, str]]) -> None:
        self._mapping = dict(mapping)

    def resolve(
        self, handle_ref: str, *, expected_environment: str
    ) -> ApiCredentialPair:
        assert_handle_matches_environment(handle_ref, expected_environment)
        pair = self._mapping.get(handle_ref)
        if pair is None:
            raise CredentialRefused(
                f"Test credential handle '{handle_ref}' is not registered"
            )
        return ApiCredentialPair(
            api_key=pair[0],
            api_secret=pair[1],
            handle_ref=handle_ref,
        )
