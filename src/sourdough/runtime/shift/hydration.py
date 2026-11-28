"""Shift startup hydration: single-snapshot context, active methods, Desk setup, and tool merging."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from ...agent_desk import (
    PRODUCTION_DESK_LIMITS,
    allocate_run_scratch,
    desk_directory,
    ensure_private_desk,
    logs_directory,
    resolve_desk,
)
from ...agent_runtime.protocol import (
    AgentProfileBinding,
    ContextPack,
    ToolHandler,
    ToolSpec,
    assign_skill_slugs,
    hydrate_shift_context,
)
from ...duty_run import DutyRefused, WorkRunClaim, work_run_claim_from_context
from ...memory import list_recent_episodes
from ...method_learning import (
    compute_active_snapshot_id,
    default_active_methods_provider,
    resolve_active_methods,
    sync_desk_harness_projections,
)
from ...storage.db import scoped_reader, write_uow
from ...storage.schema import installed_domains
from ..desk_tools import build_core_desk_tool_contribution
from ..domains import ToolContribution, ToolQueryContext, merge_tool_contributions
from ..method_tools import build_core_skill_tool_contribution
from ..registry import DutyContext, RuntimeRefused
from .policy import (
    _PEER_INPUT_TOOLS,
    _PRIVATE_DESK_ALL_TOOLS,
    _PRIVATE_DESK_READ_TOOLS,
    is_external_write_sink,
    is_peer_input,
    is_peer_outbound,
    is_private_desk_tool,
    is_private_error_tool,
)
from .receipts import build_public_offload_reader_contribution, ensure_private_dir

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
class HydratedShift:
    agent_id: str
    persisted_profile: str
    grant_scope: str
    profile: AgentProfileBinding
    claim: WorkRunClaim
    desk: Path
    context_pack: ContextPack
    tool_specs: tuple[ToolSpec, ...]
    merged_handlers: dict[str, ToolHandler]
    merged_recoverable: tuple[type[BaseException], ...]
    active_snapshot_id: str | None
    available_method_ids: list[str]
    retrieved_method_ids: list[str]
    invoked_method_ids: list[str]
    public_offloaded_paths: set[str]
    initial_had_peer_input: bool
    prior_had_peer_input: bool
    prior_peer_rows: tuple[tuple[Any, Any, Any], ...]
    has_hydrated_private_context: bool
    effective_include_desk_tools: bool
    has_external_sinks: bool


def hydrate_shift_session(
    ctx: DutyContext,
    *,
    persisted_profile: str,
    grant_scope: str,
    profile: AgentProfileBinding,
    mandate_summary: str,
    purpose: str | None,
    base_tools: Sequence[ToolSpec],
    base_handlers: Mapping[str, ToolHandler],
    history_notes: Sequence[str],
    open_receipts: Sequence[dict[str, Any]],
    include_core_desk_tools: bool,
    include_core_skill_tools: bool | None,
    include_plugin_tools: bool,
    extra_recoverable_exceptions: tuple[type[BaseException], ...],
    pre_resolved_active_methods: tuple[Sequence[Any], str | None] | None,
    allowed_tool_names: frozenset[str] | None,
    contribute_tools: Callable[[ToolQueryContext], ToolContribution],
    active_methods_provider: ActiveMethodsProvider,
) -> HydratedShift:
    """Hydrate Desk, prior receipts, episodes, active methods, ContextPack, and merged tools."""
    agent_id = ctx.work_item.owner_agent_id
    eff_purpose = purpose or ctx.work_item.duty_kind
    with write_uow(ctx.writer, tx_lock=ctx.tx_lock) as db:
        ensure_private_desk(db, ctx.company_id, agent_id, persisted_profile)
        claim = work_run_claim_from_context(db, ctx, purpose=eff_purpose)
        active_domains = frozenset(installed_domains(db))

    store_root = ctx.writer.store.resolve(strict=False).parent
    desk = desk_directory(ctx.writer.store, agent_id)
    ensure_private_dir(desk / ".desk" / "drafts", store_root)
    ensure_private_dir(logs_directory(ctx.writer.store, agent_id), store_root)
    allocate_run_scratch(
        ctx.writer.store,
        agent_id,
        ctx.run.run_id,
        limits=PRODUCTION_DESK_LIMITS,
    )

    base_spec_by_name = {spec.name: spec for spec in base_tools}
    effective_base_names = (
        set(base_handlers)
        if allowed_tool_names is None
        else (set(base_handlers) & allowed_tool_names)
    )
    has_base_external_sinks = any(
        is_peer_input(name, base_spec_by_name.get(name))
        or is_peer_outbound(name, base_spec_by_name.get(name))
        or is_external_write_sink(name, base_spec_by_name.get(name))
        for name in effective_base_names
    )

    # Single read-only SQLite snapshot for all startup shift hydration queries.
    with scoped_reader(ctx.writer.store) as db:
        prior_tool_names = {
            str(r[0])
            for r in db.execute(
                """SELECT DISTINCT t.tool_name
                   FROM tool_call_receipt t
                   JOIN work_runs wr
                     ON wr.company_id = t.company_id AND wr.run_id = t.run_id
                   WHERE t.company_id = ?
                     AND wr.work_item_id = ?
                     AND t.run_id != ?""",
                (ctx.company_id, ctx.work_item.work_item_id, ctx.run.run_id),
            ).fetchall()
        }
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

        prior_had_peer_input = bool(prior_tool_names & _PEER_INPUT_TOOLS)
        prior_peer_rows: tuple[tuple[Any, Any, Any], ...] = ()
        if prior_had_peer_input:
            prior_peer_rows = tuple(
                db.execute(
                    """SELECT t.tool_name, t.result_json, t.offloaded_log_path
                       FROM tool_call_receipt t
                       JOIN work_runs wr
                         ON wr.company_id = t.company_id AND wr.run_id = t.run_id
                       WHERE t.company_id = ?
                         AND wr.work_item_id = ?
                         AND t.run_id != ?
                         AND t.tool_name IN ('read_messages', 'read_inbox', 'discover_handoffs', 'read_shared_asset')""",
                    (ctx.company_id, ctx.work_item.work_item_id, ctx.run.run_id),
                ).fetchall()
            )

        hydrated_peer_input = any(
            isinstance(r, Mapping)
            and (
                (r.get("kind") == "peer_outcome" and r.get("outcome") != "unavailable")
                or r.get("kind") in ("peer_request", "accepted_handoff")
                or (r.get("kind") == "shared_asset" and r.get("author_id") != agent_id)
            )
            for r in open_receipts
        )
        initial_had_peer_input = prior_had_peer_input or hydrated_peer_input
        prior_had_private_desk = bool(prior_tool_names & _PRIVATE_DESK_READ_TOOLS)
        effective_include_desk_tools = (
            include_core_desk_tools and not initial_had_peer_input
        )
        desk_tools_permitted = effective_include_desk_tools and (
            allowed_tool_names is None
            or bool(allowed_tool_names & _PRIVATE_DESK_ALL_TOOLS)
        )
        hydrate_passive_private_context = (
            desk_tools_permitted and not has_base_external_sinks
        )
        recent_eps = (
            list_recent_episodes(db, ctx.company_id, agent_id, limit=5)
            if hydrate_passive_private_context
            else ()
        )
        all_agent_methods = resolve_active_methods(
            db,
            ctx.company_id,
            agent_id=agent_id,
            duty_kind=None,
        )
        raw_active_methods_in_tx: Sequence[Any] | None = None
        active_snapshot_id_in_tx: str | None = None
        if (
            pre_resolved_active_methods is None
            and active_methods_provider is default_active_methods_provider
        ):
            duty_methods = resolve_active_methods(
                db,
                ctx.company_id,
                agent_id=agent_id,
                duty_kind=ctx.work_item.duty_kind,
            )
            raw_active_methods_in_tx = duty_methods
            active_snapshot_id_in_tx = (
                compute_active_snapshot_id(duty_methods) if duty_methods else None
            )

    if hydrate_passive_private_context:
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
    else:
        desk_manifest = ()

    prior_tool_receipts: list[dict[str, Any]] = []
    prior_public_offloads: set[str] = set()
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
        tool_name_s = str(r_tool_name)
        receipt_entry: dict[str, Any] = {
            "kind": "tool_call_receipt",
            "run_id": str(r_run_id),
            "call_id": str(r_call_id),
            "tool_name": tool_name_s,
            "arg_digest": str(r_arg_digest),
            "status": str(r_status),
            "result_digest": str(r_result_digest),
            "recorded_at_ns": int(r_recorded_at_ns),
        }
        is_private_receipt = is_private_error_tool(tool_name_s)
        if r_offloaded_log_path is not None and (
            effective_include_desk_tools or not is_private_receipt
        ):
            receipt_entry["offloaded_log_path"] = str(r_offloaded_log_path)
            if not is_private_desk_tool(tool_name_s):
                prior_public_offloads.add(str(r_offloaded_log_path))
        if (
            isinstance(r_result_json, str)
            and len(r_result_json) <= 1024
            and (effective_include_desk_tools or not is_private_receipt)
        ):
            try:
                receipt_entry["result"] = json.loads(r_result_json)
            except json.JSONDecodeError:
                receipt_entry["result_preview"] = r_result_json[:256]
        prior_tool_receipts.append(receipt_entry)

    recent_episodes = (
        tuple(
            {
                "episode_id": ep.episode_id,
                "duty_kind": ep.duty_kind,
                "outcome_status": ep.outcome_status,
                "summary_text": ep.summary_text[:300],
                "recorded_at_ns": ep.recorded_at_ns,
            }
            for ep in recent_eps
        )
        if hydrate_passive_private_context
        else ()
    )

    if pre_resolved_active_methods is not None:
        raw_active_methods, active_snapshot_id = pre_resolved_active_methods
    elif raw_active_methods_in_tx is not None:
        raw_active_methods = raw_active_methods_in_tx
        active_snapshot_id = active_snapshot_id_in_tx
    else:
        raw_active_methods, active_snapshot_id = active_methods_provider(
            ctx.writer.store,
            ctx.company_id,
            agent_id,
            ctx.work_item.duty_kind,
        )
    active_methods = _normalize_method_dicts(raw_active_methods)
    if not effective_include_desk_tools:
        active_methods = tuple(
            m for m in active_methods if m.get("audience_scope") != "one_agent"
        )
        active_snapshot_id = (
            compute_active_snapshot_id(active_methods) if active_methods else None
        )
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

    available_method_ids: list[str] = [
        m.method_id
        for m in all_agent_methods
        if effective_include_desk_tools or m.audience_scope != "one_agent"
    ]
    for m in active_methods:
        mid = m.get("method_id")
        if isinstance(mid, str) and mid not in available_method_ids:
            available_method_ids.append(mid)
    retrieved_method_ids: list[str] = []
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
        effective_include_desk_tools
        if include_core_skill_tools is None
        else (include_core_skill_tools and not initial_had_peer_input)
    )
    public_offloaded_paths: set[str] = set(prior_public_offloads)
    contributions: list[ToolContribution] = []
    if effective_include_desk_tools:
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
    else:
        contributions.append(
            build_public_offload_reader_contribution(
                ctx.writer.store, agent_id, public_offloaded_paths
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
            contribute_tools(
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
    if allowed_tool_names is not None:
        missing_tools = sorted(allowed_tool_names - set(merged_handlers))
        if missing_tools:
            raise RuntimeRefused(
                f"Sealed tool manifest requires unavailable tools: {', '.join(missing_tools)}"
            )
        tool_specs = tuple(
            spec for spec in tool_specs if spec.name in allowed_tool_names
        )
        merged_handlers = {
            name: fn
            for name, fn in merged_handlers.items()
            if name in allowed_tool_names
        }
    tool_specs = tuple(sorted(tool_specs, key=lambda spec: spec.name))
    merged_recoverable = tuple(
        dict.fromkeys(
            (*contributed_recoverable, *extra_recoverable_exceptions, DutyRefused)
        )
    )
    final_has_external_sinks = has_base_external_sinks or any(
        spec.requires_desk_isolation() for spec in tool_specs
    )
    has_hydrated_private_context = (
        bool(desk_manifest)
        or bool(recent_episodes)
        or any(m.get("audience_scope") == "one_agent" for m in active_methods)
        or prior_had_private_desk
    )

    return HydratedShift(
        agent_id=agent_id,
        persisted_profile=persisted_profile,
        grant_scope=grant_scope,
        profile=profile,
        claim=claim,
        desk=desk,
        context_pack=context_pack,
        tool_specs=tool_specs,
        merged_handlers=merged_handlers,
        merged_recoverable=merged_recoverable,
        active_snapshot_id=active_snapshot_id,
        available_method_ids=available_method_ids,
        retrieved_method_ids=retrieved_method_ids,
        invoked_method_ids=invoked_method_ids,
        public_offloaded_paths=public_offloaded_paths,
        initial_had_peer_input=initial_had_peer_input,
        prior_had_peer_input=prior_had_peer_input,
        prior_peer_rows=prior_peer_rows,
        has_hydrated_private_context=has_hydrated_private_context,
        effective_include_desk_tools=effective_include_desk_tools,
        has_external_sinks=final_has_external_sinks,
    )
