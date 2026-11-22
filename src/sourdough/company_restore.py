"""Replace a core company only after durable retirement of its exact source."""

import json
import os
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from .company import (
    _RETIRED_SCHEMA_REVISION,
    _company_version,
    _connect,
    _identity,
    _retired_store,
)
from .company_checkpoint import (
    CheckpointRefused,
    _extract_checkpoint,
    _hash,
    _publish,
    _snapshot,
)
from .writer_lease import _hold_company_store


@dataclass(frozen=True)
class CoreRestore:
    company_id: str
    request_revision: str
    checkpoint_sha256: str


def _require_core(connection) -> None:
    if connection.execute(
        """SELECT 1 FROM sqlite_schema WHERE type='table'
           AND name IN ('paper_binding','paper_order','paper_fill')"""
    ).fetchone():
        raise CheckpointRefused(
            "Paper obligations require their independent restore reconciler"
        )


def _replacement_matches(target: Path, receipt: CoreRestore) -> None:
    if target.is_symlink() or not target.is_file() or target.stat().st_nlink != 1:
        raise CheckpointRefused("Replacement path is not the original independent file")
    with closing(_connect(target, "ro")) as database:
        database.execute("BEGIN")
        if (
            _identity(database) != receipt.company_id
            or _retired_store(database) is not None
            or not database.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='company_restore_receipt'"
            ).fetchone()
            or database.execute(
                "SELECT checkpoint_sha256 FROM company_restore_receipt WHERE request_revision=?",
                (receipt.request_revision,),
            ).fetchone()
            != (receipt.checkpoint_sha256,)
        ):
            raise CheckpointRefused("Replacement has no matching restore receipt")


def restore_core_checkpoint(
    store: Path, company_id: str, archive: Path, destination: Path
) -> CoreRestore:
    target = destination.parent.resolve(strict=True) / destination.name
    with (
        _hold_company_store(store, company_id) as lease,
        TemporaryDirectory(dir=target.parent, prefix=".restore-") as work,
    ):
        shadow = Path(work) / "replacement.sqlite"
        manifest = _extract_checkpoint(archive, company_id, shadow)
        revision = sha256(
            json.dumps(
                [
                    company_id,
                    lease.store_key,
                    manifest.captured_ns,
                    manifest.sha256,
                    str(target),
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        receipt = CoreRestore(company_id, revision, manifest.sha256)
        with closing(_connect(lease.store, "ro")) as original:
            original.execute("BEGIN")
            retired = _retired_store(original)
            _require_core(original)
        if retired is not None and retired.request_revision != revision:
            raise CheckpointRefused(
                "Original store is retired for another restore request"
            )
        if os.path.lexists(target):
            if retired is None:
                raise CheckpointRefused("Replacement destination already exists")
            _replacement_matches(target, receipt)
            return receipt

        if retired is None:
            current = Path(work) / "original-cut.sqlite"
            _snapshot(lease.store, current)
            if _hash(current) != manifest.sha256:
                raise CheckpointRefused(
                    "Original store does not match the checkpoint cut"
                )

        with closing(_connect(shadow, "rw")) as replacement, replacement:
            _require_core(replacement)
            replacement.execute(
                """CREATE TABLE IF NOT EXISTS company_restore_receipt (
                    request_revision TEXT PRIMARY KEY, checkpoint_sha256 TEXT NOT NULL
                )"""
            )
            replacement.execute(
                "INSERT INTO company_restore_receipt VALUES (?,?)",
                (receipt.request_revision, receipt.checkpoint_sha256),
            )
        shadow.chmod(0o600)
        lease.verify()
        if retired is None:
            with closing(_connect(lease.store, "rw")) as original, original:
                original.execute("BEGIN IMMEDIATE")
                lease.verify()
                original_id, original_schema = _company_version(original)
                if original_id != company_id:
                    raise CheckpointRefused("Original company no longer matches")
                original.execute(
                    """CREATE TABLE company_store_retired (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        request_revision TEXT NOT NULL, original_schema INTEGER NOT NULL
                    )"""
                )
                original.execute(
                    "INSERT INTO company_store_retired VALUES (1,?,?)",
                    (revision, original_schema),
                )
                original.execute(
                    "UPDATE company SET schema_revision=? WHERE singleton=1",
                    (_RETIRED_SCHEMA_REVISION,),
                )
        lease.verify()
        _publish(shadow, target)
    return receipt
