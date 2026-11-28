"""Isolated single-slot trial execution over cloned company stores or fixture inputs."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Any

from ..agent_desk import clone_desk_snapshot
from ..company import FoundingTerms, _identity, found
from ..runtime.engine import CompanyRuntime
from ..runtime.registry import DutyRegistry
from ..runtime.work_kernel import create_work_item
from ..storage.db import open_reader, open_writer, read_tx, write_tx
from ..trial_execution import (
    CompanyRuntimeOfflineAdapter,
    TrialWorkerInput,
    dispatch_registered_trial,
)
from ..trial_registry import (
    TrialInputs,
    TrialRefused,
    TrialTerminalEvidence,
    read_sealed_slot,
    reserve_trial_launch,
)
from ..writer_lease import hold_company_writer
from .sandbox_workspace import (
    _MAX_I64,
    _SAFE_RUN_ID,
    _allocate_run_workspace,
    _backup_sqlite_private,
    _build_default_isolated_case_runner,
    _canonical_digest,
    _compute_source_store_fixture,
    _count_receipt_violations,
    _current_time_ns,
    _parse_sealed_tool_manifest,
    _verify_isolated_root,
    _write_trial_execution_manifest,
)


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

    if source_store is None and (
        company_fixture is not None or worker_input is not None
    ):
        if company_fixture is None or worker_input is None:
            raise TrialRefused(
                "company_fixture and worker_input must be supplied together"
            )
        if execute_case is None:
            raise TrialRefused(
                "Fixture-backed isolated trial requires an execute_case callback"
            )

        adapter = CompanyRuntimeOfflineAdapter(
            trial_root=trial_root,
            evaluator_store=evaluator_store,
            profile=slot.inputs.runtime_profile,
            execute_case=execute_case,
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

    actual_fixture_bytes: bytes | None = None
    expected_desk_digests: dict[str, str] | None = None
    sealed_allowed_tools: list[str] | None = None
    if source_store is not None:
        if not company_id:
            raise TrialRefused("source_store requires company_id")
        if worker_input is None:
            raise TrialRefused(
                "source_store trial execution requires worker_input to verify sealed slot inputs"
            )
        if execute_case is None and duty_registry is None:
            raise TrialRefused(
                "source_store isolated trial requires an execute_case callback or production duty_registry"
            )
        if execute_case is None:
            sealed_allowed_tools = _parse_sealed_tool_manifest(
                worker_input.tool_manifest
            )
        actual_fixture_bytes, store_profiles, founder_id = (
            _compute_source_store_fixture(source_store, company_id, agent_ids=agent_ids)
        )
        if company_fixture is not None and company_fixture != actual_fixture_bytes:
            raise TrialRefused(
                "Supplied company_fixture does not match source_store state"
            )
        parsed_fixture = json.loads(actual_fixture_bytes.decode("utf-8"))
        if isinstance(parsed_fixture.get("desk_digests"), dict):
            expected_desk_digests = {
                str(k): str(v) for k, v in parsed_fixture["desk_digests"].items()
            }
        verify_actor_id = agent_ids[0] if agent_ids else founder_id
        if verify_actor_id not in store_profiles:
            raise TrialRefused("Target trial Agent is missing from source_store")
        actual_profile = store_profiles[verify_actor_id]
        actual_inputs = TrialInputs(
            company_fixture=sha256(actual_fixture_bytes).hexdigest(),
            case_inputs=sha256(worker_input.case_inputs).hexdigest(),
            worker_context=sha256(worker_input.worker_context).hexdigest(),
            tools=sha256(worker_input.tool_manifest).hexdigest(),
            runtime_profile=actual_profile,
        )
        reservation = reserve_trial_launch(
            evaluator_store,
            revision=revision,
            slot_id=slot_id,
            run_id=run_id,
            inputs=actual_inputs,
            limits=worker_input.limits,
        )
    else:
        raise TrialRefused(
            "Isolated trial execution requires verified inputs (source_store + worker_input "
            "or company_fixture + worker_input) and a real executor (execute_case or duty_registry)"
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
        clone_agents = tuple(agent_ids) if agent_ids else (actor_id,)
        clone_desk_snapshot(
            source_store,
            company_id,
            clone_agents,
            run_store,
            expected_revisions=expected_desk_digests,
        )
        cloned_fixture_bytes, _, _ = _compute_source_store_fixture(
            run_store, company_id, agent_ids=clone_agents
        )
        if (
            actual_fixture_bytes is not None
            and cloned_fixture_bytes != actual_fixture_bytes
        ):
            raise TrialRefused(
                "Cloned trial store or Desk snapshot diverged from sealed source fixture"
            )
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

    with closing(open_reader(run_store)) as pre_db, read_tx(pre_db):
        preexisting_item_ids = {
            str(r[0])
            for r in pre_db.execute(
                "SELECT work_item_id FROM work_items WHERE company_id = ?",
                (target_cid,),
            ).fetchall()
        }
        preexisting_run_ids = {
            str(r[0])
            for r in pre_db.execute(
                "SELECT run_id FROM work_runs WHERE company_id = ?",
                (target_cid,),
            ).fetchall()
        }

    def _finalize_execution_manifest(
        explicit_items: Sequence[str] = (),
        explicit_runs: Sequence[str] = (),
    ) -> None:
        with closing(open_reader(run_store)) as post_db, read_tx(post_db):
            post_items = {
                str(r[0])
                for r in post_db.execute(
                    "SELECT work_item_id FROM work_items WHERE company_id = ?",
                    (target_cid,),
                ).fetchall()
            }
            post_runs = {
                str(r[0])
                for r in post_db.execute(
                    "SELECT run_id FROM work_runs WHERE company_id = ?",
                    (target_cid,),
                ).fetchall()
            }
        new_items = (post_items - preexisting_item_ids) | set(explicit_items)
        new_runs = (post_runs - preexisting_run_ids) | set(explicit_runs)
        _write_trial_execution_manifest(
            run_dir,
            company_id=target_cid,
            run_id=run_id,
            slot_id=slot.slot_id,
            work_item_ids=sorted(new_items),
            executed_runs=sorted(new_runs),
        )

    started = monotonic()
    if execute_case is not None:
        try:
            result = execute_case(runtime, desk_dir, slot)
        except TimeoutError:
            _finalize_execution_manifest()
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
        _finalize_execution_manifest()
    else:

        def _decode_trial_blob(blob: bytes) -> Any:
            try:
                text = blob.decode("utf-8")
            except UnicodeDecodeError:
                return {
                    "bytes_sha256": sha256(blob).hexdigest(),
                    "bytes_len": len(blob),
                }
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text

        trial_payload: dict[str, Any] = {
            "slot_id": slot.slot_id,
            "case_id": slot.case_id,
            "family_id": slot.family_id,
        }
        if worker_input is not None:
            trial_payload["case_inputs"] = _decode_trial_blob(worker_input.case_inputs)
            trial_payload["worker_context"] = _decode_trial_blob(
                worker_input.worker_context
            )
            trial_payload["tool_manifest"] = _decode_trial_blob(
                worker_input.tool_manifest
            )
            if sealed_allowed_tools is not None:
                trial_payload["sealed_tool_manifest"] = list(sealed_allowed_tools)
        with hold_company_writer(
            run_store, target_cid, lease_directory=lease_dir
        ) as trial_w:
            with (
                closing(open_writer(run_store, lease=trial_w)) as db,
                write_tx(db),
            ):
                trial_item, _ = create_work_item(
                    db,
                    company_id=target_cid,
                    owner_agent_id=actor_id,
                    duty_kind="work.discovery_review",
                    source_kind="offline_trial",
                    source_id=slot.slot_id,
                    payload=trial_payload,
                    dedupe_key=f"offline-trial:{run_id}:{slot.slot_id}",
                    actor_id=actor_id,
                    now_ns=max(1, _current_time_ns()),
                )
        step_summary = runtime.step(
            max_dispatches=min(slot.limits.turns, 1),
            target_work_item_id=trial_item.work_item_id,
        )
        exec_runs = tuple(step_summary.executed_runs)
        _finalize_execution_manifest(
            explicit_items=(trial_item.work_item_id,),
            explicit_runs=exec_runs,
        )
        with closing(open_reader(run_store)) as ro_db, read_tx(ro_db):
            settled_count = int(
                ro_db.execute(
                    """SELECT COUNT(*) FROM work_items
                       WHERE company_id = ? AND work_item_id = ? AND state = 'settled'""",
                    (target_cid, trial_item.work_item_id),
                ).fetchone()[0]
            )
            if exec_runs:
                run_placeholders = ",".join("?" for _ in exec_runs)
                dec_count = int(
                    ro_db.execute(
                        f"""SELECT COUNT(*) FROM work_decision
                            WHERE company_id = ? AND work_item_id = ?
                              AND run_id IN ({run_placeholders})""",
                        (target_cid, trial_item.work_item_id, *exec_runs),
                    ).fetchone()[0]
                )
                fatal_calls, authority_refusals, trace_call_names = (
                    _count_receipt_violations(
                        ro_db, company_id=target_cid, exec_runs=exec_runs
                    )
                )
                ep_usage_row = ro_db.execute(
                    f"""SELECT COALESCE(SUM(input_tokens), 0),
                               COALESCE(SUM(output_tokens), 0),
                               COALESCE(SUM(cost_minor_units), 0)
                        FROM agent_episode
                        WHERE company_id = ? AND run_id IN ({run_placeholders})""",
                    (target_cid, *exec_runs),
                ).fetchone()
                actual_input_tokens = int(ep_usage_row[0]) if ep_usage_row else 0
                actual_output_tokens = int(ep_usage_row[1]) if ep_usage_row else 0
                actual_tokens = actual_input_tokens + actual_output_tokens
                actual_cost_minor = int(ep_usage_row[2]) if ep_usage_row else 0
            else:
                dec_count = 0
                fatal_calls = 0
                authority_refusals = 0
                trace_call_names = []
                actual_input_tokens = 0
                actual_output_tokens = 0
                actual_tokens = 0
                actual_cost_minor = 0
        total_violations = fatal_calls + authority_refusals
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
                "actual_tokens": actual_tokens,
            },
            "decision": {
                "decision_id": captured.get("decision_id"),
                "action_kind": captured.get("action_kind", "trial_decision"),
                "grounded": dec_count >= 1,
                "relevant_constraints": settled_count >= 1,
                "feasible_next_action": settled_count >= 1,
            },
            "tool_trace": {
                "calls": trace_call_names,
                "violations": total_violations,
                "fatal_calls": fatal_calls,
                "authority_refusals": authority_refusals,
            },
            "service_effects": {
                "service_correct": settled_count >= 1 and fatal_calls == 0,
                "invariant_breach": False,
            },
            "usage": {
                "turns": len(step_summary.executed_runs),
                "tokens": actual_tokens,
                "input_tokens": actual_input_tokens,
                "output_tokens": actual_output_tokens,
                "cost_minor_units": actual_cost_minor,
            },
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
