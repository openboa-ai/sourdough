"""Capture and inspect an isolated checkpoint of the company store."""

import json
import os
import shutil
import sqlite3
import stat
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import file_digest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, time_ns
from zipfile import ZIP_STORED, BadZipFile, ZipFile

from .company import _company_version, _connect
from .writer_lease import hold_company_writer


_FORMAT = "sourdough-checkpoint-v1"
_STORE = "company.sqlite"
_MANIFEST = "manifest.json"


class CheckpointRefused(Exception):
    """The archive cannot be captured or verified against its original scope."""


@dataclass(frozen=True)
class CheckpointManifest:
    format: str
    company_id: str
    schema_revision: int
    captured_ns: int
    size: int
    sha256: str

    def __post_init__(self) -> None:
        if (
            self.format != _FORMAT
            or not isinstance(self.company_id, str)
            or not self.company_id
            or any(
                type(value) is not int or value <= 0
                for value in (self.schema_revision, self.captured_ns, self.size)
            )
            or not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise CheckpointRefused("Invalid checkpoint manifest")


def _database_revision(database: Path, company_id: str) -> int:
    with closing(_connect(database, "ro")) as connection:
        identity, schema = _company_version(connection)
        if identity != company_id:
            raise CheckpointRefused("Checkpoint belongs to another company")
        if (
            connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]
            or connection.execute("PRAGMA foreign_key_check").fetchone() is not None
        ):
            raise CheckpointRefused("Checkpoint database integrity check did not pass")
        return schema


def _hash(path: Path) -> str:
    with path.open("rb") as reader:
        return file_digest(reader, "sha256").hexdigest()


def _snapshot(store: Path, database: Path) -> None:
    deadline = monotonic() + 15

    def progress(status, remaining, total):
        if monotonic() >= deadline:
            raise CheckpointRefused("Company checkpoint exceeded its backup deadline")

    with (
        closing(_connect(store, "ro")) as source,
        closing(sqlite3.connect(database)) as snapshot,
    ):
        source.backup(snapshot, pages=128, progress=progress, sleep=0.05)


def _publish(candidate: Path, target: Path) -> None:
    with candidate.open("rb") as stored:
        os.fsync(stored.fileno())
    try:
        os.link(candidate, target)
    except FileExistsError as error:
        raise CheckpointRefused("Checkpoint destination already exists") from error
    parent = os.open(target.parent, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def checkpoint_company(
    store: Path, company_id: str, archive: Path
) -> CheckpointManifest:
    with (
        hold_company_writer(store, company_id) as writer,
        TemporaryDirectory(dir=archive.parent, prefix=".checkpoint-") as directory,
    ):
        if os.path.lexists(archive):
            raise CheckpointRefused("Checkpoint destination already exists")
        database = Path(directory) / _STORE
        _snapshot(store, database)
        manifest = CheckpointManifest(
            _FORMAT,
            company_id,
            _database_revision(database, company_id),
            time_ns(),
            database.stat().st_size,
            _hash(database),
        )
        candidate = Path(directory) / "checkpoint"
        with candidate.open("xb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            with ZipFile(handle, "w", compression=ZIP_STORED) as output:
                output.writestr(_MANIFEST, json.dumps(asdict(manifest), sort_keys=True))
                output.write(database, _STORE)
        writer.verify()
        _publish(candidate, archive)
    return manifest


def inspect_company_checkpoint(archive: Path, company_id: str) -> CheckpointManifest:
    with TemporaryDirectory(prefix="sd-checkpoint-") as directory:
        return _extract_checkpoint(archive, company_id, Path(directory) / _STORE)


def _extract_checkpoint(
    archive: Path, company_id: str, database: Path
) -> CheckpointManifest:
    try:
        descriptor = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise CheckpointRefused("Expected a regular local owner checkpoint")
            with ZipFile(handle) as source:
                return _inspect_archive(source, company_id, info.st_size, database)
    except (BadZipFile, OSError, ValueError, TypeError, sqlite3.Error) as error:
        raise CheckpointRefused("Cannot verify the company checkpoint") from error


def _inspect_archive(
    source: ZipFile, company_id: str, archive_size: int, database: Path
) -> CheckpointManifest:
    entries = source.infolist()
    if (
        sorted(entry.filename for entry in entries) != [_STORE, _MANIFEST]
        or any(entry.compress_type != ZIP_STORED for entry in entries)
        or source.getinfo(_MANIFEST).file_size > 4096
    ):
        raise CheckpointRefused("Unsupported checkpoint archive contents")
    manifest = CheckpointManifest(**json.loads(source.read(_MANIFEST)))
    if manifest.company_id != company_id:
        raise CheckpointRefused("Checkpoint belongs to another company")
    if (
        source.getinfo(_STORE).file_size != manifest.size
        or manifest.size > archive_size
    ):
        raise CheckpointRefused("Checkpoint size does not match its manifest")
    with source.open(_STORE) as member, database.open("xb") as output:
        shutil.copyfileobj(member, output)
    if database.stat().st_size != manifest.size or _hash(database) != manifest.sha256:
        raise CheckpointRefused("Checkpoint digest does not match its manifest")
    if _database_revision(database, company_id) != manifest.schema_revision:
        raise CheckpointRefused("Checkpoint schema does not match its manifest")
    return manifest
