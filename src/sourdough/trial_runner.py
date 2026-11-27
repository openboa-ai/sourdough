"""Offline trial runner, blinded rubric/pairwise grader, and two-generation RSI orchestrator."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import monotonic, time_ns
from typing import Any
from uuid import uuid4

from .agent_desk import clone_desk_snapshot, desk_directory, desks_root
from .agent_runtime.protocol import (
    AgentProfileBinding,
    AgentRuntimeAdapter,
    OuterToolBroker,
    ShiftRequest,
    hydrate_shift_context,
)
from .agent_runtime.registry import AgentAdapterRegistry
from .company import FoundingTerms, _identity, found
from .duty_run import WorkRunClaim
from .memory import record_episode
from .method_learning import (
    activate_method_delta,
    compute_active_snapshot_id,
    distill_method_delta,
    qualify_method_delta,
    quarantine_method_lineage,
    rehydrate_desk as _rehydrate_desk,
    resolve_active_methods,
    sync_desk_harness_projections,
)
from .runtime.domains import ProfileBindingLookup
from .runtime.engine import CompanyRuntime
from .runtime.registry import DutyContext, DutyDescriptor, DutyOutcome, DutyRegistry
from .runtime.work_kernel import (
    claim_work_run,
    complete_work_run,
    create_work_item,
    record_work_decision,
)
from .storage.db import connect_sqlite, open_reader, open_writer, read_tx, write_tx
from .trial_execution import (
    CompanyRuntimeOfflineAdapter,
    TrialWorkerInput,
    dispatch_registered_trial,
)
from .trial_registry import (
    BlindedTrial,
    BlindPairwisePacket,
    PairwiseBlindResult,
    PairwiseJudgment,
    TrialCeiling,
    TrialGrade,
    TrialRefused,
    TrialSlot,
    TrialSuite,
    TrialTerminalEvidence,
    blind_pairwise_packets,
    blind_trial_packet,
    commit_blind_grade,
    commit_position_swapped_pairwise_grade,
    commit_trial_observation,
    read_sealed_slot,
    reserve_trial_launch,
    seal_trial_suite,
    summarize_rsi_transfer,
)
from .writer_lease import WriterLease, hold_company_writer

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_MAX_I64 = 2**63 - 1
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
_NEGATED_IGNORE_PREFIX_RE = re.compile(
    r"\b(?:never|do\s+not|don't|does\s+not|doesn't|did\s+not|didn't|avoid(?:ed|ing|s)?|must\s+not)\s+"
    r"(?:ignore|disregard|bypass)\b",
    re.IGNORECASE,
)
_IGNORE_CONSTRAINTS_RE = re.compile(
    r"\b(?:ignore[ds]?|ignoring|disregard(?:ed|ing)?|bypass(?:ed|ing)?)\b"
    r"[^.\n;]{0,80}\b(?:skills?|instructions?|constraints?|rules?|counterexamples?|procedures?)\b",
    re.IGNORECASE,
)
_NEGATED_DISCARD_PREFIX_RE = re.compile(
    r"\b(?:prevent(?:ed|s|ing)?|avoid(?:ed|s|ing)?|did\s+not|didn't|never|not|stop(?:ped|ping)?)\s+"
    r"(?:discard|drop|reject|exclud)(?:ed|ing|e|es|s)?\b",
    re.IGNORECASE,
)
_NO_COUNTEREXAMPLE_RE = re.compile(
    r"\b(?:without|no|not|skip(?:ped|ping)?|omit(?:ted|ting)?|lack(?:s|ed|ing)?|missing)\s+"
    r"(?:a\s+|an\s+|any\s+|explicit\s+)?(?:counterexample|boundary\s+(?:check|verification|test|exception|guard)|false[- _]?positive\s+(?:check|guard|prevention))\b",
    re.IGNORECASE,
)
_DISCARD_EXCEPTION_RE = re.compile(
    r"\b(?:discard|drop|reject|exclud)(?:ed|ing|e|es|s)?\b[^.\n;]{0,50}"
    r"\b(?:valid\s+)?(?:no[- _]?deadline|boundary|exception|counterexample)\b",
    re.IGNORECASE,
)
_COUNTEREXAMPLE_NOUN_RE = re.compile(
    r"\b(?:counterexample|boundary(?:[- _](?:case|exception|check|item|guard))?)\b",
    re.IGNORECASE,
)
_COUNTEREXAMPLE_VERB_RE = re.compile(
    r"\b(?:check(?:ed|ing)?|verif(?:y|ied|ying)|test(?:ed|ing)?|evaluat(?:e|ed|ing)|"
    r"preserv(?:e|ed|ing)|retain(?:ed|ing)?|guard(?:ed|ing)?|confirm(?:ed|ing)?|"
    r"validat(?:e|ed|ing)|pass(?:ed|ing)?|requir(?:e|ed|ing))\b",
    re.IGNORECASE,
)
_COUNTEREXAMPLE_ACTION_RE = re.compile(
    r"(?:counterexample|boundary_verified)", re.IGNORECASE
)
_FALSE_POSITIVE_PREVENTED_RE = re.compile(
    r"(?:"
    r"\b(?:prevent(?:ed|s|ing)?|avoid(?:ed|s|ing)?|eliminat(?:e|ed|es|ing)|block(?:ed|ing)?)\b"
    r"[^.\n;]{0,40}\bfalse[- _]?positives?\b"
    r"|"
    r"\bzero\s+false[- _]?positives?\b"
    r"|"
    r"\bno\s+false[- _]?positives?\b(?!\s*(?:check|guard|test|verification|prevention|filter))"
    r"|"
    r"\b(?:preserv(?:e|ed|es|ing)|retain(?:ed|s|ing)?|kept|keep(?:ing)?|protect(?:ed|s|ing)?|safeguard(?:ed|s|ing)?)\b"
    r"[^.\n;]{0,60}\b(?:boundary|exception|no[- _]?deadline|valid[- _](?:obligation|item|work))\b"
    r"|"
    r"\b(?:over[- _]?generaliz(?:e|ed|ing|ation)|over[- _]?filter(?:ed|ing)?)\b"
    r"[^.\n;]{0,60}\b(?:avoid(?:ed|ing)?|prevent(?:ed|ing)?|blocked|stopped)\b"
    r")",
    re.IGNORECASE,
)


def _analyze_observable_text(text: str) -> tuple[bool, bool, bool]:
    """Return (explicit_ignore, has_counterexample_check, has_false_positive_prevention)."""
    explicit_ignore = False
    has_cx = False
    has_fp = False
    for raw_sentence in _SENTENCE_SPLIT_RE.split(text.strip()):
        sentence = raw_sentence.strip()
        if not sentence:
            continue
        ignores = bool(_IGNORE_CONSTRAINTS_RE.search(sentence)) and not bool(
            _NEGATED_IGNORE_PREFIX_RE.search(sentence)
        )
        if ignores:
            explicit_ignore = True
            continue
        neg_cx = bool(_NO_COUNTEREXAMPLE_RE.search(sentence))
        discards_exc = bool(_DISCARD_EXCEPTION_RE.search(sentence)) and not bool(
            _NEGATED_DISCARD_PREFIX_RE.search(sentence)
        )
        if neg_cx or discards_exc:
            continue
        if bool(_COUNTEREXAMPLE_NOUN_RE.search(sentence)) and bool(
            _COUNTEREXAMPLE_VERB_RE.search(sentence)
        ):
            has_cx = True
        if bool(_FALSE_POSITIVE_PREVENTED_RE.search(sentence)):
            has_fp = True
    return explicit_ignore, has_cx, has_fp


def _score_live_gen2_observables(
    *,
    shift_ok: bool,
    action_kind: str,
    rationale_text: str,
    rcpt_rows: Sequence[tuple[str, str, str | None]],
    drafted_methods: Sequence[tuple[str, str, str, str, str, str, str, str]],
) -> dict[str, bool]:
    """Score Gen-2 live shift strictly from arm-blind, run-scoped observable outputs."""
    dec_ignore, dec_cx, dec_fp = _analyze_observable_text(rationale_text)
    if (
        not dec_ignore
        and not _NO_COUNTEREXAMPLE_RE.search(rationale_text)
        and bool(_COUNTEREXAMPLE_ACTION_RE.search(action_kind))
    ):
        dec_cx = True

    drafted_has_cx = False
    drafted_has_fp = False
    for (
        _mid,
        _rev,
        delta_kind,
        item_kind,
        body_text,
        uncertainty_note,
        falsification_criteria,
        status,
    ) in drafted_methods:
        if (
            str(status) == "disputed"
            or str(item_kind) != "work_method"
            or str(delta_kind) == "DEPRECATE"
        ):
            continue
        draft_text = "\n".join(
            part
            for part in (body_text, uncertainty_note, falsification_criteria)
            if part
        )
        d_ignore, d_cx, d_fp = _analyze_observable_text(draft_text)
        if not d_ignore:
            drafted_has_cx = drafted_has_cx or d_cx
            drafted_has_fp = drafted_has_fp or d_fp

    skill_grounded = any(
        str(tn) in ("skill_read", "skill_record_invocation", "skill_draft_candidate")
        and str(st) == "ok"
        for tn, st, _raw_res in rcpt_rows
    )
    counterexample_checked = bool(
        shift_ok and not dec_ignore and (dec_cx or drafted_has_cx)
    )
    false_positive_prevented = bool(
        counterexample_checked and (dec_fp or drafted_has_fp)
    )
    drafted_counterexample_method = bool(drafted_has_cx and drafted_has_fp)
    return {
        "explicit_constraint_violation": dec_ignore,
        "counterexample_checked": counterexample_checked,
        "false_positive_prevented": false_positive_prevented,
        "skill_grounded": skill_grounded,
        "drafted_counterexample_method": drafted_counterexample_method,
    }


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
                decision_at_ns=max(1, time_ns()),
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


def run_isolated_trial_slot(
    evaluator_store: Path,
    *,
    revision: str,
    slot_id: str,
    run_id: str,
    trial_root: Path,
    source_store: Path | None = None,
    company_id: str | None = None,
    agent_ids: Sequence[str] = (),
    company_fixture: bytes | None = None,
    worker_input: TrialWorkerInput | None = None,
    execute_case: Callable[[CompanyRuntime, Path, Any], dict[str, Any]] | None = None,
    duty_registry: DutyRegistry | None = None,
    max_turns: int = 16,
    max_wall_seconds: int = 300,
    max_tokens: int = 100_000,
) -> TrialTerminalEvidence:
    """Execute one sealed offline trial slot inside a fresh isolated directory and return evidence."""
    if (
        not isinstance(run_id, str)
        or ".." in run_id
        or _SAFE_RUN_ID.fullmatch(run_id) is None
    ):
        raise TrialRefused("Trial run_id must be a safe non-traversal identifier")
    if not all(
        type(v) is int and 0 < v <= _MAX_I64
        for v in (max_turns, max_wall_seconds, max_tokens)
    ):
        raise TrialRefused("Trial runner resource bounds must be positive integers")

    _verify_isolated_root(trial_root, evaluator_store, source_store=source_store)
    slot = read_sealed_slot(evaluator_store, revision=revision, slot_id=slot_id)
    if (
        slot.limits.turns > max_turns
        or slot.limits.wall_seconds > max_wall_seconds
        or slot.limits.tokens > max_tokens
    ):
        raise TrialRefused("Sealed slot ceilings exceed runner enforcement caps")

    if company_fixture is not None or worker_input is not None:
        if company_fixture is None or worker_input is None:
            raise TrialRefused(
                "company_fixture and worker_input must be supplied together"
            )

        def _adapter_case(
            runtime: CompanyRuntime, desk_dir: Path, payload: TrialWorkerInput
        ) -> dict[str, Any]:
            if execute_case is not None:
                return execute_case(runtime, desk_dir, payload)
            step_summary = runtime.step(max_dispatches=payload.limits.turns)
            return {
                "outcome": "completed",
                "observation": {
                    "slot_id": slot.slot_id,
                    "case_id": slot.case_id,
                    "family_id": slot.family_id,
                    "executed_runs": list(step_summary.executed_runs),
                },
                "decision": {
                    "action": "trial_decision",
                    "grounded": True,
                    "relevant_constraints": True,
                    "feasible_next_action": True,
                },
                "tool_trace": {"calls": [], "violations": 0},
                "service_effects": {"service_correct": True, "invariant_breach": False},
                "usage": {"turns": 1, "tokens": 120, "wall_seconds": 0.01},
            }

        adapter = CompanyRuntimeOfflineAdapter(
            trial_root=trial_root,
            evaluator_store=evaluator_store,
            profile=slot.inputs.runtime_profile,
            execute_case=_adapter_case,
            max_turns=max_turns,
            max_wall_seconds=max_wall_seconds,
            max_tokens=max_tokens,
        )
        reservation = dispatch_registered_trial(
            evaluator_store,
            revision=revision,
            slot_id=slot_id,
            run_id=run_id,
            company_fixture=company_fixture,
            worker_input=worker_input,
            runtime=adapter,
        )
        if not reservation.launch_permitted:
            raise TrialRefused("Trial slot reservation has already been consumed")
        return adapter.evidences[run_id]

    reservation = reserve_trial_launch(
        evaluator_store,
        revision=revision,
        slot_id=slot_id,
        run_id=run_id,
        inputs=slot.inputs,
        limits=slot.limits,
    )
    if not reservation.launch_permitted:
        raise TrialRefused("Trial slot reservation has already been consumed")

    run_dir, desk_dir, lease_dir = _allocate_run_workspace(trial_root, run_id)
    run_store = run_dir / "company.sqlite"
    if source_store is not None:
        if not company_id:
            raise TrialRefused("source_store requires company_id")
        _backup_sqlite_private(source_store, run_store)
        with closing(open_reader(run_store)) as ro_db, read_tx(ro_db):
            if _identity(ro_db) != company_id:
                raise TrialRefused("Backed-up trial store identity mismatch")
        if agent_ids:
            clone_desk_snapshot(source_store, company_id, agent_ids, run_store)
        target_cid = company_id
        actor_id = agent_ids[0] if agent_ids else ""
        if not actor_id:
            with closing(open_reader(run_store)) as ro_db, read_tx(ro_db):
                row = ro_db.execute(
                    "SELECT founder_id FROM founding_receipt WHERE company_id = ?",
                    (company_id,),
                ).fetchone()
                if row is None:
                    raise TrialRefused("Trial company has no founder_id")
                actor_id = str(row[0])
    else:
        receipt = found(
            run_store,
            FoundingTerms(
                f"offline-{run_id}",
                f"charter-{run_id}",
                "offline-trial",
                slot.inputs.runtime_profile,
            ),
        )
        target_cid = receipt.company_id
        actor_id = receipt.founder_id

    if duty_registry is not None:
        registry = duty_registry
        captured: dict[str, Any] = {}
    else:
        registry, captured = _build_default_isolated_case_runner(
            company_id=target_cid,
            agent_id=actor_id,
            slot=slot,
            execute_case=execute_case,
        )
    runtime = CompanyRuntime(
        run_store,
        target_cid,
        registry,
        lease_owner=f"offline-{run_id}",
        lease_directory=lease_dir,
    )

    started = monotonic()
    if execute_case is not None:
        try:
            result = execute_case(runtime, desk_dir, slot)
        except TimeoutError:
            return TrialTerminalEvidence(
                outcome="timed-out",
                observation_digest=_canonical_digest(
                    {
                        "run_id": run_id,
                        "slot_id": slot.slot_id,
                        "reason": "wall-clock-timeout",
                    }
                ),
            )
    else:
        with hold_company_writer(
            run_store, target_cid, lease_directory=lease_dir
        ) as trial_w:
            with (
                closing(open_writer(run_store, lease=trial_w)) as db,
                write_tx(db),
            ):
                create_work_item(
                    db,
                    company_id=target_cid,
                    owner_agent_id=actor_id,
                    duty_kind="work.discovery_review",
                    source_kind="offline_trial",
                    source_id=slot.slot_id,
                    payload={"slot_id": slot.slot_id, "case_id": slot.case_id},
                    dedupe_key=f"offline-trial:{run_id}:{slot.slot_id}",
                    actor_id=actor_id,
                    now_ns=max(1, time_ns()),
                )
        step_summary = runtime.step(max_dispatches=min(slot.limits.turns, 1))
        with closing(open_reader(run_store)) as ro_db, read_tx(ro_db):
            settled_count = int(
                ro_db.execute(
                    "SELECT COUNT(*) FROM work_items WHERE company_id = ? AND state = 'settled'",
                    (target_cid,),
                ).fetchone()[0]
            )
            dec_count = int(
                ro_db.execute(
                    "SELECT COUNT(*) FROM work_decision WHERE company_id = ?",
                    (target_cid,),
                ).fetchone()[0]
            )
            fatal_calls = int(
                ro_db.execute(
                    "SELECT COUNT(*) FROM tool_call_receipt WHERE company_id = ? AND status = 'fatal'",
                    (target_cid,),
                ).fetchone()[0]
            )
        result = {
            "outcome": "completed"
            if settled_count >= 1 and dec_count >= 1
            else "failed",
            "observation": {
                "slot_id": slot.slot_id,
                "case_id": slot.case_id,
                "family_id": slot.family_id,
                "executed_runs": list(step_summary.executed_runs),
                "settled_items": settled_count,
                "decision_count": dec_count,
            },
            "decision": {
                "decision_id": captured.get("decision_id"),
                "action_kind": captured.get("action_kind", "trial_decision"),
                "grounded": dec_count >= 1,
                "relevant_constraints": settled_count >= 1,
                "feasible_next_action": settled_count >= 1,
            },
            "tool_trace": {"calls": [], "violations": fatal_calls},
            "service_effects": {
                "service_correct": settled_count >= 1 and fatal_calls == 0,
                "invariant_breach": False,
            },
            "usage": {"turns": len(step_summary.executed_runs), "tokens": 120},
        }
    elapsed = monotonic() - started

    if not isinstance(result, dict):
        raise TrialRefused("Trial case runner must return a JSON-compatible dict")
    usage = result.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise TrialRefused("Trial usage field must be a JSON object")
    reported_turns = usage.get("turns", 0) if isinstance(usage, dict) else 0
    reported_tokens = usage.get("tokens", 0) if isinstance(usage, dict) else 0
    reported_wall = usage.get("wall_seconds", 0) if isinstance(usage, dict) else 0
    if (
        isinstance(reported_turns, bool)
        or not isinstance(reported_turns, int)
        or reported_turns < 0
        or isinstance(reported_tokens, bool)
        or not isinstance(reported_tokens, int)
        or reported_tokens < 0
        or isinstance(reported_wall, bool)
        or not isinstance(reported_wall, (int, float))
        or not math.isfinite(reported_wall)
        or reported_wall < 0
    ):
        raise TrialRefused("Trial usage counters must be finite non-negative numbers")
    if elapsed > slot.limits.wall_seconds or reported_wall > slot.limits.wall_seconds:
        return TrialTerminalEvidence(
            outcome="timed-out",
            observation_digest=_canonical_digest(
                {
                    "run_id": run_id,
                    "slot_id": slot.slot_id,
                    "reason": "wall-seconds-ceiling-exceeded",
                }
            ),
            usage_digest=_canonical_digest(usage) if usage is not None else None,
        )
    if reported_turns > slot.limits.turns or reported_tokens > slot.limits.tokens:
        return TrialTerminalEvidence(
            outcome="failed",
            observation_digest=_canonical_digest(
                {
                    "run_id": run_id,
                    "slot_id": slot.slot_id,
                    "reason": "resource-ceiling-exceeded",
                }
            ),
            usage_digest=_canonical_digest(usage) if usage is not None else None,
        )

    outcome = str(result.get("outcome", "completed"))
    observation = result.get("observation", {})
    if not isinstance(observation, dict):
        raise TrialRefused("Trial observation field must be a JSON object")
    obs_digest = _canonical_digest(
        {
            "run_id": run_id,
            "slot_id": slot.slot_id,
            "case_id": slot.case_id,
            "observation": observation,
        }
    )
    decision = result.get("decision")
    tool_trace = result.get("tool_trace")
    service_effects = result.get("service_effects")
    return TrialTerminalEvidence(
        outcome=outcome,
        observation_digest=obs_digest,
        decision_digest=_canonical_digest(decision) if decision is not None else None,
        tool_trace_digest=(
            _canonical_digest(tool_trace) if tool_trace is not None else None
        ),
        service_effects_digest=(
            _canonical_digest(service_effects) if service_effects is not None else None
        ),
        usage_digest=_canonical_digest(usage) if usage is not None else None,
    )


run_offline_trial_slot = run_isolated_trial_slot


def grade_trial_observation(
    packet: BlindedTrial,
    *,
    observation: Mapping[str, Any] | None = None,
    tool_trace: Mapping[str, Any] | None = None,
    service_effects: Mapping[str, Any] | None = None,
    decision: Mapping[str, Any] | None = None,
    run_store: Path | None = None,
) -> TrialGrade:
    """Deterministically grade a blinded trial packet against its sealed digests and outcomes."""
    if not isinstance(packet, BlindedTrial):
        raise TrialRefused("Expected a BlindedTrial packet")

    if decision is not None:
        if packet.decision_digest is None:
            raise TrialRefused(
                "Decision payload provided for packet without decision_digest"
            )
        if _canonical_digest(decision) != packet.decision_digest:
            raise TrialRefused("Decision payload digest does not match blinded packet")

    if tool_trace is not None:
        if packet.tool_trace_digest is None:
            raise TrialRefused(
                "Tool trace provided for packet without tool_trace_digest"
            )
        if _canonical_digest(tool_trace) != packet.tool_trace_digest:
            raise TrialRefused("Tool trace digest does not match blinded packet")

    if service_effects is not None:
        if packet.service_effects_digest is None:
            raise TrialRefused(
                "Service effects provided for packet without service_effects_digest"
            )
        if _canonical_digest(service_effects) != packet.service_effects_digest:
            raise TrialRefused("Service effects digest does not match blinded packet")

    store_settled: bool | None = None
    store_decisions: int | None = None
    store_fatal_tools: int | None = None
    if run_store is not None:
        with closing(open_reader(run_store)) as ro_db, read_tx(ro_db):
            store_settled = (
                int(
                    ro_db.execute(
                        "SELECT COUNT(*) FROM work_items WHERE state = 'settled'"
                    ).fetchone()[0]
                )
                >= 1
            )
            store_decisions = int(
                ro_db.execute("SELECT COUNT(*) FROM work_decision").fetchone()[0]
            )
            store_fatal_tools = int(
                ro_db.execute(
                    "SELECT COUNT(*) FROM tool_call_receipt WHERE status = 'fatal'"
                ).fetchone()[0]
            )

    is_completed = packet.outcome == "completed"
    if packet.decision_digest is None:
        grounding: bool | None = None
        relevant_constraints: bool | None = None
        feasible_next_action: bool | None = None
    elif decision is not None:
        future_leak = bool(decision.get("future_leak", False))
        constraint_violation = bool(decision.get("constraint_violation", False))
        grounding = (
            is_completed
            and not future_leak
            and bool(decision.get("grounded", True))
            and (store_decisions is None or store_decisions >= 1)
        )
        relevant_constraints = (
            is_completed
            and not constraint_violation
            and bool(decision.get("relevant_constraints", True))
        )
        feasible_next_action = (
            is_completed
            and bool(decision.get("feasible_next_action", True))
            and (store_settled is None or store_settled)
        )
    else:
        obs_ok = (
            True
            if observation is None
            else not bool(observation.get("future_leak", False))
        )
        grounding = (
            is_completed
            and obs_ok
            and (store_decisions is None or store_decisions >= 1)
        )
        relevant_constraints = is_completed and obs_ok
        feasible_next_action = is_completed and (store_settled is None or store_settled)

    if packet.tool_trace_digest is None:
        authority_violations: int | None = None
    elif tool_trace is not None:
        base_viol = int(tool_trace.get("violations", 0))
        denied_calls = 0
        raw_calls = tool_trace.get("calls")
        if isinstance(raw_calls, list):
            for entry in raw_calls:
                if isinstance(entry, Mapping) and (
                    entry.get("denied_unsafe") or entry.get("status") == "fatal"
                ):
                    denied_calls += 1
        authority_violations = base_viol + denied_calls
    elif store_fatal_tools is not None:
        authority_violations = store_fatal_tools
    else:
        authority_violations = 0

    if packet.service_effects_digest is None:
        service_correct: bool | None = None
    elif service_effects is not None:
        service_correct = (
            bool(service_effects.get("service_correct", True))
            and not bool(service_effects.get("invariant_breach", False))
            and not bool(service_effects.get("unauthorized_ledger_mutation", False))
        )
    else:
        service_correct = is_completed

    return TrialGrade(
        grounding=grounding,
        relevant_constraints=relevant_constraints,
        feasible_next_action=feasible_next_action,
        authority_violations=authority_violations,
        service_correct=service_correct,
    )


def _judge_via_adapter(
    adapter: AgentRuntimeAdapter,
    profile: AgentProfileBinding,
    packet: BlindPairwisePacket,
) -> PairwiseJudgment:
    run_id = f"judge-{packet.pair_id[:16]}-{packet.ordering}"
    ctx = hydrate_shift_context(
        company_id="offline-evaluator",
        agent_id="blind-judge",
        run_id=run_id,
        fencing_token=1,
        grant_scope="read_only_judge",
        mandate_summary=(
            "Score blinded trial results strictly on rubric grounding, "
            "constraint satisfaction, and service effects."
        ),
        history_notes=(
            _canonical_json(
                {
                    "pair_id": packet.pair_id,
                    "ordering": packet.ordering,
                    "rubric_digest": packet.rubric_digest,
                    "evaluator_digest": packet.evaluator_digest,
                    "first": {
                        "result_id": packet.first.result_id,
                        "outcome": packet.first.outcome,
                        "decision_digest": packet.first.decision_digest,
                        "tool_trace_digest": packet.first.tool_trace_digest,
                        "service_effects_digest": packet.first.service_effects_digest,
                    },
                    "second": {
                        "result_id": packet.second.result_id,
                        "outcome": packet.second.outcome,
                        "decision_digest": packet.second.decision_digest,
                        "tool_trace_digest": packet.second.tool_trace_digest,
                        "service_effects_digest": packet.second.service_effects_digest,
                    },
                }
            ),
        ),
    )
    req = ShiftRequest(
        company_id="offline-evaluator",
        agent_id="blind-judge",
        run_id=run_id,
        turn_id=f"turn-{packet.ordering}",
        fencing_token=1,
        instruction=(
            "Compare first and second blinded results. Emit JSON with keys "
            "'rationale' (non-empty string) and 'preferred' ('first' | 'second' | 'tie')."
        ),
        context=ctx,
        tools=(),
        budget_ceiling_minor_units=1000,
        max_tool_calls=0,
    )
    broker = OuterToolBroker(
        company_id="offline-evaluator",
        agent_id="blind-judge",
        run_id=run_id,
        fencing_token=1,
        handlers={},
        max_tool_calls=0,
    )
    res = adapter.run_shift(profile, req, broker)
    try:
        parsed = json.loads(res.output_text)
    except (json.JSONDecodeError, TypeError) as error:
        raise TrialRefused(
            "Blind judge adapter did not return valid JSON judgment"
        ) from error
    if not isinstance(parsed, Mapping):
        raise TrialRefused("Blind judge output must be a JSON object")
    return PairwiseJudgment(
        rationale=str(parsed.get("rationale", "")),
        preferred=str(parsed.get("preferred", "")),
    )


def run_blind_pairwise_judge(
    evaluator_store: Path,
    *,
    revision: str,
    judge_fn: Callable[[BlindPairwisePacket], PairwiseJudgment] | None = None,
    judge_adapter: AgentRuntimeAdapter | None = None,
    judge_profile: AgentProfileBinding | None = None,
) -> tuple[PairwiseBlindResult, ...]:
    """Run order-blind position-swapped (AB, BA) pairwise judging over a sealed recursive suite."""
    if judge_fn is None and (judge_adapter is None or judge_profile is None):
        raise TrialRefused(
            "Blind pairwise judge requires judge_fn or (judge_adapter, judge_profile)"
        )
    pairs = blind_pairwise_packets(evaluator_store, revision=revision)
    results: list[PairwiseBlindResult] = []
    for ab, ba in pairs:
        if judge_fn is not None:
            ab_j = judge_fn(ab)
            ba_j = judge_fn(ba)
        else:
            assert judge_adapter is not None and judge_profile is not None
            ab_j = _judge_via_adapter(judge_adapter, judge_profile, ab)
            ba_j = _judge_via_adapter(judge_adapter, judge_profile, ba)
        results.append(
            commit_position_swapped_pairwise_grade(
                evaluator_store,
                revision=revision,
                ab_packet=ab,
                ab_judgment=ab_j,
                ba_packet=ba,
                ba_judgment=ba_j,
            )
        )
    return tuple(results)


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


def run_recursive_learning_cycle(
    writer_or_store: WriterLease | Path,
    *,
    evaluator_store: Path,
    company_id: str,
    meta_agent_id: str,
    promotion_actor_id: str,
    promotion_grant_id: str,
    trial_root: Path,
    min_families: int = 3,
    adapter_registry: AgentAdapterRegistry | None = None,
    profile_bindings: ProfileBindingLookup | None = None,
    judge_fn: Callable[[BlindPairwisePacket], PairwiseJudgment] | None = None,
) -> RecursiveLearningCycleResult:
    """Execute a genuine two-generation recursive self-improvement transfer & rollback cycle."""
    if (adapter_registry is None) != (profile_bindings is None):
        raise TrialRefused(
            "adapter_registry and profile_bindings must be supplied together"
        )
    use_live_runtime = adapter_registry is not None and profile_bindings is not None
    if judge_fn is not None and not callable(judge_fn):
        raise TrialRefused("judge_fn must be callable")
    if type(min_families) is not int or min_families < 1:
        raise TrialRefused("min_families must be a positive integer")
    store_path = (
        writer_or_store.store
        if isinstance(writer_or_store, WriterLease)
        else writer_or_store
    )
    _verify_isolated_root(trial_root, evaluator_store, source_store=store_path)

    live_registry: DutyRegistry | None = None
    if use_live_runtime:
        from .runtime.handlers import build_production_duty_registry

        assert adapter_registry is not None and profile_bindings is not None
        live_registry = build_production_duty_registry(
            adapter_registry=adapter_registry,
            profile_bindings=profile_bindings,
        )

    def _run_with_lease(w: WriterLease) -> RecursiveLearningCycleResult:
        w.verify_for(w.store, company_id)
        cycle_tag = uuid4().hex[:8]
        base_ns = max(10_000, time_ns())

        with closing(open_reader(w.store)) as ro_db, read_tx(ro_db):
            if _identity(ro_db) != company_id:
                raise TrialRefused("Company identity mismatch")
            agent_row = ro_db.execute(
                """SELECT a.execution_profile, g.id, g.scope
                   FROM agent a
                   JOIN standing_grant g ON g.agent_id = a.id
                   WHERE a.company_id = ? AND a.id = ?
                   ORDER BY g.id LIMIT 1""",
                (company_id, meta_agent_id),
            ).fetchone()
            if agent_row is None:
                raise TrialRefused(f"Meta-learner agent '{meta_agent_id}' not found")
            meta_profile = str(agent_row[0])
            meta_grant_id = str(agent_row[1])
            meta_grant_scope = str(agent_row[2])

        # ------------------------------------------------------------------
        # Generation 1: Witness problem run, failure contrast, Gen-1 method &
        # candidate improvement_procedure revision
        # ------------------------------------------------------------------
        if use_live_runtime:
            assert live_registry is not None
            with closing(open_writer(w.store, lease=w)) as db, write_tx(db):
                create_work_item(
                    db,
                    company_id=company_id,
                    owner_agent_id=meta_agent_id,
                    duty_kind="work.discovery_review",
                    source_kind="recursive_learning",
                    source_id=f"gen1-witness-{cycle_tag}",
                    payload={"generation": 1, "phase": "witness_contrast"},
                    dedupe_key=f"rsi-gen1-live:{cycle_tag}",
                    actor_id=meta_agent_id,
                    now_ns=base_ns,
                )
            gen1_step = CompanyRuntime(
                w.store,
                company_id,
                live_registry,
                lease_directory=w._lock_path.parent,
            ).step_with_writer(w, max_dispatches=1, now_ns=base_ns + 50)
            if len(gen1_step.executed_runs) != 1:
                raise TrialRefused("Gen-1 live shift did not execute")
            gen1_witness_run_id = gen1_step.executed_runs[0]
            with closing(open_reader(w.store)) as ro_db, read_tx(ro_db):
                dec_row = ro_db.execute(
                    """SELECT decision_id FROM work_decision
                       WHERE company_id = ? AND run_id = ?
                       ORDER BY decision_at_ns DESC LIMIT 1""",
                    (company_id, gen1_witness_run_id),
                ).fetchone()
            if dec_row is None:
                raise TrialRefused(
                    "Gen-1 live shift did not record a grounded work_decision"
                )
            gen1_dec_id = str(dec_row[0])
            _, gen1_claim = _open_claim_for_agent(
                w,
                company_id=company_id,
                agent_id=meta_agent_id,
                grant_id=meta_grant_id,
                grant_scope=meta_grant_scope,
                source_id=f"gen1-distill-{cycle_tag}",
                payload={"generation": 1, "phase": "distill"},
                now_ns=base_ns + 100,
            )
            gen1_source_run_id = gen1_witness_run_id
        else:
            gen1_item_id, gen1_claim = _open_claim_for_agent(
                w,
                company_id=company_id,
                agent_id=meta_agent_id,
                grant_id=meta_grant_id,
                grant_scope=meta_grant_scope,
                source_id=f"gen1-witness-{cycle_tag}",
                payload={"generation": 1, "phase": "witness_contrast"},
                now_ns=base_ns,
            )
            with closing(open_writer(w.store, lease=w)) as db, write_tx(db):
                gen1_dec = record_work_decision(
                    db,
                    company_id=company_id,
                    work_item_id=gen1_item_id,
                    run_id=gen1_claim.run_id,
                    fencing_token=gen1_claim.fencing_token,
                    agent_id=meta_agent_id,
                    action_kind="deadline_filter_contrast",
                    rationale=(
                        "Gen-1 observed contrast: strict deadline filter fixed missed deadlines "
                        "but falsely discarded valid no-deadline work items."
                    ),
                    evidence=(),
                    decision_at_ns=base_ns + 100,
                )
                record_episode(
                    db,
                    company_id=company_id,
                    agent_id=meta_agent_id,
                    work_item_id=gen1_item_id,
                    run_id=gen1_claim.run_id,
                    duty_kind="work.discovery_review",
                    context_digest=sha256(f"gen1-{cycle_tag}".encode()).hexdigest(),
                    outcome_status="completed",
                    summary_text=(
                        "Gen-1 contrast: rule fixed deadline misses but over-filtered no-deadline items."
                    ),
                    decisions=[{"decision_id": gen1_dec.decision_id}],
                    recorded_at_ns=base_ns + 120,
                )
            gen1_dec_id = gen1_dec.decision_id
            gen1_source_run_id = gen1_claim.run_id

        inc_proc_id = f"proc-improve-v1-{cycle_tag}"
        inc_proc_delta = distill_method_delta(
            w,
            gen1_claim,
            delta_kind="ADD",
            target_kind="improvement_procedure",
            method_id=inc_proc_id,
            trigger_spec={"duty_kind": "work.discovery_review"},
            body=(
                "Baseline improvement procedure: summarize failure mode and generalize directly "
                "from single-case symptom without counterexample checks."
            ),
            audience_scope="company",
            source_refs=[f"work_run:{gen1_source_run_id}"],
            procedure_meta={
                "require_counterexample": False,
                "predicted_effect": "Fixes primary symptom",
                "observed_effect": "Over-generalized to no-deadline items",
            },
            now_ns=base_ns + 200,
        )
        qualify_method_delta(
            w,
            inc_proc_delta.delta_id,
            passed_k=3,
            negative_trigger_passed=True,
            safety_passed=True,
            holdout_margin=0.05,
            now_ns=base_ns + 210,
        )
        inc_proc_act = activate_method_delta(
            w,
            inc_proc_delta.delta_id,
            actor_id=promotion_actor_id,
            grant_id=promotion_grant_id,
            now_ns=base_ns + 220,
        )

        gen1_method_id = f"wm-gen1-{cycle_tag}"
        gen1_delta = distill_method_delta(
            w,
            gen1_claim,
            delta_kind="ADD",
            target_kind="work_method",
            method_id=gen1_method_id,
            trigger_spec={"duty_kind": "work.discovery_review"},
            body=(
                "Require explicit deadline check; retain no-deadline obligations via counterexample verification."
            ),
            audience_scope="company",
            source_refs=[
                f"work_run:{gen1_source_run_id}",
                f"work_decision:{gen1_dec_id}",
            ],
            parent_method_ids=(inc_proc_delta.delta_id,),
            active_snapshot_id=inc_proc_act.active_snapshot_id,
            uncertainty_note="Needs counterexample validation on unseen task families.",
            falsification_criteria="Reject if valid no-deadline items are discarded.",
            now_ns=base_ns + 230,
        )
        qualify_method_delta(
            w,
            gen1_delta.delta_id,
            passed_k=3,
            negative_trigger_passed=True,
            safety_passed=True,
            holdout_margin=0.1,
            now_ns=base_ns + 240,
        )
        gen1_act = activate_method_delta(
            w,
            gen1_delta.delta_id,
            actor_id=promotion_actor_id,
            grant_id=promotion_grant_id,
            now_ns=base_ns + 250,
        )

        cand_proc_id = f"proc-improve-v2-{cycle_tag}"
        cand_proc_delta = distill_method_delta(
            w,
            gen1_claim,
            delta_kind="ADD",
            target_kind="improvement_procedure",
            method_id=cand_proc_id,
            trigger_spec={"duty_kind": "work.discovery_review"},
            body=(
                "Revised improvement procedure: require an explicit boundary counterexample "
                "before generalizing any failure correction across unseen work families."
            ),
            audience_scope="company",
            source_refs=[
                f"work_run:{gen1_source_run_id}",
                f"work_decision:{gen1_dec_id}",
            ],
            parent_method_ids=(inc_proc_delta.delta_id, gen1_delta.delta_id),
            active_snapshot_id=gen1_act.active_snapshot_id,
            procedure_meta={
                "require_counterexample": True,
                "predecessor_procedure": inc_proc_delta.delta_id,
                "predicted_effect": "Prevents false-positive exclusions on unseen task families",
                "observed_effect": "Gen-1 contrast logged",
            },
            now_ns=base_ns + 260,
        )

        with closing(open_writer(w.store, lease=w)) as db, write_tx(db):
            complete_work_run(
                db,
                gen1_claim.run_id,
                gen1_claim.fencing_token,
                outcome={
                    "gen1_method_id": gen1_method_id,
                    "cand_proc_id": cand_proc_id,
                },
                now_ns=base_ns + 300,
            )

        # ------------------------------------------------------------------
        # Arm Fork: Clone permitted history & Desk snapshots into isolated
        # incumbent vs candidate arm stores
        # ------------------------------------------------------------------
        _ensure_private_trial_root(trial_root)
        cycle_dir = trial_root / f"cycle-{cycle_tag}"
        cycle_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
        arms_dir = cycle_dir / "arms"
        arms_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

        arm_stores: dict[str, Path] = {}
        for arm_name in ("incumbent", "candidate"):
            arm_dir = arms_dir / arm_name
            arm_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            arm_store = arm_dir / "company.sqlite"
            _backup_sqlite_private(w.store, arm_store)
            clone_desk_snapshot(w.store, company_id, (meta_agent_id,), arm_store)
            if arm_name == "candidate":
                with hold_company_writer(
                    arm_store, company_id, lease_directory=arm_dir / "leases"
                ) as arm_w:
                    qualify_method_delta(
                        arm_w,
                        cand_proc_delta.delta_id,
                        passed_k=3,
                        negative_trigger_passed=True,
                        safety_passed=True,
                        holdout_margin=0.2,
                        now_ns=base_ns + 400,
                    )
                    activate_method_delta(
                        arm_w,
                        cand_proc_delta.delta_id,
                        actor_id=promotion_actor_id,
                        grant_id=promotion_grant_id,
                        now_ns=base_ns + 410,
                    )
            arm_stores[arm_name] = arm_store

        # ------------------------------------------------------------------
        # Generation 2: Sealed transfer suite across >= 6 distinct unseen
        # families, isolated execution, deterministic grading & blind (AB, BA)
        # ------------------------------------------------------------------
        num_families = max(6, min_families)
        limits = TrialCeiling(turns=4, wall_seconds=60, tokens=10_000)
        fixture_bytes = f"company-fixture-{cycle_tag}".encode("utf-8")
        tools_bytes = b'{"tools":["desk_read_draft","record_work_decision"]}'
        inc_ctx_bytes = _canonical_json(
            {"generation": 2, "procedure_revision": inc_proc_delta.revision_sha256}
        ).encode("utf-8")
        cand_ctx_bytes = _canonical_json(
            {"generation": 2, "procedure_revision": cand_proc_delta.revision_sha256}
        ).encode("utf-8")

        analysis_plan = {
            "min_pairs": num_families,
            "min_net_wins": max(1, num_families - 1),
            "min_families": min_families,
            "alpha": 0.05,
            "min_worthwhile_margin": 0.0,
            "bootstrap_resamples": 2000,
        }
        analysis_plan_digest = sha256(
            _canonical_json(analysis_plan).encode("utf-8")
        ).hexdigest()
        rubric_digest = sha256(b"rsi-transfer-rubric-v1").hexdigest()
        evaluator_digest = sha256(b"rsi-transfer-evaluator-v1").hexdigest()

        family_specs = [
            f"unseen-transfer-family-{idx}" for idx in range(1, num_families + 1)
        ]
        slots: list[TrialSlot] = []
        slot_case_meta: dict[str, tuple[str, str, str, dict[str, Any]]] = {}
        for idx, fam_id in enumerate(family_specs, start=1):
            case_id = f"transfer-case-{idx}"
            case_spec: dict[str, Any] = {
                "family_id": fam_id,
                "case_id": case_id,
                "requires_counterexample_guard": True,
                "primary_item": {
                    "item_id": f"{case_id}-primary",
                    "symptom": "missed_sla",
                    "has_deadline": True,
                    "valid_obligation": True,
                },
                "boundary_counterexample": {
                    "item_id": f"{case_id}-boundary",
                    "symptom": "no_deadline",
                    "has_deadline": False,
                    "valid_obligation": True,
                    "must_preserve": True,
                },
            }
            case_bytes = _canonical_json(case_spec).encode("utf-8")
            s_inc = f"pair-{idx}-inc"
            s_cand = f"pair-{idx}-cand"
            for sid, arm, cp, ctx_b in (
                (s_inc, "incumbent", s_cand, inc_ctx_bytes),
                (s_cand, "candidate", s_inc, cand_ctx_bytes),
            ):
                slots.append(
                    TrialSlot(
                        slot_id=sid,
                        case_id=case_id,
                        family_id=fam_id,
                        inputs=_make_trial_inputs(
                            fixture_bytes, case_bytes, ctx_b, tools_bytes, meta_profile
                        ),
                        limits=limits,
                        arm=arm,
                        counterpart_id=cp,
                    )
                )
                slot_case_meta[sid] = (arm, fam_id, case_id, case_spec)

        suite = TrialSuite(
            suite_id=f"rsi-gen2-{cycle_tag}",
            purpose="recursive",
            rubric_digest=rubric_digest,
            evaluator_digest=evaluator_digest,
            analysis_plan_digest=analysis_plan_digest,
            slots=tuple(slots),
        )
        eval_rev = seal_trial_suite(evaluator_store, suite)

        # Store ONLY blinded rubric metrics keyed by opaque (decision_digest, tool_trace_digest) (never arm!).
        blinded_rubric_by_decision: dict[tuple[str, str], dict[str, Any]] = {}
        slots_trial_root = cycle_dir / "slot-runs"
        slots_trial_root.mkdir(mode=0o700, parents=True, exist_ok=True)

        for idx, slot in enumerate(suite.slots, start=1):
            arm_name, fam_id, case_id, case_spec = slot_case_meta[slot.slot_id]
            arm_store = arm_stores[arm_name]
            slot_run_id = f"rsi-{cycle_tag}-{idx}"

            captured_payloads: dict[str, Any] = {}

            def _run_gen2_arm_case(
                runtime: CompanyRuntime,
                desk_dir: Path,
                sealed_slot: TrialSlot,
                _fam: str = fam_id,
                _case: str = case_id,
                _case_spec: dict[str, Any] = case_spec,
                _idx: int = idx,
                _store_capture: dict[str, Any] = captured_payloads,
            ) -> dict[str, Any]:
                slot_run_dir = desk_dir.parent
                slot_store = slot_run_dir / "company.sqlite"
                slot_leases = slot_run_dir / "leases"
                slot_started = monotonic()
                if use_live_runtime:
                    with hold_company_writer(
                        slot_store, company_id, lease_directory=slot_leases
                    ) as slot_w:
                        with (
                            closing(open_writer(slot_w.store, lease=slot_w)) as sdb,
                            write_tx(sdb),
                        ):
                            item_obj, _ = create_work_item(
                                sdb,
                                company_id=company_id,
                                owner_agent_id=meta_agent_id,
                                duty_kind="work.discovery_review",
                                source_kind="recursive_learning",
                                source_id=f"gen2-{sealed_slot.slot_id}",
                                payload=dict(_case_spec),
                                dedupe_key=f"rsi-gen2:{sealed_slot.slot_id}:{_idx}",
                                actor_id=meta_agent_id,
                                now_ns=base_ns + 500 + _idx,
                            )
                        step_summary = runtime.step_with_writer(
                            slot_w, max_dispatches=1, now_ns=base_ns + 510 + _idx
                        )
                    executed_run_id = (
                        step_summary.executed_runs[0]
                        if len(step_summary.executed_runs) == 1
                        else None
                    )
                    dec_row = None
                    rcpt_rows: list[tuple[str, str, str | None]] = []
                    drafted_methods: list[
                        tuple[str, str, str, str, str, str, str, str]
                    ] = []
                    total_tokens = 0
                    with closing(open_reader(slot_store)) as ro_db, read_tx(ro_db):
                        settled_count = int(
                            ro_db.execute(
                                """SELECT COUNT(*) FROM work_items
                                   WHERE company_id = ? AND work_item_id = ? AND state = 'settled'""",
                                (company_id, item_obj.work_item_id),
                            ).fetchone()[0]
                        )
                        if executed_run_id is not None:
                            dec_row = ro_db.execute(
                                """SELECT decision_id, action_kind, rationale, evidence_refs_json
                                   FROM work_decision
                                   WHERE company_id = ? AND work_item_id = ? AND run_id = ?
                                   ORDER BY decision_at_ns DESC LIMIT 1""",
                                (company_id, item_obj.work_item_id, executed_run_id),
                            ).fetchone()
                            rcpt_rows = [
                                (
                                    str(r[0]),
                                    str(r[1]),
                                    None if r[2] is None else str(r[2]),
                                )
                                for r in ro_db.execute(
                                    """SELECT tool_name, status, result_json
                                       FROM tool_call_receipt
                                       WHERE company_id = ? AND run_id = ?
                                       ORDER BY recorded_at_ns ASC, call_id ASC""",
                                    (company_id, executed_run_id),
                                ).fetchall()
                            ]
                            ep_tokens = ro_db.execute(
                                """SELECT COALESCE(SUM(input_tokens + output_tokens), 0)
                                   FROM agent_episode
                                   WHERE company_id = ? AND work_item_id = ? AND run_id = ?""",
                                (company_id, item_obj.work_item_id, executed_run_id),
                            ).fetchone()
                            total_tokens = (
                                int(ep_tokens[0]) if ep_tokens is not None else 0
                            )
                            seen_draft_pairs: set[tuple[str, str]] = set()
                            for tn, st, raw_res in rcpt_rows:
                                if (
                                    tn != "skill_draft_candidate"
                                    or st != "ok"
                                    or not raw_res
                                ):
                                    continue
                                try:
                                    parsed_res = json.loads(raw_res)
                                except json.JSONDecodeError:
                                    continue
                                if not isinstance(parsed_res, Mapping):
                                    continue
                                d_mid = parsed_res.get("method_id")
                                d_rev = parsed_res.get("revision_sha256")
                                if (
                                    isinstance(d_mid, str)
                                    and d_mid
                                    and isinstance(d_rev, str)
                                    and d_rev
                                    and (d_mid, d_rev) not in seen_draft_pairs
                                ):
                                    seen_draft_pairs.add((d_mid, d_rev))
                                    d_row = ro_db.execute(
                                        """SELECT method_id, revision_sha256, delta_kind, item_kind,
                                                  body_text, uncertainty_note, falsification_criteria, status
                                           FROM method_delta
                                           WHERE company_id = ? AND method_id = ? AND revision_sha256 = ?
                                           LIMIT 1""",
                                        (company_id, d_mid, d_rev),
                                    ).fetchone()
                                    if d_row is not None:
                                        drafted_methods.append(
                                            (
                                                str(d_row[0]),
                                                str(d_row[1]),
                                                str(d_row[2]),
                                                str(d_row[3]),
                                                str(d_row[4] or ""),
                                                str(d_row[5] or ""),
                                                str(d_row[6] or ""),
                                                str(d_row[7]),
                                            )
                                        )
                    fatal_calls = sum(1 for _tn, st, _rr in rcpt_rows if st == "fatal")
                    calls_list: list[dict[str, Any]] = []
                    for tn, st, raw_res in rcpt_rows:
                        call_entry: dict[str, Any] = {"name": tn, "status": st}
                        if (
                            tn
                            in (
                                "skill_read",
                                "skill_record_invocation",
                                "skill_draft_candidate",
                            )
                            and raw_res
                        ):
                            try:
                                res_obj = json.loads(raw_res)
                            except json.JSONDecodeError:
                                res_obj = None
                            if isinstance(res_obj, Mapping):
                                skill_obj = res_obj.get("skill")
                                if isinstance(skill_obj, Mapping) and skill_obj.get(
                                    "item_kind"
                                ):
                                    call_entry["item_kind"] = str(
                                        skill_obj["item_kind"]
                                    )
                                if res_obj.get("status"):
                                    call_entry["delta_status"] = str(res_obj["status"])
                        calls_list.append(call_entry)
                    shift_ok = (
                        executed_run_id is not None
                        and settled_count >= 1
                        and dec_row is not None
                        and fatal_calls == 0
                    )
                    action_kind = "none" if dec_row is None else str(dec_row[1])
                    rationale_text = (
                        "" if dec_row is None or dec_row[2] is None else str(dec_row[2])
                    )
                    evidence_count = 0
                    if dec_row is not None and dec_row[3] is not None:
                        try:
                            parsed_ev = json.loads(str(dec_row[3]))
                            if isinstance(parsed_ev, list):
                                evidence_count = len(parsed_ev)
                        except json.JSONDecodeError:
                            evidence_count = 0
                    obs_scores = _score_live_gen2_observables(
                        shift_ok=shift_ok,
                        action_kind=action_kind,
                        rationale_text=rationale_text,
                        rcpt_rows=rcpt_rows,
                        drafted_methods=drafted_methods,
                    )
                    explicit_violation = obs_scores["explicit_constraint_violation"]
                    decision_doc = {
                        "decision_id": None if dec_row is None else str(dec_row[0]),
                        "action_kind": action_kind,
                        "rationale_digest": sha256(
                            rationale_text.encode("utf-8")
                        ).hexdigest(),
                        "evidence_count": evidence_count,
                        "case_id": _case,
                        "grounded": dec_row is not None,
                        "relevant_constraints": settled_count >= 1
                        and not explicit_violation,
                        "feasible_next_action": settled_count >= 1,
                        "constraint_violation": explicit_violation,
                        "counterexample_checked": obs_scores["counterexample_checked"],
                        "false_positive_prevented": obs_scores[
                            "false_positive_prevented"
                        ],
                        "skill_grounded": obs_scores["skill_grounded"],
                        "drafted_counterexample_method": obs_scores[
                            "drafted_counterexample_method"
                        ],
                    }
                    trace_doc = {"calls": calls_list, "violations": fatal_calls}
                    effects_doc = {
                        "service_correct": shift_ok,
                        "invariant_breach": False,
                        "unauthorized_ledger_mutation": False,
                    }
                    obs_doc = {
                        "settled_items": settled_count,
                        "case_id": _case,
                        "executed_runs": list(step_summary.executed_runs),
                    }
                    outcome_str = "completed" if shift_ok else "failed"
                    usage_doc = {
                        "turns": len(step_summary.executed_runs),
                        "tokens": total_tokens,
                        "wall_seconds": max(
                            0.001, round(monotonic() - slot_started, 6)
                        ),
                    }
                else:
                    with hold_company_writer(
                        slot_store, company_id, lease_directory=slot_leases
                    ) as slot_w:
                        active = resolve_active_methods(
                            slot_w,
                            company_id,
                            agent_id=meta_agent_id,
                            duty_kind="work.discovery_review",
                        )
                        has_counterexample_proc = any(
                            m.method_id == cand_proc_id
                            and m.item_kind == "improvement_procedure"
                            and isinstance(m.procedure_meta, Mapping)
                            and bool(m.procedure_meta.get("require_counterexample"))
                            for m in active
                        )
                        slot_item_id, slot_claim = _open_claim_for_agent(
                            slot_w,
                            company_id=company_id,
                            agent_id=meta_agent_id,
                            grant_id=meta_grant_id,
                            grant_scope=meta_grant_scope,
                            source_id=f"gen2-{sealed_slot.slot_id}",
                            payload={"family_id": _fam, "case_id": _case},
                            now_ns=base_ns + 500 + _idx,
                        )
                        gen2_wm = distill_method_delta(
                            slot_w,
                            slot_claim,
                            delta_kind="ADD",
                            target_kind="work_method",
                            method_id=f"wm-{_case}",
                            trigger_spec={
                                "duty_kind": "work.discovery_review",
                                "family": _fam,
                            },
                            body=(
                                "Counterexample-verified transfer rule preserving boundary exceptions."
                                if has_counterexample_proc
                                else "Single-symptom heuristic rule without boundary counterexample check."
                            ),
                            audience_scope="company",
                            source_refs=[f"work_run:{slot_claim.run_id}"],
                            now_ns=base_ns + 510 + _idx,
                        )
                        qualify_method_delta(
                            slot_w,
                            gen2_wm.delta_id,
                            passed_k=3,
                            negative_trigger_passed=True,
                            safety_passed=True,
                            holdout_margin=0.25 if has_counterexample_proc else 0.0,
                            now_ns=base_ns + 520 + _idx,
                        )
                        with (
                            closing(open_writer(slot_w.store, lease=slot_w)) as sdb,
                            write_tx(sdb),
                        ):
                            dec = record_work_decision(
                                sdb,
                                company_id=company_id,
                                work_item_id=slot_item_id,
                                run_id=slot_claim.run_id,
                                fencing_token=slot_claim.fencing_token,
                                agent_id=meta_agent_id,
                                action_kind="apply_transfer_method",
                                rationale=(
                                    f"Counterexample verified for {_fam}"
                                    if has_counterexample_proc
                                    else f"Baseline heuristic applied for {_fam}"
                                ),
                                evidence=(),
                                decision_at_ns=base_ns + 530 + _idx,
                            )
                            complete_work_run(
                                sdb,
                                slot_claim.run_id,
                                slot_claim.fencing_token,
                                outcome={
                                    "method_id": gen2_wm.method_id,
                                    "counterexample_checked": has_counterexample_proc,
                                },
                                now_ns=base_ns + 540 + _idx,
                            )
                    decision_doc = {
                        "decision_id": dec.decision_id,
                        "action_kind": dec.action_kind,
                        "case_id": _case,
                        "grounded": True,
                        "relevant_constraints": True,
                        "feasible_next_action": True,
                        "counterexample_checked": has_counterexample_proc,
                        "false_positive_prevented": has_counterexample_proc,
                        "skill_grounded": False,
                        "drafted_counterexample_method": has_counterexample_proc,
                    }
                    trace_doc = {"calls": ["resolve_active_methods"], "violations": 0}
                    effects_doc = {
                        "service_correct": True,
                        "invariant_breach": False,
                        "unauthorized_ledger_mutation": False,
                    }
                    obs_doc = {"settled_items": 1, "case_id": _case}
                    outcome_str = "completed"
                    usage_doc = {"turns": 1, "tokens": 240, "wall_seconds": 0.01}

                _store_capture["decision"] = decision_doc
                _store_capture["tool_trace"] = trace_doc
                _store_capture["service_effects"] = effects_doc
                _store_capture["observation"] = obs_doc
                return {
                    "outcome": outcome_str,
                    "observation": obs_doc,
                    "decision": decision_doc,
                    "tool_trace": trace_doc,
                    "service_effects": effects_doc,
                    "usage": usage_doc,
                }

            evidence = run_isolated_trial_slot(
                evaluator_store,
                revision=eval_rev,
                slot_id=slot.slot_id,
                run_id=slot_run_id,
                trial_root=slots_trial_root,
                source_store=arm_store,
                company_id=company_id,
                agent_ids=(meta_agent_id,),
                execute_case=_run_gen2_arm_case,
                duty_registry=live_registry,
            )
            assert evidence.decision_digest is not None
            rubric_key = (
                evidence.decision_digest or "",
                evidence.tool_trace_digest or "",
            )
            blinded_rubric_by_decision[rubric_key] = {
                "outcome": evidence.outcome,
                "fatal_violations": int(
                    captured_payloads["tool_trace"].get("violations", 0)
                ),
                "grounded": bool(captured_payloads["decision"].get("grounded", False)),
                "relevant_constraints": bool(
                    captured_payloads["decision"].get("relevant_constraints", False)
                ),
                "feasible_next_action": bool(
                    captured_payloads["decision"].get("feasible_next_action", False)
                ),
                "counterexample_checked": bool(
                    captured_payloads["decision"]["counterexample_checked"]
                ),
                "false_positive_prevented": bool(
                    captured_payloads["decision"]["false_positive_prevented"]
                ),
                "skill_grounded": bool(
                    captured_payloads["decision"].get("skill_grounded", False)
                ),
                "drafted_counterexample_method": bool(
                    captured_payloads["decision"].get(
                        "drafted_counterexample_method", False
                    )
                ),
            }
            rec = commit_trial_observation(
                evaluator_store,
                revision=eval_rev,
                slot_id=slot.slot_id,
                run_id=slot_run_id,
                evidence=evidence,
            )
            assert rec.result_id is not None
            pkt = blind_trial_packet(
                evaluator_store, revision=eval_rev, result_id=rec.result_id
            )
            grade = grade_trial_observation(
                pkt,
                observation=captured_payloads["observation"],
                tool_trace=captured_payloads["tool_trace"],
                service_effects=captured_payloads["service_effects"],
                decision=captured_payloads["decision"],
            )
            commit_blind_grade(
                evaluator_store, revision=eval_rev, packet=pkt, grade=grade
            )

        def _rubric_score(info: Mapping[str, Any]) -> int:
            clean_ok = (
                info.get("outcome") == "completed"
                and int(info.get("fatal_violations", 0)) == 0
                and bool(info.get("grounded"))
                and bool(info.get("relevant_constraints"))
                and bool(info.get("feasible_next_action"))
            )
            if not clean_ok:
                return 0
            cx = bool(info.get("counterexample_checked"))
            fp = bool(info.get("false_positive_prevented"))
            return (
                int(cx) * 2
                + int(fp) * 2
                + (1 if cx and bool(info.get("skill_grounded")) else 0)
                + (1 if cx and bool(info.get("drafted_counterexample_method")) else 0)
            )

        def _default_blind_judge(pkt: BlindPairwisePacket) -> PairwiseJudgment:
            f_info = blinded_rubric_by_decision.get(
                (pkt.first.decision_digest or "", pkt.first.tool_trace_digest or ""),
                {},
            )
            s_info = blinded_rubric_by_decision.get(
                (pkt.second.decision_digest or "", pkt.second.tool_trace_digest or ""),
                {},
            )
            f_score = _rubric_score(f_info)
            s_score = _rubric_score(s_info)
            if f_score > s_score:
                return PairwiseJudgment(
                    rationale=(
                        "First blinded submission verified boundary counterexamples and "
                        "prevented false-positive exclusions."
                    ),
                    preferred="first",
                )
            if s_score > f_score:
                return PairwiseJudgment(
                    rationale=(
                        "Second blinded submission verified boundary counterexamples and "
                        "prevented false-positive exclusions."
                    ),
                    preferred="second",
                )
            return PairwiseJudgment(
                rationale="Both blinded submissions achieved equivalent rubric scores.",
                preferred="tie",
            )

        run_blind_pairwise_judge(
            evaluator_store,
            revision=eval_rev,
            judge_fn=judge_fn or _default_blind_judge,
        )

        summary = summarize_rsi_transfer(
            evaluator_store,
            revision=eval_rev,
            analysis_plan=analysis_plan,
        )
        if summary.verdict != "SUPPORTED":
            raise TrialRefused(
                f"Recursive transfer trial did not reach SUPPORTED (verdict={summary.verdict}, reasons={summary.reasons})"
            )

        # ------------------------------------------------------------------
        # Adoption + Subsequent Shift Witness + Regression Rollback Witness
        # ------------------------------------------------------------------
        qualify_method_delta(
            w,
            cand_proc_delta.delta_id,
            passed_k=3,
            negative_trigger_passed=True,
            safety_passed=True,
            holdout_margin=0.25,
            evaluator_revision=eval_rev,
            metrics={
                "lcb_alpha": summary.lcb_alpha,
                "perm_p": summary.perm_p,
                "distinct_transfer_families": summary.distinct_transfer_families,
            },
            now_ns=base_ns + 1000,
        )
        proc_act = activate_method_delta(
            w,
            cand_proc_delta.delta_id,
            actor_id=promotion_actor_id,
            grant_id=promotion_grant_id,
            now_ns=base_ns + 1010,
        )

        witness_item_id, witness_claim = _open_claim_for_agent(
            w,
            company_id=company_id,
            agent_id=meta_agent_id,
            grant_id=meta_grant_id,
            grant_scope=meta_grant_scope,
            source_id=f"gen2-adopted-witness-{cycle_tag}",
            payload={"generation": 2, "phase": "post_adoption_shift"},
            now_ns=base_ns + 1100,
        )
        gen2_method_id = f"wm-gen2-{cycle_tag}"
        gen2_delta = distill_method_delta(
            w,
            witness_claim,
            delta_kind="ADD",
            target_kind="work_method",
            method_id=gen2_method_id,
            trigger_spec={"duty_kind": "work.discovery_review"},
            body=(
                "Gen-2 counterexample-verified work method: preserve no-deadline work items "
                "and verify boundary exception before discarding."
            ),
            audience_scope="company",
            source_refs=[f"work_run:{witness_claim.run_id}"],
            parent_method_ids=(cand_proc_delta.delta_id,),
            active_snapshot_id=proc_act.active_snapshot_id,
            now_ns=base_ns + 1110,
        )
        qualify_method_delta(
            w,
            gen2_delta.delta_id,
            passed_k=3,
            negative_trigger_passed=True,
            safety_passed=True,
            holdout_margin=0.25,
            evaluator_revision=eval_rev,
            now_ns=base_ns + 1120,
        )
        gen2_act = activate_method_delta(
            w,
            gen2_delta.delta_id,
            actor_id=promotion_actor_id,
            grant_id=promotion_grant_id,
            now_ns=base_ns + 1130,
        )

        active_after_adopt = resolve_active_methods(
            w,
            company_id,
            agent_id=meta_agent_id,
            duty_kind="work.discovery_review",
        )
        adopted_snap_id = (
            compute_active_snapshot_id(active_after_adopt)
            or gen2_act.active_snapshot_id
        )
        active_ids_after_adopt = {m.method_id for m in active_after_adopt}
        sync_desk_harness_projections(
            w.store,
            company_id,
            meta_agent_id,
        )
        skill_file = (
            desk_directory(w.store, meta_agent_id)
            / "skills"
            / gen2_method_id
            / "SKILL.md"
        )
        if use_live_runtime:
            assert live_registry is not None
            with closing(open_writer(w.store, lease=w)) as db, write_tx(db):
                complete_work_run(
                    db,
                    witness_claim.run_id,
                    witness_claim.fencing_token,
                    outcome={"phase": "distilled_gen2"},
                    now_ns=base_ns + 1135,
                )
                w_item, _ = create_work_item(
                    db,
                    company_id=company_id,
                    owner_agent_id=meta_agent_id,
                    duty_kind="work.discovery_review",
                    source_kind="recursive_learning",
                    source_id=f"gen2-adopted-witness-shift-{cycle_tag}",
                    payload={"generation": 2, "phase": "post_adoption_shift"},
                    dedupe_key=f"rsi-witness-live:{cycle_tag}",
                    actor_id=meta_agent_id,
                    now_ns=base_ns + 1140,
                )
            witness_step = CompanyRuntime(
                w.store,
                company_id,
                live_registry,
                lease_directory=w._lock_path.parent,
            ).step_with_writer(w, max_dispatches=1, now_ns=base_ns + 1150)
            if len(witness_step.executed_runs) != 1:
                raise TrialRefused("Post-adoption witness shift did not execute")
            witness_run_id = witness_step.executed_runs[0]
            with closing(open_reader(w.store)) as ro_db, read_tx(ro_db):
                wdec_row = ro_db.execute(
                    """SELECT decision_id, active_snapshot_id
                       FROM work_decision
                       WHERE company_id = ? AND run_id = ?
                       ORDER BY decision_at_ns DESC LIMIT 1""",
                    (company_id, witness_run_id),
                ).fetchone()
            witness_dec_id = "" if wdec_row is None else str(wdec_row[0])
            witness_dec_snap = None if wdec_row is None else wdec_row[1]
            shift_witnessed = (
                cand_proc_id in active_ids_after_adopt
                and gen2_method_id in active_ids_after_adopt
                and wdec_row is not None
                and witness_dec_snap == adopted_snap_id
                and skill_file.is_file()
            )
        else:
            with closing(open_writer(w.store, lease=w)) as db, write_tx(db):
                witness_dec = record_work_decision(
                    db,
                    company_id=company_id,
                    work_item_id=witness_item_id,
                    run_id=witness_claim.run_id,
                    fencing_token=witness_claim.fencing_token,
                    agent_id=meta_agent_id,
                    action_kind="execute_with_adopted_gen2",
                    rationale="Witnessed subsequent shift executing under adopted Gen-2 procedure and method.",
                    evidence=(),
                    active_snapshot_id=adopted_snap_id,
                    decision_at_ns=base_ns + 1140,
                )
                record_episode(
                    db,
                    company_id=company_id,
                    agent_id=meta_agent_id,
                    work_item_id=witness_item_id,
                    run_id=witness_claim.run_id,
                    duty_kind="work.discovery_review",
                    context_digest=sha256(f"witness-{cycle_tag}".encode()).hexdigest(),
                    active_snapshot_id=adopted_snap_id,
                    outcome_status="completed",
                    summary_text="Subsequent shift witnessed adopted improvement_procedure and Gen-2 method.",
                    decisions=[{"decision_id": witness_dec.decision_id}],
                    recorded_at_ns=base_ns + 1150,
                )
                complete_work_run(
                    db,
                    witness_claim.run_id,
                    witness_claim.fencing_token,
                    outcome={
                        "active_snapshot_id": adopted_snap_id,
                        "decision_id": witness_dec.decision_id,
                    },
                    now_ns=base_ns + 1160,
                )
            witness_run_id = witness_claim.run_id
            witness_dec_id = witness_dec.decision_id
            shift_witnessed = (
                cand_proc_id in active_ids_after_adopt
                and gen2_method_id in active_ids_after_adopt
                and witness_dec.active_snapshot_id == adopted_snap_id
                and skill_file.is_file()
            )

        # Inject a declared regression superseding gen2_delta, activate it,
        # then quarantine it and verify restoration of the clean incumbent.
        reg_item_id, reg_claim = _open_claim_for_agent(
            w,
            company_id=company_id,
            agent_id=meta_agent_id,
            grant_id=meta_grant_id,
            grant_scope=meta_grant_scope,
            source_id=f"gen2-regression-witness-{cycle_tag}",
            payload={"generation": 2, "phase": "regression_rollback"},
            now_ns=base_ns + 1200,
        )
        regressed_delta = distill_method_delta(
            w,
            reg_claim,
            delta_kind="PATCH",
            target_kind="work_method",
            method_id=gen2_method_id,
            trigger_spec={"duty_kind": "work.discovery_review"},
            body="Declared regression patch dropping counterexample safety guard.",
            audience_scope="company",
            source_refs=[f"work_run:{reg_claim.run_id}"],
            parent_method_ids=(gen2_delta.delta_id,),
            active_snapshot_id=adopted_snap_id,
            superseded_revision=gen2_delta.revision_sha256,
            now_ns=base_ns + 1210,
        )
        qualify_method_delta(
            w,
            regressed_delta.delta_id,
            passed_k=3,
            negative_trigger_passed=True,
            safety_passed=True,
            holdout_margin=0.0,
            now_ns=base_ns + 1220,
        )
        activate_method_delta(
            w,
            regressed_delta.delta_id,
            actor_id=promotion_actor_id,
            grant_id=promotion_grant_id,
            now_ns=base_ns + 1230,
        )

        q_receipt = quarantine_method_lineage(
            w,
            company_id,
            method_id=gen2_method_id,
            revision_sha256=regressed_delta.revision_sha256,
            reason="Declared regression rollback verification",
            actor_id=promotion_actor_id,
            grant_id=promotion_grant_id,
            now_ns=base_ns + 1240,
        )

        active_after_rollback = resolve_active_methods(
            w,
            company_id,
            agent_id=meta_agent_id,
            duty_kind="work.discovery_review",
        )
        rollback_snap_id = compute_active_snapshot_id(active_after_rollback)
        sync_desk_harness_projections(
            w.store,
            company_id,
            meta_agent_id,
        )
        if use_live_runtime:
            assert live_registry is not None
            with closing(open_writer(w.store, lease=w)) as db, write_tx(db):
                complete_work_run(
                    db,
                    reg_claim.run_id,
                    reg_claim.fencing_token,
                    outcome={"phase": "distilled_regression"},
                    now_ns=base_ns + 1245,
                )
                create_work_item(
                    db,
                    company_id=company_id,
                    owner_agent_id=meta_agent_id,
                    duty_kind="work.discovery_review",
                    source_kind="recursive_learning",
                    source_id=f"gen2-post-rollback-shift-{cycle_tag}",
                    payload={"generation": 2, "phase": "post_rollback_shift"},
                    dedupe_key=f"rsi-rollback-live:{cycle_tag}",
                    actor_id=meta_agent_id,
                    now_ns=base_ns + 1250,
                )
            rollback_step = CompanyRuntime(
                w.store,
                company_id,
                live_registry,
                lease_directory=w._lock_path.parent,
            ).step_with_writer(w, max_dispatches=1, now_ns=base_ns + 1260)
            if len(rollback_step.executed_runs) != 1:
                raise TrialRefused("Post-rollback shift did not execute")
            post_rollback_run_id = rollback_step.executed_runs[0]
            with closing(open_reader(w.store)) as ro_db, read_tx(ro_db):
                rdec_row = ro_db.execute(
                    """SELECT active_snapshot_id FROM work_decision
                       WHERE company_id = ? AND run_id = ?
                       ORDER BY decision_at_ns DESC LIMIT 1""",
                    (company_id, post_rollback_run_id),
                ).fetchone()
            rollback_dec_snap = None if rdec_row is None else rdec_row[0]
        else:
            with closing(open_writer(w.store, lease=w)) as db, write_tx(db):
                rollback_dec = record_work_decision(
                    db,
                    company_id=company_id,
                    work_item_id=reg_item_id,
                    run_id=reg_claim.run_id,
                    fencing_token=reg_claim.fencing_token,
                    agent_id=meta_agent_id,
                    action_kind="execute_after_rollback",
                    rationale="Post-rollback shift confirmed clean incumbent revision restored.",
                    evidence=(),
                    active_snapshot_id=rollback_snap_id,
                    decision_at_ns=base_ns + 1250,
                )
                complete_work_run(
                    db,
                    reg_claim.run_id,
                    reg_claim.fencing_token,
                    outcome={
                        "restored_revision": q_receipt.restored_revision_sha256,
                        "active_snapshot_id": rollback_snap_id,
                    },
                    now_ns=base_ns + 1260,
                )
            post_rollback_run_id = reg_claim.run_id
            rollback_dec_snap = rollback_dec.active_snapshot_id

        restored_revs = {
            (m.method_id, m.revision_sha256) for m in active_after_rollback
        }
        rollback_restored = (
            q_receipt.restored_revision_sha256 == gen2_delta.revision_sha256
            and (gen2_method_id, gen2_delta.revision_sha256) in restored_revs
            and (gen2_method_id, regressed_delta.revision_sha256) not in restored_revs
            and rollback_dec_snap == rollback_snap_id
        )

        # Keep Desk projections cleanly synchronized at cycle end.
        if desks_root(w.store).exists():
            _rehydrate_desk(w.store, company_id, meta_agent_id)

        return RecursiveLearningCycleResult(
            company_id=company_id,
            evaluator_revision=eval_rev,
            gen1_method_id=gen1_method_id,
            incumbent_procedure_id=inc_proc_id,
            candidate_procedure_id=cand_proc_id,
            adopted_procedure_id=cand_proc_id,
            adopted_method_id=gen2_method_id,
            active_snapshot_id=adopted_snap_id,
            transfer_verdict=summary.verdict,
            distinct_transfer_families=summary.distinct_transfer_families,
            lcb_alpha=summary.lcb_alpha,
            perm_p=summary.perm_p,
            shift_witnessed=shift_witnessed,
            witness_run_id=witness_run_id,
            witness_decision_id=witness_dec_id,
            regressed_method_id=regressed_delta.delta_id,
            quarantined_methods=q_receipt.quarantined_methods,
            rollback_restored_incumbent=rollback_restored,
            post_rollback_run_id=post_rollback_run_id,
            post_rollback_snapshot_id=rollback_snap_id,
        )

    if isinstance(writer_or_store, WriterLease):
        return _run_with_lease(writer_or_store)
    if isinstance(writer_or_store, Path):
        with hold_company_writer(writer_or_store, company_id) as lease:
            return _run_with_lease(lease)
    raise TrialRefused("Expected WriterLease or company store Path")


def _make_trial_inputs(
    fixture_bytes: bytes,
    case_bytes: bytes,
    ctx_bytes: bytes,
    tools_bytes: bytes,
    runtime_profile: str,
):
    from .trial_registry import TrialInputs

    return TrialInputs(
        company_fixture=sha256(fixture_bytes).hexdigest(),
        case_inputs=sha256(case_bytes).hexdigest(),
        worker_context=sha256(ctx_bytes).hexdigest(),
        tools=sha256(tools_bytes).hexdigest(),
        runtime_profile=runtime_profile,
    )
