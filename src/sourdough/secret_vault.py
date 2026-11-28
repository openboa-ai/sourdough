"""Unified host SecretVault, opaque handle validation, environment isolation, and secret redaction.

All external credentials (LLM providers, trading venues, data sources, custom connectors)
resolve exclusively in memory inside the trusted outer host from opaque `envref:...` or
`keychain:<service>/<account>` handles. Raw API keys and secrets must never be persisted
in SQLite, written to Agent Desks, logged, or echoed in exceptions or receipts.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

_SECRET_HANDLE_RE = re.compile(
    r"^(?P<scheme>keychain|envref):(?P<target>[A-Za-z0-9][A-Za-z0-9._:/-]{2,120})$"
)
_NON_LIVE_MARKERS = ("paper", "testnet", "sandbox", "mock", "demo")
_SUPPORTED_ENVIRONMENTS = frozenset({"paper", "external_test", "live", "internal"})


class CredentialRefused(Exception):
    """A credential handle was invalid, environment-mismatched, or unresolvable."""


@dataclass(frozen=True)
class ParsedSecretHandle:
    scheme: str
    target: str
    raw: str


def _assert_safe_secret_value(
    value: str, handle_ref: str, *, field_label: str = "secret"
) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CredentialRefused(f"Resolved {field_label} value is empty")
    if any(ch in value for ch in ("\r", "\n", "\x00")):
        raise CredentialRefused(
            f"Resolved {field_label} for '{handle_ref}' contains prohibited control characters"
        )


@dataclass(frozen=True)
class MaskedSecret:
    """In-memory single API secret that masks itself in repr/str."""

    value: str
    handle_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.handle_ref, str) or not self.handle_ref.strip():
            raise CredentialRefused("handle_ref must be a nonempty string")
        _assert_safe_secret_value(self.value, self.handle_ref)

    def __repr__(self) -> str:
        return f"MaskedSecret(handle_ref={self.handle_ref!r}, value='***')"

    def __str__(self) -> str:
        return self.__repr__()

    def raw_values(self) -> tuple[str, ...]:
        return (self.value,)


@dataclass(frozen=True)
class MaskedSecretBundle:
    """In-memory multi-field credential bundle (e.g. api_key + api_secret) with masked repr/str."""

    values: Mapping[str, str]
    handle_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.handle_ref, str) or not self.handle_ref.strip():
            raise CredentialRefused("handle_ref must be a nonempty string")
        if not isinstance(self.values, Mapping) or not self.values:
            raise CredentialRefused("Resolved secret bundle is empty")
        normalized: dict[str, str] = {}
        for k, v in self.values.items():
            if not isinstance(k, str) or not k.strip():
                raise CredentialRefused("Secret bundle field name must be nonempty")
            _assert_safe_secret_value(
                v, self.handle_ref, field_label=f"secret field '{k}'"
            )
            normalized[k] = v
        object.__setattr__(self, "values", normalized)

    def get(self, field_name: str) -> str:
        val = self.values.get(field_name)
        if val is None:
            raise CredentialRefused(
                f"Secret bundle for '{self.handle_ref}' does not contain field '{field_name}'"
            )
        return val

    def raw_values(self) -> tuple[str, ...]:
        return tuple(self.values.values())

    def __repr__(self) -> str:
        keys = ", ".join(f"{k}='***'" for k in sorted(self.values))
        return f"MaskedSecretBundle(handle_ref={self.handle_ref!r}, {keys})"

    def __str__(self) -> str:
        return self.__repr__()


class SecretVault(Protocol):
    """Host-side vault resolving opaque credential handles strictly in memory."""

    def resolve_secret(
        self,
        handle_ref: str,
        *,
        expected_environment: str | None = None,
    ) -> MaskedSecret: ...

    def resolve_bundle(
        self,
        handle_ref: str,
        fields: Sequence[str],
        *,
        expected_environment: str | None = None,
    ) -> MaskedSecretBundle: ...


def parse_secret_handle(handle_ref: str) -> ParsedSecretHandle:
    """Validate and parse an opaque `envref:...` or `keychain:...` handle."""
    if not isinstance(handle_ref, str):
        raise CredentialRefused("Credential handle must be a string")
    match = _SECRET_HANDLE_RE.fullmatch(handle_ref)
    if match is None:
        raise CredentialRefused(
            "Credential handle must be an opaque 'keychain:...' or 'envref:...' reference"
        )
    return ParsedSecretHandle(
        scheme=match.group("scheme"),
        target=match.group("target"),
        raw=handle_ref,
    )


def assert_handle_environment_isolation(
    handle_ref: str,
    expected_environment: str,
    *,
    allow_paper: bool = False,
) -> ParsedSecretHandle:
    """Fail closed if a credential handle violates environment isolation boundaries."""
    parsed = parse_secret_handle(handle_ref)
    if expected_environment not in _SUPPORTED_ENVIRONMENTS:
        raise CredentialRefused(
            f"Unsupported credential environment: {expected_environment}"
        )
    if expected_environment == "paper" and not allow_paper:
        raise CredentialRefused(
            "Paper execution bindings must not resolve external venue credentials"
        )
    lowered = parsed.target.lower()
    if expected_environment == "live" and any(
        marker in lowered for marker in _NON_LIVE_MARKERS
    ):
        raise CredentialRefused(
            "Live execution cannot resolve a paper, testnet, or sandbox credential handle"
        )
    if expected_environment in ("external_test", "paper") and "live" in lowered:
        raise CredentialRefused(
            f"{expected_environment} execution cannot resolve a live credential handle"
        )
    return parsed


def read_macos_keychain_secret(service: str, account: str) -> str:
    """Read a generic password item from macOS Keychain via `/usr/bin/security`."""
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


def _normalize_env_var(target: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", target).upper()


class HostSecretVault:
    """Resolve `envref:` from host environment and `keychain:<service>/<account>` via macOS Keychain."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def resolve_secret(
        self,
        handle_ref: str,
        *,
        expected_environment: str | None = None,
    ) -> MaskedSecret:
        parsed = (
            assert_handle_environment_isolation(
                handle_ref, expected_environment, allow_paper=True
            )
            if expected_environment is not None
            else parse_secret_handle(handle_ref)
        )
        if parsed.scheme == "envref":
            var_name = _normalize_env_var(parsed.target)
            raw = str(self._environ.get(var_name, "")).strip()
            if not raw:
                raise CredentialRefused(
                    f"Host environment variable for '{handle_ref}' is not set"
                )
            return MaskedSecret(value=raw, handle_ref=handle_ref)
        if "/" not in parsed.target:
            raise CredentialRefused(
                "Keychain handle must use 'keychain:<service>/<account>' format"
            )
        service, account = parsed.target.split("/", 1)
        val = read_macos_keychain_secret(service, account)
        return MaskedSecret(value=val, handle_ref=handle_ref)

    def resolve_bundle(
        self,
        handle_ref: str,
        fields: Sequence[str],
        *,
        expected_environment: str | None = None,
    ) -> MaskedSecretBundle:
        if not fields:
            raise CredentialRefused("At least one secret field is required")
        parsed = (
            assert_handle_environment_isolation(handle_ref, expected_environment)
            if expected_environment is not None
            else parse_secret_handle(handle_ref)
        )
        resolved: dict[str, str] = {}
        if parsed.scheme == "envref":
            prefix = _normalize_env_var(parsed.target)
            for field_name in fields:
                suffix = _normalize_env_var(field_name)
                val = str(self._environ.get(f"{prefix}_{suffix}", "")).strip()
                if not val:
                    raise CredentialRefused(
                        f"Host environment variables for handle '{handle_ref}' are unavailable"
                    )
                resolved[field_name] = val
            return MaskedSecretBundle(values=resolved, handle_ref=handle_ref)
        if "/" not in parsed.target:
            raise CredentialRefused(
                "Keychain handle must use 'keychain:<service>/<account>' format"
            )
        service, account = parsed.target.split("/", 1)
        for field_name in fields:
            resolved[field_name] = read_macos_keychain_secret(
                f"{service}.{field_name}", account
            )
        return MaskedSecretBundle(values=resolved, handle_ref=handle_ref)


class InMemorySecretVault:
    """In-memory SecretVault strictly for isolated unit and loopback tests."""

    def __init__(
        self,
        secrets: Mapping[str, str | Mapping[str, str]] | None = None,
    ) -> None:
        self._secrets: dict[str, str | dict[str, str]] = {}
        for handle, val in (secrets or {}).items():
            if isinstance(val, Mapping):
                self._secrets[handle] = {str(k): str(v) for k, v in val.items()}
            else:
                self._secrets[handle] = str(val)

    def resolve_secret(
        self,
        handle_ref: str,
        *,
        expected_environment: str | None = None,
    ) -> MaskedSecret:
        if expected_environment is not None:
            assert_handle_environment_isolation(
                handle_ref, expected_environment, allow_paper=True
            )
        else:
            parse_secret_handle(handle_ref)
        entry = self._secrets.get(handle_ref)
        if not isinstance(entry, str) or not entry.strip():
            raise CredentialRefused(f"Secret handle '{handle_ref}' is not registered")
        return MaskedSecret(value=entry, handle_ref=handle_ref)

    def resolve_bundle(
        self,
        handle_ref: str,
        fields: Sequence[str],
        *,
        expected_environment: str | None = None,
    ) -> MaskedSecretBundle:
        if not fields:
            raise CredentialRefused("At least one secret field is required")
        if expected_environment is not None:
            assert_handle_environment_isolation(handle_ref, expected_environment)
        else:
            parse_secret_handle(handle_ref)
        entry = self._secrets.get(handle_ref)
        if not isinstance(entry, dict):
            raise CredentialRefused(
                f"Secret bundle handle '{handle_ref}' is not registered"
            )
        resolved: dict[str, str] = {}
        for field_name in fields:
            val = entry.get(field_name)
            if not isinstance(val, str) or not val.strip():
                raise CredentialRefused(
                    f"Secret bundle handle '{handle_ref}' missing field '{field_name}'"
                )
            resolved[field_name] = val
        return MaskedSecretBundle(values=resolved, handle_ref=handle_ref)


def redact_secret_values(text: str, secrets: Sequence[str]) -> str:
    """Scrub raw, URL-encoded, and base64-encoded secret values from text."""
    if not text or not secrets:
        return text
    sanitized = text
    variants: list[str] = []
    for secret in secrets:
        if not isinstance(secret, str) or len(secret) < 4:
            continue
        variants.append(secret)
        encoded_url = urllib.parse.quote(secret, safe="")
        if encoded_url != secret:
            variants.append(encoded_url)
        encoded_b64 = base64.b64encode(secret.encode("utf-8")).decode("ascii")
        if encoded_b64 != secret:
            variants.append(encoded_b64)
    # Replace longest variants first so partial substrings do not leave residues.
    for token in sorted(set(variants), key=len, reverse=True):
        sanitized = sanitized.replace(token, "[REDACTED_SECRET]")
    return sanitized


def redact_secret_structure(value: Any, secrets: Sequence[str]) -> Any:
    """Recursively scrub resolved secret values from JSON-serializable structures."""
    if not secrets:
        return value
    if isinstance(value, str):
        return redact_secret_values(value, secrets)
    if isinstance(value, Mapping):
        return {
            (
                redact_secret_values(k, secrets) if isinstance(k, str) else k
            ): redact_secret_structure(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_secret_structure(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [redact_secret_structure(item, secrets) for item in value]
    return value


__all__ = [
    "CredentialRefused",
    "HostSecretVault",
    "InMemorySecretVault",
    "MaskedSecret",
    "MaskedSecretBundle",
    "ParsedSecretHandle",
    "SecretVault",
    "assert_handle_environment_isolation",
    "parse_secret_handle",
    "read_macos_keychain_secret",
    "redact_secret_structure",
    "redact_secret_values",
]
