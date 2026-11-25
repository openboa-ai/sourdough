"""Activate, inspect, revert, and quarantine company evidence review tools."""

import json
from collections import deque
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, astuple, dataclass
from hashlib import sha256
from pathlib import Path
from sqlite3 import Connection
from time import time_ns

from .company import _identity
from .evidence_review import (
    EVALUATOR_REVISION,
    EvidenceClass,
    ReviewCase,
    ReviewRuleRefused,
    review_criteria_digest,
)
from .review_qualification_store import (
    PublishedRuleResolver,
    ReviewQualificationRecord,
    read_review_qualification_record,
    reopen_published_review_rule,
)
from .storage.db import open_reader, open_writer, read_tx, write_tx
from .storage.kernel import (
    CommandConflict,
    admit_command_receipt,
    append_audit_event,
)
from .writer_lease import WriterLease


ACTIVATION_CONTRACT_VERSION = "review-tool-activation-v1"
_AUDIT_EVENT_BY_OP = {
    "activate": "ops.review_tool_activated",
    "revert": "ops.review_tool_reverted",
}


def _identity_value(value: str) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 128


def _payload_sha256(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ReviewToolAuthority:
    principal_id: str
    grant_id: str
    owner_id: str


AuthorizeReviewTool = Callable[[Connection, str, str, str], ReviewToolAuthority | None]


@dataclass(frozen=True)
class ReviewActivation:
    request_id: str
    operation: str
    candidate_id: str
    qualification_id: str
    expected_revision: int
    parent_asset_id: str | None = None

    def __post_init__(self) -> None:
        if (
            self.operation not in ("activate", "revert")
            or type(self.expected_revision) is not int
            or not 0 <= self.expected_revision < 2**63 - 1
            or not all(
                _identity_value(value)
                for value in (self.request_id, self.candidate_id, self.qualification_id)
            )
            or (
                self.parent_asset_id is not None
                and (
                    not _identity_value(self.parent_asset_id)
                    or self.parent_asset_id == self.candidate_id
                )
            )
        ):
            raise ReviewRuleRefused("Invalid review tool activation")


@dataclass(frozen=True)
class ReviewActivationReceipt:
    request_id: str
    operation: str
    candidate_id: str
    qualification_id: str
    revision: int
    principal_id: str
    grant_id: str
    owner_id: str


@dataclass(frozen=True)
class _ActivationRow:
    receipt: ReviewActivationReceipt
    parent_asset_id: str | None
    quarantined: bool
    quarantine_reason: str | None


@dataclass(frozen=True)
class QuarantineReceipt:
    request_id: str
    faulted_asset_id: str
    quarantined_assets: tuple[str, ...]
    restored_candidate_id: str | None
    restored_revision: int | None
    principal_id: str
    grant_id: str
    owner_id: str


def _authorize(
    database: Connection,
    authorize: AuthorizeReviewTool,
    company_id: str,
    operation: str,
    asset_id: str,
) -> ReviewToolAuthority:
    authority = authorize(database, company_id, operation, asset_id)
    if not isinstance(authority, ReviewToolAuthority) or not all(
        _identity_value(value) for value in astuple(authority)
    ):
        raise ReviewRuleRefused("Current scoped review tool authority is required")
    return authority


def _history_row(
    database: Connection,
    company_id: str,
    *,
    request_id: str | None = None,
    before: int | None = None,
    exclude_quarantined: bool = False,
    exclude_candidate_id: str | None = None,
) -> _ActivationRow | None:
    row = database.execute(
        """SELECT request_id, operation, candidate_id, qualification_id, revision,
                  principal_id, grant_id, owner_id, parent_asset_id,
                  quarantined, quarantine_reason
           FROM review_tool_activation
           WHERE company_id = ?
             AND (? IS NULL OR request_id = ?)
             AND (? IS NULL OR revision < ?)
             AND (? = 0 OR quarantined = 0)
             AND (? IS NULL OR candidate_id != ?)
           ORDER BY revision DESC
           LIMIT 1""",
        (
            company_id,
            request_id,
            request_id,
            before,
            before,
            int(exclude_quarantined),
            exclude_candidate_id,
            exclude_candidate_id,
        ),
    ).fetchone()
    if row is None:
        return None
    return _ActivationRow(
        receipt=ReviewActivationReceipt(*row[:8]),
        parent_asset_id=None if row[8] is None else str(row[8]),
        quarantined=bool(row[9]),
        quarantine_reason=None if row[10] is None else str(row[10]),
    )


def _qualification(
    database: Connection,
    company_id: str,
    qualification_id: str,
    candidate_id: str,
) -> ReviewQualificationRecord:
    record = read_review_qualification_record(
        database, qualification_id, company_id=company_id
    )
    if (
        record is None
        or record.receipt.candidate_asset_id != candidate_id
        or not record.passed
    ):
        raise ReviewRuleRefused("Matching independent qualification is required")
    return record


def _rule(database, company_id, record: ReviewQualificationRecord, resolve):
    rule = reopen_published_review_rule(
        database, company_id, record.receipt.candidate_asset_id, resolve
    )
    if rule.digest != record.receipt.qualification.candidate_digest:
        raise ReviewRuleRefused("Published review rule changed after qualification")
    return rule


def _is_asset_quarantined(database: Connection, company_id: str, asset_id: str) -> bool:
    row = database.execute(
        """SELECT 1 FROM review_tool_activation
           WHERE company_id = ? AND candidate_id = ? AND quarantined = 1
           LIMIT 1""",
        (company_id, asset_id),
    ).fetchone()
    return row is not None


def _asset_was_activated(database: Connection, company_id: str, asset_id: str) -> bool:
    row = database.execute(
        """SELECT 1 FROM review_tool_activation
           WHERE company_id = ? AND candidate_id = ?
           LIMIT 1""",
        (company_id, asset_id),
    ).fetchone()
    return row is not None


def _all_lineage_edges(
    database: Connection, company_id: str
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return (parent -> children, child -> parents) across activation and qualification records."""
    children_by_parent: dict[str, set[str]] = {}
    parents_by_child: dict[str, set[str]] = {}
    for sql in (
        """SELECT parent_asset_id, candidate_id
           FROM review_tool_activation
           WHERE company_id = ? AND parent_asset_id IS NOT NULL""",
        """SELECT parent_asset_id, candidate_asset_id
           FROM review_qualification
           WHERE company_id = ? AND parent_asset_id IS NOT NULL""",
    ):
        for parent_id, child_id in database.execute(sql, (company_id,)).fetchall():
            p = str(parent_id)
            c = str(child_id)
            children_by_parent.setdefault(p, set()).add(c)
            parents_by_child.setdefault(c, set()).add(p)
    return children_by_parent, parents_by_child


def _has_quarantined_ancestor(
    database: Connection, company_id: str, start_asset_id: str | None
) -> bool:
    if start_asset_id is None:
        return False
    _, parents_by_child = _all_lineage_edges(database, company_id)
    visited: set[str] = set()
    frontier: deque[str] = deque([start_asset_id])
    while frontier:
        current = frontier.popleft()
        if current in visited:
            continue
        visited.add(current)
        if _is_asset_quarantined(database, company_id, current):
            return True
        for ancestor in parents_by_child.get(current, ()):
            if ancestor not in visited:
                frontier.append(ancestor)
    return False


def _collect_lineage_descendants(
    database: Connection, company_id: str, faulted_asset_id: str
) -> tuple[str, ...]:
    children_by_parent, _ = _all_lineage_edges(database, company_id)
    visited: set[str] = {faulted_asset_id}
    queue: deque[str] = deque([faulted_asset_id])
    while queue:
        node = queue.popleft()
        for child in children_by_parent.get(node, ()):
            if child not in visited:
                visited.add(child)
                queue.append(child)
    return tuple(sorted(visited))


def apply_review_activation(
    writer: WriterLease,
    command: ReviewActivation,
    *,
    authorize: AuthorizeReviewTool,
    resolve: PublishedRuleResolver,
    cases: tuple[ReviewCase, ...],
) -> ReviewActivationReceipt:
    writer.verify()
    with (
        closing(open_writer(writer.store, lease=writer)) as database,
        write_tx(database),
    ):
        writer.verify()
        company_id = _identity(database)
        if company_id != writer.company_id:
            raise ReviewRuleRefused("Company identity mismatch")
        authority = _authorize(
            database,
            authorize,
            company_id,
            command.operation,
            command.candidate_id,
        )
        payload_digest = _payload_sha256(
            {
                "operation": command.operation,
                "candidate_id": command.candidate_id,
                "qualification_id": command.qualification_id,
                "expected_revision": command.expected_revision,
                "parent_asset_id": command.parent_asset_id,
            }
        )
        preconditions = {
            "expected_revision": command.expected_revision,
            "qualification_id": command.qualification_id,
        }
        if prior_row := _history_row(
            database, company_id, request_id=command.request_id
        ):
            prior = prior_row.receipt
            if (
                astuple(prior)[:4] != astuple(command)[:4]
                or prior.revision != command.expected_revision + 1
                or prior.principal_id != authority.principal_id
            ):
                raise ReviewRuleRefused("Activation request identity already in use")
            try:
                cmd_receipt, _ = admit_command_receipt(
                    database,
                    company_id=company_id,
                    command_id=command.request_id,
                    actor_id=authority.principal_id,
                    peer_pid=None,
                    command_type=f"review_tool.{command.operation}",
                    contract_version=ACTIVATION_CONTRACT_VERSION,
                    payload_sha256=payload_digest,
                    preconditions=preconditions,
                    state="applied",
                    result=asdict(prior),
                )
            except CommandConflict as error:
                raise ReviewRuleRefused(
                    "Activation request identity already in use"
                ) from error
            if cmd_receipt.state != "applied":
                raise ReviewRuleRefused("Activation request identity already in use")
            return prior

        current_row = _history_row(database, company_id)
        current = None if current_row is None else current_row.receipt
        if command.expected_revision != (current.revision if current else 0):
            raise ReviewRuleRefused("Effective review tool revision has changed")
        qualified = _qualification(
            database, company_id, command.qualification_id, command.candidate_id
        )
        outcome = qualified.receipt.qualification
        if (
            outcome.evaluator_revision != EVALUATOR_REVISION
            or outcome.criteria_digest != review_criteria_digest(cases)
        ):
            raise ReviewRuleRefused("Independent qualification is stale")

        # Authoritative lineage binding: caller cannot override or sever qualified.parent_asset_id.
        if (
            command.parent_asset_id is not None
            and qualified.parent_asset_id is not None
            and command.parent_asset_id != qualified.parent_asset_id
        ):
            raise ReviewRuleRefused(
                "Activation parent does not match qualified lineage parent"
            )
        if command.operation == "revert" and command.parent_asset_id is not None:
            raise ReviewRuleRefused("Revert cannot introduce a new lineage parent")
        effective_parent = (
            qualified.parent_asset_id
            if qualified.parent_asset_id is not None
            else command.parent_asset_id
        )
        if effective_parent == command.candidate_id:
            raise ReviewRuleRefused("Review asset cannot be its own lineage parent")
        if effective_parent is not None:
            if not _asset_was_activated(database, company_id, effective_parent):
                raise ReviewRuleRefused(
                    "Lineage parent asset must exist in activation history"
                )
            descendants = _collect_lineage_descendants(
                database, company_id, command.candidate_id
            )
            if effective_parent in descendants:
                raise ReviewRuleRefused("Lineage parent would create a lineage cycle")
        if (
            _is_asset_quarantined(database, company_id, command.candidate_id)
            or _has_quarantined_ancestor(database, company_id, command.candidate_id)
            or _has_quarantined_ancestor(database, company_id, effective_parent)
        ):
            raise ReviewRuleRefused("Candidate or ancestor is quarantined")

        _rule(database, company_id, qualified, resolve)
        baseline_id = baseline_digest = None
        if current is not None and not current_row.quarantined:
            baseline_id = current.candidate_id
            baseline = _qualification(
                database, company_id, current.qualification_id, baseline_id
            )
            baseline_digest = _rule(database, company_id, baseline, resolve).digest
        if (qualified.receipt.baseline_asset_id, outcome.baseline_digest) != (
            baseline_id,
            baseline_digest,
        ):
            raise ReviewRuleRefused(
                "Qualification does not cover the effective baseline"
            )
        if command.operation == "revert":
            if current is None or command.candidate_id == current.candidate_id:
                raise ReviewRuleRefused(
                    "Revert must target a distinct prior eligible artifact"
                )
            previous_row = _history_row(
                database,
                company_id,
                before=current.revision,
                exclude_quarantined=True,
                exclude_candidate_id=current.candidate_id,
            )
            previous = None if previous_row is None else previous_row.receipt
            if previous is None or previous.candidate_id != command.candidate_id:
                raise ReviewRuleRefused(
                    "Revert must target the prior eligible artifact"
                )
            previous_result = _qualification(
                database, company_id, previous.qualification_id, previous.candidate_id
            )
            if (
                previous_result.receipt.qualification.candidate_digest
                != outcome.candidate_digest
            ):
                raise ReviewRuleRefused(
                    "Revert artifact differs from the previous version"
                )

        receipt = ReviewActivationReceipt(
            *astuple(command)[:4],
            command.expected_revision + 1,
            *astuple(authority),
        )
        now_ns = time_ns()
        event_seq = append_audit_event(
            database,
            company_id=company_id,
            domain="ops",
            entity_type="review_tool_activation",
            entity_id=command.candidate_id,
            event_type=_AUDIT_EVENT_BY_OP[command.operation],
            actor_id=authority.principal_id,
            command_id=command.request_id,
            payload={
                "operation": command.operation,
                "candidate_id": command.candidate_id,
                "qualification_id": command.qualification_id,
                "parent_asset_id": effective_parent,
                "revision": receipt.revision,
                "grant_id": authority.grant_id,
                "owner_id": authority.owner_id,
            },
            recorded_at_ns=now_ns,
        )
        try:
            _cmd_receipt, replayed = admit_command_receipt(
                database,
                company_id=company_id,
                command_id=command.request_id,
                actor_id=authority.principal_id,
                peer_pid=None,
                command_type=f"review_tool.{command.operation}",
                contract_version=ACTIVATION_CONTRACT_VERSION,
                payload_sha256=payload_digest,
                preconditions=preconditions,
                state="applied",
                result=asdict(receipt),
                applied_seq=event_seq,
                now_ns=now_ns,
            )
        except CommandConflict as error:
            raise ReviewRuleRefused(
                "Activation request identity already in use"
            ) from error
        if replayed:
            raise ReviewRuleRefused("Activation request identity already in use")
        database.execute(
            """INSERT INTO review_tool_activation (
                   company_id, request_id, operation, candidate_id, qualification_id,
                   parent_asset_id, revision, principal_id, grant_id, owner_id,
                   quarantined, quarantine_reason, recorded_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)""",
            (
                company_id,
                receipt.request_id,
                receipt.operation,
                receipt.candidate_id,
                receipt.qualification_id,
                effective_parent,
                receipt.revision,
                receipt.principal_id,
                receipt.grant_id,
                receipt.owner_id,
                now_ns,
            ),
        )
        return receipt


def quarantine_review_tool_lineage(
    writer: WriterLease,
    *,
    request_id: str,
    faulted_asset_id: str,
    reason: str,
    authorize: AuthorizeReviewTool,
    resolve: PublishedRuleResolver,
) -> QuarantineReceipt:
    """Quarantine a faulted asset and all transitive lineage descendants, rolling back to the last healthy artifact."""
    if not all(
        _identity_value(value) for value in (request_id, faulted_asset_id, reason)
    ):
        raise ReviewRuleRefused("Invalid review tool quarantine request")
    writer.verify()
    with (
        closing(open_writer(writer.store, lease=writer)) as database,
        write_tx(database),
    ):
        writer.verify()
        company_id = _identity(database)
        if company_id != writer.company_id:
            raise ReviewRuleRefused("Company identity mismatch")
        authority = _authorize(
            database, authorize, company_id, "quarantine", faulted_asset_id
        )
        payload_digest = _payload_sha256(
            {
                "operation": "quarantine",
                "faulted_asset_id": faulted_asset_id,
                "reason": reason,
            }
        )
        preconditions = {"faulted_asset_id": faulted_asset_id}
        existing_cmd = database.execute(
            """SELECT actor_id, command_type, contract_version, payload_sha256,
                      preconditions_json, state, result_json
               FROM command_receipt
               WHERE company_id = ? AND command_id = ?""",
            (company_id, request_id),
        ).fetchone()
        if existing_cmd is not None:
            if (
                existing_cmd[0] != authority.principal_id
                or existing_cmd[1] != "review_tool.quarantine"
                or existing_cmd[2] != ACTIVATION_CONTRACT_VERSION
                or existing_cmd[3] != payload_digest
                or existing_cmd[4]
                != json.dumps(preconditions, sort_keys=True, separators=(",", ":"))
                or existing_cmd[5] != "applied"
                or existing_cmd[6] is None
            ):
                raise ReviewRuleRefused("Quarantine request identity already in use")
            stored = json.loads(existing_cmd[6])
            return QuarantineReceipt(
                request_id=stored["request_id"],
                faulted_asset_id=stored["faulted_asset_id"],
                quarantined_assets=tuple(stored["quarantined_assets"]),
                restored_candidate_id=stored["restored_candidate_id"],
                restored_revision=stored["restored_revision"],
                principal_id=stored["principal_id"],
                grant_id=stored["grant_id"],
                owner_id=stored["owner_id"],
            )

        if not _asset_was_activated(database, company_id, faulted_asset_id):
            raise ReviewRuleRefused(
                "Faulted review tool asset is not in activation history"
            )

        quarantined_assets = _collect_lineage_descendants(
            database, company_id, faulted_asset_id
        )
        placeholders = ",".join("?" for _ in quarantined_assets)
        database.execute(
            f"""UPDATE review_tool_activation
                SET quarantined = 1, quarantine_reason = ?
                WHERE company_id = ? AND quarantined = 0
                  AND candidate_id IN ({placeholders})""",
            (reason, company_id, *quarantined_assets),
        )

        current_row = _history_row(database, company_id)
        assert current_row is not None
        restored_candidate_id: str | None = None
        restored_revision: int | None = None
        fallback_row: _ActivationRow | None = None
        if current_row.receipt.candidate_id in quarantined_assets:
            candidate_rows = database.execute(
                """SELECT request_id, operation, candidate_id, qualification_id, revision,
                          principal_id, grant_id, owner_id, parent_asset_id,
                          quarantined, quarantine_reason
                   FROM review_tool_activation
                   WHERE company_id = ? AND quarantined = 0
                   ORDER BY revision DESC""",
                (company_id,),
            ).fetchall()
            for raw in candidate_rows:
                candidate = _ActivationRow(
                    receipt=ReviewActivationReceipt(*raw[:8]),
                    parent_asset_id=None if raw[8] is None else str(raw[8]),
                    quarantined=bool(raw[9]),
                    quarantine_reason=None if raw[10] is None else str(raw[10]),
                )
                if _is_asset_quarantined(
                    database, company_id, candidate.receipt.candidate_id
                ) or _has_quarantined_ancestor(
                    database, company_id, candidate.receipt.candidate_id
                ):
                    continue
                try:
                    qual = _qualification(
                        database,
                        company_id,
                        candidate.receipt.qualification_id,
                        candidate.receipt.candidate_id,
                    )
                    if (
                        qual.receipt.qualification.evaluator_revision
                        != EVALUATOR_REVISION
                    ):
                        continue
                    _rule(database, company_id, qual, resolve)
                except ReviewRuleRefused:
                    continue
                fallback_row = candidate
                break

        now_ns = time_ns()
        if fallback_row is not None:
            restored_candidate_id = fallback_row.receipt.candidate_id
            restored_revision = current_row.receipt.revision + 1

        receipt = QuarantineReceipt(
            request_id=request_id,
            faulted_asset_id=faulted_asset_id,
            quarantined_assets=quarantined_assets,
            restored_candidate_id=restored_candidate_id,
            restored_revision=restored_revision,
            principal_id=authority.principal_id,
            grant_id=authority.grant_id,
            owner_id=authority.owner_id,
        )
        event_seq = append_audit_event(
            database,
            company_id=company_id,
            domain="ops",
            entity_type="review_tool_activation",
            entity_id=faulted_asset_id,
            event_type="ops.review_lineage_quarantined",
            actor_id=authority.principal_id,
            command_id=request_id,
            payload={
                "faulted_asset_id": faulted_asset_id,
                "reason": reason,
                "quarantined_assets": list(quarantined_assets),
                "restored_candidate_id": restored_candidate_id,
                "restored_revision": restored_revision,
            },
            recorded_at_ns=now_ns,
        )
        result_dict = {
            "request_id": receipt.request_id,
            "faulted_asset_id": receipt.faulted_asset_id,
            "quarantined_assets": list(receipt.quarantined_assets),
            "restored_candidate_id": receipt.restored_candidate_id,
            "restored_revision": receipt.restored_revision,
            "principal_id": receipt.principal_id,
            "grant_id": receipt.grant_id,
            "owner_id": receipt.owner_id,
        }
        try:
            _cmd_receipt, replayed = admit_command_receipt(
                database,
                company_id=company_id,
                command_id=request_id,
                actor_id=authority.principal_id,
                peer_pid=None,
                command_type="review_tool.quarantine",
                contract_version=ACTIVATION_CONTRACT_VERSION,
                payload_sha256=payload_digest,
                preconditions=preconditions,
                state="applied",
                result=result_dict,
                applied_seq=event_seq,
                now_ns=now_ns,
            )
        except CommandConflict as error:
            raise ReviewRuleRefused(
                "Quarantine request identity already in use"
            ) from error
        if replayed:
            raise ReviewRuleRefused("Quarantine request identity already in use")
        if fallback_row is not None and restored_revision is not None:
            database.execute(
                """INSERT INTO review_tool_activation (
                       company_id, request_id, operation, candidate_id, qualification_id,
                       parent_asset_id, revision, principal_id, grant_id, owner_id,
                       quarantined, quarantine_reason, recorded_at_ns
                   ) VALUES (?, ?, 'quarantine', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                (
                    company_id,
                    request_id,
                    fallback_row.receipt.candidate_id,
                    fallback_row.receipt.qualification_id,
                    fallback_row.parent_asset_id,
                    restored_revision,
                    authority.principal_id,
                    authority.grant_id,
                    authority.owner_id,
                    reason,
                    now_ns,
                ),
            )
        return receipt


@dataclass(frozen=True)
class ReviewWorkInput:
    company_id: str
    result_id: str
    revision: str
    present: tuple[EvidenceClass, ...]


ReadReviewInput = Callable[[Connection, str, str, str], ReviewWorkInput | None]


@dataclass(frozen=True)
class ReviewToolInspection:
    effective_revision: int
    principal_id: str
    result_id: str
    result_revision: str
    missing: tuple[EvidenceClass, ...]


def inspect_with_review_tool(
    store: Path,
    company_id: str,
    revision: int,
    result_id: str,
    *,
    authorize: AuthorizeReviewTool,
    resolve: PublishedRuleResolver,
    read_input: ReadReviewInput,
) -> ReviewToolInspection:
    if type(revision) is not int or revision < 1 or not _identity_value(result_id):
        raise ReviewRuleRefused("Invalid review tool inspection")
    with closing(open_reader(store)) as database, read_tx(database):
        if _identity(database) != company_id:
            raise ReviewRuleRefused("Company identity mismatch")
        current_row = _history_row(database, company_id)
        if current_row is None or revision != current_row.receipt.revision:
            raise ReviewRuleRefused("Review tool revision is not effective")
        if current_row.quarantined or _has_quarantined_ancestor(
            database, company_id, current_row.receipt.candidate_id
        ):
            raise ReviewRuleRefused("Review tool revision is quarantined")
        current = current_row.receipt
        authority = _authorize(
            database, authorize, company_id, "inspect", current.candidate_id
        )
        source = read_input(database, company_id, authority.principal_id, result_id)
        if (
            not isinstance(source, ReviewWorkInput)
            or (source.company_id, source.result_id) != (company_id, result_id)
            or not _identity_value(source.revision)
        ):
            raise ReviewRuleRefused("Work result is unavailable in this scope")
        qualified = _qualification(
            database, company_id, current.qualification_id, current.candidate_id
        )
        if qualified.receipt.qualification.evaluator_revision != EVALUATOR_REVISION:
            raise ReviewRuleRefused("Independent qualification is stale")
        rule = _rule(database, company_id, qualified, resolve)
        return ReviewToolInspection(
            revision,
            authority.principal_id,
            result_id,
            source.revision,
            rule.missing(source.present),
        )
