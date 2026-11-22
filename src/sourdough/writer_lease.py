"""Hold a process-owned lease keyed by the actual local company store."""

import fcntl
import os
import pwd
import stat
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from .company import _connect, _require_writable_store, reopen


class WriterRefused(Exception):
    """This process does not hold the expected company's writer lease."""


def _file_key(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _private(info: os.stat_result) -> bool:
    return info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0


def _lease_directory() -> Path:
    home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    return home / "Library/Application Support/Sourdough/Writer Leases"


@dataclass(frozen=True)
class WriterLease:
    store: Path
    company_id: str
    _store_descriptor: int
    _store_key: tuple[int, int]
    _lock_path: Path
    _lock_descriptor: int
    _lock_key: tuple[int, int]

    @property
    def store_key(self) -> tuple[int, int]:
        return self._store_key

    def verify_for(self, store: Path, company_id: str) -> None:
        if self.store != store.absolute() or self.company_id != company_id:
            raise WriterRefused("Writer lease belongs to another store or company")
        self.verify()

    def verify(self) -> None:
        try:
            opened = os.fstat(self._store_descriptor)
            current = self.store.stat(follow_symlinks=False)
            lock = os.fstat(self._lock_descriptor)
            named_lock = self._lock_path.stat(follow_symlinks=False)
        except OSError as error:
            raise WriterRefused(
                "Original store or writer lease is unavailable"
            ) from error
        if (
            not stat.S_ISREG(current.st_mode)
            or _file_key(opened) != self._store_key
            or _file_key(current) != self._store_key
            or not stat.S_ISREG(named_lock.st_mode)
            or _file_key(lock) != self._lock_key
            or _file_key(named_lock) != self._lock_key
        ):
            raise WriterRefused("Company store or writer lease identity changed")
        if current.st_nlink != 1:
            raise WriterRefused(
                "Hard-link aliases are not a supported company store path"
            )


@contextmanager
def hold_company_writer(
    store: Path,
    company_id: str,
    *,
    lease_directory: Path | None = None,
    expected_store_key: tuple[int, int] | None = None,
) -> Iterator[WriterLease]:
    with _hold_company_store(
        store,
        company_id,
        lease_directory=lease_directory,
        expected_store_key=expected_store_key,
    ) as lease:
        with closing(_connect(lease.store, "ro")) as connection:
            _require_writable_store(connection)
        lease.verify()
        yield lease


@contextmanager
def _hold_company_store(
    store: Path,
    company_id: str,
    *,
    lease_directory: Path | None = None,
    expected_store_key: tuple[int, int] | None = None,
) -> Iterator[WriterLease]:
    store = store.absolute()
    with ExitStack() as stack:
        try:
            descriptor = os.open(store, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as error:
            raise WriterRefused("Cannot open the original company store") from error
        stack.callback(os.close, descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise WriterRefused(
                "A company store must be a regular file owned by this user"
            )
        if expected_store_key is not None and _file_key(info) != expected_store_key:
            raise WriterRefused("Store does not match the configured writer identity")

        directory = lease_directory or _lease_directory()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_descriptor = os.open(
            directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        stack.callback(os.close, directory_descriptor)
        if not _private(os.fstat(directory_descriptor)):
            raise WriterRefused("Company writer lease directory is not private")
        device, inode = _file_key(info)
        name = f"{device:x}-{inode:x}.lease"
        lock_descriptor = os.open(
            name,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory_descriptor,
        )
        stack.callback(os.close, lock_descriptor)
        lock_info = os.fstat(lock_descriptor)
        if not stat.S_ISREG(lock_info.st_mode) or not _private(lock_info):
            raise WriterRefused("Invalid company writer lease file")
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise WriterRefused("The company store already has a writer") from error
        lease = WriterLease(
            store,
            company_id,
            descriptor,
            _file_key(info),
            directory / name,
            lock_descriptor,
            _file_key(lock_info),
        )
        lease.verify()
        reopen(store, company_id)
        lease.verify()
        yield lease
