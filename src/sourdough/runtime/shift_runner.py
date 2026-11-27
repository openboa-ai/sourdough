"""Unified ShiftRunner orchestrating Core & DomainPlugin agent shifts with hardened mediation."""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..agent_desk import (
    PRODUCTION_DESK_LIMITS,
    allocate_run_scratch,
    cleanup_run_scratch,
    desk_directory,
    ensure_private_desk,
    logs_directory,
    resolve_desk,
)
from ..agent_runtime._http import compute_stable_prefix_sha256
from ..agent_runtime.cost_accounting import (
    DEFAULT_MODEL_PRICING,
    TurnCostSettlement,
    compute_usage_cost_minor_units,
    settle_shift_cost_to_ledger,
)
from ..agent_runtime.protocol import (
    PROVIDER_CODEX_CLI,
    AgentBudgetExceeded,
    AgentProfileBinding,
    OuterToolBroker,
    ShiftRequest,
    ShiftResult,
    ToolHandler,
    ToolSpec,
    assign_skill_slugs,
    hydrate_shift_context,
)
from ..duty_run import DutyRefused, work_run_claim_from_context
from ..episode_index import upsert_episode_entry
from ..harness import (
    DEFAULT_OFFLOAD_THRESHOLD_BYTES,
    create_agent_session,
    offload_large_output,
)
from ..memory import (
    AgentEpisode,
    list_recent_episodes,
    record_episode,
    update_episode_outcome,
)
from ..method_learning import (
    build_causal_method_trace,
    compute_active_snapshot_id,
    default_active_methods_provider,
    resolve_active_methods,
    sync_desk_harness_projections,
)
from ..storage.db import open_reader, open_writer, read_tx, write_tx
from ..storage.kernel import append_audit_event
from ..storage.schema import installed_domains
from .desk_tools import build_core_desk_tool_contribution
from .domains import (
    ProfileBindingLookup,
    ToolContribution,
    ToolQueryContext,
    merge_tool_contributions,
)
from .method_tools import build_core_skill_tool_contribution
from .registry import DutyContext, RuntimeRefused
from .work_kernel import RunFenced, assert_run_fencing

if TYPE_CHECKING:
    from ..agent_runtime.registry import AgentAdapterRegistry

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_PRIVATE_DESK_RECEIPT_TOOLS = frozenset(
    {
        "desk_list_drafts",
        "desk_read_draft",
        "desk_save_draft",
        "desk_delete_draft",
        "desk_read_offloaded_log",
        "desk_run_command",
    }
)
_SAFE_DESK_RECEIPT_FIELDS = (
    "ok",
    "revision",
    "count",
    "deleted",
    "returncode",
    "timed_out",
    "quota_exceeded",
)


def _sanitize_receipt_result_json(
    tool_name: str, result_digest: str, raw_result_json: str
) -> str:
    """Keep private Desk filenames, draft bodies, and command outputs out of company.sqlite."""
    if tool_name not in _PRIVATE_DESK_RECEIPT_TOOLS:
        return raw_result_json
    safe_payload: dict[str, Any] = {
        "redacted": "private_desk_payload",
        "result_digest": result_digest,
    }
    try:
        parsed = json.loads(raw_result_json)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, Mapping):
        for key in _SAFE_DESK_RECEIPT_FIELDS:
            if key in parsed:
                safe_payload[key] = parsed[key]
    return json.dumps(safe_payload, sort_keys=True, separators=(",", ":"))


_LOGGER = logging.getLogger(__name__)

ActiveMethodsProvider = Callable[
    [Path, str, str, str],
    tuple[Sequence[Any], str | None],
]


def _normalize_method_dicts(items: Sequence[Any]) -> tuple[dict[str, Any], ...]:
    normalized: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            normalized.append(dict(item))
        elif hasattr(item, "to_dict") and callable(item.to_dict):
            normalized.append(dict(item.to_dict()))
        elif is_dataclass(item) and not isinstance(item, type):
            normalized.append(asdict(item))
        elif isinstance(item, Mapping):
            normalized.append(dict(item))
        else:
            normalized.append({"method_id": str(item)})
    return tuple(normalized)


@dataclass(frozen=True)
class ShiftRunResult:
    shift_result: ShiftResult
    settlement: TurnCostSettlement
    episode: AgentEpisode | None
    desk_path: Path
    run_status: str = "completed"
    wait_until_ns: int | None = None
    wait_event_key: str | None = None
    active_snapshot_id: str | None = None
    causal_method_trace: dict[str, Any] | None = None
    stable_prefix_sha256: str = ""


def _ensure_private_dir(path: Path, stop_at: Path) -> None:
    rel = path.relative_to(stop_at)
    current = stop_at
    for part in rel.parts:
        current = current / part
        if os.path.lexists(current):
            st = os.lstat(current)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise RuntimeRefused("Private runtime directory must not be a symlink")
        else:
            os.mkdir(current, 0o700)
        os.chmod(current, 0o700)


def _append_trace_line(store: Path, trace_path: Path, payload: dict[str, Any]) -> None:
    root = store.resolve(strict=False).parent
    _ensure_private_dir(trace_path.parent, root)
    if os.path.lexists(trace_path):
        st = os.lstat(trace_path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise RuntimeRefused("Trace file must be a regular file")
    line = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    fd = os.open(
        trace_path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | _O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    os.chmod(trace_path, 0o600)


def reconcile_shift_episode_outcome(
    ctx: DutyContext,
    *,
    outcome_status: str,
    reason: str | None = None,
    causal_method_trace: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Reconcile recorded episode + episode index + causal trace after duty validation."""
    reconciled: dict[str, Any] | None = None
    if causal_method_trace is not None:
        reconciled = dict(causal_method_trace)
        od = reconciled.get("outcome_delta")
        patched_od = dict(od) if isinstance(od, dict) else {}
        patched_od["status"] = outcome_status
        if reason is not None:
            patched_od["reason"] = reason
        reconciled["outcome_delta"] = patched_od
    with (
        ctx.write_lock_ctx(),
        closing(open_writer(ctx.writer.store, lease=ctx.writer)) as db,
        write_tx(db),
    ):
        updated_ep = update_episode_outcome(
            db,
            ctx.company_id,
            ctx.run.run_id,
            outcome_status=outcome_status,
            reason=reason,
        )
        if reconciled is not None and (
            reconciled.get("available")
            or reconciled.get("retrieved")
            or reconciled.get("invoked")
        ):
            append_audit_event(
                db,
                company_id=ctx.company_id,
                domain="learning",
                entity_type="agent_episode",
                entity_id=ctx.run.run_id,
                event_type="learning.causal_method_trace_reconciled",
                actor_id=ctx.work_item.owner_agent_id,
                run_id=ctx.run.run_id,
                payload=reconciled,
                recorded_at_ns=max(1, int(ctx.now_ns)),
            )
    if updated_ep is not None:
        upsert_episode_entry(ctx.writer.store, updated_ep)
    return reconciled


class ShiftRunner:
    """Shared shift orchestrator for Core and DomainPlugin duties."""

    def __init__(
        self,
        *,
        adapter_registry: AgentAdapterRegistry,
        profile_bindings: ProfileBindingLookup,
        default_budget_ceiling_minor: int,
        read_agent_state: Callable[[Path, str, str], tuple[str, str]],
        resolve_profile: Callable[[ProfileBindingLookup, str], AgentProfileBinding],
        contribute_tools: Callable[[ToolQueryContext], ToolContribution],
        active_methods_provider: ActiveMethodsProvider | None = None,
    ) -> None:
        self._adapter_registry = adapter_registry
        self._profile_bindings = profile_bindings
        self._default_budget_ceiling_minor = int(default_budget_ceiling_minor)
        self._read_agent_state = read_agent_state
        self._resolve_profile = resolve_profile
        self._contribute_tools = contribute_tools
        self._active_methods_provider = (
            active_methods_provider
            if active_methods_provider is not None
            else default_active_methods_provider
        )

    def run(
        self,
        ctx: DutyContext,
        *,
        mandate_summary: str,
        instruction: str,
        turn_id: str = "turn-1",
        purpose: str | None = None,
        base_tools: Sequence[ToolSpec] = (),
        base_handlers: Mapping[str, ToolHandler] = (),
        history_notes: Sequence[str] = (),
        open_receipts: Sequence[dict[str, Any]] = (),
        include_core_desk_tools: bool = True,
        include_core_skill_tools: bool | None = None,
        include_plugin_tools: bool = True,
        extra_recoverable_exceptions: tuple[type[BaseException], ...] = (),
        max_tool_calls: int = 16,
        timeout_seconds: float | None = None,
        pre_resolved_active_methods: tuple[Sequence[Any], str | None] | None = None,
    ) -> ShiftRunResult:
        agent_id = ctx.work_item.owner_agent_id
        persisted_profile, grant_scope = self._read_agent_state(
            ctx.writer.store, ctx.company_id, agent_id
        )
        profile = self._resolve_profile(self._profile_bindings, agent_id)
        profile.verify_matches_agent(persisted_profile)

        eff_purpose = purpose or ctx.work_item.duty_kind
        with (
            ctx.write_lock_ctx(),
            closing(open_writer(ctx.writer.store, lease=ctx.writer)) as db,
            write_tx(db),
        ):
            ensure_private_desk(db, ctx.company_id, agent_id, persisted_profile)
            claim = work_run_claim_from_context(db, ctx, purpose=eff_purpose)
            active_domains = frozenset(installed_domains(db))

        store_root = ctx.writer.store.resolve(strict=False).parent
        desk = desk_directory(ctx.writer.store, agent_id)
        _ensure_private_dir(desk / ".desk" / "drafts", store_root)
        _ensure_private_dir(logs_directory(ctx.writer.store, agent_id), store_root)
        allocate_run_scratch(
            ctx.writer.store,
            agent_id,
            ctx.run.run_id,
            limits=PRODUCTION_DESK_LIMITS,
        )

        _, drafts = resolve_desk(
            ctx.writer,
            claim,
            claim.purpose,
            include_drafts=True,
            limits=PRODUCTION_DESK_LIMITS,
        )
        desk_manifest = tuple(
            {
                "name": d.name,
                "revision": d.revision,
                "bytes": len(d.content.encode("utf-8")),
            }
            for d in drafts
        )

        with closing(open_reader(ctx.writer.store)) as db, read_tx(db):
            recent_eps = list_recent_episodes(db, ctx.company_id, agent_id, limit=5)
            prior_receipt_rows = db.execute(
                """SELECT t.run_id, t.call_id, t.tool_name, t.arg_digest,
                          t.status, t.result_digest, t.result_json,
                          t.offloaded_log_path, t.recorded_at_ns
                   FROM tool_call_receipt t
                   JOIN work_runs wr
                     ON wr.company_id = t.company_id AND wr.run_id = t.run_id
                   WHERE t.company_id = ?
                     AND wr.work_item_id = ?
                     AND t.run_id != ?
                   ORDER BY t.recorded_at_ns DESC, t.call_id DESC
                   LIMIT 32""",
                (ctx.company_id, ctx.work_item.work_item_id, ctx.run.run_id),
            ).fetchall()
        prior_tool_receipts: list[dict[str, Any]] = []
        for (
            r_run_id,
            r_call_id,
            r_tool_name,
            r_arg_digest,
            r_status,
            r_result_digest,
            r_result_json,
            r_offloaded_log_path,
            r_recorded_at_ns,
        ) in reversed(prior_receipt_rows):
            receipt_entry: dict[str, Any] = {
                "kind": "tool_call_receipt",
                "run_id": str(r_run_id),
                "call_id": str(r_call_id),
                "tool_name": str(r_tool_name),
                "arg_digest": str(r_arg_digest),
                "status": str(r_status),
                "result_digest": str(r_result_digest),
                "recorded_at_ns": int(r_recorded_at_ns),
            }
            if r_offloaded_log_path is not None:
                receipt_entry["offloaded_log_path"] = str(r_offloaded_log_path)
            if isinstance(r_result_json, str) and len(r_result_json) <= 1024:
                try:
                    receipt_entry["result"] = json.loads(r_result_json)
                except json.JSONDecodeError:
                    receipt_entry["result_preview"] = r_result_json[:256]
            prior_tool_receipts.append(receipt_entry)
        recent_episodes = tuple(
            {
                "episode_id": ep.episode_id,
                "duty_kind": ep.duty_kind,
                "outcome_status": ep.outcome_status,
                "summary_text": ep.summary_text[:300],
                "recorded_at_ns": ep.recorded_at_ns,
            }
            for ep in recent_eps
        )

        all_agent_methods = resolve_active_methods(
            ctx.writer.store,
            ctx.company_id,
            agent_id=agent_id,
            duty_kind=None,
        )
        if pre_resolved_active_methods is not None:
            raw_active_methods, active_snapshot_id = pre_resolved_active_methods
        else:
            raw_active_methods, active_snapshot_id = self._active_methods_provider(
                ctx.writer.store,
                ctx.company_id,
                agent_id,
                ctx.work_item.duty_kind,
            )
        active_methods = _normalize_method_dicts(raw_active_methods)
        slug_map = assign_skill_slugs(
            [
                *(m.method_id for m in all_agent_methods),
                *(
                    str(m["method_id"])
                    for m in active_methods
                    if isinstance(m.get("method_id"), str)
                ),
            ]
        )
        for m in active_methods:
            mid = m.get("method_id")
            if isinstance(mid, str) and mid in slug_map:
                if not m.get("skill_name"):
                    m["skill_name"] = slug_map[mid]
                if not m.get("skill_path"):
                    m["skill_path"] = f"skills/{slug_map[mid]}/SKILL.md"
        if active_snapshot_id is None and active_methods:
            active_snapshot_id = compute_active_snapshot_id(active_methods)

        sync_desk_harness_projections(
            ctx.writer.store,
            ctx.company_id,
            agent_id,
            active_methods=all_agent_methods,
            active_snapshot_id=compute_active_snapshot_id(all_agent_methods),
        )

        available_method_ids: list[str] = [m.method_id for m in all_agent_methods]
        for m in active_methods:
            mid = m.get("method_id")
            if isinstance(mid, str) and mid not in available_method_ids:
                available_method_ids.append(mid)
        retrieved_method_ids: list[str] = [
            str(m["method_id"])
            for m in active_methods
            if isinstance(m.get("method_id"), str)
        ]
        invoked_method_ids: list[str] = []

        def _record_retrieved(mid: str) -> None:
            if mid not in retrieved_method_ids:
                retrieved_method_ids.append(mid)
            if mid not in available_method_ids:
                available_method_ids.append(mid)

        def _record_invoked(mid: str, _note: str | None) -> None:
            _record_retrieved(mid)
            if mid not in invoked_method_ids:
                invoked_method_ids.append(mid)

        context_pack = hydrate_shift_context(
            company_id=ctx.company_id,
            agent_id=agent_id,
            run_id=ctx.run.run_id,
            fencing_token=ctx.run.fencing_token,
            grant_scope=grant_scope,
            mandate_summary=mandate_summary,
            open_receipts=(*prior_tool_receipts, *open_receipts),
            history_notes=tuple(history_notes),
            desk_manifest=desk_manifest,
            active_methods=active_methods,
            active_snapshot_id=active_snapshot_id,
            recent_episodes=recent_episodes,
        )

        eff_skill_tools = (
            include_core_desk_tools
            if include_core_skill_tools is None
            else include_core_skill_tools
        )
        contributions: list[ToolContribution] = []
        if include_core_desk_tools:
            contributions.append(
                build_core_desk_tool_contribution(
                    writer=ctx.writer,
                    claim=claim,
                    purpose=claim.purpose,
                    sandbox_backend=profile.sandbox_backend,
                    tx_lock=ctx.tx_lock,
                    desk_limits=PRODUCTION_DESK_LIMITS,
                )
            )
        if eff_skill_tools:
            contributions.append(
                build_core_skill_tool_contribution(
                    writer=ctx.writer,
                    claim=claim,
                    active_snapshot_id=active_snapshot_id,
                    retrieved_recorder=_record_retrieved,
                    invoked_recorder=_record_invoked,
                    tx_lock=ctx.tx_lock,
                )
            )
        if include_plugin_tools:
            contributions.append(
                self._contribute_tools(
                    ToolQueryContext(
                        store=ctx.writer.store,
                        company_id=ctx.company_id,
                        writer=ctx.writer,
                        agent_id=agent_id,
                        duty_kind=ctx.work_item.duty_kind,
                        grant_scope=grant_scope,
                        installed_domains=active_domains,
                    )
                )
            )

        tool_specs, merged_handlers, contributed_recoverable = merge_tool_contributions(
            base_tools, base_handlers, contributions
        )
        tool_specs = tuple(sorted(tool_specs, key=lambda spec: spec.name))
        merged_recoverable = tuple(
            dict.fromkeys((*contributed_recoverable, *extra_recoverable_exceptions))
        )

        def _wrap_handler(fn: ToolHandler) -> ToolHandler:
            def _wrapped(args: dict[str, Any]) -> dict[str, Any]:
                with ctx.write_lock_ctx():
                    return fn(args)

            return _wrapped

        locked_handlers: dict[str, ToolHandler] = {
            name: (fn if name == "desk_run_command" else _wrap_handler(fn))
            for name, fn in merged_handlers.items()
        }

        session = create_agent_session(ctx.writer, claim, claim.purpose)

        def _fencing_check() -> None:
            with closing(open_reader(ctx.writer.store)) as db, read_tx(db):
                assert_run_fencing(
                    db, ctx.run.run_id, ctx.run.fencing_token, now_ns=ctx.now_ns
                )

        def _record_receipt(receipt: dict[str, Any]) -> None:
            stored_result_json = _sanitize_receipt_result_json(
                str(receipt["tool_name"]),
                str(receipt["result_digest"]),
                str(receipt["result_json"]),
            )
            with (
                ctx.write_lock_ctx(),
                closing(open_writer(ctx.writer.store, lease=ctx.writer)) as db,
                write_tx(db),
            ):
                db.execute(
                    """INSERT INTO tool_call_receipt (
                           company_id, run_id, agent_id, call_id, tool_name, arg_digest,
                           status, result_digest, result_json, error_type, error_message,
                           offloaded_log_path, recorded_at_ns
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(company_id, run_id, call_id) DO NOTHING""",
                    (
                        receipt["company_id"],
                        receipt["run_id"],
                        receipt["agent_id"],
                        receipt["call_id"],
                        receipt["tool_name"],
                        receipt["arg_digest"],
                        receipt["status"],
                        receipt["result_digest"],
                        stored_result_json,
                        receipt["error_type"],
                        receipt["error_message"],
                        receipt["offloaded_log_path"],
                        receipt["recorded_at_ns"],
                    ),
                )

        budget_ceiling = (
            ctx.run.budget_ceiling_minor
            if ctx.run.budget_ceiling_minor > 0
            else self._default_budget_ceiling_minor
        )
        eff_timeout_seconds = (
            (120.0 if profile.provider == PROVIDER_CODEX_CLI else 30.0)
            if timeout_seconds is None
            else timeout_seconds
        )
        shift_req = ShiftRequest(
            company_id=ctx.company_id,
            agent_id=agent_id,
            run_id=ctx.run.run_id,
            turn_id=turn_id,
            fencing_token=ctx.run.fencing_token,
            instruction=instruction,
            context=context_pack,
            tools=tool_specs,
            budget_ceiling_minor_units=budget_ceiling,
            max_tool_calls=max_tool_calls,
            timeout_seconds=eff_timeout_seconds,
            desk_path=desk,
            store_path=ctx.writer.store,
        )
        stable_prefix_sha256 = compute_stable_prefix_sha256(shift_req)

        def _record_trace(trace_payload: dict[str, Any]) -> None:
            enriched = {
                **trace_payload,
                "stable_prefix_sha256": stable_prefix_sha256,
            }
            _append_trace_line(ctx.writer.store, session.trace_path, enriched)

        def _offload(
            seq: int, tool_name: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            return offload_large_output(
                ctx.writer.store,
                session,
                tool_name,
                payload,
                seq=seq,
                max_inline_bytes=DEFAULT_OFFLOAD_THRESHOLD_BYTES,
            )

        broker = OuterToolBroker(
            company_id=ctx.company_id,
            agent_id=agent_id,
            run_id=ctx.run.run_id,
            fencing_token=ctx.run.fencing_token,
            handlers=locked_handlers,
            max_tool_calls=max_tool_calls,
            fencing_check=_fencing_check,
            receipt_recorder=_record_receipt,
            trace_recorder=_record_trace,
            offload_handler=_offload,
            recoverable_exceptions=merged_recoverable,
            clock=lambda: max(1, int(ctx.now_ns)),
        )

        _append_trace_line(
            ctx.writer.store,
            session.trace_path,
            {
                "seq": 0,
                "kind": "shift_prefix",
                "recorded_at_ns": max(1, int(ctx.now_ns)),
                "company_id": ctx.company_id,
                "agent_id": agent_id,
                "run_id": ctx.run.run_id,
                "turn_id": turn_id,
                "fencing_token": ctx.run.fencing_token,
                "active_snapshot_id": active_snapshot_id,
                "stable_prefix_sha256": stable_prefix_sha256,
                "tool_names": [spec.name for spec in shift_req.tools],
            },
        )

        shift_result: ShiftResult | None = None
        shift_error: BaseException | None = None
        projection_error: BaseException | None = None
        settlement: TurnCostSettlement | None = None
        settlement_error: BaseException | None = None
        episode: AgentEpisode | None = None
        causal_trace_dict: dict[str, Any] | None = None
        final_run_status = "completed"
        final_wait_until_ns: int | None = None
        final_wait_event_key: str | None = None

        try:
            shift_result = self._adapter_registry.execute_shift(
                persisted_profile_id=persisted_profile,
                profile=profile,
                request=shift_req,
                broker=broker,
            )
        except BaseException as exc:
            shift_error = exc
            raise
        finally:
            try:
                cleanup_run_scratch(ctx.writer.store, agent_id, ctx.run.run_id)
            except Exception as exc:
                _LOGGER.warning(
                    "Scratch cleanup failed for agent=%s run=%s: %s",
                    agent_id,
                    ctx.run.run_id,
                    exc,
                )
            try:
                sync_desk_harness_projections(
                    ctx.writer.store,
                    ctx.company_id,
                    agent_id,
                )
            except (DutyRefused, OSError) as exc:
                projection_error = exc
                _LOGGER.warning(
                    "Desk harness projection sync failed for agent=%s run=%s: %s",
                    agent_id,
                    ctx.run.run_id,
                    exc,
                )
                try:
                    _append_trace_line(
                        ctx.writer.store,
                        session.trace_path,
                        {
                            "kind": "desk.harness_projection_error",
                            "recorded_at_ns": max(1, int(ctx.now_ns)),
                            "company_id": ctx.company_id,
                            "agent_id": agent_id,
                            "run_id": ctx.run.run_id,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                    )
                except Exception:
                    pass
            except Exception as exc:
                _LOGGER.warning(
                    "Unexpected Desk harness projection error for agent=%s run=%s: %s",
                    agent_id,
                    ctx.run.run_id,
                    exc,
                )

            incurred_usage = (
                shift_result.usage if shift_result is not None else broker.accrued_usage
            )
            try:
                settlement = settle_shift_cost_to_ledger(
                    ctx.writer,
                    profile=profile,
                    run_id=ctx.run.run_id,
                    turn_id=turn_id,
                    usage=incurred_usage,
                    budget_ceiling_minor_units=budget_ceiling,
                    tx_lock=ctx.tx_lock,
                )
            except BaseException as exc:
                settlement_error = exc

            cost_minor = (
                settlement.cost_minor_units
                if settlement is not None
                else compute_usage_cost_minor_units(
                    incurred_usage, DEFAULT_MODEL_PRICING[profile.provider]
                )
            )
            if shift_result is not None and settlement_error is None:
                with closing(open_reader(ctx.writer.store)) as db, read_tx(db):
                    run_state_row = db.execute(
                        """SELECT status, wait_until_ns, wait_event_key
                           FROM work_runs
                           WHERE run_id = ? AND fencing_token = ?""",
                        (ctx.run.run_id, ctx.run.fencing_token),
                    ).fetchone()
                if run_state_row is not None and str(run_state_row[0]) == "waiting":
                    final_run_status = "waiting"
                    final_wait_until_ns = (
                        None if run_state_row[1] is None else int(run_state_row[1])
                    )
                    final_wait_event_key = (
                        None if run_state_row[2] is None else str(run_state_row[2])
                    )
                ep_status = final_run_status
                ep_summary = shift_result.output_text.strip() or (
                    f"Waiting on {final_wait_event_key or ctx.work_item.duty_kind}"
                    if final_run_status == "waiting"
                    else f"Completed {ctx.work_item.duty_kind}"
                )
            elif isinstance(shift_error, AgentBudgetExceeded) or (
                isinstance(settlement_error, AgentBudgetExceeded)
                and not isinstance(shift_error, RunFenced)
            ):
                ep_status = "blocked"
                ep_summary = str(
                    settlement_error
                    if isinstance(settlement_error, AgentBudgetExceeded)
                    else shift_error
                )
            else:
                active_err = shift_error or settlement_error
                ep_status = "failed"
                ep_summary = (
                    f"{type(active_err).__name__}: {active_err}"
                    if active_err is not None
                    else "Shift failed"
                )

            if shift_result is not None and shift_result.output_text:
                out_text = shift_result.output_text
                for mid in retrieved_method_ids:
                    if mid in out_text and mid not in invoked_method_ids:
                        invoked_method_ids.append(mid)

            decision_ids: list[str] = []
            try:
                with closing(open_reader(ctx.writer.store)) as db, read_tx(db):
                    decision_ids = [
                        str(r[0])
                        for r in db.execute(
                            "SELECT decision_id FROM work_decision WHERE company_id = ? AND run_id = ? ORDER BY decision_at_ns, decision_id",
                            (ctx.company_id, ctx.run.run_id),
                        ).fetchall()
                    ]
            except Exception:
                decision_ids = []

            causal_trace = build_causal_method_trace(
                company_id=ctx.company_id,
                agent_id=agent_id,
                run_id=ctx.run.run_id,
                duty_kind=ctx.work_item.duty_kind,
                active_snapshot_id=active_snapshot_id,
                available_methods=available_method_ids,
                retrieved_methods=retrieved_method_ids,
                invoked_methods=invoked_method_ids,
                outcome_delta={
                    "status": ep_status,
                    "tool_calls_executed": (
                        shift_result.tool_calls_executed
                        if shift_result is not None
                        else broker.call_count
                    ),
                    "input_tokens": incurred_usage.input_tokens,
                    "cached_input_tokens": incurred_usage.cached_input_tokens,
                    "output_tokens": incurred_usage.output_tokens,
                    "cost_minor_units": cost_minor,
                    "work_decisions": decision_ids,
                    "stable_prefix_sha256": stable_prefix_sha256,
                },
                stable_prefix_sha256=stable_prefix_sha256,
            )
            causal_trace_dict = causal_trace.to_dict()

            receipt_refs = tuple(
                str(ev.payload["call_id"])
                for ev in broker.events
                if ev.kind == "tool_result" and ev.payload.get("call_id")
            )
            try:
                ep_ts = max(1, int(ctx.now_ns))
                with (
                    ctx.write_lock_ctx(),
                    closing(open_writer(ctx.writer.store, lease=ctx.writer)) as db,
                    write_tx(db),
                ):
                    episode = record_episode(
                        db,
                        company_id=ctx.company_id,
                        agent_id=agent_id,
                        work_item_id=ctx.work_item.work_item_id,
                        run_id=ctx.run.run_id,
                        duty_kind=ctx.work_item.duty_kind,
                        context_digest=context_pack.digest,
                        outcome_status=ep_status,
                        summary_text=ep_summary,
                        active_snapshot_id=active_snapshot_id,
                        decisions=[causal_trace_dict],
                        receipts=receipt_refs,
                        tool_calls_count=broker.call_count,
                        input_tokens=incurred_usage.input_tokens,
                        output_tokens=incurred_usage.output_tokens,
                        cost_minor_units=cost_minor,
                        recorded_at_ns=ep_ts,
                    )
                    if (
                        available_method_ids
                        or retrieved_method_ids
                        or invoked_method_ids
                    ):
                        append_audit_event(
                            db,
                            company_id=ctx.company_id,
                            domain="learning",
                            entity_type="agent_episode",
                            entity_id=ctx.run.run_id,
                            event_type="learning.causal_method_trace",
                            actor_id=agent_id,
                            run_id=ctx.run.run_id,
                            payload=causal_trace_dict,
                            recorded_at_ns=ep_ts,
                        )
                if episode is not None:
                    upsert_episode_entry(ctx.writer.store, episode)
            except Exception:
                pass

            if isinstance(settlement_error, AgentBudgetExceeded) and not isinstance(
                shift_error, RunFenced
            ):
                raise settlement_error from shift_error
            if shift_error is None and settlement_error is not None:
                raise settlement_error
            if shift_error is None and projection_error is not None:
                raise projection_error

        assert shift_result is not None
        assert settlement is not None
        return ShiftRunResult(
            shift_result=shift_result,
            settlement=settlement,
            episode=episode,
            desk_path=desk,
            run_status=final_run_status,
            wait_until_ns=final_wait_until_ns,
            wait_event_key=final_wait_event_key,
            active_snapshot_id=active_snapshot_id,
            causal_method_trace=causal_trace_dict,
            stable_prefix_sha256=stable_prefix_sha256,
        )
