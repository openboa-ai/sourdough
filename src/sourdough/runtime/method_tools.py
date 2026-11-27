"""Core OuterToolBroker tool suite for reading active Skills and drafting candidate Method deltas."""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Callable
from contextlib import closing, nullcontext
from pathlib import PurePosixPath
from time import time_ns
from typing import Any

from ..agent_desk import desk_directory, require_desk
from ..agent_runtime.protocol import ToolHandler, ToolSpec
from ..duty_run import DutyRefused, WorkRunClaim
from ..method_learning import (
    MethodRefused,
    compute_active_snapshot_id,
    distill_method_delta,
    render_skill_markdown,
    resolve_active_methods,
    skill_precedence_key,
)
from ..storage.db import open_reader, read_tx
from ..writer_lease import WriterLease
from .collab_tools import _optional_str, _require_object, _require_str
from .domains import ToolContribution
from .work_kernel import WorkKernelRefused, assert_run_fencing

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def build_core_skill_tool_contribution(
    *,
    writer: WriterLease,
    claim: WorkRunClaim,
    active_snapshot_id: str | None = None,
    retrieved_recorder: Callable[[str], None] | None = None,
    invoked_recorder: Callable[[str, str | None], None] | None = None,
    tx_lock: threading.RLock | None = None,
) -> ToolContribution:
    """Build safe OuterToolBroker tools for reading active Skills and drafting candidates."""
    lock_ctx = tx_lock if tx_lock is not None else nullcontext()

    def _now_ns() -> int:
        ts = claim.current_now_ns()
        return time_ns() if ts is None else int(ts)

    def _verify_fencing_and_desk(db: Any) -> None:
        assert_run_fencing(
            db, claim.run_id, claim.fencing_token, now_ns=claim.current_now_ns()
        )
        require_desk(db, claim, claim.purpose)

    def _read_projected_skill_markdown(
        slug: str, method: Any, *, allow_disk: bool = True
    ) -> str:
        desk = desk_directory(writer.store, claim.founder_id)
        skill_file = desk / "skills" / slug / "SKILL.md"
        for part in (desk / "skills", desk / "skills" / slug, skill_file):
            if os.path.lexists(part) and stat.S_ISLNK(os.lstat(part).st_mode):
                raise MethodRefused("Symlinks are forbidden inside Desk skills/")
        if not allow_disk or not os.path.lexists(skill_file):
            return render_skill_markdown(method)
        st = os.lstat(skill_file)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise MethodRefused("Projected SKILL.md must be a regular file")
        fd = os.open(skill_file, os.O_RDONLY | _O_NOFOLLOW)
        try:
            fst = os.fstat(fd)
            if not stat.S_ISREG(fst.st_mode) or fst.st_nlink != 1:
                raise MethodRefused("Projected SKILL.md failed regular-file validation")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 16_384)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(fd)
        return b"".join(chunks).decode("utf-8")

    def _skill_list_active(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={"duty_kind"},
            required=set(),
            label="skill_list_active",
        )
        duty_filter = _optional_str(obj.get("duty_kind"), "duty_kind")
        writer.verify_for(writer.store, claim.company_id)
        with closing(open_reader(writer.store)) as db, read_tx(db):
            _verify_fencing_and_desk(db)
            methods = resolve_active_methods(
                db,
                claim.company_id,
                agent_id=claim.founder_id,
                duty_kind=duty_filter,
            )
        snap = active_snapshot_id or compute_active_snapshot_id(methods)
        sorted_methods = sorted(
            methods, key=lambda m: (m.method_id, m.revision_sha256, m.audience_scope)
        )
        return {
            "ok": True,
            "active_snapshot_id": snap,
            "skills": [m.to_index_dict() for m in sorted_methods],
        }

    def _skill_read(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={"method_id", "revision_sha256", "skill_path"},
            required=set(),
            label="skill_read",
        )
        method_id = _optional_str(obj.get("method_id"), "method_id")
        skill_path = _optional_str(obj.get("skill_path"), "skill_path")
        rev_filter = _optional_str(obj.get("revision_sha256"), "revision_sha256")
        if method_id is None and skill_path is None:
            raise MethodRefused("skill_read requires method_id or skill_path")

        slug_filter: str | None = None
        if skill_path is not None:
            if "\\" in skill_path or "\x00" in skill_path or skill_path.startswith("/"):
                raise MethodRefused("Invalid skill_path")
            posix = PurePosixPath(skill_path)
            if (
                len(posix.parts) != 3
                or posix.parts[0] != "skills"
                or posix.parts[2] != "SKILL.md"
                or posix.parts[1] in ("", ".", "..")
            ):
                raise MethodRefused(
                    "skill_path must use 'skills/<skill-name>/SKILL.md' format"
                )
            slug_filter = posix.parts[1]

        writer.verify_for(writer.store, claim.company_id)
        with closing(open_reader(writer.store)) as db, read_tx(db):
            _verify_fencing_and_desk(db)
            methods = resolve_active_methods(
                db,
                claim.company_id,
                agent_id=claim.founder_id,
                duty_kind=None,
            )
        candidates = [
            m
            for m in methods
            if (method_id is None or m.method_id == method_id)
            and (slug_filter is None or m.to_index_dict()["skill_name"] == slug_filter)
            and (rev_filter is None or m.revision_sha256 == rev_filter)
        ]
        matched = max(candidates, key=skill_precedence_key) if candidates else None
        if matched is None:
            target_label = method_id or skill_path or "unknown"
            raise MethodRefused(
                f"Active skill '{target_label}' is unavailable in this Agent's scope"
            )
        if retrieved_recorder is not None:
            retrieved_recorder(matched.method_id)
        idx = matched.to_index_dict()
        slug = str(idx["skill_name"])
        slug_peers = [m for m in methods if m.to_index_dict()["skill_name"] == slug]
        disk_winner = (
            max(slug_peers, key=skill_precedence_key) if slug_peers else matched
        )
        skill_md_text = _read_projected_skill_markdown(
            slug,
            matched,
            allow_disk=(
                disk_winner.method_id == matched.method_id
                and disk_winner.revision_sha256 == matched.revision_sha256
                and disk_winner.audience_scope == matched.audience_scope
            ),
        )
        return {
            "ok": True,
            "skill_path": idx["skill_path"],
            "skill_markdown": skill_md_text,
            "skill": matched.to_dict(),
        }

    def _skill_draft_candidate(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={
                "method_id",
                "delta_kind",
                "item_kind",
                "trigger_spec",
                "body",
                "audience_scope",
                "source_refs",
                "audience_target_id",
                "superseded_revision",
                "parent_method_ids",
                "uncertainty_note",
                "falsification_criteria",
                "metadata",
            },
            required={
                "method_id",
                "delta_kind",
                "item_kind",
                "trigger_spec",
                "body",
                "audience_scope",
                "source_refs",
            },
            label="skill_draft_candidate",
        )
        method_id = _require_str(obj["method_id"], "method_id")
        delta_kind = _require_str(obj["delta_kind"], "delta_kind")
        item_kind = _require_str(obj["item_kind"], "item_kind")
        audience_scope = _require_str(obj["audience_scope"], "audience_scope")
        body = _require_str(obj["body"], "body", max_len=65_536)
        trigger_spec = obj["trigger_spec"]
        if not isinstance(trigger_spec, dict) or not trigger_spec:
            raise MethodRefused("trigger_spec must be a nonempty JSON object")
        source_refs = obj["source_refs"]
        if not isinstance(source_refs, list) or not all(
            isinstance(r, str) for r in source_refs
        ):
            raise MethodRefused("source_refs must be an array of strings")
        parent_ids_raw = obj.get("parent_method_ids", [])
        if not isinstance(parent_ids_raw, list) or not all(
            isinstance(p, str) for p in parent_ids_raw
        ):
            raise MethodRefused("parent_method_ids must be an array of strings")
        aud_target = _optional_str(obj.get("audience_target_id"), "audience_target_id")
        sup_rev = _optional_str(obj.get("superseded_revision"), "superseded_revision")
        uncertainty = obj.get("uncertainty_note", "")
        falsification = obj.get("falsification_criteria", "")
        metadata = obj.get("metadata")
        if not isinstance(uncertainty, str) or not isinstance(falsification, str):
            raise MethodRefused(
                "uncertainty_note and falsification_criteria must be strings"
            )
        if metadata is not None and not isinstance(metadata, dict):
            raise MethodRefused("metadata must be a JSON object")

        now = _now_ns()
        with lock_ctx:
            delta = distill_method_delta(
                writer,
                claim,
                delta_kind=delta_kind,
                item_kind=item_kind,
                method_id=method_id,
                trigger_spec=trigger_spec,
                body=body,
                audience_scope=audience_scope,
                source_refs=source_refs,
                audience_target_id=aud_target,
                parent_method_ids=parent_ids_raw,
                active_snapshot_id=active_snapshot_id,
                superseded_revision=sup_rev,
                metadata=metadata,
                uncertainty_note=uncertainty,
                falsification_criteria=falsification,
                now_ns=now,
            )
        return {
            "ok": True,
            "delta_id": delta.delta_id,
            "method_id": delta.method_id,
            "revision_sha256": delta.revision_sha256,
            "status": delta.status,
            "audience_scope": delta.audience_scope,
        }

    def _skill_record_invocation(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={"method_id", "note"},
            required={"method_id"},
            label="skill_record_invocation",
        )
        method_id = _require_str(obj["method_id"], "method_id")
        note = _optional_str(obj.get("note"), "note", max_len=1000)
        writer.verify_for(writer.store, claim.company_id)
        with closing(open_reader(writer.store)) as db, read_tx(db):
            _verify_fencing_and_desk(db)
            methods = resolve_active_methods(
                db,
                claim.company_id,
                agent_id=claim.founder_id,
                duty_kind=None,
            )
        if not any(m.method_id == method_id for m in methods):
            raise MethodRefused(
                f"Cannot record invocation for inactive or out-of-scope skill '{method_id}'"
            )
        if invoked_recorder is not None:
            invoked_recorder(method_id, note)
        return {
            "ok": True,
            "method_id": method_id,
            "active_snapshot_id": active_snapshot_id,
            "recorded": True,
        }

    specs: tuple[ToolSpec, ...] = (
        ToolSpec(
            name="skill_list_active",
            description="List currently active Work Methods and Skills authorized for your Employee Desk.",
            parameters={
                "type": "object",
                "properties": {"duty_kind": {"type": "string"}},
                "required": [],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="skill_read",
            description="Read the full SKILL.md instructions, trigger, and falsification criteria for an active Skill on demand.",
            parameters={
                "type": "object",
                "properties": {
                    "method_id": {"type": "string"},
                    "skill_path": {"type": "string"},
                    "revision_sha256": {"type": "string"},
                },
                "required": [],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="skill_draft_candidate",
            description=(
                "Draft an evidence-linked candidate Method or Skill delta (ADD, PATCH, or DEPRECATE) "
                "for gated qualification without modifying live active Skills."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "method_id": {"type": "string"},
                    "delta_kind": {
                        "type": "string",
                        "enum": ["ADD", "PATCH", "DEPRECATE"],
                    },
                    "item_kind": {
                        "type": "string",
                        "enum": [
                            "work_method",
                            "improvement_procedure",
                            "helper_script",
                        ],
                    },
                    "trigger_spec": {"type": "object"},
                    "body": {"type": "string"},
                    "audience_scope": {
                        "type": "string",
                        "enum": ["one_agent", "project", "company"],
                    },
                    "source_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "audience_target_id": {"type": "string"},
                    "superseded_revision": {"type": "string"},
                    "parent_method_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "uncertainty_note": {"type": "string"},
                    "falsification_criteria": {"type": "string"},
                    "metadata": {"type": "object"},
                },
                "required": [
                    "method_id",
                    "delta_kind",
                    "item_kind",
                    "trigger_spec",
                    "body",
                    "audience_scope",
                    "source_refs",
                ],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="skill_record_invocation",
            description="Record that an active Skill guided your decision or action during this shift.",
            parameters={
                "type": "object",
                "properties": {
                    "method_id": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["method_id"],
                "additionalProperties": False,
            },
        ),
    )
    handlers: dict[str, ToolHandler] = {
        "skill_list_active": _skill_list_active,
        "skill_read": _skill_read,
        "skill_draft_candidate": _skill_draft_candidate,
        "skill_record_invocation": _skill_record_invocation,
    }
    return ToolContribution(
        specs=specs,
        handlers=handlers,
        recoverable_exceptions=(DutyRefused, MethodRefused, WorkKernelRefused),
    )
