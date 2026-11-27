"""Own $0 deterministic Agent episodic memory inside company.sqlite (STRICT)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from time import time_ns
from typing import Any
from uuid import uuid4

_OUTCOME_STATUSES = frozenset({"completed", "waiting", "blocked", "failed"})
_MAX_SUMMARY_CHARS = 4000


class MemoryRefused(Exception):
    """Episodic memory operation violated scoping, identity, or validation rules."""


@dataclass(frozen=True)
class AgentEpisode:
    episode_id: str
    company_id: str
    agent_id: str
    work_item_id: str
    run_id: str
    duty_kind: str
    context_digest: str
    active_snapshot_id: str | None
    outcome_status: str
    summary_text: str
    decisions_json: str
    evidence_refs_json: str
    receipts_json: str
    tool_calls_count: int
    input_tokens: int
    output_tokens: int
    cost_minor_units: int
    recorded_at_ns: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "company_id": self.company_id,
            "agent_id": self.agent_id,
            "work_item_id": self.work_item_id,
            "run_id": self.run_id,
            "duty_kind": self.duty_kind,
            "context_digest": self.context_digest,
            "active_snapshot_id": self.active_snapshot_id,
            "outcome_status": self.outcome_status,
            "summary_text": self.summary_text,
            "decisions": json.loads(self.decisions_json),
            "evidence_refs": json.loads(self.evidence_refs_json),
            "receipts": json.loads(self.receipts_json),
            "tool_calls_count": self.tool_calls_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_minor_units": self.cost_minor_units,
            "recorded_at_ns": self.recorded_at_ns,
        }


_EPISODE_COLUMNS = """episode_id, company_id, agent_id, work_item_id, run_id,
    duty_kind, context_digest, active_snapshot_id, outcome_status, summary_text,
    decisions_json, evidence_refs_json, receipts_json, tool_calls_count,
    input_tokens, output_tokens, cost_minor_units, recorded_at_ns"""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def record_episode(
    connection: sqlite3.Connection,
    *,
    company_id: str,
    agent_id: str,
    work_item_id: str,
    run_id: str,
    duty_kind: str,
    context_digest: str,
    outcome_status: str,
    summary_text: str,
    active_snapshot_id: str | None = None,
    decisions: Sequence[Any] = (),
    evidence_refs: Sequence[str] = (),
    receipts: Sequence[Any] = (),
    tool_calls_count: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_minor_units: int = 0,
    recorded_at_ns: int | None = None,
    episode_id: str | None = None,
) -> AgentEpisode:
    """Record a $0 deterministic episode for one run, idempotently by (company_id, run_id)."""
    if outcome_status not in _OUTCOME_STATUSES:
        raise MemoryRefused(f"Unsupported episode outcome_status: {outcome_status}")
    for field_name, val in (
        ("company_id", company_id),
        ("agent_id", agent_id),
        ("work_item_id", work_item_id),
        ("run_id", run_id),
        ("duty_kind", duty_kind),
        ("context_digest", context_digest),
    ):
        if not isinstance(val, str) or not val.strip():
            raise MemoryRefused(f"{field_name} must be a nonempty string")
    if not isinstance(summary_text, str):
        raise MemoryRefused("summary_text must be a string")
    for count_name, count_val in (
        ("tool_calls_count", tool_calls_count),
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("cost_minor_units", cost_minor_units),
    ):
        if type(count_val) is not int or count_val < 0:
            raise MemoryRefused(f"{count_name} must be a nonnegative integer")

    existing = connection.execute(
        f"""SELECT {_EPISODE_COLUMNS}
            FROM agent_episode
            WHERE company_id = ? AND run_id = ?""",
        (company_id, run_id),
    ).fetchone()
    if existing is not None:
        return AgentEpisode(*existing)

    timestamp = time_ns() if recorded_at_ns is None else recorded_at_ns
    if type(timestamp) is not int or timestamp <= 0:
        raise MemoryRefused("recorded_at_ns must be a positive integer")
    bounded_summary = summary_text.strip()[:_MAX_SUMMARY_CHARS]
    decisions_json = _canonical_json(list(decisions))
    evidence_refs_json = _canonical_json(list(evidence_refs))
    receipts_json = _canonical_json(list(receipts))
    eid = episode_id or uuid4().hex

    connection.execute(
        """INSERT INTO agent_episode (
               episode_id, company_id, agent_id, work_item_id, run_id,
               duty_kind, context_digest, active_snapshot_id, outcome_status,
               summary_text, decisions_json, evidence_refs_json, receipts_json,
               tool_calls_count, input_tokens, output_tokens, cost_minor_units,
               recorded_at_ns
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            eid,
            company_id,
            agent_id,
            work_item_id,
            run_id,
            duty_kind,
            context_digest,
            active_snapshot_id,
            outcome_status,
            bounded_summary,
            decisions_json,
            evidence_refs_json,
            receipts_json,
            tool_calls_count,
            input_tokens,
            output_tokens,
            cost_minor_units,
            timestamp,
        ),
    )
    return AgentEpisode(
        episode_id=eid,
        company_id=company_id,
        agent_id=agent_id,
        work_item_id=work_item_id,
        run_id=run_id,
        duty_kind=duty_kind,
        context_digest=context_digest,
        active_snapshot_id=active_snapshot_id,
        outcome_status=outcome_status,
        summary_text=bounded_summary,
        decisions_json=decisions_json,
        evidence_refs_json=evidence_refs_json,
        receipts_json=receipts_json,
        tool_calls_count=tool_calls_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_minor_units=cost_minor_units,
        recorded_at_ns=timestamp,
    )


def read_episode(
    connection: sqlite3.Connection,
    company_id: str,
    agent_id: str,
    episode_id: str,
) -> AgentEpisode:
    """Read one episode strictly scoped to (company_id, agent_id)."""
    if not isinstance(episode_id, str) or not episode_id.strip():
        raise MemoryRefused("episode_id must be a nonempty string")
    row = connection.execute(
        f"""SELECT {_EPISODE_COLUMNS}
            FROM agent_episode
            WHERE company_id = ? AND agent_id = ? AND episode_id = ?""",
        (company_id, agent_id, episode_id),
    ).fetchone()
    if row is None:
        raise MemoryRefused("Episode is unavailable for this Agent")
    return AgentEpisode(*row)


def list_recent_episodes(
    connection: sqlite3.Connection,
    company_id: str,
    agent_id: str,
    *,
    limit: int = 5,
) -> tuple[AgentEpisode, ...]:
    """List recent episodes strictly scoped to (company_id, agent_id)."""
    if type(limit) is not int or limit < 1:
        raise MemoryRefused("limit must be a positive integer")
    bounded = min(limit, 50)
    rows = connection.execute(
        f"""SELECT {_EPISODE_COLUMNS}
            FROM agent_episode
            WHERE company_id = ? AND agent_id = ?
            ORDER BY recorded_at_ns DESC, episode_id DESC
            LIMIT ?""",
        (company_id, agent_id, bounded),
    ).fetchall()
    return tuple(AgentEpisode(*row) for row in rows)
