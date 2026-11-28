"""Core OuterToolBroker tool suite for Private Agent Desk, Shared Assets, and Episodic Memory."""

from __future__ import annotations

import math
import os
import stat
import threading
from collections.abc import Callable
from contextlib import closing, nullcontext
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from ..agent_desk import (
    PRODUCTION_DESK_LIMITS,
    DeskLimits,
    compute_agent_desk_digest,
    delete_draft,
    desk_directory,
    resolve_desk,
    save_draft,
)
from ..agent_runtime.protocol import ToolHandler, ToolSpec
from ..company_assets import (
    Selection,
    discover_project_references,
    project_access,
    publish_selection,
    read_shared_asset,
)
from ..duty_run import DutyRefused, WorkRunClaim
from ..episode_index import search_episodes
from ..harness import (
    DeskCommandLimits,
    create_agent_session,
    has_desk_exec_grant,
    run_desk_command_sandboxed,
)
from ..memory import read_episode
from ..storage.db import open_reader, read_tx
from ..writer_lease import WriterLease
from .domains import ToolContribution
from .work_kernel import assert_run_fencing

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_MAX_OFFLOAD_READ_BYTES = 16_384


def _require_object(
    args: Any, *, allowed: set[str], required: set[str], label: str
) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise DutyRefused(f"{label} arguments must be a JSON object")
    keys = set(args.keys())
    if not required <= keys or not keys <= allowed:
        raise DutyRefused(f"{label} arguments do not match schema")
    return args


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DutyRefused(f"{label} must be a nonempty string")
    return value


def _check_regular_no_symlink(path: Path, stop_at: Path) -> None:
    if not os.path.lexists(path):
        raise DutyRefused("Requested log file does not exist")
    rel = path.relative_to(stop_at)
    current = stop_at
    for part in rel.parts:
        current = current / part
        if not os.path.lexists(current):
            raise DutyRefused("Requested log file does not exist")
        st = os.lstat(current)
        if stat.S_ISLNK(st.st_mode):
            raise DutyRefused("Symlinks are forbidden inside private Desk storage")


_DEFAULT_OFFLOAD_READ_BYTES = 2048


def read_offloaded_log_slice(
    store: Path,
    agent_id: str,
    log_path: str,
    *,
    offset: int = 0,
    limit: int = _DEFAULT_OFFLOAD_READ_BYTES,
) -> dict[str, Any]:
    """Read a bounded byte slice from <desk>/.desk/logs/<file> with symlink/inode checks."""
    if "\\" in log_path or "\x00" in log_path or log_path.startswith("/"):
        raise DutyRefused("Invalid offloaded log path")
    posix = PurePosixPath(log_path)
    if (
        len(posix.parts) != 3
        or posix.parts[0] != ".desk"
        or posix.parts[1] != "logs"
        or posix.parts[2] in ("", ".", "..")
        or posix.parts[2].startswith(".")
    ):
        raise DutyRefused("Offloaded log path must target .desk/logs/<file>")
    if type(offset) is not int or offset < 0:
        raise DutyRefused("offset must be a nonnegative integer")
    if type(limit) is not int or limit < 1:
        raise DutyRefused("limit must be a positive integer")
    bounded_limit = min(limit, _MAX_OFFLOAD_READ_BYTES)

    root = store.resolve(strict=False).parent
    target = desk_directory(store, agent_id) / ".desk" / "logs" / posix.parts[2]
    _check_regular_no_symlink(target, root)
    st = os.lstat(target)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise DutyRefused("Offloaded log target must be a regular file")
    fd = os.open(target, os.O_RDONLY | _O_NOFOLLOW)
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or fst.st_nlink != 1:
            raise DutyRefused("Offloaded log target failed regular-file validation")
        total_bytes = int(fst.st_size)
        if offset >= total_bytes:
            raw_chunk = b""
        else:
            os.lseek(fd, offset, os.SEEK_SET)
            raw_chunk = os.read(fd, bounded_limit)
    finally:
        os.close(fd)
    return {
        "ok": True,
        "log_path": log_path,
        "offset": offset,
        "total_bytes": total_bytes,
        "content": raw_chunk.decode("utf-8", errors="replace"),
        "eof": offset + len(raw_chunk) >= total_bytes,
    }


def build_core_desk_tool_contribution(
    *,
    writer: WriterLease,
    claim: WorkRunClaim,
    purpose: str,
    sandbox_backend: str,
    tx_lock: threading.RLock | None = None,
    desk_limits: DeskLimits = PRODUCTION_DESK_LIMITS,
    command_limits: DeskCommandLimits | None = None,
    clock: Callable[[], int] | None = None,
) -> ToolContribution:
    """Build Core Desk, Asset, and Memory OuterToolBroker tools fenced to WorkRunClaim."""
    del clock
    eff_cmd_limits = command_limits or DeskCommandLimits()
    lock_ctx = tx_lock if tx_lock is not None else nullcontext()

    def _verify_fencing() -> None:
        with closing(open_reader(writer.store)) as db, read_tx(db):
            assert_run_fencing(
                db, claim.run_id, claim.fencing_token, now_ns=claim.current_now_ns()
            )

    def _desk_list_drafts(args: dict[str, Any]) -> dict[str, Any]:
        _require_object(args, allowed=set(), required=set(), label="desk_list_drafts")
        manifest_rev, drafts = resolve_desk(
            writer, claim, purpose, include_drafts=True, limits=desk_limits
        )
        return {
            "ok": True,
            "manifest_revision": manifest_rev,
            "desk_digest": compute_agent_desk_digest(writer.store, claim.founder_id),
            "drafts": [
                {
                    "name": d.name,
                    "revision": d.revision,
                    "bytes": len(d.content.encode("utf-8")),
                }
                for d in drafts
            ],
        }

    def _desk_read_draft(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args, allowed={"name"}, required={"name"}, label="desk_read_draft"
        )
        name = _require_str(obj["name"], "name")
        _rev, drafts = resolve_desk(
            writer, claim, purpose, include_drafts=True, limits=desk_limits
        )
        for draft in drafts:
            if draft.name == name:
                return {
                    "ok": True,
                    "name": draft.name,
                    "revision": draft.revision,
                    "content": draft.content,
                }
        raise DutyRefused(f"Private draft '{name}' does not exist")

    def _desk_save_draft(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={"name", "content", "expected_revision"},
            required={"name", "content"},
            label="desk_save_draft",
        )
        name = _require_str(obj["name"], "name")
        content = obj["content"]
        if not isinstance(content, str):
            raise DutyRefused("content must be a string")
        exp_rev = obj.get("expected_revision")
        if exp_rev is not None and not isinstance(exp_rev, str):
            raise DutyRefused("expected_revision must be a string or null")
        with lock_ctx:
            draft = save_draft(
                writer,
                claim,
                purpose,
                name=name,
                content=content,
                expected_revision=exp_rev,
                limits=desk_limits,
            )
        return {
            "ok": True,
            "name": draft.name,
            "revision": draft.revision,
            "bytes": len(draft.content.encode("utf-8")),
        }

    def _desk_delete_draft(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={"name", "expected_revision"},
            required={"name"},
            label="desk_delete_draft",
        )
        name = _require_str(obj["name"], "name")
        exp_rev = obj.get("expected_revision")
        if exp_rev is not None and not isinstance(exp_rev, str):
            raise DutyRefused("expected_revision must be a string or null")
        with lock_ctx:
            digest = delete_draft(
                writer,
                claim,
                purpose,
                name=name,
                expected_revision=exp_rev,
                limits=desk_limits,
            )
        return {
            "ok": True,
            "deleted": True,
            "name": name,
            "desk_digest": digest,
        }

    def _desk_read_offloaded_log(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={"log_path", "offset", "limit"},
            required={"log_path"},
            label="desk_read_offloaded_log",
        )
        _verify_fencing()
        log_path = _require_str(obj["log_path"], "log_path")
        offset = obj.get("offset", 0)
        limit = obj.get("limit", _DEFAULT_OFFLOAD_READ_BYTES)
        return read_offloaded_log_slice(
            writer.store,
            claim.founder_id,
            log_path,
            offset=offset,
            limit=limit,
        )

    def _desk_publish_selection(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={
                "request_id",
                "draft_name",
                "draft_revision",
                "project_id",
                "project_revision",
                "audience",
                "start",
                "end",
            },
            required={
                "request_id",
                "draft_name",
                "draft_revision",
                "project_id",
                "project_revision",
                "audience",
                "start",
                "end",
            },
            label="desk_publish_selection",
        )
        audience_raw = obj["audience"]
        if not isinstance(audience_raw, list) or not all(
            isinstance(item, str) for item in audience_raw
        ):
            raise DutyRefused("audience must be a list of agent IDs")
        selection = Selection(
            request_id=obj["request_id"],
            draft_name=obj["draft_name"],
            draft_revision=obj["draft_revision"],
            project_id=obj["project_id"],
            project_revision=obj["project_revision"],
            audience=tuple(audience_raw),
            start=obj["start"],
            end=obj["end"],
        )
        with lock_ctx:
            asset = publish_selection(writer, claim, purpose, selection)
        return {
            "ok": True,
            "asset_id": asset.asset_id,
            "project_id": asset.project_id,
            "work_item_id": asset.duty_id,
            "request_id": selection.request_id,
            "content_sha256": sha256(asset.content.encode("utf-8")).hexdigest(),
        }

    def _discover_project_references(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={"after"},
            required=set(),
            label="discover_project_references",
        )
        after = obj.get("after", "")
        if not isinstance(after, str):
            raise DutyRefused("after cursor must be a string")
        projects = project_access(writer, claim, purpose)
        assets, receipts, next_cursor = discover_project_references(
            writer, claim, purpose, after
        )
        return {
            "ok": True,
            "projects": [
                {
                    "project_id": p.project_id,
                    "revision": p.revision,
                    "members": list(p.members),
                    "can_publish": p.can_publish,
                }
                for p in projects
            ],
            "shared_assets": [
                {
                    "asset_id": a.asset_id,
                    "project_id": a.project_id,
                    "author_id": a.author_id,
                    "work_item_id": a.duty_id,
                }
                for a in assets
            ],
            "publication_receipts": [
                {
                    "asset_id": r.asset_id,
                    "project_id": r.project_id,
                    "work_item_id": r.duty_id,
                    "request_id": r.request_id,
                }
                for r in receipts
            ],
            "next_cursor": next_cursor,
        }

    def _read_shared_asset(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={"asset_id"},
            required={"asset_id"},
            label="read_shared_asset",
        )
        asset_id = _require_str(obj["asset_id"], "asset_id")
        asset = read_shared_asset(writer, claim, purpose, asset_id)
        return {
            "ok": True,
            "asset_id": asset.asset_id,
            "project_id": asset.project_id,
            "author_id": asset.author_id,
            "work_item_id": asset.duty_id,
            "content": asset.content,
        }

    def _memory_search_episodes(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={"query", "limit"},
            required={"query"},
            label="memory_search_episodes",
        )
        _verify_fencing()
        query = obj["query"]
        if not isinstance(query, str):
            raise DutyRefused("query must be a string")
        limit = obj.get("limit", 10)
        if type(limit) is not int or limit < 1:
            raise DutyRefused("limit must be a positive integer")
        results = search_episodes(
            writer.store,
            claim.company_id,
            claim.founder_id,
            query,
            limit=min(limit, 20),
        )
        return {"ok": True, "episodes": results}

    def _memory_read_episode(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={"episode_id"},
            required={"episode_id"},
            label="memory_read_episode",
        )
        _verify_fencing()
        episode_id = _require_str(obj["episode_id"], "episode_id")
        with closing(open_reader(writer.store)) as db, read_tx(db):
            episode = read_episode(db, claim.company_id, claim.founder_id, episode_id)
        return {"ok": True, "episode": episode.to_dict()}

    specs: list[ToolSpec] = [
        ToolSpec(
            name="desk_list_drafts",
            description="List private draft names, revisions, and byte sizes on your private Agent Desk.",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="desk_read_draft",
            description="Read the content and revision of one private draft from your private Agent Desk.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="desk_save_draft",
            description="Create or update a private draft on your private Agent Desk with optimistic expected_revision.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "content": {"type": "string"},
                    "expected_revision": {"type": ["string", "null"]},
                },
                "required": ["name", "content"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="desk_delete_draft",
            description="Delete a private draft from your private Agent Desk.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "expected_revision": {"type": ["string", "null"]},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="desk_read_offloaded_log",
            description="Read a bounded slice of an offloaded tool output file under .desk/logs/.",
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
        ToolSpec(
            name="desk_publish_selection",
            description="Publish an explicit byte/character range from a private draft to an authorized shared project.",
            parameters={
                "type": "object",
                "properties": {
                    "request_id": {"type": "string"},
                    "draft_name": {"type": "string"},
                    "draft_revision": {"type": "string"},
                    "project_id": {"type": "string"},
                    "project_revision": {"type": "string"},
                    "audience": {"type": "array", "items": {"type": "string"}},
                    "start": {"type": "integer", "minimum": 0},
                    "end": {"type": "integer", "minimum": 1},
                },
                "required": [
                    "request_id",
                    "draft_name",
                    "draft_revision",
                    "project_id",
                    "project_revision",
                    "audience",
                    "start",
                    "end",
                ],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="discover_project_references",
            description="Discover shared projects, authorized shared asset references, and your publication receipts.",
            parameters={
                "type": "object",
                "properties": {"after": {"type": "string"}},
                "required": [],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="read_shared_asset",
            description="Read the content of a shared project asset if you are an authorized project member and audience.",
            parameters={
                "type": "object",
                "properties": {"asset_id": {"type": "string"}},
                "required": ["asset_id"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="memory_search_episodes",
            description="Search your own past shift episodes using full-text search.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="memory_read_episode",
            description="Read the full structured record of one of your own past shift episodes.",
            parameters={
                "type": "object",
                "properties": {"episode_id": {"type": "string"}},
                "required": ["episode_id"],
                "additionalProperties": False,
            },
        ),
    ]
    handlers: dict[str, ToolHandler] = {
        "desk_list_drafts": _desk_list_drafts,
        "desk_read_draft": _desk_read_draft,
        "desk_save_draft": _desk_save_draft,
        "desk_delete_draft": _desk_delete_draft,
        "desk_read_offloaded_log": _desk_read_offloaded_log,
        "desk_publish_selection": _desk_publish_selection,
        "discover_project_references": _discover_project_references,
        "read_shared_asset": _read_shared_asset,
        "memory_search_episodes": _memory_search_episodes,
        "memory_read_episode": _memory_read_episode,
    }

    with closing(open_reader(writer.store)) as db, read_tx(db):
        exec_granted = sandbox_backend == "local-macos" and has_desk_exec_grant(
            db, claim.company_id, claim.founder_id
        )

    if exec_granted:

        def _desk_run_command(args: dict[str, Any]) -> dict[str, Any]:
            obj = _require_object(
                args,
                allowed={"command", "timeout_seconds"},
                required={"command"},
                label="desk_run_command",
            )
            _verify_fencing()
            if sandbox_backend != "local-macos":
                raise DutyRefused(
                    "desk_run_command requires local-macos sandbox backend"
                )
            with closing(open_reader(writer.store)) as db, read_tx(db):
                if not has_desk_exec_grant(db, claim.company_id, claim.founder_id):
                    raise DutyRefused(
                        "desk_run_command requires explicit desk:exec grant"
                    )
            raw_cmd = obj["command"]
            if (
                not isinstance(raw_cmd, list)
                or not raw_cmd
                or not all(isinstance(part, str) and part for part in raw_cmd)
            ):
                raise DutyRefused("command must be a nonempty list of nonempty strings")
            req_timeout = obj.get("timeout_seconds")
            if req_timeout is not None:
                if isinstance(req_timeout, bool) or not isinstance(
                    req_timeout, (int, float)
                ):
                    raise DutyRefused(
                        "timeout_seconds must be a finite positive number"
                    )
                try:
                    req_timeout_f = float(req_timeout)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise DutyRefused(
                        "timeout_seconds must be a finite positive number"
                    ) from exc
                if not math.isfinite(req_timeout_f) or req_timeout_f <= 0:
                    raise DutyRefused(
                        "timeout_seconds must be a finite positive number"
                    )
                effective_limits = DeskCommandLimits(
                    timeout_seconds=min(req_timeout_f, eff_cmd_limits.timeout_seconds),
                    cpu_seconds=eff_cmd_limits.cpu_seconds,
                    memory_bytes=eff_cmd_limits.memory_bytes,
                    max_processes=eff_cmd_limits.max_processes,
                    max_open_files=eff_cmd_limits.max_open_files,
                    max_file_bytes=eff_cmd_limits.max_file_bytes,
                    max_stdout_bytes=eff_cmd_limits.max_stdout_bytes,
                    max_stderr_bytes=eff_cmd_limits.max_stderr_bytes,
                )
            else:
                effective_limits = eff_cmd_limits
            session = create_agent_session(writer, claim, purpose)
            return run_desk_command_sandboxed(
                session,
                raw_cmd,
                store=writer.store,
                sandbox_backend=sandbox_backend,
                limits=effective_limits,
            )

        specs.append(
            ToolSpec(
                name="desk_run_command",
                description="Execute a bounded non-shell command inside the macOS sandbox restricted to your private Desk.",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                        "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
            )
        )
        handlers["desk_run_command"] = _desk_run_command

    return ToolContribution(specs=tuple(specs), handlers=handlers)
