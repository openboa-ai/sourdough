"""Unified ShiftRunner orchestrating Core & DomainPlugin agent shifts with hardened mediation."""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass, is_dataclass
from hashlib import sha256
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
from .desk_tools import (
    _DEFAULT_OFFLOAD_READ_BYTES,
    _MAX_OFFLOAD_READ_BYTES,
    build_core_desk_tool_contribution,
    read_offloaded_log_slice,
)
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
_PRIVATE_DESK_READ_TOOLS = frozenset(
    {
        "desk_list_drafts",
        "desk_read_draft",
        "desk_read_offloaded_log",
        "desk_run_command",
        "memory_search_episodes",
        "memory_read_episode",
        "skill_list_active",
        "skill_read",
    }
)
_PRIVATE_DESK_ALL_TOOLS = frozenset(
    {
        *_PRIVATE_DESK_READ_TOOLS,
        "desk_save_draft",
        "desk_delete_draft",
        "desk_publish_selection",
        "skill_draft_candidate",
        "skill_record_invocation",
    }
)
_PEER_INPUT_TOOLS = frozenset(
    {
        "read_messages",
        "read_inbox",
        "discover_handoffs",
        "read_shared_asset",
    }
)
_PEER_OUTBOUND_SINKS = frozenset(
    {
        "append_message",
        "request_peer_response",
        "respond_peer_request",
        "offer_handoff",
        "decline_handoff",
    }
)
_TRADING_WRITE_SINKS = frozenset(
    {
        "submit_company_paper_order",
        "cancel_company_paper_order",
        "submit_company_live_order",
        "propose_live_migration",
        "open_agent_sandbox",
        "submit_agent_sandbox_order",
        "cancel_agent_sandbox_order",
    }
)
_PRIVATE_DESK_RECEIPT_TOOLS = frozenset(
    {
        "desk_list_drafts",
        "desk_read_draft",
        "desk_save_draft",
        "desk_delete_draft",
        "desk_read_offloaded_log",
        "desk_run_command",
        "memory_search_episodes",
        "memory_read_episode",
    }
)
_PRIVATE_RECEIPT_ERROR_TOOLS = frozenset(
    {
        *_PRIVATE_DESK_RECEIPT_TOOLS,
        "desk_publish_selection",
        "skill_read",
        "skill_list_active",
        "skill_draft_candidate",
        "skill_record_invocation",
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
_NON_AUTHORITY_INPUT_MARKERS = (
    "cursor is unavailable",
    "arguments must be",
    "do not match schema",
    "must be a ",
    "must be an ",
    "invalid cursor",
    "duplicate message",
    "does not exist",
    "revision changed",
    "state version mismatch",
    "trajectory loop detected",
)
_AUTHORITY_REFUSAL_MARKERS = (
    "project discussion is unavailable",
    "shared project is unavailable",
    "shared asset is unavailable",
    "handoff offer is unavailable",
    "decision route is unavailable",
    "tool is unavailable",
    "unauthorized",
    "not authorized",
    "lacks '",
    "lacks ",
    "authority",
    "grant",
    "forbidden",
    "permission",
    "not permitted",
    "not admitted",
    "only the named recipient",
    "only the owner",
    "cannot read private desk",
    "cannot access private desk",
    "cannot write free-form peer",
    "cannot read peer-authored",
    "cannot access",
    "cannot publish",
    "cannot offer",
    "outside the recipient",
    "outside ",
    "belongs to another",
    "not a member",
    "redaction",
    "private desk",
    "symlink",
    "sandbox",
    "spoofed",
    "cross-agent",
    "cross-company",
    "unreadable",
    "not readable",
    "not writable",
)


def is_authority_refusal_message(
    error_type: str | None, error_message: str | None
) -> bool:
    """Classify whether a tool refusal represents an authority/isolation breach vs input error."""
    if error_type in (
        "ExecutionTierRefused",
        "AgentRuntimeRefused",
        "RunFenced",
        "LeaseLost",
    ):
        return True
    if not error_message:
        return False
    msg = str(error_message)
    if msg.startswith("redacted_private_authority_error:"):
        return True
    low = msg.lower()
    if any(marker in low for marker in _NON_AUTHORITY_INPUT_MARKERS):
        return False
    return any(marker in low for marker in _AUTHORITY_REFUSAL_MARKERS)


def _sanitize_receipt_result_json(
    tool_name: str,
    result_digest: str,
    raw_result_json: str,
    *,
    status: str = "ok",
) -> str:
    """Keep private Desk payloads and private-tool refusal details out of company.sqlite."""
    is_private_desk = tool_name in _PRIVATE_DESK_RECEIPT_TOOLS
    is_private_error_tool = (
        tool_name in _PRIVATE_RECEIPT_ERROR_TOOLS or tool_name.startswith("desk_")
    )
    if not is_private_desk and not (is_private_error_tool and status != "ok"):
        return raw_result_json
    try:
        parsed = json.loads(raw_result_json)
    except (ValueError, TypeError):
        parsed = None
    if is_private_desk:
        safe_payload: dict[str, Any] = {
            "redacted": "private_desk_payload",
            "result_digest": result_digest,
        }
        if isinstance(parsed, Mapping):
            for key in _SAFE_DESK_RECEIPT_FIELDS:
                if key in parsed:
                    safe_payload[key] = parsed[key]
        return json.dumps(safe_payload, sort_keys=True, separators=(",", ":"))
    safe_err: dict[str, Any] = {
        "ok": False,
        "redacted": "private_error_payload",
        "result_digest": result_digest,
    }
    if isinstance(parsed, Mapping):
        if isinstance(parsed.get("ok"), bool):
            safe_err["ok"] = parsed["ok"]
        if isinstance(parsed.get("error_type"), str):
            safe_err["error_type"] = parsed["error_type"]
    return json.dumps(safe_err, sort_keys=True, separators=(",", ":"))


def _sanitize_receipt_error_message(
    tool_name: str, error_type: str | None, raw_error_message: str | None
) -> str | None:
    """Keep private Desk filenames, draft identifiers, and skill details out of company.sqlite receipts."""
    if raw_error_message is None:
        return None
    if tool_name in _PRIVATE_RECEIPT_ERROR_TOOLS or tool_name.startswith("desk_"):
        err_kind = str(error_type or "Error")
        digest = sha256(str(raw_error_message).encode("utf-8")).hexdigest()[:12]
        prefix = (
            "redacted_private_authority_error"
            if is_authority_refusal_message(error_type, raw_error_message)
            else "redacted_private_error"
        )
        return f"{prefix}:{err_kind}:{digest}"
    return str(raw_error_message)


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
            from ..method_learning import opaque_one_agent_method_id

            one_agent_mids = {
                str(r[0])
                for r in db.execute(
                    """SELECT DISTINCT method_id FROM method_delta
                       WHERE company_id = ? AND audience_scope = 'one_agent'""",
                    (ctx.company_id,),
                ).fetchall()
            }
            audit_reconciled = dict(reconciled)
            for key in ("available", "retrieved", "invoked"):
                vals = audit_reconciled.get(key)
                if isinstance(vals, list):
                    audit_reconciled[key] = [
                        opaque_one_agent_method_id(v)
                        if isinstance(v, str) and v in one_agent_mids
                        else v
                        for v in vals
                    ]
            append_audit_event(
                db,
                company_id=ctx.company_id,
                domain="learning",
                entity_type="agent_episode",
                entity_id=ctx.run.run_id,
                event_type="learning.causal_method_trace_reconciled",
                actor_id=ctx.work_item.owner_agent_id,
                run_id=ctx.run.run_id,
                payload=audit_reconciled,
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
        allowed_tool_names: frozenset[str] | None = None,
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

        with closing(open_reader(ctx.writer.store)) as db, read_tx(db):
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
        prior_had_private_desk = bool(prior_tool_names & _PRIVATE_DESK_READ_TOOLS)
        effective_include_desk_tools = (
            include_core_desk_tools and not prior_had_peer_input
        )
        effective_base_names = (
            set(base_handlers)
            if allowed_tool_names is None
            else (set(base_handlers) & allowed_tool_names)
        )
        has_peer_collab_tools = bool(
            effective_base_names & (_PEER_INPUT_TOOLS | _PEER_OUTBOUND_SINKS)
        )
        has_trading_write_sinks = bool(effective_base_names & _TRADING_WRITE_SINKS)
        has_external_sinks = has_peer_collab_tools or has_trading_write_sinks
        desk_tools_permitted = effective_include_desk_tools and (
            allowed_tool_names is None
            or bool(allowed_tool_names & _PRIVATE_DESK_ALL_TOOLS)
        )
        hydrate_passive_private_context = (
            desk_tools_permitted and not has_external_sinks
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

        with closing(open_reader(ctx.writer.store)) as db, read_tx(db):
            recent_eps = (
                list_recent_episodes(db, ctx.company_id, agent_id, limit=5)
                if hydrate_passive_private_context
                else ()
            )
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
            is_private_receipt = str(
                r_tool_name
            ) in _PRIVATE_RECEIPT_ERROR_TOOLS or str(r_tool_name).startswith("desk_")
            if r_offloaded_log_path is not None and (
                effective_include_desk_tools or not is_private_receipt
            ):
                receipt_entry["offloaded_log_path"] = str(r_offloaded_log_path)
                if str(r_tool_name) not in _PRIVATE_DESK_ALL_TOOLS:
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
            else (include_core_skill_tools and not prior_had_peer_input)
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

            def _read_public_offloaded_output(args: dict[str, Any]) -> dict[str, Any]:
                if not isinstance(args, dict):
                    raise DutyRefused(
                        "read_offloaded_output arguments must be a JSON object"
                    )
                unknown = set(args) - {"log_path", "offset", "limit"}
                if unknown or "log_path" not in args:
                    raise DutyRefused(
                        "read_offloaded_output arguments do not match schema"
                    )
                lp = args["log_path"]
                if not isinstance(lp, str) or not lp.strip():
                    raise DutyRefused("log_path must be a nonempty string")
                if lp not in public_offloaded_paths:
                    raise DutyRefused(
                        "Offloaded log path is not an authorized public result of this shift"
                    )
                return read_offloaded_log_slice(
                    ctx.writer.store,
                    agent_id,
                    lp,
                    offset=args.get("offset", 0),
                    limit=args.get("limit", _DEFAULT_OFFLOAD_READ_BYTES),
                )

            contributions.append(
                ToolContribution(
                    specs=(
                        ToolSpec(
                            name="read_offloaded_output",
                            description="Read a bounded slice of a public tool output offloaded during this shift.",
                            parameters={
                                "type": "object",
                                "properties": {
                                    "log_path": {"type": "string"},
                                    "offset": {"type": "integer", "minimum": 0},
                                    "limit": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "maximum": _MAX_OFFLOAD_READ_BYTES,
                                    },
                                },
                                "required": ["log_path"],
                                "additionalProperties": False,
                            },
                        ),
                    ),
                    handlers={"read_offloaded_output": _read_public_offloaded_output},
                    recoverable_exceptions=(DutyRefused,),
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

        has_hydrated_private_context = (
            bool(desk_manifest)
            or bool(recent_episodes)
            or any(m.get("audience_scope") == "one_agent" for m in active_methods)
            or prior_had_private_desk
        )
        flow_state = {
            "read_private_desk": has_hydrated_private_context,
            "read_peer_input": prior_had_peer_input,
        }

        def _wrap_handler(name: str, fn: ToolHandler, *, use_lock: bool) -> ToolHandler:
            def _wrapped(args: dict[str, Any]) -> dict[str, Any]:
                is_public_offload_read = (
                    name == "desk_read_offloaded_log"
                    and isinstance(args, Mapping)
                    and isinstance(args.get("log_path"), str)
                    and args["log_path"] in public_offloaded_paths
                )
                if (
                    name in (_PEER_OUTBOUND_SINKS | _TRADING_WRITE_SINKS)
                    and flow_state["read_private_desk"]
                ):
                    raise DutyRefused(
                        "Cannot write free-form peer messages, peer requests, or trading actions in the same shift "
                        "after reading private Desk data; publish explicitly via desk_publish_selection"
                    )
                if (
                    name in _PRIVATE_DESK_ALL_TOOLS
                    and not is_public_offload_read
                    and flow_state["read_peer_input"]
                ):
                    raise DutyRefused(
                        "Cannot access private Desk drafts, publications, or skills in the same shift "
                        "after reading peer-authored messages, handoffs, or shared assets"
                    )
                if name in _PEER_INPUT_TOOLS and flow_state["read_private_desk"]:
                    raise DutyRefused(
                        "Cannot read peer-authored messages, handoffs, or shared assets "
                        "in the same shift after reading private Desk data"
                    )
                if use_lock:
                    with ctx.write_lock_ctx():
                        out = fn(args)
                else:
                    out = fn(args)
                if name in _PRIVATE_DESK_READ_TOOLS and not is_public_offload_read:
                    flow_state["read_private_desk"] = True
                if name in _PEER_INPUT_TOOLS:
                    flow_state["read_peer_input"] = True
                return out

            return _wrapped

        locked_handlers: dict[str, ToolHandler] = {
            name: _wrap_handler(name, fn, use_lock=(name != "desk_run_command"))
            for name, fn in merged_handlers.items()
        }

        session = create_agent_session(ctx.writer, claim, claim.purpose)

        def _fencing_check() -> None:
            with closing(open_reader(ctx.writer.store)) as db, read_tx(db):
                assert_run_fencing(
                    db, ctx.run.run_id, ctx.run.fencing_token, now_ns=ctx.now_ns
                )

        def _record_receipt(receipt: dict[str, Any]) -> None:
            tool_name = str(receipt["tool_name"])
            stored_result_json = _sanitize_receipt_result_json(
                tool_name,
                str(receipt["result_digest"]),
                str(receipt["result_json"]),
                status=str(receipt["status"]),
            )
            stored_error_message = _sanitize_receipt_error_message(
                tool_name,
                None if receipt["error_type"] is None else str(receipt["error_type"]),
                None
                if receipt["error_message"] is None
                else str(receipt["error_message"]),
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
                        tool_name,
                        receipt["arg_digest"],
                        receipt["status"],
                        receipt["result_digest"],
                        stored_result_json,
                        receipt["error_type"],
                        stored_error_message,
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
            desk_path=(
                desk
                if (effective_include_desk_tools and not has_external_sinks)
                else None
            ),
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
            if tool_name in ("desk_read_offloaded_log", "read_offloaded_output"):
                return payload
            res = offload_large_output(
                ctx.writer.store,
                session,
                tool_name,
                payload,
                seq=seq,
                max_inline_bytes=DEFAULT_OFFLOAD_THRESHOLD_BYTES,
            )
            if (
                res.get("offloaded") is True
                and isinstance(res.get("log_path"), str)
                and tool_name not in _PRIVATE_DESK_ALL_TOOLS
            ):
                public_offloaded_paths.add(res["log_path"])
            return res

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
                raw_out = shift_result.output_text.strip()
                out_bytes = raw_out.encode("utf-8")
                out_digest = sha256(out_bytes).hexdigest()
                if final_run_status == "waiting":
                    ep_summary = (
                        f"status={final_run_status} duty={ctx.work_item.duty_kind} "
                        f"wait_key={final_wait_event_key or 'none'} "
                        f"tools={broker.call_count} output_bytes={len(out_bytes)} "
                        f"output_digest={out_digest}"
                    )
                else:
                    ep_summary = (
                        f"status={final_run_status} duty={ctx.work_item.duty_kind} "
                        f"tools={broker.call_count} output_bytes={len(out_bytes)} "
                        f"output_digest={out_digest}"
                    )
                _append_trace_line(
                    ctx.writer.store,
                    session.trace_path,
                    {
                        "seq": broker.call_count + 1,
                        "kind": "shift_terminal",
                        "recorded_at_ns": max(1, int(ctx.now_ns)),
                        "run_id": ctx.run.run_id,
                        "status": final_run_status,
                        "output_bytes": len(out_bytes),
                        "output_digest": out_digest,
                        "output_text": raw_out,
                    },
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
                        from ..method_learning import opaque_one_agent_method_id

                        one_agent_mids = {
                            str(r[0])
                            for r in db.execute(
                                """SELECT DISTINCT method_id FROM method_delta
                                   WHERE company_id = ? AND audience_scope = 'one_agent'""",
                                (ctx.company_id,),
                            ).fetchall()
                        }
                        audit_trace = dict(causal_trace_dict)
                        for key in ("available", "retrieved", "invoked"):
                            vals = audit_trace.get(key)
                            if isinstance(vals, list):
                                audit_trace[key] = [
                                    opaque_one_agent_method_id(v)
                                    if isinstance(v, str) and v in one_agent_mids
                                    else v
                                    for v in vals
                                ]
                        append_audit_event(
                            db,
                            company_id=ctx.company_id,
                            domain="learning",
                            entity_type="agent_episode",
                            entity_id=ctx.run.run_id,
                            event_type="learning.causal_method_trace",
                            actor_id=agent_id,
                            run_id=ctx.run.run_id,
                            payload=audit_trace,
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
