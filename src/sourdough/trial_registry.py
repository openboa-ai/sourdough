"""Seal an evaluator's offline suite and record one launch reservation per slot."""

import json
import os
import re
import sqlite3
import stat
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path


_FORMAT = "offline-trial-registry-v1"
_TABLES = {"trial_suite", "trial_reservation"}


class TrialRefused(Exception):
    """The suite, its boundary or the proposed launch is not eligible."""


def _label(value: str) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256 and bool(value.strip())


def _digest(value: str) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _revision(document: str) -> str:
    return sha256(document.encode()).hexdigest()


@dataclass(frozen=True)
class TrialInputs:
    company_fixture: str
    case_inputs: str
    worker_context: str
    tools: str
    runtime_profile: str

    def __post_init__(self) -> None:
        if not _label(self.runtime_profile) or not all(
            _digest(value)
            for value in (
                self.company_fixture,
                self.case_inputs,
                self.worker_context,
                self.tools,
            )
        ):
            raise TrialRefused("Trial inputs require exact revisions")


@dataclass(frozen=True)
class TrialCeiling:
    turns: int
    wall_seconds: int
    tokens: int

    def __post_init__(self) -> None:
        if not all(
            type(value) is int and 0 < value < 2**63
            for value in (self.turns, self.wall_seconds, self.tokens)
        ):
            raise TrialRefused("Trial resource ceilings must be positive integers")


@dataclass(frozen=True)
class TrialSlot:
    slot_id: str
    case_id: str
    family_id: str
    inputs: TrialInputs
    limits: TrialCeiling
    arm: str | None = None
    counterpart_id: str | None = None

    def __post_init__(self) -> None:
        if not all(
            _label(value) for value in (self.slot_id, self.case_id, self.family_id)
        ):
            raise TrialRefused("Invalid trial slot identity")
        if not isinstance(self.inputs, TrialInputs) or not isinstance(
            self.limits, TrialCeiling
        ):
            raise TrialRefused("Trial slot must pin its inputs and ceilings")
        if self.arm is None and self.counterpart_id is None:
            return
        if self.arm not in ("incumbent", "candidate") or not _label(
            self.counterpart_id
        ):
            raise TrialRefused("Treatment and counterpart must be registered together")


@dataclass(frozen=True)
class TrialSuite:
    suite_id: str
    purpose: str
    rubric_digest: str
    evaluator_digest: str
    analysis_plan_digest: str
    slots: tuple[TrialSlot, ...]

    def __post_init__(self) -> None:
        if (
            not _label(self.suite_id)
            or self.purpose not in ("judgment", "method", "recursive")
            or not all(
                _digest(value)
                for value in (
                    self.rubric_digest,
                    self.evaluator_digest,
                    self.analysis_plan_digest,
                )
            )
            or type(self.slots) is not tuple
            or not 1 <= len(self.slots) <= 512
            or not all(isinstance(slot, TrialSlot) for slot in self.slots)
        ):
            raise TrialRefused("Invalid sealed trial suite")
        by_id = {slot.slot_id: slot for slot in self.slots}
        if len(by_id) != len(self.slots):
            raise TrialRefused("Trial slot identities must be unique")
        for slot in self.slots:
            if slot.counterpart_id is None:
                if self.purpose == "recursive":
                    raise TrialRefused(
                        "Recursive trials require complete declared pairs"
                    )
                continue
            other = by_id.get(slot.counterpart_id)
            if (
                other is None
                or other.arm == slot.arm
                or other.counterpart_id != slot.slot_id
                or (other.case_id, other.family_id, other.limits)
                != (slot.case_id, slot.family_id, slot.limits)
                or (
                    other.inputs.company_fixture,
                    other.inputs.case_inputs,
                    other.inputs.tools,
                )
                != (
                    slot.inputs.company_fixture,
                    slot.inputs.case_inputs,
                    slot.inputs.tools,
                )
                or (
                    self.purpose == "recursive"
                    and other.inputs.runtime_profile != slot.inputs.runtime_profile
                )
            ):
                raise TrialRefused("Paired trial inputs and ceilings are not matched")


@dataclass(frozen=True)
class TrialReservation:
    run_id: str
    launch_permitted: bool


def _private_key(path: Path) -> tuple[int, int]:
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise TrialRefused("Offline trial store does not exist") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_mode & 0o077
    ):
        raise TrialRefused(
            "Offline trials require a private standalone evaluator store"
        )
    return info.st_dev, info.st_ino


def _open(path: Path, *, create: bool = False) -> sqlite3.Connection:
    if create:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
    key = _private_key(path)
    database = sqlite3.connect(
        path.absolute().as_uri() + "?mode=rw", uri=True, timeout=3
    )
    try:
        if _private_key(path) != key:
            raise TrialRefused("Offline trial store changed during open")
    except BaseException:
        database.close()
        raise
    return database


def _tables(database: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in database.execute("SELECT name FROM sqlite_schema WHERE type='table'")
    }


def seal_trial_suite(path: Path, suite: TrialSuite) -> str:
    document = json.dumps(
        {"format": _FORMAT, **asdict(suite)}, separators=(",", ":"), sort_keys=True
    )
    revision = _revision(document)
    with closing(_open(path, create=True)) as database, database:
        database.execute("BEGIN IMMEDIATE")
        tables = _tables(database)
        if tables == _TABLES:
            prior = database.execute(
                "SELECT revision,document FROM trial_suite WHERE singleton=1"
            ).fetchone()
            if prior != (revision, document):
                raise TrialRefused("Existing trial suite is already sealed")
            return revision
        if tables:
            raise TrialRefused("Store is not an offline evaluator registry")
        database.execute(
            """CREATE TABLE trial_suite (
                   singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                   revision TEXT NOT NULL, document TEXT NOT NULL
               )"""
        )
        database.execute(
            """CREATE TABLE trial_reservation (
                   slot_id TEXT PRIMARY KEY NOT NULL, run_id TEXT UNIQUE NOT NULL
               )"""
        )
        database.execute("INSERT INTO trial_suite VALUES (1,?,?)", (revision, document))
        return revision


def reserve_trial_launch(
    path: Path,
    *,
    revision: str,
    slot_id: str,
    run_id: str,
    inputs: TrialInputs,
    limits: TrialCeiling,
) -> TrialReservation:
    if (
        not _digest(revision)
        or not _label(slot_id)
        or not _label(run_id)
        or not isinstance(inputs, TrialInputs)
        or not isinstance(limits, TrialCeiling)
    ):
        raise TrialRefused("Invalid sealed trial launch")
    with closing(_open(path)) as database, database:
        database.execute("BEGIN IMMEDIATE")
        if _tables(database) != _TABLES:
            raise TrialRefused("Store is not a sealed trial registry")
        stored = database.execute(
            "SELECT revision,document FROM trial_suite WHERE singleton=1"
        ).fetchone()
        if stored is None or stored[0] != revision or _revision(stored[1]) != revision:
            raise TrialRefused("Trial registration revision changed")
        suite = json.loads(stored[1])
        if suite["format"] != _FORMAT:
            raise TrialRefused("Unsupported trial registry format")
        slot = next(
            (slot for slot in suite["slots"] if slot["slot_id"] == slot_id), None
        )
        if slot is None or (slot["inputs"], slot["limits"]) != (
            asdict(inputs),
            asdict(limits),
        ):
            raise TrialRefused("Slot, trial inputs or ceilings are not registered")
        original = database.execute(
            "SELECT run_id FROM trial_reservation WHERE slot_id=?", (slot_id,)
        ).fetchone()
        if original is not None:
            if original != (run_id,):
                raise TrialRefused("Trial slot has already reserved its original run")
            return TrialReservation(run_id, False)
        if database.execute(
            "SELECT 1 FROM trial_reservation WHERE run_id=?", (run_id,)
        ).fetchone():
            raise TrialRefused("Run identity already belongs to a different trial slot")
        database.execute(
            "INSERT INTO trial_reservation VALUES (?,?)", (slot_id, run_id)
        )
        return TrialReservation(run_id, True)
