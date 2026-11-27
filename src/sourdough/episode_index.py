"""Physically separate, rebuildable FTS5 index for Agent-scoped episode search."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from typing import Any

from .memory import AgentEpisode
from .storage.db import BUSY_TIMEOUT_MS, connect_sqlite, open_reader, read_tx

_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class EpisodeIndexRefused(Exception):
    """Derived episode index operation failed path safety or validation."""


def _safe_segment(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not _SAFE_SEGMENT.fullmatch(value)
        or ".." in value
    ):
        raise EpisodeIndexRefused(f"Invalid {label} for episode index")
    return value


def episode_index_path(store: Path, company_id: str) -> Path:
    """Return the derived FTS5 DB path strictly outside company.sqlite and all Desks."""
    safe_company = _safe_segment(company_id, "company_id")
    return (
        store.resolve(strict=False).parent
        / ".sourdough-indexes"
        / safe_company
        / "episodes_fts.sqlite"
    )


def _ensure_private_parent(path: Path, stop_at: Path) -> None:
    rel = path.relative_to(stop_at)
    current = stop_at
    for part in rel.parts:
        current = current / part
        if os.path.lexists(current):
            st = os.lstat(current)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise EpisodeIndexRefused(
                    "Episode index directory must not be a symlink"
                )
        else:
            os.mkdir(current, 0o700)
        os.chmod(current, 0o700)


def _open_index_db(store: Path, company_id: str) -> sqlite3.Connection:
    db_path = episode_index_path(store, company_id)
    root = store.resolve(strict=False).parent
    _ensure_private_parent(db_path.parent, root)
    if os.path.lexists(db_path):
        st = os.lstat(db_path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise EpisodeIndexRefused("Episode index file must be a regular file")
    else:
        fd = os.open(db_path, os.O_CREAT | os.O_RDWR | _O_NOFOLLOW, 0o600)
        os.close(fd)
    os.chmod(db_path, 0o600)
    conn = connect_sqlite(db_path, autocommit=True)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA trusted_schema = OFF")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS episodes_fts_meta (
               singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
               company_id TEXT NOT NULL,
               last_recorded_at_ns INTEGER NOT NULL
           ) STRICT"""
    )
    conn.execute(
        """CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
               episode_id UNINDEXED,
               company_id UNINDEXED,
               agent_id UNINDEXED,
               duty_kind,
               outcome_status,
               summary_text,
               decisions_text,
               tokenize='unicode61'
           )"""
    )
    return conn


def _decisions_as_text(decisions_json: str) -> str:
    try:
        parsed = json.loads(decisions_json)
    except json.JSONDecodeError:
        return decisions_json
    if isinstance(parsed, list):
        return " ".join(
            json.dumps(item, sort_keys=True, separators=(",", ":"))
            if isinstance(item, dict)
            else str(item)
            for item in parsed
        )
    return str(parsed)


def _insert_episode_row(conn: sqlite3.Connection, episode: AgentEpisode) -> None:
    conn.execute(
        "DELETE FROM episodes_fts WHERE episode_id = ?",
        (episode.episode_id,),
    )
    conn.execute(
        """INSERT INTO episodes_fts (
               episode_id, company_id, agent_id, duty_kind, outcome_status,
               summary_text, decisions_text
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            episode.episode_id,
            episode.company_id,
            episode.agent_id,
            episode.duty_kind,
            episode.outcome_status,
            episode.summary_text,
            _decisions_as_text(episode.decisions_json),
        ),
    )


def _quarantine_corrupt_index(store: Path, company_id: str) -> None:
    db_path = episode_index_path(store, company_id)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{db_path}{suffix}")
        if os.path.lexists(candidate):
            st = os.lstat(candidate)
            if stat.S_ISLNK(st.st_mode):
                raise EpisodeIndexRefused("Episode index path must not be a symlink")
            candidate.unlink()


def upsert_episode_entry(store: Path, episode: AgentEpisode) -> None:
    """Incrementally update the derived FTS5 index for one AgentEpisode."""

    def _write_entry() -> None:
        with closing(_open_index_db(store, episode.company_id)) as idx:
            with idx:
                _insert_episode_row(idx, episode)
                idx.execute(
                    """INSERT INTO episodes_fts_meta (singleton, company_id, last_recorded_at_ns)
                       VALUES (1, ?, ?)
                       ON CONFLICT(singleton) DO UPDATE SET
                           last_recorded_at_ns = MAX(episodes_fts_meta.last_recorded_at_ns, excluded.last_recorded_at_ns)""",
                    (episode.company_id, episode.recorded_at_ns),
                )

    try:
        _write_entry()
    except sqlite3.DatabaseError:
        _quarantine_corrupt_index(store, episode.company_id)
        if os.path.lexists(store) and store.is_file():
            rebuild_episodes_index(store, episode.company_id)
        _write_entry()


def rebuild_episodes_index(store: Path, company_id: str) -> int:
    """Rebuild the derived FTS5 index completely from authoritative company.sqlite."""
    db_path = episode_index_path(store, company_id)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{db_path}{suffix}")
        if os.path.lexists(candidate):
            st = os.lstat(candidate)
            if stat.S_ISLNK(st.st_mode):
                raise EpisodeIndexRefused("Episode index path must not be a symlink")
            candidate.unlink()

    with closing(open_reader(store)) as reader, read_tx(reader):
        rows = reader.execute(
            """SELECT episode_id, company_id, agent_id, work_item_id, run_id,
                      duty_kind, context_digest, active_snapshot_id, outcome_status,
                      summary_text, decisions_json, evidence_refs_json, receipts_json,
                      tool_calls_count, input_tokens, output_tokens, cost_minor_units,
                      recorded_at_ns
               FROM agent_episode
               WHERE company_id = ?
               ORDER BY recorded_at_ns ASC, episode_id ASC""",
            (company_id,),
        ).fetchall()

    with closing(_open_index_db(store, company_id)) as idx:
        max_ns = 0
        with idx:
            for row in rows:
                ep = AgentEpisode(*row)
                _insert_episode_row(idx, ep)
                if ep.recorded_at_ns > max_ns:
                    max_ns = ep.recorded_at_ns
            idx.execute(
                """INSERT INTO episodes_fts_meta (singleton, company_id, last_recorded_at_ns)
                   VALUES (1, ?, ?)
                   ON CONFLICT(singleton) DO UPDATE SET
                       last_recorded_at_ns = excluded.last_recorded_at_ns""",
                (company_id, max_ns),
            )
    return len(rows)


def sync_episodes_index(store: Path, company_id: str) -> int:
    """Incrementally sync new agent_episode rows into the derived FTS5 index."""
    db_path = episode_index_path(store, company_id)
    if not os.path.lexists(db_path):
        return rebuild_episodes_index(store, company_id)
    with closing(open_reader(store)) as reader, read_tx(reader):
        ep_count = int(
            reader.execute(
                "SELECT COUNT(*) FROM agent_episode WHERE company_id = ?",
                (company_id,),
            ).fetchone()[0]
        )
    try:
        with closing(_open_index_db(store, company_id)) as idx:
            meta = idx.execute(
                "SELECT last_recorded_at_ns FROM episodes_fts_meta WHERE singleton = 1"
            ).fetchone()
            last_ns = int(meta[0]) if meta is not None else -1
            fts_count = int(
                idx.execute(
                    "SELECT COUNT(*) FROM episodes_fts WHERE company_id = ?",
                    (company_id,),
                ).fetchone()[0]
            )
    except sqlite3.DatabaseError:
        return rebuild_episodes_index(store, company_id)

    if last_ns < 0 or (ep_count > 0 and fts_count == 0) or fts_count > ep_count:
        return rebuild_episodes_index(store, company_id)

    with closing(open_reader(store)) as reader, read_tx(reader):
        rows = reader.execute(
            """SELECT episode_id, company_id, agent_id, work_item_id, run_id,
                      duty_kind, context_digest, active_snapshot_id, outcome_status,
                      summary_text, decisions_json, evidence_refs_json, receipts_json,
                      tool_calls_count, input_tokens, output_tokens, cost_minor_units,
                      recorded_at_ns
               FROM agent_episode
               WHERE company_id = ? AND recorded_at_ns > ?
               ORDER BY recorded_at_ns ASC, episode_id ASC""",
            (company_id, last_ns),
        ).fetchall()
    if not rows:
        if fts_count != ep_count:
            return rebuild_episodes_index(store, company_id)
        return 0
    with closing(_open_index_db(store, company_id)) as idx:
        max_ns = last_ns
        with idx:
            for row in rows:
                ep = AgentEpisode(*row)
                _insert_episode_row(idx, ep)
                if ep.recorded_at_ns > max_ns:
                    max_ns = ep.recorded_at_ns
            idx.execute(
                """UPDATE episodes_fts_meta
                   SET last_recorded_at_ns = ?
                   WHERE singleton = 1""",
                (max_ns,),
            )
        final_count = int(
            idx.execute(
                "SELECT COUNT(*) FROM episodes_fts WHERE company_id = ?",
                (company_id,),
            ).fetchone()[0]
        )
    if final_count != ep_count:
        return rebuild_episodes_index(store, company_id)
    return len(rows)


def _sanitize_fts_query(query: str) -> str:
    tokens = _TOKEN_RE.findall(query)
    if not tokens:
        return ""
    return " OR ".join(f'"{tok}"' for tok in tokens[:16])


def search_episodes(
    store: Path,
    company_id: str,
    agent_id: str,
    query: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search derived FTS5 episode index strictly scoped to (company_id, agent_id)."""
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise EpisodeIndexRefused("agent_id must be a nonempty string")
    if not isinstance(query, str):
        raise EpisodeIndexRefused("query must be a string")
    if type(limit) is not int or limit < 1:
        raise EpisodeIndexRefused("limit must be a positive integer")
    bounded = min(limit, 20)
    fts_expr = _sanitize_fts_query(query)
    if not fts_expr:
        return []
    sync_episodes_index(store, company_id)
    try:
        with closing(_open_index_db(store, company_id)) as idx:
            rows = idx.execute(
                """SELECT episode_id, duty_kind, outcome_status, summary_text,
                          bm25(episodes_fts) AS rank
                   FROM episodes_fts
                   WHERE episodes_fts MATCH ?
                     AND company_id = ?
                     AND agent_id = ?
                   ORDER BY rank ASC, episode_id ASC
                   LIMIT ?""",
                (fts_expr, company_id, agent_id, bounded),
            ).fetchall()
    except sqlite3.DatabaseError:
        rebuild_episodes_index(store, company_id)
        with closing(_open_index_db(store, company_id)) as idx:
            rows = idx.execute(
                """SELECT episode_id, duty_kind, outcome_status, summary_text,
                          bm25(episodes_fts) AS rank
                   FROM episodes_fts
                   WHERE episodes_fts MATCH ?
                     AND company_id = ?
                     AND agent_id = ?
                   ORDER BY rank ASC, episode_id ASC
                   LIMIT ?""",
                (fts_expr, company_id, agent_id, bounded),
            ).fetchall()

    results: list[dict[str, Any]] = []
    if not rows:
        return results
    episode_ids = [str(r[0]) for r in rows]
    placeholders = ", ".join("?" for _ in episode_ids)
    with closing(open_reader(store)) as reader, read_tx(reader):
        meta_rows = {
            str(r[0]): r
            for r in reader.execute(
                f"""SELECT episode_id, work_item_id, run_id, recorded_at_ns,
                           active_snapshot_id, cost_minor_units
                    FROM agent_episode
                    WHERE company_id = ? AND agent_id = ?
                      AND episode_id IN ({placeholders})""",
                (company_id, agent_id, *episode_ids),
            ).fetchall()
        }
    for eid, duty_kind, outcome_status, summary_text, _rank in rows:
        meta = meta_rows.get(str(eid))
        if meta is None:
            continue
        results.append(
            {
                "episode_id": str(eid),
                "duty_kind": str(duty_kind),
                "outcome_status": str(outcome_status),
                "summary_text": str(summary_text),
                "work_item_id": str(meta[1]),
                "run_id": str(meta[2]),
                "recorded_at_ns": int(meta[3]),
                "active_snapshot_id": meta[4],
                "cost_minor_units": int(meta[5]),
            }
        )
    return results
