"""Isolated trial workspace allocation, SQLite snapshot backup, digests, and manifest utilities."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from time import time_ns
from typing import Any
from uuid import uuid4

from ..agent_desk import EMPTY_DESK_DIGEST, compute_agent_desk_digest
from ..company import _identity
from ..duty_run import WorkRunClaim, employment_cutoff
from ..runtime.engine import CompanyRuntime
from ..runtime.registry import DutyContext, DutyDescriptor, DutyOutcome, DutyRegistry
from ..runtime.shift_runner import is_authority_refusal_message
from ..runtime.work_kernel import (
    claim_work_run,
    create_work_item,
    record_work_decision,
)
from ..storage.db import connect_sqlite, open_reader, open_writer, read_tx, write_tx
from ..trial_registry import TrialInputs, TrialRefused, TrialSlot
from ..writer_lease import WriterLease

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_MAX_I64 = 2**63 - 1
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

_GEN2_TRANSFER_TOOL_MANIFEST: tuple[str, ...] = (
    "desk_list_drafts",
    "desk_read_draft",
    "desk_save_draft",
    "desk_read_offloaded_log",
    "skill_list_active",
    "skill_read",
    "skill_record_invocation",
    "skill_draft_candidate",
    "record_work_decision",
)


@dataclass(frozen=True)
class RecursiveLearningCycleResult:
    company_id: str
    evaluator_revision: str
    gen1_method_id: str
    incumbent_procedure_id: str
    candidate_procedure_id: str
    adopted_procedure_id: str
    adopted_method_id: str
    active_snapshot_id: str
    transfer_verdict: str
    distinct_transfer_families: int
    lcb_alpha: float
    perm_p: float
    shift_witnessed: bool
    witness_run_id: str
    witness_decision_id: str
    regressed_method_id: str
    quarantined_methods: tuple[str, ...]
    rollback_restored_incumbent: bool
    post_rollback_run_id: str
    post_rollback_snapshot_id: str | None


def _current_time_ns() -> int:
    """Read wall-clock nanoseconds, honoring patches on the sourdough.trial_runner facade."""
    facade = sys.modules.get("sourdough.trial_runner")
    fn = getattr(facade, "time_ns", None) if facade is not None else None
    if callable(fn):
        return int(fn())
    return time_ns()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    if not isinstance(payload, Mapping):
        raise TrialRefused("Trial payload must be a JSON object")
    try:
        encoded = _canonical_json(dict(payload)).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise TrialRefused("Trial payload must be finite canonical JSON") from error
    return sha256(encoded).hexdigest()


def _is_private_dir(info: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and (info.st_mode & 0o077) == 0
    )


def _verify_isolated_root(
    trial_root: Path,
    evaluator_store: Path,
    *,
    source_store: Path | None = None,
) -> None:
    if not isinstance(trial_root, Path) or not isinstance(evaluator_store, Path):
        raise TrialRefused("Trial root and evaluator store must be Paths")
    root_abs = trial_root.absolute()
    eval_abs = evaluator_store.absolute()

    if os.path.lexists(root_abs):
        try:
            info = root_abs.stat(follow_symlinks=False)
        except OSError as error:
            raise TrialRefused("Trial root is unavailable") from error
        if stat.S_ISLNK(info.st_mode) or not _is_private_dir(info):
            raise TrialRefused("Trial root must be a non-symlink 0700 directory")
        if os.path.lexists(root_abs / "company.sqlite"):
            raise TrialRefused("Trial root cannot alias an operating company directory")
    elif os.path.lexists(root_abs.parent):
        try:
            pinfo = root_abs.parent.stat(follow_symlinks=False)
        except OSError as error:
            raise TrialRefused("Trial root parent is unavailable") from error
        if stat.S_ISLNK(pinfo.st_mode) or not stat.S_ISDIR(pinfo.st_mode):
            raise TrialRefused("Trial root parent must be a real directory")

    try:
        eval_resolved = eval_abs.resolve()
        root_resolved = root_abs.resolve()
    except OSError as error:
        raise TrialRefused("Cannot resolve trial isolation paths") from error
    if eval_resolved == root_resolved:
        raise TrialRefused("Trial root cannot equal evaluator store")
    if eval_resolved.is_relative_to(root_resolved) or root_resolved.is_relative_to(
        eval_resolved.parent
    ):
        raise TrialRefused(
            "Trial root must be physically isolated from evaluator store"
        )

    if source_store is not None:
        try:
            src_resolved = source_store.absolute().resolve()
        except OSError as error:
            raise TrialRefused("Cannot resolve operating store path") from error
        if (
            src_resolved == root_resolved
            or src_resolved.parent == root_resolved
            or src_resolved.is_relative_to(root_resolved)
        ):
            raise TrialRefused("Trial root must be isolated from operating store")
        for op_subdir in (
            "desks",
            "leases",
            "checkpoints",
            ".sourdough-traces",
            ".sourdough-indexes",
        ):
            op_path = (src_resolved.parent / op_subdir).resolve()
            if root_resolved == op_path or root_resolved.is_relative_to(op_path):
                raise TrialRefused(
                    "Trial root must be isolated from operating company directories"
                )


def _ensure_private_trial_root(trial_root: Path) -> None:
    if not os.path.lexists(trial_root):
        trial_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(trial_root, 0o700)


def _backup_sqlite_private(source_store: Path, target_store: Path) -> None:
    target_store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target_store.parent, 0o700)
    if os.path.lexists(target_store):
        raise TrialRefused(f"Target trial store already exists: {target_store}")
    fd = os.open(
        target_store, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_NOFOLLOW, 0o600
    )
    os.close(fd)
    with (
        closing(open_reader(source_store)) as src_db,
        closing(connect_sqlite(target_store, timeout=5.0)) as dst_db,
    ):
        src_db.backup(dst_db)
    os.chmod(target_store, 0o600)


def _allocate_run_workspace(trial_root: Path, run_id: str) -> tuple[Path, Path, Path]:
    _ensure_private_trial_root(trial_root)
    root_fd = os.open(trial_root, os.O_RDONLY | os.O_DIRECTORY | _O_NOFOLLOW)
    try:
        root_info = os.fstat(root_fd)
        if not _is_private_dir(root_info):
            raise TrialRefused("Trial root directory must be 0700 user-owned")
        run_name = f"trial-{run_id}"
        try:
            os.mkdir(run_name, mode=0o700, dir_fd=root_fd)
        except OSError as error:
            raise TrialRefused(
                f"Cannot allocate fresh trial directory for {run_id}"
            ) from error
    finally:
        os.close(root_fd)

    run_dir = trial_root / f"trial-{run_id}"
    desk_dir = run_dir / "desk"
    desk_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    lease_dir = run_dir / "leases"
    lease_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    return run_dir, desk_dir, lease_dir


def _build_default_isolated_case_runner(
    *,
    company_id: str,
    agent_id: str,
    slot: TrialSlot,
    execute_case: Callable[[CompanyRuntime, Path, TrialSlot], dict[str, Any]]
    | None = None,
) -> tuple[DutyRegistry, dict[str, Any]]:
    captured: dict[str, Any] = {}
    registry = DutyRegistry()

    def _handle_trial_review(ctx: DutyContext) -> DutyOutcome:
        if execute_case is not None:
            return DutyOutcome(disposition="completed", result={"delegated": True})
        with (
            closing(open_writer(ctx.writer.store, lease=ctx.writer)) as db,
            write_tx(db),
        ):
            dec = record_work_decision(
                db,
                company_id=ctx.company_id,
                work_item_id=ctx.work_item.work_item_id,
                run_id=ctx.run.run_id,
                fencing_token=ctx.run.fencing_token,
                agent_id=ctx.work_item.owner_agent_id,
                action_kind="trial_decision",
                rationale=f"Grounded offline evaluation decision for {slot.case_id}",
                evidence=(),
                decision_at_ns=max(1, _current_time_ns()),
            )
        captured["decision_id"] = dec.decision_id
        captured["action_kind"] = dec.action_kind
        captured["work_item_id"] = ctx.work_item.work_item_id
        captured["run_id"] = ctx.run.run_id
        return DutyOutcome(
            disposition="completed",
            result={
                "decision_id": dec.decision_id,
                "case_id": slot.case_id,
                "family_id": slot.family_id,
            },
        )

    registry.register(
        DutyDescriptor(
            duty_kind="work.discovery_review",
            execute=_handle_trial_review,
        )
    )
    return registry, captured


def _compute_sqlite_state_digest(connection: Any) -> str:
    """Compute a deterministic SHA-256 over all user tables and ordered rows in a SQLite store."""
    hasher = sha256()
    tables = [
        str(r[0])
        for r in connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
               ORDER BY name"""
        ).fetchall()
    ]
    for table in tables:
        escaped = table.replace('"', '""')
        cols = [
            str(r[1])
            for r in connection.execute(f'PRAGMA table_info("{escaped}")').fetchall()
        ]
        order_clause = ", ".join(f'"{c.replace(chr(34), chr(34) * 2)}"' for c in cols)
        query = f'SELECT * FROM "{escaped}"' + (
            f" ORDER BY {order_clause}" if order_clause else ""
        )
        hasher.update(f"TABLE:{table}\n".encode("utf-8"))
        for row in connection.execute(query).fetchall():
            encoded_cells: list[Any] = []
            for cell in row:
                if isinstance(cell, bytes):
                    encoded_cells.append({"hex": cell.hex()})
                else:
                    encoded_cells.append(cell)
            hasher.update(_canonical_json(encoded_cells).encode("utf-8"))
            hasher.update(b"\n")
    return hasher.hexdigest()


def _count_receipt_violations(
    connection: Any,
    *,
    company_id: str | None = None,
    exec_runs: Sequence[str] | None = None,
) -> tuple[int, int, list[str]]:
    """Return (fatal_calls, authority_refusal_calls, tool_names) from tool_call_receipt."""
    clauses: list[str] = []
    params: list[Any] = []
    if company_id is not None:
        clauses.append("company_id = ?")
        params.append(company_id)
    if exec_runs is not None:
        if not exec_runs:
            return 0, 0, []
        placeholders = ",".join("?" for _ in exec_runs)
        clauses.append(f"run_id IN ({placeholders})")
        params.extend(exec_runs)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = connection.execute(
        f"""SELECT tool_name, status, error_type, error_message
            FROM tool_call_receipt
            {where_sql}
            ORDER BY recorded_at_ns ASC, call_id ASC""",
        tuple(params),
    ).fetchall()
    fatal_calls = 0
    authority_refusals = 0
    tool_names: list[str] = []
    for raw_tool, raw_status, raw_err_type, raw_err_msg in rows:
        tool_names.append(str(raw_tool))
        st = str(raw_status)
        if st in ("fatal", "denied"):
            fatal_calls += 1
        elif st == "refused" and is_authority_refusal_message(
            None if raw_err_type is None else str(raw_err_type),
            None if raw_err_msg is None else str(raw_err_msg),
        ):
            authority_refusals += 1
    return fatal_calls, authority_refusals, tool_names


def _parse_sealed_tool_manifest(blob: bytes) -> list[str]:
    """Extract canonical allowed tool names from a sealed TrialWorkerInput.tool_manifest."""
    try:
        parsed = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrialRefused("Sealed tool_manifest must be valid UTF-8 JSON") from error
    raw_items: Any
    if isinstance(parsed, Mapping):
        if "tools" not in parsed or not isinstance(parsed["tools"], list):
            raise TrialRefused(
                "Sealed tool_manifest JSON object must define a 'tools' list"
            )
        raw_items = parsed["tools"]
    elif isinstance(parsed, list):
        raw_items = parsed
    else:
        raise TrialRefused(
            "Sealed tool_manifest must be a JSON list or object with a 'tools' list"
        )
    names: list[str] = []
    for item in raw_items:
        if isinstance(item, str) and item.strip():
            name = item.strip()
        elif (
            isinstance(item, Mapping)
            and isinstance(item.get("name"), str)
            and item["name"].strip()
        ):
            name = item["name"].strip()
        else:
            raise TrialRefused(
                "Sealed tool_manifest entries must be nonempty tool name strings or objects with 'name'"
            )
        if name not in names:
            names.append(name)
    return sorted(names)


def _write_trial_execution_manifest(
    run_dir: Path,
    *,
    company_id: str,
    run_id: str,
    slot_id: str,
    work_item_ids: Sequence[str],
    executed_runs: Sequence[str],
) -> None:
    """Persist the trial's own newly created work items and executed runs for scoped grading."""
    manifest_path = run_dir / "trial_execution_manifest.json"
    data = _canonical_json(
        {
            "company_id": company_id,
            "run_id": run_id,
            "slot_id": slot_id,
            "work_item_ids": sorted(set(work_item_ids)),
            "executed_runs": sorted(set(executed_runs)),
        }
    ).encode("utf-8")
    tmp_path = run_dir / f".tmp-manifest-{uuid4().hex}.json"
    fd = os.open(
        tmp_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, manifest_path)


def _read_trial_execution_manifest(
    run_store: Path,
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Read (work_item_ids, executed_runs) from trial_execution_manifest.json beside run_store."""
    manifest_path = run_store.parent / "trial_execution_manifest.json"
    if not os.path.lexists(manifest_path):
        return None
    st = os.lstat(manifest_path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise TrialRefused("Trial execution manifest must be a regular file")
    fd = os.open(manifest_path, os.O_RDONLY | _O_NOFOLLOW)
    try:
        raw = os.read(fd, 1_048_576)
    finally:
        os.close(fd)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrialRefused("Trial execution manifest is corrupted") from error
    if not isinstance(payload, Mapping):
        raise TrialRefused("Trial execution manifest must be a JSON object")
    raw_items = payload.get("work_item_ids")
    raw_runs = payload.get("executed_runs")
    if not isinstance(raw_items, list) or not isinstance(raw_runs, list):
        raise TrialRefused("Trial execution manifest has invalid scope fields")
    items = tuple(str(x) for x in raw_items if isinstance(x, str) and x)
    runs = tuple(str(x) for x in raw_runs if isinstance(x, str) and x)
    return items, runs


def _compute_source_store_fixture(
    source_store: Path,
    company_id: str,
    *,
    agent_ids: Sequence[str] = (),
) -> tuple[bytes, dict[str, str], str]:
    """Derive canonical company + DB + Desk snapshot fixture bytes, agent profiles, and founder_id."""
    with closing(open_reader(source_store)) as ro_db, read_tx(ro_db):
        if _identity(ro_db) != company_id:
            raise TrialRefused("Source trial store company identity mismatch")
        co_row = ro_db.execute(
            "SELECT id, charter_revision, schema_revision FROM company WHERE id = ?",
            (company_id,),
        ).fetchone()
        if co_row is None:
            raise TrialRefused("Source trial store is missing company row")
        founder_row = ro_db.execute(
            "SELECT setup_identity, founder_id FROM founding_receipt WHERE company_id = ?",
            (company_id,),
        ).fetchone()
        if founder_row is None:
            raise TrialRefused("Source trial store is missing founding receipt")
        founder_id = str(founder_row[1])
        profiles = {
            str(r[0]): str(r[1])
            for r in ro_db.execute(
                "SELECT id, execution_profile FROM agent WHERE company_id = ? ORDER BY id",
                (company_id,),
            ).fetchall()
        }
        target_agents = tuple(dict.fromkeys(agent_ids)) if agent_ids else (founder_id,)
        desk_digests: dict[str, str] = {}
        for aid in sorted(target_agents):
            eligible = (
                aid in profiles
                and employment_cutoff(ro_db, aid) is None
                and ro_db.execute(
                    """SELECT 1 FROM standing_grant
                       WHERE agent_id = ? AND trim(scope) != '' AND lower(trim(scope)) != 'revoked'""",
                    (aid,),
                ).fetchone()
                is not None
                and ro_db.execute(
                    "SELECT 1 FROM private_desk WHERE agent_id = ?",
                    (aid,),
                ).fetchone()
                is not None
            )
            desk_digests[aid] = (
                compute_agent_desk_digest(source_store, aid)
                if eligible
                else EMPTY_DESK_DIGEST
            )
        db_state_sha256 = _compute_sqlite_state_digest(ro_db)
        fixture_bytes = _canonical_json(
            {
                "company_id": str(co_row[0]),
                "charter_revision": str(co_row[1]),
                "schema_revision": int(co_row[2]),
                "setup_identity": str(founder_row[0]),
                "db_state_sha256": db_state_sha256,
                "desk_digests": desk_digests,
            }
        ).encode("utf-8")
        return fixture_bytes, profiles, founder_id


def _open_claim_for_agent(
    writer: WriterLease,
    *,
    company_id: str,
    agent_id: str,
    grant_id: str,
    grant_scope: str,
    source_id: str,
    payload: Mapping[str, Any],
    now_ns: int,
) -> tuple[str, WorkRunClaim]:
    with closing(open_writer(writer.store, lease=writer)) as db, write_tx(db):
        item, _ = create_work_item(
            db,
            company_id=company_id,
            owner_agent_id=agent_id,
            duty_kind="work.discovery_review",
            source_kind="recursive_learning",
            source_id=source_id,
            payload=dict(payload),
            dedupe_key=f"rsi:{source_id}:{uuid4().hex[:8]}",
            actor_id=agent_id,
            now_ns=now_ns,
        )
        run = claim_work_run(
            db,
            company_id=company_id,
            work_item_id=item.work_item_id,
            lease_owner=f"rsi-runner:{agent_id}",
            lease_ttl_ns=60_000_000_000,
            now_ns=now_ns,
        )
    claim = WorkRunClaim(
        company_id=company_id,
        founder_id=agent_id,
        duty_id=item.work_item_id,
        run_id=run.run_id,
        fencing_token=run.fencing_token,
        grant_id=grant_id,
        grant_scope=grant_scope,
        purpose="work.discovery_review",
    )
    return item.work_item_id, claim


def _read_offloaded_receipt_payload(
    desk_dir: Path,
    raw_res: str | None,
    offloaded_log_path: str | None,
) -> Mapping[str, Any] | None:
    if not raw_res:
        return None
    try:
        parsed = json.loads(raw_res)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, Mapping):
        return None
    if parsed.get("offloaded") is not True:
        return parsed
    log_rel = offloaded_log_path or parsed.get("log_path")
    if not isinstance(log_rel, str) or not log_rel.startswith(".desk/logs/"):
        return parsed
    posix = PurePosixPath(log_rel)
    if posix.is_absolute() or any(part in ("", ".", "..") for part in posix.parts):
        return parsed
    cur = desk_dir
    for part in posix.parts:
        cur = cur / part
        if not os.path.lexists(cur) or stat.S_ISLNK(os.lstat(cur).st_mode):
            return parsed
    if not stat.S_ISREG(os.lstat(cur).st_mode):
        return parsed
    try:
        fd = os.open(cur, os.O_RDONLY | _O_NOFOLLOW)
        try:
            chunks: list[bytes] = []
            while chunk := os.read(fd, 65_536):
                chunks.append(chunk)
        finally:
            os.close(fd)
        loaded = json.loads(b"".join(chunks).decode("utf-8"))
        return loaded if isinstance(loaded, Mapping) else parsed
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return parsed


def _make_trial_inputs(
    fixture_bytes: bytes,
    case_bytes: bytes,
    ctx_bytes: bytes,
    tools_bytes: bytes,
    runtime_profile: str,
) -> TrialInputs:
    return TrialInputs(
        company_fixture=sha256(fixture_bytes).hexdigest(),
        case_inputs=sha256(case_bytes).hexdigest(),
        worker_context=sha256(ctx_bytes).hexdigest(),
        tools=sha256(tools_bytes).hexdigest(),
        runtime_profile=runtime_profile,
    )
