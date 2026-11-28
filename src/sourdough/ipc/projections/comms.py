"""Read-only SQLite projection for Board-to-CEO and internal company conversations."""

from __future__ import annotations

from pathlib import Path
from time import time_ns
from typing import Any

from ...board_control import AuthorizeBoard, resolve_board_access
from ...company import FoundingRefused, _identity
from ...storage.kernel import max_event_seq
from ._shared import (
    PROJECTION_MESSAGES,
    _agent_directory,
    _json_loads_safe,
    _open_projection_snapshot,
)


def read_messages_projection(
    store: Path,
    company_id: str,
    *,
    authorize_board: AuthorizeBoard | None = None,
    principal_hint: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Read Area 3 Unified Company Messages: writable CEO 1:1 + read-only internal channels."""
    with _open_projection_snapshot(store) as db:
        if _identity(db) != company_id:
            raise FoundingRefused("Company identity mismatch")
        as_of_ns = time_ns()
        event_seq = max_event_seq(db, company_id)
        charter_revision = str(
            db.execute(
                "SELECT charter_revision FROM company WHERE id = ?", (company_id,)
            ).fetchone()[0]
        )
        directory = _agent_directory(db, company_id)
        access = resolve_board_access(
            db,
            company_id,
            authorize_board=authorize_board,
            principal_hint=principal_hint,
        )
        ceo_agent = next((a for a in directory.values() if a["is_ceo"]), None)
        ceo_id = ceo_agent["agent_id"] if ceo_agent else ""

        ceo_msgs = [
            {
                "message_id": str(r[0]),
                "sender_kind": str(r[1]),
                "sender_id": str(r[2]),
                "sender_name": (
                    "Board"
                    if str(r[1]) == "board"
                    else (ceo_agent["display_name"] if ceo_agent else "CEO")
                ),
                "body": str(r[3]),
                "context_ref": (
                    {
                        "kind": str(r[4]),
                        "id": str(r[5]),
                        "label": str(r[6] or r[5]),
                    }
                    if r[4] and r[5]
                    else None
                ),
                "command_id": None if r[7] is None else str(r[7]),
                "delivery_state": str(r[8]),
                "recorded_at_ns": int(r[9]),
            }
            for r in db.execute(
                """SELECT message_id, sender_kind, sender_id, body,
                          context_kind, context_id, context_label, command_id,
                          delivery_state, recorded_at_ns
                   FROM ceo_message
                   WHERE company_id = ?
                   ORDER BY recorded_at_ns ASC, rowid ASC""",
                (company_id,),
            ).fetchall()
        ]

        # Read-only internal channels from shared_project + project_message
        channels: list[dict[str, Any]] = []
        for proj in db.execute(
            """SELECT project_id, revision, members, publishers
               FROM shared_project
               WHERE company_id = ?
               ORDER BY rowid ASC""",
            (company_id,),
        ).fetchall():
            pid = str(proj[0])
            member_ids = _json_loads_safe(str(proj[2]), [])
            member_profiles = [
                {
                    "agent_id": mid,
                    "display_name": directory.get(mid, {}).get("display_name", mid),
                    "role_label": directory.get(mid, {}).get("role_label", "Agent"),
                    "avatar_variant": directory.get(mid, {}).get(
                        "avatar_variant", "wide-oval-top"
                    ),
                }
                for mid in (member_ids if isinstance(member_ids, list) else [])
            ]
            msgs = [
                {
                    "message_id": str(m[0]),
                    "sender_id": str(m[1]),
                    "sender_name": directory.get(str(m[1]), {}).get(
                        "display_name", str(m[1])
                    ),
                    "sender_role": directory.get(str(m[1]), {}).get(
                        "role_label", "Agent"
                    ),
                    "avatar_variant": directory.get(str(m[1]), {}).get(
                        "avatar_variant", "wide-oval-top"
                    ),
                    "body": str(m[2]),
                    "asset_id": None if m[3] is None else str(m[3]),
                    "audience": _json_loads_safe(str(m[4]), []),
                    "delivery_count": int(m[5]),
                    "read_count": int(m[6]),
                }
                for m in db.execute(
                    """SELECT pm.message_id, pm.sender_id, pm.body, pm.asset_id,
                              pm.audience,
                              (SELECT COUNT(*) FROM project_delivery pd WHERE pd.message_id = pm.message_id),
                              (SELECT COUNT(*) FROM project_delivery pd WHERE pd.message_id = pm.message_id AND pd.read_run_id IS NOT NULL)
                       FROM project_message pm
                       WHERE pm.company_id = ? AND pm.project_id = ?
                       ORDER BY pm.rowid ASC""",
                    (company_id, pid),
                ).fetchall()
            ]
            channels.append(
                {
                    "channel_id": pid,
                    "name": f"#{pid}",
                    "revision": str(proj[1]),
                    "read_only": True,
                    "members": member_profiles,
                    "message_count": len(msgs),
                    "messages": msgs,
                }
            )

        return (
            {
                "projection": PROJECTION_MESSAGES,
                "company_id": company_id,
                "charter_revision": charter_revision,
                "as_of_ns": as_of_ns,
                "event_seq": event_seq,
                "ceo_thread": {
                    "channel_id": "ceo-direct",
                    "ceo_agent_id": ceo_id,
                    "ceo_display_name": ceo_agent["display_name"]
                    if ceo_agent
                    else "Founder · CEO",
                    "ceo_role_label": ceo_agent["role_label"] if ceo_agent else "CEO",
                    "ceo_avatar_variant": (
                        ceo_agent["avatar_variant"] if ceo_agent else "wide-oval-top"
                    ),
                    "writable": access.role == "board_control",
                    "advisory_notice": "Advisory message to CEO · Does not directly modify authority, mutate mandates, or retire employees.",
                    "messages": ceo_msgs,
                },
                "internal_channels": channels,
            },
            event_seq,
        )
