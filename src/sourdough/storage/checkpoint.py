"""WAL-safe online checkpointing, DELETE-mode normalization, and logical-watermark restore."""

import json
import os
import shutil
import sqlite3
import stat
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import file_digest, sha256
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from time import monotonic, time_ns
from uuid import uuid4
from zipfile import ZIP_STORED, BadZipFile, ZipFile

from .db import (
    RETIRED_SCHEMA_REVISION,
    SCHEMA_VERSION,
    StorageRefused,
    StoreRetirement,
    connect_sqlite,
    open_reader,
    open_writer,
    read_retirement,
    read_tx,
    verify_no_wal_sidecars,
    write_tx,
)
from .kernel import append_audit_event, max_event_seq
from .schema import trusted_triggers_and_views, verify_v1_schema


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
            or self.schema_version != SCHEMA_VERSION
            or type(self.max_event_seq) is not int
            or self.max_event_seq < 0
            or not isinstance(self.domain_digest, str)
            or len(self.domain_digest) != 64
            or any(ch not in "0123456789abcdef" for ch in self.domain_digest)
        ):
            raise CheckpointRefused("Invalid logical watermark")


def _compute_desk_digest(entries: Iterable[tuple[str, str]]) -> str:
    """Compute a canonical SHA-256 digest over sorted (relative_path, file_sha256) pairs."""
    canonical = sorted((str(rel), str(digest)) for rel, digest in entries)
    return sha256(
        json.dumps(canonical, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


EMPTY_DESK_DIGEST = _compute_desk_digest(())


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
    desk_digest: str = EMPTY_DESK_DIGEST
    cut_id: str = ""

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
            or not isinstance(self.desk_digest, str)
            or len(self.desk_digest) != 64
            or any(ch not in "0123456789abcdef" for ch in self.desk_digest)
            or not isinstance(self.cut_id, str)
            or not self.cut_id.strip()
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
    raw_schema_version = int(row[1])
    schema_version = raw_schema_version
    if raw_schema_version == RETIRED_SCHEMA_REVISION:
        retirement = read_retirement(connection)
        if retirement is None:
            raise CheckpointRefused("Retired store is missing retirement metadata")
        schema_version = retirement.original_schema
    if schema_version != SCHEMA_VERSION:
        raise CheckpointRefused(f"Unsupported company schema version: {schema_version}")

    schema_objects = [
        (
            str(item[0]),
            str(item[1]),
            str(item[2]),
            "" if item[3] is None else str(item[3]),
        )
        for item in connection.execute(
            """SELECT type, name, tbl_name, sql FROM sqlite_schema
               WHERE name NOT LIKE 'sqlite_%'
               ORDER BY type, name, tbl_name, COALESCE(sql, '')"""
        ).fetchall()
        if str(item[2]) not in _WATERMARK_EXCLUDED_TABLES
        and str(item[1]) not in _WATERMARK_EXCLUDED_TABLES
    ]
    tables = [
        name for obj_type, name, _tbl, _sql in schema_objects if obj_type == "table"
    ]
    hasher = sha256()
    hasher.update(
        json.dumps(
            {"schema": schema_objects},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    hasher.update(b"\n")
    for table in tables:
        columns = [
            str(col[1])
            for col in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        ]
        raw_rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
        if (
            table == "company"
            and raw_schema_version == RETIRED_SCHEMA_REVISION
            and "schema_revision" in columns
        ):
            rev_index = columns.index("schema_revision")
            raw_rows = [
                tuple(
                    schema_version if idx == rev_index else cell
                    for idx, cell in enumerate(record)
                )
                for record in raw_rows
            ]
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


def _verify_trusted_schema_objects(connection: sqlite3.Connection) -> frozenset[str]:
    """Refuse archives containing untrusted, altered, or missing schema objects."""
    try:
        active_domains = verify_v1_schema(connection)
    except StorageRefused as error:
        raise CheckpointRefused(str(error)) from error
    actual = tuple(
        (str(r[0]), str(r[1]), str(r[2]), str(r[3]))
        for r in connection.execute(
            """SELECT type, name, tbl_name, COALESCE(sql, '')
               FROM sqlite_schema
               WHERE type IN ('trigger', 'view') AND name NOT LIKE 'sqlite_%'
               ORDER BY type, name, tbl_name, COALESCE(sql, '')"""
        ).fetchall()
    )
    expected = trusted_triggers_and_views(active_domains)
    if actual != expected:
        raise CheckpointRefused(
            "Checkpoint database contains unexpected, altered, or missing triggers or views"
        )
    return active_domains


def _normalize_database_to_delete_mode(database: Path) -> None:
    """Convert a staged SQLite file to DELETE journal mode and verify zero sidecars."""
    with closing(
        connect_sqlite(
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
            closing(connect_sqlite(destination, autocommit=True)) as snapshot,
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


def _stage_pinned_desk(
    source_root: Path,
    dest_root: Path,
    *,
    fsync_files: bool = False,
) -> tuple[str, ...]:
    """Copy Desk files using O_NOFOLLOW dir_fd traversal and fstat-pinned reads.

    Every subdirectory and regular file is opened relative to its parent directory
    descriptor with O_NOFOLLOW, verified via fstat on the open descriptor, and
    copied directly from that pinned descriptor into dest_root so pathnames inside
    an Agent-writable Desk are never reopened after validation.
    """
    if os.path.lexists(dest_root):
        raise CheckpointRefused("Desk staging destination already exists")
    if source_root.is_symlink():
        raise CheckpointRefused("Symlink prohibited for Desk snapshot root")
    try:
        parent = source_root.parent.resolve(strict=True)
        root_target = parent / source_root.name
        root_fd = os.open(root_target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise CheckpointRefused("Desk snapshot root is not a safe directory") from error

    dest_root.mkdir(mode=0o700, parents=True)
    created_dirs: set[Path] = {dest_root}
    collected: list[str] = []

    def _walk(dir_fd: int, prefix: PurePosixPath) -> None:
        try:
            names = sorted(os.listdir(dir_fd))
        except OSError as error:
            raise CheckpointRefused("Failed to list Desk directory") from error
        for name in names:
            rel_path = prefix / name if prefix.parts else PurePosixPath(name)
            rel_str = rel_path.as_posix()
            _validate_desk_relative_path(rel_str)
            try:
                info = os.lstat(name, dir_fd=dir_fd)
            except OSError as error:
                raise CheckpointRefused(f"Desk entry disappeared: {rel_str}") from error
            if stat.S_ISLNK(info.st_mode):
                raise CheckpointRefused(f"Symlink prohibited inside Desk: {name}")
            if stat.S_ISDIR(info.st_mode):
                try:
                    child_fd = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=dir_fd,
                    )
                except OSError as error:
                    raise CheckpointRefused(
                        f"Symlink or changed directory prohibited inside Desk: {name}"
                    ) from error
                try:
                    _walk(child_fd, rel_path)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise CheckpointRefused(f"Non-regular file prohibited in Desk: {name}")
            try:
                file_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=dir_fd,
                )
            except OSError as error:
                raise CheckpointRefused(
                    f"Symlink or unreadable file prohibited inside Desk: {name}"
                ) from error
            with os.fdopen(file_fd, "rb") as pinned:
                pinned_info = os.fstat(pinned.fileno())
                if not stat.S_ISREG(pinned_info.st_mode):
                    raise CheckpointRefused(
                        f"Non-regular file prohibited in Desk: {name}"
                    )
                target = dest_root.joinpath(*rel_path.parts)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                ancestor = target.parent
                while ancestor != dest_root and ancestor not in created_dirs:
                    created_dirs.add(ancestor)
                    ancestor = ancestor.parent
                with target.open("xb") as out:
                    os.fchmod(out.fileno(), 0o600)
                    shutil.copyfileobj(pinned, out)
                if fsync_files:
                    _fsync_file(target)
            collected.append(rel_str)

    try:
        _walk(root_fd, PurePosixPath())
    finally:
        os.close(root_fd)
    if fsync_files:
        for directory in sorted(
            created_dirs, key=lambda path: len(path.parts), reverse=True
        ):
            _fsync_dir(directory)
    return tuple(collected)


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
        # Stage and hash Desk files first so symlink/file refusals fail before mutating the live store.
        staged_desk = Path(directory) / "desk"
        desk_files = (
            _stage_pinned_desk(desk_root, staged_desk) if desk_root is not None else ()
        )
        desk_digest = _compute_desk_digest(
            (
                rel,
                _hash_file(
                    staged_desk.joinpath(*_validate_desk_relative_path(rel).parts)
                ),
            )
            for rel in desk_files
        )
        # Bind the Desk digest into the authoritative append-only audit log before snapshotting SQLite.
        cut_id = uuid4().hex
        with (
            closing(
                open_writer(writer.store, create=False, lease=writer)
            ) as live_writer,
            write_tx(live_writer),
        ):
            append_audit_event(
                live_writer,
                company_id=company_id,
                domain="ops",
                entity_type="checkpoint",
                entity_id=cut_id,
                event_type="company.checkpoint_cut",
                actor_id="checkpoint",
                payload={
                    "desk_digest": desk_digest,
                    "desk_files": list(desk_files),
                },
            )
        staged_db = Path(directory) / _STORE_ENTRY
        snapshot_normalized_store(store, staged_db)
        with closing(open_reader(staged_db)) as reader, read_tx(reader):
            watermark = compute_logical_watermark(reader, company_id)
        manifest = CheckpointManifest(
            format=CHECKPOINT_FORMAT,
            company_id=company_id,
            schema_version=watermark.schema_version,
            captured_ns=time_ns(),
            size=staged_db.stat().st_size,
            sha256=_hash_file(staged_db),
            watermark=watermark,
            desk_files=desk_files,
            desk_digest=desk_digest,
            cut_id=cut_id,
        )
        candidate = Path(directory) / "checkpoint.zip"
        with candidate.open("xb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            with ZipFile(handle, "w", compression=ZIP_STORED) as output:
                output.writestr(
                    _MANIFEST_ENTRY, json.dumps(asdict(manifest), sort_keys=True)
                )
                output.write(staged_db, _STORE_ENTRY)
                for rel in desk_files:
                    safe = _validate_desk_relative_path(rel)
                    output.write(
                        staged_desk.joinpath(*safe.parts), f"{_DESK_PREFIX}{rel}"
                    )
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
            desk_digest=payload["desk_digest"],
            cut_id=payload["cut_id"],
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
                    desk_hashes: list[tuple[str, str]] = []
                    if manifest.desk_files:
                        if desk_dir is not None:
                            desk_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                        for rel in manifest.desk_files:
                            safe = _validate_desk_relative_path(rel)
                            content = zf.read(f"{_DESK_PREFIX}{rel}")
                            desk_hashes.append((rel, sha256(content).hexdigest()))
                            if desk_dir is not None:
                                target = desk_dir.joinpath(*safe.parts)
                                target.parent.mkdir(
                                    mode=0o700, parents=True, exist_ok=True
                                )
                                with target.open("xb") as out:
                                    os.fchmod(out.fileno(), 0o600)
                                    out.write(content)
                    if _compute_desk_digest(desk_hashes) != manifest.desk_digest:
                        raise CheckpointRefused(
                            "Extracted Desk contents failed SHA-256 verification"
                        )
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
            _verify_trusted_schema_objects(reader)
            cut_rows = reader.execute(
                """SELECT payload_json FROM audit_event
                   WHERE company_id = ? AND domain = 'ops'
                     AND entity_type = 'checkpoint' AND entity_id = ?
                     AND event_type = 'company.checkpoint_cut'""",
                (company_id, manifest.cut_id),
            ).fetchall()
            if len(cut_rows) != 1:
                raise CheckpointRefused(
                    "Checkpoint cut is not authenticated by authoritative audit log"
                )
            try:
                cut_payload = json.loads(str(cut_rows[0][0]))
            except json.JSONDecodeError as error:
                raise CheckpointRefused(
                    "Malformed checkpoint cut audit event"
                ) from error
            if (
                not isinstance(cut_payload, dict)
                or cut_payload.get("desk_digest") != manifest.desk_digest
                or cut_payload.get("desk_files") != list(manifest.desk_files)
            ):
                raise CheckpointRefused(
                    "Checkpoint Desk contents do not match authoritative audit digest"
                )
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
    with TemporaryDirectory(
        dir=dest_root.parent, prefix=".sourdough-desk-tmp-"
    ) as staging:
        candidate = Path(staging) / "desk"
        _stage_pinned_desk(source_root, candidate, fsync_files=True)
        if os.path.lexists(dest_root):
            raise CheckpointRefused("Desk restore destination already exists")
        os.rename(candidate, dest_root)
    try:
        _fsync_dir(dest_root.parent)
    except Exception:
        shutil.rmtree(dest_root, ignore_errors=True)
        raise


def _restore_request_revision(
    company_id: str,
    store_key: tuple[int, int],
    manifest: CheckpointManifest,
    target: Path,
    *,
    external_scope: tuple[object, ...] | None = None,
) -> str:
    terms: list[object] = [
        company_id,
        list(store_key),
        manifest.captured_ns,
        manifest.sha256,
        manifest.watermark.domain_digest,
        str(target),
    ]
    if external_scope is not None:
        terms.append(list(external_scope))
    return sha256(json.dumps(terms, separators=(",", ":")).encode("utf-8")).hexdigest()


def _staged_restore_paths(
    target: Path, desk_target: Path | None, request_revision: str, has_desk: bool
) -> tuple[Path, Path | None]:
    db_stage = target.with_name(f".sourdough-restore-{request_revision[:16]}.sqlite")
    desk_stage = (
        desk_target.with_name(f".sourdough-desk-stage-{request_revision[:16]}")
        if desk_target is not None and has_desk
        else None
    )
    return db_stage, desk_stage


def _open_private_parent_dir(directory: Path) -> int:
    """Open and verify a destination parent directory is owner-only (0o700)."""
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise CheckpointRefused(
            "Restore destination parent directory is missing or unsafe"
        ) from error
    try:
        info = os.fstat(dir_fd)
        if info.st_uid != os.geteuid() or (info.st_mode & 0o077) != 0:
            raise CheckpointRefused(
                "Restore destination parent directory must be owner-only (0o700)"
            )
    except BaseException:
        os.close(dir_fd)
        raise
    return dir_fd


def _copy_and_verify_replacement(
    candidate: Path,
    private_dir_fd: int,
    private_copy: Path,
    company_id: str,
    request_revision: str,
    manifest: CheckpointManifest,
    *,
    expected_sha256: str | None,
    pinned_src_fd: int | None = None,
) -> tuple[int, tuple[int, int]]:
    """Stream candidate from a pinned O_NOFOLLOW fd into a private file and return its open pinned fd."""
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in expected_sha256)
    ):
        raise CheckpointRefused(
            "Retired store is missing authenticated replacement digest"
        )
    try:
        verify_no_wal_sidecars(candidate)
    except StorageRefused as error:
        raise CheckpointRefused(str(error)) from error
    if pinned_src_fd is not None:
        src_fd = os.dup(pinned_src_fd)
        os.lseek(src_fd, 0, os.SEEK_SET)
    else:
        try:
            src_fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as error:
            raise CheckpointRefused(
                "Staged replacement database is missing or unsafe"
            ) from error
    out_fd: int | None = None
    try:
        with os.fdopen(src_fd, "rb") as pinned:
            if not stat.S_ISREG(os.fstat(pinned.fileno()).st_mode):
                raise CheckpointRefused(
                    "Staged replacement database must be a regular file"
                )
            out_fd = os.open(
                private_copy.name,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
                dir_fd=private_dir_fd,
            )
            os.fchmod(out_fd, 0o600)
            hasher = sha256()
            while chunk := pinned.read(131_072):
                hasher.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(out_fd, view)
                    view = view[written:]
        assert out_fd is not None
        os.fsync(out_fd)
        os.fchmod(out_fd, 0o400)
        computed_sha256 = hasher.hexdigest()
        if expected_sha256 is not None and computed_sha256 != expected_sha256:
            raise CheckpointRefused(
                "Staged replacement database digest does not match retired store record"
            )
        out_st = os.fstat(out_fd)
        verified_key = (out_st.st_dev, out_st.st_ino)
        try:
            with closing(open_reader(private_copy)) as reader, read_tx(reader):
                mode = str(reader.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                if mode != "delete":
                    raise CheckpointRefused(
                        f"Staged replacement must be in DELETE mode, got {mode}"
                    )
                if reader.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise CheckpointRefused("Staged replacement failed quick_check")
                if reader.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise CheckpointRefused(
                        "Staged replacement failed foreign_key_check"
                    )
                company_row = reader.execute(
                    "SELECT id FROM company WHERE singleton = 1"
                ).fetchone()
                if company_row is None or str(company_row[0]) != company_id:
                    raise CheckpointRefused("Staged replacement company mismatch")
                _verify_trusted_schema_objects(reader)
                row = reader.execute(
                    """SELECT checkpoint_sha256, watermark_json
                       FROM company_restore_receipt
                       WHERE request_revision = ?""",
                    (request_revision,),
                ).fetchone()
        except (StorageRefused, sqlite3.Error) as error:
            raise CheckpointRefused(
                f"Staged replacement verification failed: {error}"
            ) from error
        expected_wm = json.dumps(asdict(manifest.watermark), sort_keys=True)
        if row is None or row[0] != manifest.sha256 or row[1] != expected_wm:
            raise CheckpointRefused(
                "Staged replacement receipt does not match this restore request"
            )
        return out_fd, verified_key
    except BaseException:
        if out_fd is not None:
            os.close(out_fd)
        raise


def _verify_staged_replacement(
    candidate: Path,
    company_id: str,
    request_revision: str,
    manifest: CheckpointManifest,
    *,
    expected_sha256: str | None,
    pinned_src_fd: int | None = None,
) -> None:
    """Verify a staged or published replacement DB via a private pinned copy."""
    with TemporaryDirectory(prefix="sd-db-verify-") as check_dir:
        check_path = Path(check_dir)
        check_dir_fd = os.open(check_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            pinned_fd, _key = _copy_and_verify_replacement(
                candidate,
                check_dir_fd,
                check_path / "replacement.sqlite",
                company_id,
                request_revision,
                manifest,
                expected_sha256=expected_sha256,
                pinned_src_fd=pinned_src_fd,
            )
            os.close(pinned_fd)
        finally:
            os.close(check_dir_fd)


def _pub_dir_path(target: Path, request_revision: str) -> Path:
    return target.with_name(f".sourdough-pub-{request_revision[:16]}")


def _finalize_published_replacement(
    target: Path,
    company_id: str,
    request_revision: str,
    manifest: CheckpointManifest,
    *,
    expected_sha256: str,
) -> None:
    """Pin target first, authenticate that pinned inode, recover pub links/mode, and confirm identity."""
    parent_fd = _open_private_parent_dir(target.parent)
    try:
        try:
            pub_fd = os.open(
                target.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
        except OSError as error:
            raise CheckpointRefused(
                "Published replacement database is missing or unsafe"
            ) from error
        try:
            pub_st = os.fstat(pub_fd)
            if not stat.S_ISREG(pub_st.st_mode):
                raise CheckpointRefused(
                    "Published replacement database must be a regular file"
                )
            target_key = (pub_st.st_dev, pub_st.st_ino)
            _verify_staged_replacement(
                target,
                company_id,
                request_revision,
                manifest,
                expected_sha256=expected_sha256,
                pinned_src_fd=pub_fd,
            )
            try:
                pre_st = os.lstat(target.name, dir_fd=parent_fd)
            except OSError as error:
                raise CheckpointRefused(
                    "Published replacement disappeared during recovery"
                ) from error
            if (
                not stat.S_ISREG(pre_st.st_mode)
                or (pre_st.st_dev, pre_st.st_ino) != target_key
            ):
                raise CheckpointRefused(
                    "Published replacement inode changed during recovery"
                )
            pub_dir = _pub_dir_path(target, request_revision)
            try:
                pub_dir_fd = os.open(
                    pub_dir.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                pub_dir_fd = None
            except OSError as error:
                raise CheckpointRefused(
                    "Restore publication directory is unsafe"
                ) from error
            if pub_dir_fd is not None:
                try:
                    dir_st = os.fstat(pub_dir_fd)
                    if dir_st.st_uid != os.geteuid() or (dir_st.st_mode & 0o077) != 0:
                        raise CheckpointRefused(
                            "Restore publication directory must be owner-only (0o700)"
                        )
                    try:
                        link_st = os.lstat("replacement.sqlite", dir_fd=pub_dir_fd)
                    except FileNotFoundError:
                        link_st = None
                    if link_st is not None:
                        if (
                            not stat.S_ISREG(link_st.st_mode)
                            or (link_st.st_dev, link_st.st_ino) != target_key
                        ):
                            raise CheckpointRefused(
                                "Unexpected artifact inside restore publication directory"
                            )
                        os.unlink("replacement.sqlite", dir_fd=pub_dir_fd)
                        os.fsync(pub_dir_fd)
                finally:
                    os.close(pub_dir_fd)
                os.rmdir(pub_dir.name, dir_fd=parent_fd)
                os.fsync(parent_fd)

            try:
                os.fchmod(pub_fd, 0o600)
                os.fsync(pub_fd)
            except OSError as error:
                raise CheckpointRefused(
                    "Published replacement inode changed during recovery"
                ) from error
            final_st = os.fstat(pub_fd)
            if final_st.st_nlink != 1:
                raise CheckpointRefused(
                    "Published replacement has unexpected hard-link aliases"
                )
            # Confirm destination still names the exact authenticated inode and bytes.
            try:
                cur_st = os.lstat(target.name, dir_fd=parent_fd)
            except OSError as error:
                raise CheckpointRefused(
                    "Published replacement disappeared during recovery"
                ) from error
            if (
                not stat.S_ISREG(cur_st.st_mode)
                or (cur_st.st_dev, cur_st.st_ino) != target_key
            ):
                raise CheckpointRefused(
                    "Published replacement inode changed during recovery"
                )
            dup_fd = os.dup(pub_fd)
            os.lseek(dup_fd, 0, os.SEEK_SET)
            with os.fdopen(dup_fd, "rb") as verified:
                if file_digest(verified, "sha256").hexdigest() != expected_sha256:
                    raise CheckpointRefused(
                        "Published replacement digest changed during recovery"
                    )
            try:
                verify_no_wal_sidecars(target)
            except StorageRefused as error:
                raise CheckpointRefused(str(error)) from error
        finally:
            os.close(pub_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _publish_authenticated_replacement(
    staged_db_sibling: Path,
    target: Path,
    company_id: str,
    request_revision: str,
    manifest: CheckpointManifest,
    *,
    expected_sha256: str,
) -> None:
    """Verify staged DB via a pinned fd in an owner-only parent and verify post-link inode/digest."""
    parent_fd = _open_private_parent_dir(target.parent)
    pub_dir = _pub_dir_path(target, request_revision)
    published_verified = False
    try:
        # Discard any incomplete deterministic publication dir from an earlier crash
        # before os.link (target does not exist here).
        try:
            old_dir_fd = os.open(
                pub_dir.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            old_dir_fd = None
        except OSError as error:
            raise CheckpointRefused("Restore publication path is unsafe") from error
        if old_dir_fd is not None:
            try:
                old_st = os.fstat(old_dir_fd)
                if old_st.st_uid != os.geteuid() or (old_st.st_mode & 0o077) != 0:
                    raise CheckpointRefused(
                        "Restore publication directory must be owner-only (0o700)"
                    )
            finally:
                os.close(old_dir_fd)
            shutil.rmtree(pub_dir)
            os.fsync(parent_fd)

        os.mkdir(pub_dir.name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
        try:
            private_dir_fd = os.open(
                pub_dir.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            try:
                private_copy = pub_dir / "replacement.sqlite"
                pinned_fd, verified_key = _copy_and_verify_replacement(
                    staged_db_sibling,
                    private_dir_fd,
                    private_copy,
                    company_id,
                    request_revision,
                    manifest,
                    expected_sha256=expected_sha256,
                )
                try:
                    os.fsync(pinned_fd)
                    try:
                        os.link(
                            private_copy.name,
                            target.name,
                            src_dir_fd=private_dir_fd,
                            dst_dir_fd=parent_fd,
                        )
                    except FileExistsError as error:
                        raise CheckpointRefused("Destination already exists") from error
                    try:
                        pub_fd = os.open(
                            target.name,
                            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            dir_fd=parent_fd,
                        )
                        with os.fdopen(pub_fd, "rb") as published:
                            pub_st = os.fstat(published.fileno())
                            if (
                                not stat.S_ISREG(pub_st.st_mode)
                                or (pub_st.st_dev, pub_st.st_ino) != verified_key
                                or file_digest(published, "sha256").hexdigest()
                                != expected_sha256
                            ):
                                raise CheckpointRefused(
                                    "Published replacement inode or digest changed during publication"
                                )
                    except BaseException:
                        try:
                            os.unlink(target.name, dir_fd=parent_fd)
                            os.fsync(parent_fd)
                        except OSError:
                            pass
                        raise
                    published_verified = True
                    os.fchmod(pinned_fd, 0o600)
                    os.fsync(pinned_fd)
                    os.fsync(parent_fd)
                    os.unlink(private_copy.name, dir_fd=private_dir_fd)
                    os.fsync(private_dir_fd)
                finally:
                    os.close(pinned_fd)
            finally:
                os.close(private_dir_fd)
            os.rmdir(pub_dir.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except BaseException:
            if not published_verified and os.path.lexists(pub_dir):
                shutil.rmtree(pub_dir, ignore_errors=True)
                try:
                    os.fsync(parent_fd)
                except OSError:
                    pass
            raise
    finally:
        os.close(parent_fd)
    verify_no_wal_sidecars(target)


def _publish_authenticated_desk(
    staged_desk_sibling: Path,
    desk_target: Path,
    manifest: CheckpointManifest,
) -> None:
    """Pin, verify, and atomically publish a staged Desk tree inside an owner-only parent."""
    parent_fd = _open_private_parent_dir(desk_target.parent)
    try:
        with TemporaryDirectory(
            dir=desk_target.parent, prefix=".sourdough-desk-pub-"
        ) as private_dir:
            private_dir_path = Path(private_dir)
            private_dir_fd = os.open(
                private_dir_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                private_desk = private_dir_path / "desk"
                files = _stage_pinned_desk(
                    staged_desk_sibling, private_desk, fsync_files=True
                )
                digest = _compute_desk_digest(
                    (
                        rel,
                        _hash_file(
                            private_desk.joinpath(
                                *_validate_desk_relative_path(rel).parts
                            )
                        ),
                    )
                    for rel in files
                )
                if files != manifest.desk_files or digest != manifest.desk_digest:
                    raise CheckpointRefused(
                        "Staged Desk snapshot does not match authenticated checkpoint digest"
                    )
                pinned_desk_fd = os.open(
                    "desk",
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=private_dir_fd,
                )
                try:
                    desk_st = os.fstat(pinned_desk_fd)
                    verified_desk_key = (desk_st.st_dev, desk_st.st_ino)
                    try:
                        os.lstat(desk_target.name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
                    else:
                        raise CheckpointRefused(
                            "Desk restore destination already exists"
                        )
                    os.rename(
                        "desk",
                        desk_target.name,
                        src_dir_fd=private_dir_fd,
                        dst_dir_fd=parent_fd,
                    )
                    try:
                        pub_desk_fd = os.open(
                            desk_target.name,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=parent_fd,
                        )
                        try:
                            pub_st = os.fstat(pub_desk_fd)
                            if (pub_st.st_dev, pub_st.st_ino) != verified_desk_key:
                                raise CheckpointRefused(
                                    "Published Desk inode changed during publication"
                                )
                        finally:
                            os.close(pub_desk_fd)
                        _verify_staged_desk_snapshot(desk_target, manifest)
                    except BaseException:
                        shutil.rmtree(desk_target, ignore_errors=True)
                        try:
                            os.fsync(parent_fd)
                        except OSError:
                            pass
                        raise
                finally:
                    os.close(pinned_desk_fd)
            finally:
                os.close(private_dir_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    shutil.rmtree(staged_desk_sibling, ignore_errors=True)
    _fsync_dir(staged_desk_sibling.parent)


def _verify_staged_desk_snapshot(
    staged_desk_dir: Path, manifest: CheckpointManifest
) -> None:
    """Verify a durable staged Desk directory matches the authenticated manifest digest."""
    with TemporaryDirectory(prefix="sd-desk-verify-") as check_dir:
        verify_root = Path(check_dir) / "desk"
        files = _stage_pinned_desk(staged_desk_dir, verify_root)
        digest = _compute_desk_digest(
            (
                rel,
                _hash_file(
                    verify_root.joinpath(*_validate_desk_relative_path(rel).parts)
                ),
            )
            for rel in files
        )
    if files != manifest.desk_files or digest != manifest.desk_digest:
        raise CheckpointRefused(
            "Staged Desk snapshot does not match authenticated checkpoint digest"
        )


def _resume_retired_restore(
    store: Path,
    store_key: tuple[int, int],
    company_id: str,
    archive: Path,
    target: Path,
    desk_target: Path | None,
    retirement: StoreRetirement,
    *,
    check_original: Callable[[sqlite3.Connection], None] | None = None,
    external_scope: tuple[object, ...] | None = None,
) -> CoreRestoreReceipt:
    """Finish publishing an interrupted restore without re-enabling the retired original."""
    manifest = inspect_checkpoint(archive, company_id)
    if manifest.desk_files and desk_target is None:
        raise CheckpointRefused(
            "Checkpoint archive bundles Desk files; desk_destination is required"
        )
    with closing(open_reader(store)) as original, read_tx(original):
        if check_original is not None:
            check_original(original)
        live_watermark = compute_logical_watermark(original, company_id)
    if live_watermark != manifest.watermark:
        raise CheckpointRefused(
            "Retired store logical watermark does not match the checkpoint cut"
        )
    request_revision = _restore_request_revision(
        company_id, store_key, manifest, target, external_scope=external_scope
    )
    if retirement.request_revision != request_revision:
        raise CheckpointRefused(
            "Original store is already retired by a different restore request"
        )
    staged_db_sibling, staged_desk_sibling = _staged_restore_paths(
        target, desk_target, request_revision, bool(manifest.desk_files)
    )
    has_pending_db = not os.path.lexists(target)
    has_pending_desk = False
    if staged_desk_sibling is not None and desk_target is not None:
        if os.path.lexists(desk_target):
            if has_pending_db and os.path.lexists(staged_desk_sibling):
                raise CheckpointRefused(
                    "Desk restore destination already exists while staged Desk is unpublished"
                )
            _verify_staged_desk_snapshot(desk_target, manifest)
        else:
            if not os.path.lexists(staged_desk_sibling):
                raise CheckpointRefused("Staged Desk recovery snapshot is missing")
            _verify_staged_desk_snapshot(staged_desk_sibling, manifest)
            has_pending_desk = True

    if not has_pending_db and not has_pending_desk:
        _finalize_published_replacement(
            target,
            company_id,
            request_revision,
            manifest,
            expected_sha256=retirement.replacement_sha256,
        )
        if staged_desk_sibling is not None and os.path.lexists(staged_desk_sibling):
            shutil.rmtree(staged_desk_sibling, ignore_errors=True)
            _fsync_dir(staged_desk_sibling.parent)
        if os.path.lexists(staged_db_sibling):
            os.unlink(staged_db_sibling)
        _fsync_dir(target.parent)
        if staged_desk_sibling is not None and desk_target is not None:
            _fsync_dir(desk_target.parent)
        return CoreRestoreReceipt(
            company_id=company_id,
            request_revision=request_revision,
            checkpoint_sha256=manifest.sha256,
            watermark=manifest.watermark,
        )

    if has_pending_db:
        _publish_authenticated_replacement(
            staged_db_sibling,
            target,
            company_id,
            request_revision,
            manifest,
            expected_sha256=retirement.replacement_sha256,
        )
    else:
        _finalize_published_replacement(
            target,
            company_id,
            request_revision,
            manifest,
            expected_sha256=retirement.replacement_sha256,
        )

    if has_pending_desk and staged_desk_sibling is not None and desk_target is not None:
        _publish_authenticated_desk(staged_desk_sibling, desk_target, manifest)

    if os.path.lexists(staged_db_sibling):
        os.unlink(staged_db_sibling)
    _fsync_dir(target.parent)

    return CoreRestoreReceipt(
        company_id=company_id,
        request_revision=request_revision,
        checkpoint_sha256=manifest.sha256,
        watermark=manifest.watermark,
    )


def restore_store(
    store: Path,
    company_id: str,
    archive: Path,
    destination: Path,
    *,
    desk_destination: Path | None = None,
    reconcile: Callable[[Path], None] | None = None,
    check_original: Callable[[sqlite3.Connection], None] | None = None,
    external_scope: tuple[object, ...] | None = None,
    lease_directory: Path | None = None,
) -> CoreRestoreReceipt:
    """Restore a verified checkpoint into a new isolated store and retire the original.

    Staging, optional reconciliation, and restore receipt persistence all execute
    in DELETE journal mode with verified zero -wal/-shm sidecars before atomic
    publication, preventing silent WAL drop on file move/link. Durable staged
    artifacts are preserved across post-retirement publication failures so a
    matching retry can finish publication without re-enabling the retired store.
    """
    from ..writer_lease import hold_company_writer

    target = destination.parent.resolve(strict=True) / destination.name
    os.close(_open_private_parent_dir(target.parent))
    desk_target: Path | None = None
    if desk_destination is not None:
        desk_target = (
            desk_destination.parent.resolve(strict=True) / desk_destination.name
        )
        os.close(_open_private_parent_dir(desk_target.parent))

    # Check whether the original store was already retired by an interrupted publication
    # of this exact restore request; if so, finish publication read-only without
    # re-enabling the original writer.
    try:
        store_fd = os.open(store, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise CheckpointRefused("Original company store could not be opened") from error
    try:
        store_info = os.fstat(store_fd)
        if not stat.S_ISREG(store_info.st_mode):
            raise CheckpointRefused("Original company store must be a regular file")
        store_key = (store_info.st_dev, store_info.st_ino)
    finally:
        os.close(store_fd)

    with closing(open_reader(store)) as probe, read_tx(probe):
        existing_retirement = read_retirement(probe)
    if existing_retirement is not None:
        return _resume_retired_restore(
            store,
            store_key,
            company_id,
            archive,
            target,
            desk_target,
            existing_retirement,
            check_original=check_original,
            external_scope=external_scope,
        )

    if os.path.lexists(target):
        raise CheckpointRefused("Replacement destination already exists")
    if desk_target is not None and os.path.lexists(desk_target):
        raise CheckpointRefused("Desk restore destination already exists")

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
            desk_dir=staged_desk if desk_target is not None else None,
        )
        if manifest.desk_files and desk_target is None:
            raise CheckpointRefused(
                "Checkpoint archive bundles Desk files; desk_destination is required"
            )

        # Verify logical watermark against live original store (immune to WAL layout)
        with closing(open_reader(lease.store)) as original, read_tx(original):
            if read_retirement(original) is not None:
                raise CheckpointRefused("Original store is already retired")
            if check_original is not None:
                check_original(original)
            live_watermark = compute_logical_watermark(original, company_id)
        if live_watermark != manifest.watermark:
            raise CheckpointRefused(
                "Original store logical watermark does not match the checkpoint cut"
            )

        request_revision = _restore_request_revision(
            company_id,
            lease.store_key,
            manifest,
            target,
            external_scope=external_scope,
        )

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

        # Persist durable staged artifacts on destination filesystems BEFORE retiring
        # the original store so any post-retirement publication I/O error is resumable.
        # Under the live writer lease, verify and discard any uncommitted deterministic
        # stages left behind by a prior pre-retirement process crash before restaging.
        staged_db_sibling, staged_desk_sibling = _staged_restore_paths(
            target, desk_target, request_revision, bool(manifest.desk_files)
        )
        if os.path.lexists(staged_db_sibling):
            _verify_staged_replacement(
                staged_db_sibling,
                company_id,
                request_revision,
                manifest,
                expected_sha256=None,
            )
            os.unlink(staged_db_sibling)
            _fsync_dir(staged_db_sibling.parent)
        if staged_desk_sibling is not None and os.path.lexists(staged_desk_sibling):
            _verify_staged_desk_snapshot(staged_desk_sibling, manifest)
            shutil.rmtree(staged_desk_sibling)
            _fsync_dir(staged_desk_sibling.parent)

        retired = False
        db_staged = False
        desk_staged = False
        try:
            # Hash the finalized private shadow before exposing it, publish durable DB
            # stage atomically, record ownership immediately on link creation, and
            # unlink temporary shadow so staged_db_sibling keeps st_nlink == 1 across crashes.
            _fsync_file(shadow)
            replacement_sha256 = _hash_file(shadow)
            try:
                os.link(shadow, staged_db_sibling)
            except FileExistsError as error:
                raise CheckpointRefused(
                    "Staged replacement path already exists"
                ) from error
            db_staged = True
            _fsync_dir(staged_db_sibling.parent)
            os.unlink(shadow)
            _fsync_dir(shadow.parent)
            if staged_desk_sibling is not None and desk_target is not None:
                if os.path.lexists(desk_target):
                    raise CheckpointRefused("Desk restore destination already exists")
                _copy_desk_tree(staged_desk, staged_desk_sibling)
                desk_staged = True

            # Fence and retire the original store only after all durable candidates are staged.
            # Re-verify retirement state, logical watermark, and unoccupied targets
            # inside the BEGIN IMMEDIATE transaction so any intervening write (even
            # from a caller that did not acquire WriterLease) refuses restore cleanly.
            with (
                closing(
                    open_writer(lease.store, create=False, lease=lease)
                ) as original_writer,
                write_tx(original_writer),
            ):
                if read_retirement(original_writer) is not None:
                    raise CheckpointRefused("Original store is already retired")
                if check_original is not None:
                    check_original(original_writer)
                current_watermark = compute_logical_watermark(
                    original_writer, company_id
                )
                if current_watermark != manifest.watermark:
                    raise CheckpointRefused(
                        "Original store logical watermark changed before restore retirement"
                    )
                if os.path.lexists(target):
                    raise CheckpointRefused("Replacement destination already exists")
                if (
                    staged_desk_sibling is not None
                    and desk_target is not None
                    and os.path.lexists(desk_target)
                ):
                    raise CheckpointRefused("Desk restore destination already exists")
                original_writer.execute(
                    """INSERT INTO company_store_retired (
                           singleton, request_revision, original_schema, replacement_sha256, retired_at_ns
                       ) VALUES (1, ?, ?, ?, ?)""",
                    (
                        request_revision,
                        manifest.schema_version,
                        replacement_sha256,
                        time_ns(),
                    ),
                )
                original_writer.execute(
                    "UPDATE company SET schema_revision = ? WHERE singleton = 1",
                    (RETIRED_SCHEMA_REVISION,),
                )
            retired = True

            _publish_authenticated_replacement(
                staged_db_sibling,
                target,
                company_id,
                request_revision,
                manifest,
                expected_sha256=replacement_sha256,
            )

            if staged_desk_sibling is not None and desk_target is not None:
                _publish_authenticated_desk(staged_desk_sibling, desk_target, manifest)
                desk_staged = False

            os.unlink(staged_db_sibling)
            _fsync_dir(target.parent)
            db_staged = False
        finally:
            if not retired:
                if db_staged and os.path.lexists(staged_db_sibling):
                    os.unlink(staged_db_sibling)
                if (
                    desk_staged
                    and staged_desk_sibling is not None
                    and os.path.lexists(staged_desk_sibling)
                ):
                    shutil.rmtree(staged_desk_sibling, ignore_errors=True)

        return CoreRestoreReceipt(
            company_id=company_id,
            request_revision=request_revision,
            checkpoint_sha256=manifest.sha256,
            watermark=manifest.watermark,
        )
