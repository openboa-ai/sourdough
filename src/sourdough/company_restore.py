"""Thin compatibility adapter delegating isolated company restore to storage.checkpoint."""

from collections.abc import Callable
from pathlib import Path
from sqlite3 import Connection

from .storage.checkpoint import (
    CheckpointRefused,
    CoreRestoreReceipt,
    restore_store,
)
from .storage.schema import is_domain_installed

CoreRestore = CoreRestoreReceipt

__all__ = [
    "CheckpointRefused",
    "CoreRestore",
    "restore_core_checkpoint",
]


def _require_core_only(connection: Connection) -> None:
    if is_domain_installed(connection, "paper"):
        raise CheckpointRefused(
            "Paper company requires an independent restore reconciler"
        )


def _restore_checkpoint(
    store: Path,
    company_id: str,
    archive: Path,
    destination: Path,
    *,
    check_original: Callable[[Connection], None] | None = None,
    reconcile: Callable[[Path], None] | None = None,
    external_scope: tuple[object, ...] | None = None,
) -> CoreRestore:
    return restore_store(
        store,
        company_id,
        archive,
        destination,
        check_original=check_original,
        reconcile=reconcile,
        external_scope=external_scope,
    )


def restore_core_checkpoint(
    store: Path, company_id: str, archive: Path, destination: Path
) -> CoreRestore:
    return _restore_checkpoint(
        store,
        company_id,
        archive,
        destination,
        check_original=_require_core_only,
    )
