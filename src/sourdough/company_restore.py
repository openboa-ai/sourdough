"""Thin compatibility adapter delegating isolated company restore to storage.checkpoint."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from sqlite3 import Connection

from .runtime.domains import RestoreDomainGuard
from .storage.checkpoint import (
    CheckpointRefused,
    CoreRestoreReceipt,
    restore_store,
)
from .storage.db import StorageRefused, open_reader, read_tx
from .storage.schema import canonical_domain_name, installed_domains

CoreRestore = CoreRestoreReceipt

__all__ = [
    "CheckpointRefused",
    "CoreRestore",
    "RestoreDomainGuard",
    "restore_core_checkpoint",
]


def _resolve_guard(
    domain: str,
    domain_guards: Mapping[str, RestoreDomainGuard] | None,
) -> RestoreDomainGuard | None:
    if not domain_guards:
        return None
    if domain in domain_guards:
        return domain_guards[domain]
    try:
        canonical = canonical_domain_name(domain)
    except StorageRefused:
        canonical = domain
    if canonical in domain_guards:
        return domain_guards[canonical]
    for guard in domain_guards.values():
        try:
            guard_canonical = canonical_domain_name(guard.domain_name)
        except StorageRefused:
            guard_canonical = guard.domain_name
        if guard_canonical == canonical or guard.domain_name == domain:
            return guard
    return None


def _check_active_domains(
    connection: Connection,
    domain_guards: Mapping[str, RestoreDomainGuard] | None = None,
) -> None:
    for domain in sorted(installed_domains(connection)):
        if domain == "core":
            continue
        guard = _resolve_guard(domain, domain_guards)
        if guard is None or not callable(getattr(guard, "reconcile", None)):
            raise CheckpointRefused(
                f"Domain '{domain}' requires an independent restore reconciler"
            )
        check_fn = getattr(guard, "check_original", None)
        if check_fn is not None:
            if not callable(check_fn):
                raise CheckpointRefused(
                    f"Domain '{domain}' has invalid check_original hook"
                )
            check_fn(connection)


def _reconcile_active_domains(
    shadow_store: Path,
    domain_guards: Mapping[str, RestoreDomainGuard],
) -> None:
    with closing(open_reader(shadow_store)) as connection, read_tx(connection):
        domains = sorted(installed_domains(connection))
    for domain in domains:
        if domain == "core":
            continue
        guard = _resolve_guard(domain, domain_guards)
        if guard is None or not callable(getattr(guard, "reconcile", None)):
            raise CheckpointRefused(
                f"Domain '{domain}' requires an independent restore reconciler"
            )
        guard.reconcile(shadow_store)


def _restore_checkpoint(
    store: Path,
    company_id: str,
    archive: Path,
    destination: Path,
    *,
    desk_destination: Path | None = None,
    check_original: Callable[[Connection], None] | None = None,
    reconcile: Callable[[Path], None] | None = None,
    external_scope: tuple[object, ...] | None = None,
) -> CoreRestore:
    return restore_store(
        store,
        company_id,
        archive,
        destination,
        desk_destination=desk_destination,
        check_original=check_original,
        reconcile=reconcile,
        external_scope=external_scope,
    )


def restore_core_checkpoint(
    store: Path,
    company_id: str,
    archive: Path,
    destination: Path,
    *,
    desk_destination: Path | None = None,
    domain_guards: Mapping[str, RestoreDomainGuard] | None = None,
) -> CoreRestore:
    external_scope: tuple[object, ...] | None = None
    reconcile_fn: Callable[[Path], None] | None = None
    if domain_guards:
        scopes_list: list[tuple[str, tuple[object, ...]]] = []
        for name, guard in sorted(domain_guards.items()):
            if guard is None:
                raise CheckpointRefused(
                    f"Domain '{name}' requires a valid restore guard"
                )
            ext_fn = getattr(guard, "external_scope", None)
            if ext_fn is not None:
                if not callable(ext_fn):
                    raise CheckpointRefused(
                        f"Domain '{name}' has invalid external_scope hook"
                    )
                scope_val = ext_fn()
                if scope_val is not None:
                    scopes_list.append((name, scope_val))
        if scopes_list:
            external_scope = tuple(scopes_list)

        def _reconcile(shadow: Path) -> None:
            _reconcile_active_domains(shadow, domain_guards)

        reconcile_fn = _reconcile
    return _restore_checkpoint(
        store,
        company_id,
        archive,
        destination,
        desk_destination=desk_destination,
        check_original=lambda conn: _check_active_domains(conn, domain_guards),
        reconcile=reconcile_fn,
        external_scope=external_scope,
    )
