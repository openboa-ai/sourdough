"""Core OuterToolBroker tool suite for Multi-Agent Collaboration, Messaging, Peer Decisions, and Handoffs."""

from __future__ import annotations

import json
import threading
from contextlib import closing, nullcontext
from hashlib import sha256
from time import time_ns
from typing import Any
from uuid import uuid4

from ..agent_desk import require_desk
from ..agent_runtime.protocol import ToolHandler, ToolSpec
from ..company_assets import shared_asset_for
from ..conversation_delivery import acknowledge_delivery, read_inbox
from ..conversations import Post, append_message, read_messages
from ..duty_run import DutyRefused, WorkRunClaim
from ..storage.db import open_reader, open_writer, read_tx, write_tx
from ..work_handoff import (
    HandoffOffer,
    accept_handoff,
    decline_handoff,
    discover_handoffs,
    offer_handoff,
)
from ..writer_lease import WriterLease
from .domains import ToolContribution
from .work_kernel import (
    _PEER_REQUEST_COLUMNS,
    PeerRequest,
    WorkKernelRefused,
    assert_run_fencing,
    open_peer_request,
    resolve_peer_request,
)


_MAX_EXPIRES_AT_SECONDS = (2**63 - 1) // 1_000_000_000


def _require_object(
    args: Any, *, allowed: set[str], required: set[str], label: str
) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise DutyRefused(f"{label} arguments must be a JSON object")
    keys = set(args.keys())
    if not required <= keys or not keys <= allowed:
        raise DutyRefused(f"{label} arguments do not match schema")
    return args


def _require_str(value: Any, label: str, *, max_len: int = 128) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise DutyRefused(f"{label} must be a nonempty string")
    return value


def _optional_str(value: Any, label: str, *, max_len: int = 128) -> str | None:
    if value is None:
        return None
    return _require_str(value, label, max_len=max_len)


def _require_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value >= 2**63:
        raise DutyRefused(f"{label} must be a valid integer")
    return value


def build_core_collab_tool_contribution(
    *,
    writer: WriterLease,
    claim: WorkRunClaim,
    purpose: str,
    tx_lock: threading.RLock | None = None,
    include_messaging: bool = True,
    include_peer_request: bool = True,
    include_peer_respond: bool = False,
    include_handoffs: bool = True,
    include_roster: bool = True,
    scoped_project_id: str | None = None,
) -> ToolContribution:
    """Build Core collaboration OuterToolBroker tools fenced to WorkRunClaim."""
    lock_ctx = tx_lock if tx_lock is not None else nullcontext()

    def _now_ns() -> int:
        ts = claim.current_now_ns()
        return time_ns() if ts is None else int(ts)

    def _now_sec() -> int:
        return max(0, _now_ns() // 1_000_000_000)

    def _verify_fencing() -> None:
        with closing(open_reader(writer.store)) as db, read_tx(db):
            assert_run_fencing(
                db, claim.run_id, claim.fencing_token, now_ns=claim.current_now_ns()
            )

    def _append_message(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={
                "project_id",
                "project_revision",
                "request_id",
                "body",
                "asset_id",
                "expected_membership_revision",
            },
            required={"project_id", "project_revision", "request_id", "body"},
            label="append_message",
        )
        project_id = _require_str(obj["project_id"], "project_id")
        if scoped_project_id is not None and project_id != scoped_project_id:
            raise DutyRefused(
                "Peer response messaging is scoped to the request project"
            )
        project_revision = _require_str(obj["project_revision"], "project_revision")
        request_id = _require_str(obj["request_id"], "request_id")
        body = _require_str(obj["body"], "body", max_len=1600)
        raw_asset = obj.get("asset_id", "")
        if not isinstance(raw_asset, str):
            raise DutyRefused("asset_id must be a string")
        exp_rev = _optional_str(
            obj.get("expected_membership_revision"), "expected_membership_revision"
        )
        post = Post(
            project_id=project_id,
            project_revision=project_revision,
            request_id=request_id,
            body=body,
            asset_id=raw_asset,
        )
        with lock_ctx:
            with closing(open_reader(writer.store)) as db, read_tx(db):
                exact_prior = db.execute(
                    """SELECT 1 FROM project_message
                       WHERE company_id = ? AND project_id = ?
                         AND sender_id = ? AND request_id = ?""",
                    (claim.company_id, project_id, claim.founder_id, request_id),
                ).fetchone()
                if exact_prior is None:
                    dup_body = db.execute(
                        """SELECT 1 FROM project_message
                           WHERE company_id = ? AND project_id = ?
                             AND run_id = ? AND body = ?
                             AND COALESCE(asset_id, '') = ?""",
                        (claim.company_id, project_id, claim.run_id, body, raw_asset),
                    ).fetchone()
                    if dup_body is not None:
                        raise DutyRefused(
                            "Duplicate message body in the same run requires the original request_id"
                        )
            msg = append_message(
                writer,
                claim,
                purpose,
                post,
                expected_membership_revision=exp_rev,
            )
        return {
            "ok": True,
            "message_id": msg.message_id,
            "project_id": msg.project_id,
            "sender_id": msg.sender_id,
            "run_id": msg.run_id,
            "asset_id": msg.asset_id,
        }

    def _read_messages(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={"project_id", "after", "expected_membership_revision"},
            required={"project_id"},
            label="read_messages",
        )
        project_id = _require_str(obj["project_id"], "project_id")
        if scoped_project_id is not None and project_id != scoped_project_id:
            raise DutyRefused(
                "Peer response messaging is scoped to the request project"
            )
        after = obj.get("after", "")
        if not isinstance(after, str):
            raise DutyRefused("after must be a string")
        exp_rev = _optional_str(
            obj.get("expected_membership_revision"), "expected_membership_revision"
        )
        _verify_fencing()
        msgs, next_cursor = read_messages(
            writer,
            claim,
            purpose,
            project_id,
            after,
            expected_membership_revision=exp_rev,
        )
        return {
            "ok": True,
            "messages": [
                {
                    "message_id": m.message_id,
                    "project_id": m.project_id,
                    "sender_id": m.sender_id,
                    "run_id": m.run_id,
                    "body": m.body,
                    "asset_id": m.asset_id,
                }
                for m in msgs
            ],
            "next_cursor": next_cursor,
        }

    def _read_inbox(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args, allowed={"after"}, required=set(), label="read_inbox"
        )
        after = obj.get("after", "")
        if not isinstance(after, str):
            raise DutyRefused("after must be a string")
        _verify_fencing()
        deliveries, next_cursor = read_inbox(
            writer, claim, purpose, after, project_id=scoped_project_id
        )
        return {
            "ok": True,
            "deliveries": [
                {
                    "delivery_id": d.delivery_id,
                    "message_id": d.message_id,
                    "project_id": d.project_id,
                    "sender_id": d.sender_id,
                    "read_run": d.read_run,
                }
                for d in deliveries
            ],
            "next_cursor": next_cursor,
        }

    def _acknowledge_delivery(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={"delivery_id", "expected_membership_revision"},
            required={"delivery_id"},
            label="acknowledge_delivery",
        )
        delivery_id = _require_str(obj["delivery_id"], "delivery_id")
        exp_rev = _optional_str(
            obj.get("expected_membership_revision"), "expected_membership_revision"
        )
        with lock_ctx:
            if scoped_project_id is not None:
                with closing(open_reader(writer.store)) as db, read_tx(db):
                    proj_row = db.execute(
                        """SELECT m.project_id FROM project_delivery d
                           JOIN project_message m ON m.message_id = d.message_id
                           WHERE d.company_id = ? AND d.delivery_id = ?""",
                        (claim.company_id, delivery_id),
                    ).fetchone()
                    if proj_row is not None and str(proj_row[0]) != scoped_project_id:
                        raise DutyRefused(
                            "Peer response messaging is scoped to the request project"
                        )
            delivery = acknowledge_delivery(
                writer,
                claim,
                purpose,
                delivery_id,
                expected_membership_revision=exp_rev,
            )
        return {
            "ok": True,
            "delivery_id": delivery.delivery_id,
            "message_id": delivery.message_id,
            "project_id": delivery.project_id,
            "sender_id": delivery.sender_id,
            "read_run": delivery.read_run,
        }

    def _request_peer_response(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={
                "request_key",
                "recipient_id",
                "question",
                "expected_response",
                "expires_at",
                "project_id",
                "asset_id",
                "asset_revision",
                "evidence_refs",
            },
            required={
                "request_key",
                "recipient_id",
                "question",
                "expected_response",
                "expires_at",
            },
            label="request_peer_response",
        )
        request_key = _require_str(obj["request_key"], "request_key")
        recipient_id = _require_str(obj["recipient_id"], "recipient_id")
        question = _require_str(obj["question"], "question", max_len=1600)
        expected_response = _require_str(
            obj["expected_response"], "expected_response", max_len=500
        )
        expires_at = _require_int(obj["expires_at"], "expires_at", minimum=1)
        if expires_at > _MAX_EXPIRES_AT_SECONDS:
            raise DutyRefused(
                "expires_at must be in Unix epoch seconds (within 64-bit nanosecond range)"
            )
        project_id = _optional_str(obj.get("project_id"), "project_id")
        raw_asset_id = obj.get("asset_id")
        raw_asset_rev = obj.get("asset_revision")
        if raw_asset_id is not None and not isinstance(raw_asset_id, str):
            raise DutyRefused("asset_id must be a string")
        if raw_asset_rev is not None and not isinstance(raw_asset_rev, str):
            raise DutyRefused("asset_revision must be a string")
        raw_refs = obj.get("evidence_refs")
        refs: list[str] = []
        if raw_refs is not None:
            if not isinstance(raw_refs, list) or not all(
                isinstance(item, str) and item.strip() for item in raw_refs
            ):
                raise DutyRefused("evidence_refs must be a list of nonempty strings")
            refs.extend(raw_refs)

        now_ns = _now_ns()
        deadline_ns = expires_at * 1_000_000_000
        with (
            lock_ctx,
            closing(open_writer(writer.store, lease=writer)) as db,
            write_tx(db),
        ):
            require_desk(db, claim, purpose)
            if isinstance(raw_asset_id, str) and raw_asset_id.strip():
                asset_id = raw_asset_id.strip()
                asset = shared_asset_for(db, claim.founder_id, asset_id)
                asset_rev = (
                    raw_asset_rev.strip()
                    if isinstance(raw_asset_rev, str) and raw_asset_rev.strip()
                    else sha256(asset.content.encode("utf-8")).hexdigest()
                )
                ref = f"shared_publication:{asset_id}@{asset_rev}"
                if ref not in refs:
                    refs.append(ref)
                if project_id is None:
                    project_id = asset.project_id
            req, _ = open_peer_request(
                db,
                company_id=claim.company_id,
                requester_work_item_id=claim.duty_id,
                requester_run_id=claim.run_id,
                fencing_token=claim.fencing_token,
                recipient_agent_id=recipient_id,
                question=question,
                expected_decision=expected_response,
                evidence_refs=refs,
                deadline_ns=deadline_ns,
                request_id=uuid4().hex,
                request_key=request_key,
                project_id=project_id,
                now_ns=now_ns,
            )
        return {
            "ok": True,
            "request_id": req.request_id,
            "request_key": req.request_key,
            "response_work_item_id": req.response_work_item_id,
            "wait_event_key": f"peer_response:{req.request_id}",
            "deadline_ns": req.deadline_ns,
            "state": req.state,
        }

    def _respond_peer_request(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={"outcome", "answer", "evidence_refs"},
            required={"outcome", "answer"},
            label="respond_peer_request",
        )
        outcome = _require_str(obj["outcome"], "outcome")
        if outcome not in ("answered", "declined"):
            raise DutyRefused("outcome must be 'answered' or 'declined'")
        answer = _require_str(obj["answer"], "answer", max_len=1600)
        raw_refs = obj.get("evidence_refs")
        refs: list[str] | None = None
        if raw_refs is not None:
            if not isinstance(raw_refs, list) or not all(
                isinstance(item, str) and item.strip() for item in raw_refs
            ):
                raise DutyRefused("evidence_refs must be a list of nonempty strings")
            refs = list(raw_refs)

        now_ns = _now_ns()
        with (
            lock_ctx,
            closing(open_writer(writer.store, lease=writer)) as db,
            write_tx(db),
        ):
            require_desk(db, claim, purpose)
            row = db.execute(
                f"""SELECT {_PEER_REQUEST_COLUMNS}
                    FROM work_peer_requests
                    WHERE company_id = ? AND response_work_item_id = ?""",
                (claim.company_id, claim.duty_id),
            ).fetchone()
            if row is None:
                raise DutyRefused(
                    "No peer request is linked to this response work item"
                )
            peer = PeerRequest(*row)
            if peer.recipient_agent_id != claim.founder_id:
                raise DutyRefused("Only the named recipient Agent may respond")
            if refs:
                for ref in refs:
                    asset_id = (
                        ref.removeprefix("shared_publication:").split("@", 1)[0]
                        if ref.startswith("shared_publication:")
                        else ref
                    )
                    shared_asset_for(db, claim.founder_id, asset_id)
            if peer.state != "open":
                saved = (
                    json.loads(peer.response_json)
                    if peer.response_json is not None
                    else {}
                )
                saved_answer = saved.get("answer") if isinstance(saved, dict) else None
                if (peer.state, saved_answer, peer.answered_run_id) != (
                    outcome,
                    answer,
                    claim.run_id,
                ):
                    raise DutyRefused("Peer outcome conflicts with the original result")
                return {
                    "ok": True,
                    "request_id": peer.request_id,
                    "outcome": peer.state,
                    "response_work_item_id": peer.response_work_item_id,
                }
            payload: dict[str, Any] = {"answer": answer}
            if refs is not None:
                payload["evidence_refs"] = refs
            resolved, applied = resolve_peer_request(
                db,
                company_id=claim.company_id,
                request_id=peer.request_id,
                actor_id=claim.founder_id,
                resolution_state=outcome,
                response_payload=payload,
                response_run_id=claim.run_id,
                response_fencing_token=claim.fencing_token,
                now_ns=now_ns,
            )
            if not applied and (resolved.state, resolved.answered_run_id) != (
                outcome,
                claim.run_id,
            ):
                raise DutyRefused("Peer request is no longer open")
        return {
            "ok": True,
            "request_id": resolved.request_id,
            "outcome": resolved.state,
            "response_work_item_id": resolved.response_work_item_id,
        }

    def _offer_handoff(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={
                "request_id",
                "recipient_id",
                "asset_id",
                "expires_at",
                "expected_result",
            },
            required={
                "request_id",
                "recipient_id",
                "asset_id",
                "expires_at",
                "expected_result",
            },
            label="offer_handoff",
        )
        expires_at = _require_int(obj["expires_at"], "expires_at", minimum=1)
        if expires_at > _MAX_EXPIRES_AT_SECONDS:
            raise DutyRefused(
                "expires_at must be in Unix epoch seconds (within 64-bit nanosecond range)"
            )
        offer = HandoffOffer(
            request_id=_require_str(obj["request_id"], "request_id"),
            recipient_id=_require_str(obj["recipient_id"], "recipient_id"),
            asset_id=_require_str(obj["asset_id"], "asset_id"),
            expires_at=expires_at,
            expected_result=_require_str(
                obj["expected_result"], "expected_result", max_len=500
            ),
        )
        with lock_ctx:
            offer_id = offer_handoff(writer, claim, offer, now=_now_sec())
        return {"ok": True, "offer_id": offer_id, "state": "offered"}

    def _discover_handoffs(args: dict[str, Any]) -> dict[str, Any]:
        _require_object(args, allowed=set(), required=set(), label="discover_handoffs")
        _verify_fencing()
        offers = discover_handoffs(writer, claim, purpose, now=_now_sec())
        return {"ok": True, "offers": offers}

    def _accept_handoff(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={"offer_id", "expected_state_version"},
            required={"offer_id"},
            label="accept_handoff",
        )
        offer_id = _require_str(obj["offer_id"], "offer_id")
        exp_ver = obj.get("expected_state_version")
        if exp_ver is not None:
            exp_ver = _require_int(exp_ver, "expected_state_version", minimum=1)
        with lock_ctx:
            successor_id = accept_handoff(
                writer,
                claim,
                purpose,
                offer_id,
                now=_now_sec(),
                expected_state_version=exp_ver,
            )
        return {
            "ok": True,
            "offer_id": offer_id,
            "successor_work_item_id": successor_id,
            "state": "accepted",
        }

    def _decline_handoff(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={"offer_id", "reason"},
            required={"offer_id"},
            label="decline_handoff",
        )
        offer_id = _require_str(obj["offer_id"], "offer_id")
        raw_reason = obj.get("reason", "")
        if not isinstance(raw_reason, str) or len(raw_reason) > 500:
            raise DutyRefused("reason must be a bounded string")
        with lock_ctx:
            declined_id = decline_handoff(
                writer,
                claim,
                purpose,
                offer_id,
                now=_now_sec(),
                reason=raw_reason,
            )
        return {"ok": True, "offer_id": declined_id, "state": "declined"}

    def _org_list_roster(args: dict[str, Any]) -> dict[str, Any]:
        obj = _require_object(
            args,
            allowed={"include_archived"},
            required=set(),
            label="org_list_roster",
        )
        include_archived = obj.get("include_archived", False)
        if type(include_archived) is not bool:
            raise DutyRefused("include_archived must be a boolean")
        _verify_fencing()
        with closing(open_reader(writer.store)) as db, read_tx(db):
            agent_rows = db.execute(
                """SELECT a.id,
                          COALESCE(p.display_name, a.id),
                          COALESCE(p.role_label, CASE WHEN f.founder_id = a.id THEN 'Founder-CEO' ELSE 'Agent' END),
                          CASE WHEN f.founder_id = a.id THEN 1 ELSE 0 END,
                          CASE
                              WHEN ar.agent_id IS NOT NULL THEN 'archived'
                              WHEN EXISTS (
                                  SELECT 1 FROM standing_grant g
                                  WHERE g.agent_id = a.id
                                    AND trim(g.scope) != ''
                                    AND lower(trim(g.scope)) != 'revoked'
                              ) THEN 'active'
                              ELSE 'revoked'
                          END AS agent_status
                   FROM agent a
                   LEFT JOIN agent_profile p
                     ON p.company_id = a.company_id AND p.agent_id = a.id
                   LEFT JOIN founding_receipt f
                     ON f.company_id = a.company_id AND f.founder_id = a.id
                   LEFT JOIN agent_archive ar
                     ON ar.company_id = a.company_id AND ar.agent_id = a.id
                   WHERE a.company_id = ?
                   ORDER BY a.id""",
                (claim.company_id,),
            ).fetchall()
            agents = [
                {
                    "agent_id": str(aid),
                    "display_name": str(display_name),
                    "role_label": str(role_label),
                    "is_ceo": bool(is_ceo),
                    "status": str(status),
                }
                for aid, display_name, role_label, is_ceo, status in agent_rows
                if include_archived or str(status) != "archived"
            ]
            project_rows = db.execute(
                """SELECT project_id, revision, members, publishers
                   FROM shared_project
                   WHERE company_id = ?
                     AND EXISTS (SELECT 1 FROM json_each(members) WHERE value = ?)
                   ORDER BY project_id""",
                (claim.company_id, claim.founder_id),
            ).fetchall()
            projects = [
                {
                    "project_id": str(pid),
                    "revision": str(rev),
                    "members": list(json.loads(str(members_json))),
                    "can_publish": claim.founder_id in json.loads(str(publishers_json)),
                }
                for pid, rev, members_json, publishers_json in project_rows
            ]
        return {"ok": True, "agents": agents, "projects": projects}

    specs: list[ToolSpec] = []
    handlers: dict[str, ToolHandler] = {}

    def _add(
        name: str,
        description: str,
        properties: dict[str, Any],
        required: list[str],
        handler: ToolHandler,
    ) -> None:
        specs.append(
            ToolSpec(
                name=name,
                description=description,
                parameters={
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            )
        )
        handlers[name] = handler

    if include_messaging:
        _add(
            "append_message",
            "Append a message to an authorized shared project discussion thread without creating or transferring work.",
            {
                "project_id": {"type": "string"},
                "project_revision": {"type": "string"},
                "request_id": {"type": "string"},
                "body": {"type": "string"},
                "asset_id": {"type": "string"},
                "expected_membership_revision": {"type": "string"},
            },
            ["project_id", "project_revision", "request_id", "body"],
            _append_message,
        )
        _add(
            "read_messages",
            "Read ordered messages from an authorized shared project thread with bounded cursor pagination.",
            {
                "project_id": {"type": "string"},
                "after": {"type": "string"},
                "expected_membership_revision": {"type": "string"},
            },
            ["project_id"],
            _read_messages,
        )
        _add(
            "read_inbox",
            "List project message delivery references for this Agent without acknowledging them or creating work.",
            {"after": {"type": "string"}},
            [],
            _read_inbox,
        )
        _add(
            "acknowledge_delivery",
            "Explicitly acknowledge receipt of a project message delivery for an accessible project.",
            {
                "delivery_id": {"type": "string"},
                "expected_membership_revision": {"type": "string"},
            },
            ["delivery_id"],
            _acknowledge_delivery,
        )

    if include_peer_request:
        _add(
            "request_peer_response",
            "Request a scoped decision from an authorized colleague and yield this run into a zero-compute wait (expires_at in Unix epoch seconds).",
            {
                "request_key": {"type": "string"},
                "recipient_id": {"type": "string"},
                "question": {"type": "string"},
                "expected_response": {"type": "string"},
                "expires_at": {
                    "type": "integer",
                    "description": "Deadline in Unix epoch seconds.",
                },
                "project_id": {"type": "string"},
                "asset_id": {"type": "string"},
                "asset_revision": {"type": "string"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
            },
            [
                "request_key",
                "recipient_id",
                "question",
                "expected_response",
                "expires_at",
            ],
            _request_peer_response,
        )

    if include_peer_respond:
        _add(
            "respond_peer_request",
            "Answer or decline the open blocking peer decision request assigned to this peer response work item.",
            {
                "outcome": {"type": "string", "enum": ["answered", "declined"]},
                "answer": {"type": "string"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
            },
            ["outcome", "answer"],
            _respond_peer_request,
        )

    if include_handoffs:
        _add(
            "offer_handoff",
            "Offer an explicit responsibility handoff referencing a shared asset published during this work item (expires_at in Unix epoch seconds).",
            {
                "request_id": {"type": "string"},
                "recipient_id": {"type": "string"},
                "asset_id": {"type": "string"},
                "expires_at": {
                    "type": "integer",
                    "description": "Offer expiration deadline in Unix epoch seconds.",
                },
                "expected_result": {"type": "string"},
            },
            [
                "request_id",
                "recipient_id",
                "asset_id",
                "expires_at",
                "expected_result",
            ],
            _offer_handoff,
        )
        _add(
            "discover_handoffs",
            "Discover open actionable handoff offers addressed to this Agent.",
            {},
            [],
            _discover_handoffs,
        )
        _add(
            "accept_handoff",
            "Atomically accept an open handoff offer via CAS, fencing the sender and scheduling one successor item.",
            {
                "offer_id": {"type": "string"},
                "expected_state_version": {"type": "integer"},
            },
            ["offer_id"],
            _accept_handoff,
        )
        _add(
            "decline_handoff",
            "Atomically decline an open handoff offer addressed to this Agent without transferring ownership.",
            {
                "offer_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            ["offer_id"],
            _decline_handoff,
        )

    if include_roster:
        _add(
            "org_list_roster",
            "Read-only view of company Agent roles/status and shared projects visible to the caller.",
            {"include_archived": {"type": "boolean"}},
            [],
            _org_list_roster,
        )

    return ToolContribution(
        specs=tuple(specs),
        handlers=handlers,
        recoverable_exceptions=(DutyRefused, WorkKernelRefused),
    )
