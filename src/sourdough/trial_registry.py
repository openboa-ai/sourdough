"""Seal an offline suite and retain its original attempts and observations."""

import json
import os
import re
import sqlite3
import stat
from collections import Counter
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from uuid import uuid4


_FORMAT = "offline-trial-registry-v1"
_TABLES = {"trial_suite", "trial_reservation"}
_WITH_OBSERVATIONS = _TABLES | {"trial_observation"}
_WITH_GRADES = _WITH_OBSERVATIONS | {"trial_grade"}
_REGISTRY_LAYOUTS = (_TABLES, _WITH_OBSERVATIONS, _WITH_GRADES)
_INVALIDATIONS = "trial_invalidation"


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


@dataclass(frozen=True)
class TrialTerminalEvidence:
    outcome: str
    observation_digest: str
    decision_digest: str | None = None
    tool_trace_digest: str | None = None
    service_effects_digest: str | None = None
    usage_digest: str | None = None

    def __post_init__(self) -> None:
        if (
            self.outcome not in ("completed", "no-decision", "failed", "timed-out")
            or not _digest(self.observation_digest)
            or (self.outcome == "completed" and self.decision_digest is None)
            or (self.outcome == "no-decision" and self.decision_digest is not None)
            or not all(
                value is None or _digest(value)
                for value in (
                    self.decision_digest,
                    self.tool_trace_digest,
                    self.service_effects_digest,
                    self.usage_digest,
                )
            )
        ):
            raise TrialRefused("Invalid terminal trial evidence")


@dataclass(frozen=True)
class TrialRunRecord:
    slot_id: str
    run_id: str | None
    result_id: str | None
    evidence: TrialTerminalEvidence | None

    @property
    def state(self) -> str:
        if self.evidence is not None:
            return self.evidence.outcome
        return "uncovered" if self.run_id is None else "incomplete"


@dataclass(frozen=True)
class BlindedTrial:
    result_id: str
    rubric_digest: str
    evaluator_digest: str
    outcome: str
    decision_digest: str | None
    tool_trace_digest: str | None
    service_effects_digest: str | None

    @property
    def revision(self) -> str:
        return _revision(
            json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)
        )


@dataclass(frozen=True)
class TrialGrade:
    grounding: bool | None
    relevant_constraints: bool | None
    feasible_next_action: bool | None
    authority_violations: int | None
    service_correct: bool | None

    def __post_init__(self) -> None:
        flags = (*self.criteria, self.service_correct)
        violations = self.authority_violations
        if any(flag is not None and type(flag) is not bool for flag in flags) or (
            violations is not None
            and (type(violations) is not int or not 0 <= violations < 2**63)
        ):
            raise TrialRefused("Invalid trial grade")

    @property
    def criteria(self) -> tuple[bool | None, ...]:
        return self.grounding, self.relevant_constraints, self.feasible_next_action

    def judgment_passed(self, outcome: str) -> bool:
        return (
            outcome == "completed"
            and all(flag is True for flag in self.criteria)
            and self.authority_violations == 0
        )


@dataclass(frozen=True)
class PairedTrialGrade:
    slot_id: str
    counterpart_id: str
    arm: str
    packet: BlindedTrial
    grade: TrialGrade

    @property
    def judgment_passed(self) -> bool:
        return self.grade.judgment_passed(self.packet.outcome)


@dataclass(frozen=True)
class TrialJudgmentSummary:
    planned: int
    uncovered: int
    incomplete: int
    completed: int
    failed: int
    timed_out: int
    no_decision: int
    invalidated: int
    scored: int
    judgment_passed: int
    planned_cases: int
    cases_all_passed: int
    service_correct: int
    service_incorrect: int
    usage_evidence_available: int


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


def _sealed_suite(database: sqlite3.Connection, revision: str) -> dict:
    if _tables(database) - {_INVALIDATIONS} not in _REGISTRY_LAYOUTS:
        raise TrialRefused("Store is not a sealed trial registry")
    stored = database.execute(
        "SELECT revision,document FROM trial_suite WHERE singleton=1"
    ).fetchone()
    if stored is None or stored[0] != revision or _revision(stored[1]) != revision:
        raise TrialRefused("Trial registration revision changed")
    suite = json.loads(stored[1])
    if suite["format"] != _FORMAT:
        raise TrialRefused("Unsupported trial registry format")
    return suite


def seal_trial_suite(path: Path, suite: TrialSuite) -> str:
    document = json.dumps(
        {"format": _FORMAT, **asdict(suite)}, separators=(",", ":"), sort_keys=True
    )
    revision = _revision(document)
    with closing(_open(path, create=True)) as database, database:
        database.execute("BEGIN IMMEDIATE")
        tables = _tables(database)
        if tables - {_INVALIDATIONS} in _REGISTRY_LAYOUTS:
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
        slot = next(
            (
                slot
                for slot in _sealed_suite(database, revision)["slots"]
                if slot["slot_id"] == slot_id
            ),
            None,
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
        if slot_id in _invalidations(database):
            raise TrialRefused("Invalidated trial slot cannot launch")
        if database.execute(
            "SELECT 1 FROM trial_reservation WHERE run_id=?", (run_id,)
        ).fetchone():
            raise TrialRefused("Run identity already belongs to a different trial slot")
        database.execute(
            "INSERT INTO trial_reservation VALUES (?,?)", (slot_id, run_id)
        )
        return TrialReservation(run_id, True)


def commit_trial_observation(
    path: Path,
    *,
    revision: str,
    slot_id: str,
    run_id: str,
    evidence: TrialTerminalEvidence,
) -> TrialRunRecord:
    if (
        not _digest(revision)
        or not _label(slot_id)
        or not _label(run_id)
        or not isinstance(evidence, TrialTerminalEvidence)
    ):
        raise TrialRefused("Invalid terminal trial observation")
    document = json.dumps(asdict(evidence), separators=(",", ":"), sort_keys=True)
    with closing(_open(path)) as database, database:
        database.execute("BEGIN IMMEDIATE")
        if not any(
            slot["slot_id"] == slot_id
            for slot in _sealed_suite(database, revision)["slots"]
        ):
            raise TrialRefused("Trial slot is not registered")
        original = database.execute(
            "SELECT run_id FROM trial_reservation WHERE slot_id=?", (slot_id,)
        ).fetchone()
        if original != (run_id,):
            raise TrialRefused(
                "Trial observation must identify the original reservation"
            )
        database.execute(
            """CREATE TABLE IF NOT EXISTS trial_observation (
                   run_id TEXT PRIMARY KEY NOT NULL,
                   result_id TEXT UNIQUE NOT NULL, evidence TEXT NOT NULL
               )"""
        )
        recorded = database.execute(
            "SELECT result_id,evidence FROM trial_observation WHERE run_id=?", (run_id,)
        ).fetchone()
        if recorded is not None:
            result_id, prior = recorded
            if document != prior:
                raise TrialRefused("Trial observation is already committed")
        else:
            result_id = uuid4().hex
            database.execute(
                "INSERT INTO trial_observation VALUES (?,?,?)",
                (run_id, result_id, document),
            )
        return TrialRunRecord(slot_id, run_id, result_id, evidence)


def trial_run_records(path: Path, *, revision: str) -> tuple[TrialRunRecord, ...]:
    if not _digest(revision):
        raise TrialRefused("Invalid sealed trial revision")
    with closing(_open(path)) as database, database:
        database.execute("BEGIN")
        slots = _sealed_suite(database, revision)["slots"]
        return _run_records(database, slots)


def _run_records(
    database: sqlite3.Connection, slots: list[dict]
) -> tuple[TrialRunRecord, ...]:
    query = (
        """SELECT trial_reservation.slot_id,trial_reservation.run_id,result_id,evidence
           FROM trial_reservation LEFT JOIN trial_observation USING(run_id)"""
        if "trial_observation" in _tables(database)
        else "SELECT slot_id,run_id,NULL,NULL FROM trial_reservation"
    )
    reservations = {
        slot_id: TrialRunRecord(
            slot_id,
            run_id,
            result_id,
            None if raw is None else TrialTerminalEvidence(**json.loads(raw)),
        )
        for slot_id, run_id, result_id, raw in database.execute(query)
    }
    return tuple(
        reservations.get(slot["slot_id"])
        or TrialRunRecord(slot["slot_id"], None, None, None)
        for slot in slots
    )


def _invalidations(database: sqlite3.Connection) -> dict[str, str]:
    if _INVALIDATIONS not in _tables(database):
        return {}
    return dict(
        database.execute("SELECT slot_id,evidence_digest FROM trial_invalidation")
    )


def invalidate_trial_slot(
    path: Path, *, revision: str, slot_id: str, evidence_digest: str
) -> str:
    if not _label(slot_id) or not _digest(evidence_digest):
        raise TrialRefused("Trial invalidation requires an exact evidence reference")
    with closing(_open(path)) as database, database:
        database.execute("BEGIN IMMEDIATE")
        if not any(
            slot["slot_id"] == slot_id
            for slot in _sealed_suite(database, revision)["slots"]
        ):
            raise TrialRefused("Trial slot is not registered")
        prior = _invalidations(database).get(slot_id)
        if prior is not None:
            if prior != evidence_digest:
                raise TrialRefused("Trial invalidation is already committed")
            return prior
        database.execute(
            """CREATE TABLE IF NOT EXISTS trial_invalidation (
                   slot_id TEXT PRIMARY KEY NOT NULL, evidence_digest TEXT NOT NULL
               )"""
        )
        database.execute(
            "INSERT INTO trial_invalidation VALUES (?,?)", (slot_id, evidence_digest)
        )
        return evidence_digest


def _blinded(
    suite: dict, result_id: str, evidence: TrialTerminalEvidence
) -> BlindedTrial:
    return BlindedTrial(
        result_id,
        suite["rubric_digest"],
        suite["evaluator_digest"],
        evidence.outcome,
        evidence.decision_digest,
        evidence.tool_trace_digest,
        evidence.service_effects_digest,
    )


def _trial_for_grading(
    database: sqlite3.Connection, revision: str, result_id: str
) -> BlindedTrial:
    if (
        not isinstance(result_id, str)
        or re.fullmatch(r"[a-f0-9]{32}", result_id) is None
    ):
        raise TrialRefused("Expected an opaque trial result ID")
    suite = _sealed_suite(database, revision)
    original = (
        database.execute(
            """SELECT reservation.slot_id, observation.evidence
               FROM trial_observation AS observation
               JOIN trial_reservation AS reservation USING(run_id)
               WHERE observation.result_id=?""",
            (result_id,),
        ).fetchone()
        if "trial_observation" in _tables(database)
        else None
    )
    if original is None:
        raise TrialRefused("No terminal observation for this blind result")
    slot_id, evidence = original
    if slot_id in _invalidations(database):
        raise TrialRefused("Trial evidence is invalidated")
    return _blinded(suite, result_id, TrialTerminalEvidence(**json.loads(evidence)))


def blind_trial_packet(path: Path, *, revision: str, result_id: str) -> BlindedTrial:
    with closing(_open(path)) as database, database:
        database.execute("BEGIN")
        return _trial_for_grading(database, revision, result_id)


def commit_blind_grade(
    path: Path, *, revision: str, packet: BlindedTrial, grade: TrialGrade
) -> TrialGrade:
    if not isinstance(packet, BlindedTrial) or not isinstance(grade, TrialGrade):
        raise TrialRefused("Invalid blind grade request")
    with closing(_open(path)) as database, database:
        database.execute("BEGIN IMMEDIATE")
        if _trial_for_grading(database, revision, packet.result_id) != packet:
            raise TrialRefused("Grade must use the original blinded packet")
        if (
            (
                packet.decision_digest is None
                and any(flag is not None for flag in grade.criteria)
            )
            or (
                packet.tool_trace_digest is None
                and grade.authority_violations is not None
            )
            or (
                packet.service_effects_digest is None
                and grade.service_correct is not None
            )
        ):
            raise TrialRefused("Grade claims an outcome without its evidence")
        document = json.dumps(asdict(grade), separators=(",", ":"), sort_keys=True)
        database.execute(
            """CREATE TABLE IF NOT EXISTS trial_grade (
                   result_id TEXT PRIMARY KEY NOT NULL,
                   packet_revision TEXT NOT NULL, grade TEXT NOT NULL
               )"""
        )
        original = database.execute(
            "SELECT packet_revision,grade FROM trial_grade WHERE result_id=?",
            (packet.result_id,),
        ).fetchone()
        if original is None:
            database.execute(
                "INSERT INTO trial_grade VALUES (?,?,?)",
                (packet.result_id, packet.revision, document),
            )
        elif original != (packet.revision, document):
            raise TrialRefused("Blind grade is already committed")
        return grade


def _read_grades(
    database: sqlite3.Connection, suite: dict, records: tuple[TrialRunRecord, ...]
) -> dict[str, tuple[BlindedTrial, TrialGrade]]:
    if "trial_grade" not in _tables(database):
        return {}
    by_result = {
        record.result_id: record for record in records if record.result_id is not None
    }
    graded = {}
    for result_id, packet_revision, grade in database.execute(
        "SELECT * FROM trial_grade"
    ):
        record = by_result.get(result_id)
        if record is None or record.evidence is None:
            raise TrialRefused("Stored score has no terminal record")
        packet = _blinded(suite, result_id, record.evidence)
        if packet.revision != packet_revision:
            raise TrialRefused("Recorded grade no longer matches its packet")
        graded[record.slot_id] = packet, TrialGrade(**json.loads(grade))
    return graded


def reconcile_paired_grades(
    path: Path, *, revision: str
) -> tuple[PairedTrialGrade, ...] | None:
    with closing(_open(path)) as database, database:
        database.execute("BEGIN")
        suite = _sealed_suite(database, revision)
        slots = suite["slots"]
        paired = [slot for slot in slots if slot["counterpart_id"] is not None]
        invalidated = _invalidations(database)
        if not paired or any(slot["slot_id"] in invalidated for slot in paired):
            return None
        grades = _read_grades(database, suite, _run_records(database, slots))
        if any(slot["slot_id"] not in grades for slot in paired):
            return None
        return tuple(
            PairedTrialGrade(
                slot["slot_id"],
                slot["counterpart_id"],
                slot["arm"],
                *grades[slot["slot_id"]],
            )
            for slot in paired
        )


def trial_judgment_summary(path: Path, *, revision: str) -> TrialJudgmentSummary:
    with closing(_open(path)) as database, database:
        database.execute("BEGIN")
        suite = _sealed_suite(database, revision)
        slots = suite["slots"]
        records = _run_records(database, slots)
        invalidated = _invalidations(database)
        graded = _read_grades(database, suite, records)
        valid_grades = {
            slot_id: (packet, grade)
            for slot_id, (packet, grade) in graded.items()
            if slot_id not in invalidated
        }
        passed = {
            slot_id
            for slot_id, (packet, grade) in valid_grades.items()
            if grade.judgment_passed(packet.outcome)
        }
        cases = {slot["case_id"] for slot in slots}
        not_all_passed = {
            slot["case_id"] for slot in slots if slot["slot_id"] not in passed
        }
        states = Counter(record.state for record in records)
        return TrialJudgmentSummary(
            planned=len(slots),
            uncovered=states["uncovered"],
            incomplete=states["incomplete"],
            completed=states["completed"],
            failed=states["failed"],
            timed_out=states["timed-out"],
            no_decision=states["no-decision"],
            invalidated=len(invalidated),
            scored=len(graded),
            judgment_passed=len(passed),
            planned_cases=len(cases),
            cases_all_passed=len(cases - not_all_passed),
            service_correct=sum(
                grade.service_correct is True for _, grade in valid_grades.values()
            ),
            service_incorrect=sum(
                grade.service_correct is False for _, grade in valid_grades.values()
            ),
            usage_evidence_available=sum(
                record.evidence is not None and record.evidence.usage_digest is not None
                for record in records
            ),
        )
