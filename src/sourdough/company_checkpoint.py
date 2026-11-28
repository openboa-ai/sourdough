"""Thin compatibility adapter delegating company checkpoints to storage.checkpoint."""

import os
from pathlib import Path

from .agent_desk import desks_root
from .storage.checkpoint import (
    CheckpointManifest,
    CheckpointRefused,
    checkpoint_store,
    inspect_checkpoint,
)

__all__ = [
    "CheckpointManifest",
    "CheckpointRefused",
    "checkpoint_company",
    "inspect_company_checkpoint",
]


def checkpoint_company(
    store: Path, company_id: str, archive: Path
) -> CheckpointManifest:
    desks = desks_root(store)
    return checkpoint_store(
        store,
        company_id,
        archive,
        desk_root=desks if os.path.lexists(desks) else None,
    )


def inspect_company_checkpoint(archive: Path, company_id: str) -> CheckpointManifest:
    return inspect_checkpoint(archive, company_id)
