"""Shift tool receipt sanitization, JSONL trace logging, offloading, and SQLite persistence."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from ...agent_runtime.protocol import ToolSpec
from ...duty_run import DutyRefused
from ...harness import (
    DEFAULT_OFFLOAD_THRESHOLD_BYTES,
    AgentSession,
    offload_large_output,
)
from ...storage.db import write_uow
from ...storage.kernel import append_audit_event
from ...writer_lease import WriterLease
from ..desk_tools import (
    _DEFAULT_OFFLOAD_READ_BYTES,
    _MAX_OFFLOAD_READ_BYTES,
    read_offloaded_log_slice,
)
from ..domains import ToolContribution
from ..registry import RuntimeRefused
from .policy import (
    _SAFE_DESK_RECEIPT_FIELDS,
    classify_tool_flow,
    is_authority_refusal_message,
    is_private_desk_tool,
    is_private_error_tool,
    is_private_receipt_tool,
)

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def ensure_private_dir(path: Path, stop_at: Path) -> None:
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


def append_trace_line(store: Path, trace_path: Path, payload: dict[str, Any]) -> None:
    root = store.resolve(strict=False).parent
    ensure_private_dir(trace_path.parent, root)
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


def _sanitize_receipt_result_json(
    tool_name: str,
    result_digest: str,
    raw_result_json: str,
    *,
    status: str = "ok",
    spec: ToolSpec | None = None,
) -> str:
    """Keep private Desk payloads and private-tool refusal details out of company.sqlite."""
    is_private_desk = is_private_receipt_tool(tool_name, spec)
    is_private_err = is_private_error_tool(tool_name, spec)
    if not is_private_desk and not (is_private_err and status != "ok"):
        return raw_result_json
    try:
        parsed = json.loads(raw_result_json)
    except ValueError, TypeError:
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
    tool_name: str,
    error_type: str | None,
    raw_error_message: str | None,
    *,
    spec: ToolSpec | None = None,
) -> str | None:
    """Keep private Desk filenames, draft identifiers, and skill details out of company.sqlite receipts."""
    if raw_error_message is None:
        return None
    if is_private_error_tool(tool_name, spec):
        err_kind = str(error_type or "Error")
        digest = sha256(str(raw_error_message).encode("utf-8")).hexdigest()[:12]
        prefix = (
            "redacted_private_authority_error"
            if is_authority_refusal_message(error_type, raw_error_message)
            else "redacted_private_error"
        )
        return f"{prefix}:{err_kind}:{digest}"
    return str(raw_error_message)


def build_public_offload_reader_contribution(
    store: Path,
    agent_id: str,
    public_offloaded_paths: set[str],
) -> ToolContribution:
    """Expose a bounded reader strictly for public tool outputs offloaded during a Desk-isolated shift."""

    def _read_public_offloaded_output(args: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise DutyRefused("read_offloaded_output arguments must be a JSON object")
        unknown = set(args) - {"log_path", "offset", "limit"}
        if unknown or "log_path" not in args:
            raise DutyRefused("read_offloaded_output arguments do not match schema")
        lp = args["log_path"]
        if not isinstance(lp, str) or not lp.strip():
            raise DutyRefused("log_path must be a nonempty string")
        if lp not in public_offloaded_paths:
            raise DutyRefused(
                "Offloaded log path is not an authorized public result of this shift"
            )
        return read_offloaded_log_slice(
            store,
            agent_id,
            lp,
            offset=args.get("offset", 0),
            limit=args.get("limit", _DEFAULT_OFFLOAD_READ_BYTES),
        )

    return ToolContribution(
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


class ShiftReceiptStore:
    """Manages sanitized tool_call_receipt SQLite rows, JSONL shift traces, and offloaded outputs."""

    def __init__(
        self,
        *,
        writer: WriterLease,
        session: AgentSession,
        specs_by_name: Mapping[str, ToolSpec],
        public_offloaded_paths: set[str],
        stable_prefix_sha256: str,
        work_item_id: str = "",
        tx_lock: Any = None,
    ) -> None:
        self._writer = writer
        self._session = session
        self._specs_by_name = dict(specs_by_name)
        self._public_offloaded_paths = public_offloaded_paths
        self._stable_prefix_sha256 = stable_prefix_sha256
        self._work_item_id = work_item_id
        self._tx_lock = tx_lock

    def record_receipt(self, receipt: dict[str, Any]) -> None:
        tool_name = str(receipt["tool_name"])
        spec = self._specs_by_name.get(tool_name)
        effective_flow = classify_tool_flow(tool_name, spec)
        legacy_flow = classify_tool_flow(tool_name, None)
        stored_result_json = _sanitize_receipt_result_json(
            tool_name,
            str(receipt["result_digest"]),
            str(receipt["result_json"]),
            status=str(receipt["status"]),
            spec=spec,
        )
        stored_error_message = _sanitize_receipt_error_message(
            tool_name,
            None if receipt["error_type"] is None else str(receipt["error_type"]),
            None if receipt["error_message"] is None else str(receipt["error_message"]),
            spec=spec,
        )
        with write_uow(self._writer, tx_lock=self._tx_lock) as db:
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
            if (
                self._work_item_id
                and effective_flow != "neutral"
                and effective_flow != legacy_flow
            ):
                append_audit_event(
                    db,
                    company_id=str(receipt["company_id"]),
                    domain="work",
                    entity_type="work_item",
                    entity_id=self._work_item_id,
                    event_type="work.tool_flow_recorded",
                    actor_id=str(receipt["agent_id"]),
                    run_id=str(receipt["run_id"]),
                    payload={
                        "call_id": str(receipt["call_id"]),
                        "flow_class": effective_flow,
                    },
                    recorded_at_ns=int(receipt["recorded_at_ns"]),
                )

    def record_trace(self, trace_payload: dict[str, Any]) -> None:
        enriched = {
            **trace_payload,
            "stable_prefix_sha256": self._stable_prefix_sha256,
        }
        append_trace_line(self._writer.store, self._session.trace_path, enriched)

    def offload(
        self, seq: int, tool_name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if tool_name in ("desk_read_offloaded_log", "read_offloaded_output"):
            return payload
        res = offload_large_output(
            self._writer.store,
            self._session,
            tool_name,
            payload,
            seq=seq,
            max_inline_bytes=DEFAULT_OFFLOAD_THRESHOLD_BYTES,
        )
        spec = self._specs_by_name.get(tool_name)
        if (
            res.get("offloaded") is True
            and isinstance(res.get("log_path"), str)
            and not is_private_desk_tool(tool_name, spec)
        ):
            self._public_offloaded_paths.add(res["log_path"])
        return res
