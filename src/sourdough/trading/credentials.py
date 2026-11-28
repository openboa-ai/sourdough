"""Opaque host credential handles and sign-time secret resolution for trading venues.

Delegates core opaque handle parsing, environment isolation, and host SecretVault
lookups to `sourdough.secret_vault` while preserving trading's `ApiCredentialPair`
and `CredentialProvider` contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..secret_vault import (
    CredentialRefused,
    HostSecretVault,
    InMemorySecretVault,
    ParsedSecretHandle,
    assert_handle_environment_isolation,
    parse_secret_handle,
    read_macos_keychain_secret,
)
from .domain import ENV_EXTERNAL_TEST, ENV_LIVE, ENV_PAPER

ParsedCredentialHandle = ParsedSecretHandle


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
    return parse_secret_handle(handle_ref)


def assert_handle_matches_environment(
    handle_ref: str, expected_environment: str
) -> ParsedCredentialHandle:
    """Fail closed if a credential handle violates environment isolation rules."""
    if expected_environment == ENV_PAPER:
        raise CredentialRefused(
            "Paper execution bindings must not resolve external venue credentials"
        )
    if expected_environment not in (ENV_EXTERNAL_TEST, ENV_LIVE):
        raise CredentialRefused(
            f"Unsupported credential environment: {expected_environment}"
        )
    return assert_handle_environment_isolation(
        handle_ref, expected_environment, allow_paper=False
    )


class EnvRefCredentialProvider:
    """Resolve `envref:<PREFIX>` handles from host `<PREFIX>_API_KEY` and `<PREFIX>_API_SECRET`."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._vault = HostSecretVault(environ)

    def resolve(
        self, handle_ref: str, *, expected_environment: str
    ) -> ApiCredentialPair:
        parsed = assert_handle_matches_environment(handle_ref, expected_environment)
        if parsed.scheme != "envref":
            raise CredentialRefused(
                f"EnvRefCredentialProvider cannot resolve scheme '{parsed.scheme}'"
            )
        bundle = self._vault.resolve_bundle(
            handle_ref,
            ("api_key", "api_secret"),
            expected_environment=expected_environment,
        )
        return ApiCredentialPair(
            api_key=bundle.get("api_key"),
            api_secret=bundle.get("api_secret"),
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
        return read_macos_keychain_secret(service, account)


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
        self._vault = InMemorySecretVault(
            {
                handle: {"api_key": pair[0], "api_secret": pair[1]}
                for handle, pair in mapping.items()
            }
        )

    def resolve(
        self, handle_ref: str, *, expected_environment: str
    ) -> ApiCredentialPair:
        assert_handle_matches_environment(handle_ref, expected_environment)
        try:
            bundle = self._vault.resolve_bundle(
                handle_ref,
                ("api_key", "api_secret"),
                expected_environment=expected_environment,
            )
        except CredentialRefused as error:
            if "not registered" in str(error):
                raise CredentialRefused(
                    f"Test credential handle '{handle_ref}' is not registered"
                ) from error
            raise
        return ApiCredentialPair(
            api_key=bundle.get("api_key"),
            api_secret=bundle.get("api_secret"),
            handle_ref=handle_ref,
        )
