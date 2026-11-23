"""WAL-safe online checkpointing, DELETE-mode normalization, and logical-watermark restore."""

import json
import os
import sqlite3
import stat
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import file_digest, sha256
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from time import monotonic, time_ns
from zipfile import ZIP_STORED, BadZipFile, ZipFile

from .db import (
    MAX_SCHEMA_VERSION,
    RETIRED_SCHEMA_REVISION,
    StorageRefused,
    open_reader,
    open_writer,
    read_retirement,
    read_tx,
    verify_no_wal_sidecars,
    write_tx,
)
from .kernel import max_event_seq


CHECKPOINT_FORMAT = "sourdough-checkpoint-v2"
_STORE_ENTRY = "company.sqlite"
_MANIFEST_ENTRY = "manifest.json"
_DESK_PREFIX = "desk/"

_WATERMARK_EXCLUDED_TABLES = frozenset(
    {
        "company_restore_receipt",
        "company_store_retired",
    }
)


class CheckpointRefused(Exception):
    """The checkpoint or restore could not be safely verified or published."""


@dataclass(frozen=True)
class LogicalWatermark:
    company_id: str
    schema_version: int
    max_event_seq: int
    domain_digest: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.company_id, str)
            or not self.company_id
            or type(self.schema_version) is not int
            or not 1 <= self.schema_version <= MAX_SCHEMA_VERSION
            or type(self.max_event_seq) is not int
            or self.max_event_seq < 0
            or not isinstance(self.domain_digest, str)
            or len(self.domain_digest) != 64
            or any(ch not in "0123456789abcdef" for ch in self.domain_digest)
        ):
            raise CheckpointRefused("Invalid logical watermark")


@dataclass(frozen=True)
class CheckpointManifest:
    format: str
    company_id: str
    schema_version: int
    captured_ns: int
    size: int
    sha256: str
    watermark: LogicalWatermark
    desk_files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.format != CHECKPOINT_FORMAT
            or self.company_id != self.watermark.company_id
            or self.schema_version != self.watermark.schema_version
            or type(self.captured_ns) is not int
            or self.captured_ns <= 0
            or type(self.size) is not int
            or self.size <= 0
            or not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in self.sha256)
        ):
            raise CheckpointRefused("Invalid checkpoint manifest")


@dataclass(frozen=True)
class CoreRestoreReceipt:
    company_id: str
    request_revision: str
    checkpoint_sha256: str
    watermark: LogicalWatermark


def _hash_file(path: Path) -> str:
    with path.open("rb") as handle:
        return file_digest(handle, "sha256").hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(directory: Path) -> None:
    descriptor = os.open(directory, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _normalize_cell(value: object) -> object:
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    return value


def compute_logical_watermark(
    connection: sqlite3.Connection, company_id: str
) -> LogicalWatermark:
    """Compute a physical-layout-independent watermark of authoritative domain state."""
    row = connection.execute(
        "SELECT id, schema_revision FROM company WHERE singleton = 1"
    ).fetchone()
    if row is None or str(row[0]) != company_id:
        raise CheckpointRefused("Store does not match the expected company")
    schema_version = int(row[1])
    if schema_version == RETIRED_SCHEMA_REVISION:
        retirement = read_retirement(connection)
        if retirement is None:
            raise CheckpointRefused("Retired store is missing retirement metadata")
        schema_version = retirement.original_schema
    if not 1 <= schema_version <= MAX_SCHEMA_VERSION:
        raise CheckpointRefused(f"Unsupported company schema version: {schema_version}")

    tables = [
        str(item[0])
        for item in connection.execute(
            """SELECT name FROM sqlite_schema
               WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
               ORDER BY name"""
        ).fetchall()
        if str(item[0]) not in _WATERMARK_EXCLUDED_TABLES
    ]
    hasher = sha256()
    for table in tables:
        columns = [
            str(col[1])
            for col in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        ]
        raw_rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
        serialized_rows = sorted(
            json.dumps(
                [_normalize_cell(cell) for cell in record],
                sort_keys=True,
                separators=(",", ":"),
            )
            for record in raw_rows
        )
        hasher.update(
            json.dumps(
                {"table": table, "columns": columns, "rows": serialized_rows},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        hasher.update(b"\n")
    return LogicalWatermark(
        company_id=company_id,
        schema_version=schema_version,
        max_event_seq=max_event_seq(connection, company_id),
        domain_digest=hasher.hexdigest(),
    )


def _normalize_database_to_delete_mode(database: Path) -> None:
    """Convert a staged SQLite file to DELETE journal mode and verify zero sidecars."""
    with closing(
        sqlite3.connect(
            f"{database.absolute().as_uri()}?mode=rw",
            uri=True,
            autocommit=True,
        )
    ) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA trusted_schema = OFF")
        mode = str(conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]).lower()
        if mode != "delete":
            raise CheckpointRefused(
                f"Failed to normalize database to DELETE journal mode: {mode}"
            )
        if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise CheckpointRefused("Staged database failed PRAGMA quick_check")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise CheckpointRefused("Staged database failed PRAGMA foreign_key_check")
    verify_no_wal_sidecars(database)
    _fsync_file(database)


def snapshot_normalized_store(store: Path, destination: Path) -> None:
    """Copy a live WAL store via online backup and normalize it into DELETE mode."""
    if os.path.lexists(destination):
        raise CheckpointRefused("Snapshot destination already exists")
    deadline = monotonic() + 15.0

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if monotonic() >= deadline:
            raise CheckpointRefused("Company checkpoint exceeded its backup deadline")

    try:
        with (
            closing(open_reader(store)) as source,
            closing(sqlite3.connect(destination, autocommit=True)) as snapshot,
        ):
            source.backup(snapshot, pages=128, progress=progress, sleep=0.05)
    except (StorageRefused, sqlite3.Error) as error:
        raise CheckpointRefused(f"Online backup failed: {error}") from error
    _normalize_database_to_delete_mode(destination)


def _validate_desk_relative_path(relative: str) -> PurePosixPath:
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in ("", ".", "..") for part in pure.parts)
    ):
        raise CheckpointRefused(f"Unsafe Desk snapshot path: {relative}")
    return pure


def _collect_desk_files(desk_root: Path) -> list[tuple[str, Path]]:
    resolved_root = desk_root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise CheckpointRefused("Desk snapshot root is not a directory")
    collected: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(resolved_root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        current_dir = Path(dirpath)
        for name in list(dirnames):
            candidate = current_dir / name
            if candidate.is_symlink():
                raise CheckpointRefused(f"Symlink prohibited inside Desk: {name}")
        for name in filenames:
            candidate = current_dir / name
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise CheckpointRefused(f"Symlink prohibited inside Desk: {name}")
            if not stat.S_ISREG(info.st_mode):
                raise CheckpointRefused(f"Non-regular file prohibited in Desk: {name}")
            rel = candidate.relative_to(resolved_root).as_posix()
            _validate_desk_relative_path(rel)
            collected.append((rel, candidate))
    return collected


def _publish_file(candidate: Path, target: Path) -> None:
    _fsync_file(candidate)
    try:
        os.link(candidate, target)
    except FileExistsError as error:
        raise CheckpointRefused("Destination already exists") from error
    _fsync_dir(target.parent)


def checkpoint_store(
    store: Path,
    company_id: str,
    archive: Path,
    *,
    desk_root: Path | None = None,
    lease_directory: Path | None = None,
) -> CheckpointManifest:
    """Capture a verified, DELETE-normalized checkpoint archive under WriterLease."""
    from ..writer_lease import hold_company_writer

    with (
        hold_company_writer(
            store, company_id, lease_directory=lease_directory
        ) as writer,
        TemporaryDirectory(dir=archive.parent, prefix=".checkpoint-") as directory,
    ):
        if os.path.lexists(archive):
            raise CheckpointRefused("Checkpoint destination already exists")
        staged_db = Path(directory) / _STORE_ENTRY
        snapshot_normalized_store(store, staged_db)
        with closing(open_reader(staged_db)) as reader, read_tx(reader):
            watermark = compute_logical_watermark(reader, company_id)
        desk_entries = _collect_desk_files(desk_root) if desk_root is not None else []
        manifest = CheckpointManifest(
            format=CHECKPOINT_FORMAT,
            company_id=company_id,
            schema_version=watermark.schema_version,
            captured_ns=time_ns(),
            size=staged_db.stat().st_size,
            sha256=_hash_file(staged_db),
            watermark=watermark,
            desk_files=tuple(rel for rel, _ in desk_entries),
        )
        candidate = Path(directory) / "checkpoint.zip"
        with candidate.open("xb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            with ZipFile(handle, "w", compression=ZIP_STORED) as output:
                output.writestr(
                    _MANIFEST_ENTRY, json.dumps(asdict(manifest), sort_keys=True)
                )
                output.write(staged_db, _STORE_ENTRY)
                for rel, path in desk_entries:
                    output.write(path, f"{_DESK_PREFIX}{rel}")
        writer.verify()
        _publish_file(candidate, archive)
    return manifest


def _parse_manifest(raw: bytes) -> CheckpointManifest:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CheckpointRefused("Malformed checkpoint manifest JSON") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("watermark"), dict):
        raise CheckpointRefused("Incomplete checkpoint manifest")
    wm = payload["watermark"]
    try:
        watermark = LogicalWatermark(
            company_id=wm["company_id"],
            schema_version=wm["schema_version"],
            max_event_seq=wm["max_event_seq"],
            domain_digest=wm["domain_digest"],
        )
        desk_files = payload.get("desk_files", [])
        if not isinstance(desk_files, list) or not all(
            isinstance(item, str) for item in desk_files
        ):
            raise CheckpointRefused("Invalid desk_files manifest entry")
        for item in desk_files:
            _validate_desk_relative_path(item)
        return CheckpointManifest(
            format=payload["format"],
            company_id=payload["company_id"],
            schema_version=payload["schema_version"],
            captured_ns=payload["captured_ns"],
            size=payload["size"],
            sha256=payload["sha256"],
            watermark=watermark,
            desk_files=tuple(desk_files),
        )
    except KeyError as error:
        raise CheckpointRefused("Missing checkpoint manifest field") from error


def _extract_checkpoint(
    archive: Path,
    company_id: str,
    database: Path,
    *,
    desk_dir: Path | None = None,
) -> CheckpointManifest:
    try:
        descriptor = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise CheckpointRefused("Checkpoint archive could not be opened") from error
    try:
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise CheckpointRefused("Checkpoint archive must be a regular file")
            try:
                with ZipFile(handle, "r") as zf:
                    names = zf.namelist()
                    if _MANIFEST_ENTRY not in names or _STORE_ENTRY not in names:
                        raise CheckpointRefused(
                            "Checkpoint archive is missing required entries"
                        )
                    manifest = _parse_manifest(zf.read(_MANIFEST_ENTRY))
                    if manifest.company_id != company_id:
                        raise CheckpointRefused(
                            "Checkpoint archive belongs to another company"
                        )
                    expected_names = {
                        _MANIFEST_ENTRY,
                        _STORE_ENTRY,
                        *(f"{_DESK_PREFIX}{rel}" for rel in manifest.desk_files),
                    }
                    if set(names) != expected_names or len(names) != len(
                        expected_names
                    ):
                        raise CheckpointRefused(
                            "Checkpoint archive contains unexpected or duplicate entries"
                        )
                    with database.open("xb") as out:
                        os.fchmod(out.fileno(), 0o600)
                        out.write(zf.read(_STORE_ENTRY))
                    if desk_dir is not None and manifest.desk_files:
                        desk_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                        for rel in manifest.desk_files:
                            safe = _validate_desk_relative_path(rel)
                            target = desk_dir.joinpath(*safe.parts)
                            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                            with target.open("xb") as out:
                                os.fchmod(out.fileno(), 0o600)
                                out.write(zf.read(f"{_DESK_PREFIX}{rel}"))
            except BadZipFile as error:
                raise CheckpointRefused("Checkpoint archive is corrupt") from error
    except CheckpointRefused:
        raise
    except OSError as error:
        raise CheckpointRefused("Checkpoint extraction failed") from error

    if (
        database.stat().st_size != manifest.size
        or _hash_file(database) != manifest.sha256
    ):
        raise CheckpointRefused(
            "Extracted checkpoint failed size or SHA-256 verification"
        )

    try:
        with closing(open_reader(database)) as reader, read_tx(reader):
            journal_mode = str(
                reader.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()
            if journal_mode != "delete":
                raise CheckpointRefused(
                    f"Checkpoint archive database must be in DELETE mode, got {journal_mode}"
                )
            if reader.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise CheckpointRefused("Extracted checkpoint failed quick_check")
            if reader.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise CheckpointRefused("Extracted checkpoint failed foreign_key_check")
            actual_watermark = compute_logical_watermark(reader, company_id)
    except StorageRefused as error:
        raise CheckpointRefused(str(error)) from error
    verify_no_wal_sidecars(database)
    if actual_watermark != manifest.watermark:
        raise CheckpointRefused("Extracted checkpoint logical watermark mismatch")
    return manifest


def inspect_checkpoint(archive: Path, company_id: str) -> CheckpointManifest:
    """Validate a checkpoint archive without mutating any live store."""
    with TemporaryDirectory(prefix="sd-checkpoint-inspect-") as directory:
        return _extract_checkpoint(archive, company_id, Path(directory) / _STORE_ENTRY)


def _copy_desk_tree(source_root: Path, dest_root: Path) -> None:
    if os.path.lexists(dest_root):
        raise CheckpointRefused("Desk restore destination already exists")
    dest_root.mkdir(mode=0o700, parents=True)
    for rel, path in _collect_desk_files(source_root):
        safe = _validate_desk_relative_path(rel)
        target = dest_root.joinpath(*safe.parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with target.open("xb") as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(path.read_bytes())
        _fsync_file(target)
    _fsync_dir(dest_root)


def restore_store(
    store: Path,
    company_id: str,
    archive: Path,
    destination: Path,
    *,
    desk_destination: Path | None = None,
    reconcile: Callable[[Path], None] | None = None,
    lease_directory: Path | None = None,
) -> CoreRestoreReceipt:
    """Restore a verified checkpoint into a new isolated store and retire the original.

    Staging, optional reconciliation, and restore receipt persistence all execute
    in DELETE journal mode with verified zero -wal/-shm sidecars before atomic
    publication, preventing silent WAL drop on file move/link.
    """
    from ..writer_lease import hold_company_writer

    target = destination.parent.resolve(strict=True) / destination.name
    if os.path.lexists(target):
        raise CheckpointRefused("Replacement destination already exists")

    with (
        hold_company_writer(
            store, company_id, lease_directory=lease_directory
        ) as lease,
        TemporaryDirectory(dir=target.parent, prefix=".restore-") as work,
    ):
        shadow = Path(work) / "replacement.sqlite"
        staged_desk = Path(work) / "desk"
        manifest = _extract_checkpoint(
            archive,
            company_id,
            shadow,
            desk_dir=staged_desk if desk_destination is not None else None,
        )

        # Verify logical watermark against live original store (immune to WAL layout)
        with closing(open_reader(lease.store)) as original, read_tx(original):
            if read_retirement(original) is not None:
                raise CheckpointRefused("Original store is already retired")
            live_watermark = compute_logical_watermark(original, company_id)
        if live_watermark != manifest.watermark:
            raise CheckpointRefused(
                "Original store logical watermark does not match the checkpoint cut"
            )

        terms = [
            company_id,
            list(lease.store_key),
            manifest.captured_ns,
            manifest.sha256,
            manifest.watermark.domain_digest,
            str(target),
        ]
        request_revision = sha256(
            json.dumps(terms, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        if reconcile is not None:
            reconcile(shadow)
            _normalize_database_to_delete_mode(shadow)

        # Persist restore receipt strictly in DELETE mode with zero WAL sidecars
        with (
            closing(
                open_writer(shadow, create=False, journal_mode="DELETE")
            ) as replacement,
            write_tx(replacement),
        ):
            replacement.execute(
                """CREATE TABLE IF NOT EXISTS company_restore_receipt (
                    request_revision TEXT PRIMARY KEY,
                    checkpoint_sha256 TEXT NOT NULL,
                    watermark_json TEXT NOT NULL,
                    restored_at_ns INTEGER NOT NULL
                ) STRICT"""
            )
            replacement.execute(
                """INSERT INTO company_restore_receipt (
                       request_revision, checkpoint_sha256, watermark_json, restored_at_ns
                   ) VALUES (?, ?, ?, ?)""",
                (
                    request_revision,
                    manifest.sha256,
                    json.dumps(asdict(manifest.watermark), sort_keys=True),
                    time_ns(),
                ),
            )
        _normalize_database_to_delete_mode(shadow)

        # Fence and retire the original store before publishing the replacement
        with (
            closing(
                open_writer(lease.store, create=False, lease=lease)
            ) as original_writer,
            write_tx(original_writer),
        ):
            original_writer.execute(
                """CREATE TABLE IF NOT EXISTS company_store_retired (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    request_revision TEXT NOT NULL,
                    original_schema INTEGER NOT NULL,
                    retired_at_ns INTEGER NOT NULL
                ) STRICT"""
            )
            original_writer.execute(
                """INSERT INTO company_store_retired (
                       singleton, request_revision, original_schema, retired_at_ns
                   ) VALUES (1, ?, ?, ?)""",
                (request_revision, manifest.schema_version, time_ns()),
            )
            original_writer.execute(
                "UPDATE company SET schema_revision = ? WHERE singleton = 1",
                (RETIRED_SCHEMA_REVISION,),
            )

        _publish_file(shadow, target)
        verify_no_wal_sidecars(target)

        if desk_destination is not None and manifest.desk_files:
            _copy_desk_tree(staged_desk, desk_destination)

        return CoreRestoreReceipt(
            company_id=company_id,
            request_revision=request_revision,
            checkpoint_sha256=manifest.sha256,
            watermark=manifest.watermark,
        )
