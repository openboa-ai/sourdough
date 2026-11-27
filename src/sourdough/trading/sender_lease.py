"""Keep one original paper venue sender across preflight, worker and recovery."""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .execution import VenueRefused
from .market_worker import journal_key


def verify_sender_lease(
    journal: Path, descriptor: int, expected: tuple[int, int]
) -> None:
    if journal_key(journal) != expected:
        raise VenueRefused("Original sandbox journal changed before sender admission")
    info = os.fstat(descriptor)
    if journal_key(journal.with_suffix(".writer-lock")) != (info.st_dev, info.st_ino):
        raise VenueRefused("Original sandbox sender lock changed")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise VenueRefused(
            "The sandbox already has an active or pending sender"
        ) from error


@contextmanager
def hold_sender_lease(journal: Path) -> Iterator[tuple[int, tuple[int, int]]]:
    expected = journal_key(journal)
    descriptor = os.open(
        journal.with_suffix(".writer-lock"),
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    try:
        verify_sender_lease(journal, descriptor, expected)
        yield descriptor, expected
    finally:
        os.close(descriptor)


def verify_worker_sender(journal: Path, descriptor: int, expected_key: str) -> None:
    expected = journal_key(journal)
    if expected_key != f"{expected[0]}:{expected[1]}":
        raise VenueRefused("Worker points to a different sandbox journal")
    verify_sender_lease(journal, descriptor, expected)
