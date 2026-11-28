"""Lineage graph traversal, quarantine ancestry checks, and transitive source authorization."""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

from ..company import _identity
from .models import (
    _DELTA_COLUMNS,
    _SHA256_RE,
    MethodDelta,
    MethodRefused,
    _row_to_delta,
    _sha256_json,
    _trigger_matches_duty,
    opaque_one_agent_method_id,
)


def _all_lineage_edges(
    db: sqlite3.Connection, company_id: str
) -> tuple[
    dict[tuple[str, str], set[tuple[str, str]]],
    dict[tuple[str, str], set[tuple[str, str]]],
]:
    """Build directed lineage graph across parent_method_ids, superseded_revision, and active_snapshot_id."""
    children_by_node: dict[tuple[str, str], set[tuple[str, str]]] = {}
    parents_by_node: dict[tuple[str, str], set[tuple[str, str]]] = {}

    def _add_edge(parent: tuple[str, str], child: tuple[str, str]) -> None:
        if parent == child:
            return
        children_by_node.setdefault(parent, set()).add(child)
        parents_by_node.setdefault(child, set()).add(parent)

    delta_rows = db.execute(
        """SELECT method_id, revision_sha256, parent_method_ids_json,
                  active_snapshot_id, superseded_revision, status,
                  trigger_spec_json, audience_scope, audience_target_id,
                  author_agent_id, delta_kind
           FROM method_delta
           WHERE company_id = ?""",
        (company_id,),
    ).fetchall()

    nodes_by_method: dict[str, list[tuple[str, str]]] = {}
    all_nodes: set[tuple[str, str]] = set()
    node_meta: dict[
        tuple[str, str], tuple[dict[str, Any], str, str | None, str, str]
    ] = {}
    extra_agents: set[str] = set()
    duty_kinds: set[str | None] = {None}
    for (
        mid,
        rev,
        _,
        _,
        _,
        status,
        raw_trig,
        aud_scope,
        aud_target,
        author_id,
        dkind,
    ) in delta_rows:
        node = (str(mid), str(rev))
        all_nodes.add(node)
        nodes_by_method.setdefault(str(mid), []).append(node)
        extra_agents.add(str(author_id))
        try:
            trig = json.loads(str(raw_trig))
        except json.JSONDecodeError:
            trig = {}
        if not isinstance(trig, dict):
            trig = {}
        expected_dk = trig.get("duty_kind")
        if isinstance(expected_dk, str) and expected_dk:
            duty_kinds.add(expected_dk)
        elif isinstance(expected_dk, list):
            for entry_dk in expected_dk:
                if isinstance(entry_dk, str) and entry_dk:
                    duty_kinds.add(entry_dk)
        node_meta[node] = (
            trig,
            str(aud_scope),
            None if aud_target is None else str(aud_target),
            str(status),
            str(dkind),
        )

    # Map each recorded active_snapshot_id (and its content-addressed active subsets)
    # to member (method_id, revision_sha256) nodes.
    snapshot_members: dict[str, set[tuple[str, str]]] = {}
    historical_member_sets: list[set[tuple[str, str]]] = []
    cmd_rows = db.execute(
        """SELECT result_json FROM command_receipt
           WHERE company_id = ? AND command_type LIKE 'method_learning.%'
             AND state = 'applied' AND result_json IS NOT NULL""",
        (company_id,),
    ).fetchall()
    for (raw_res,) in cmd_rows:
        try:
            res = json.loads(str(raw_res))
        except json.JSONDecodeError:
            continue
        if not isinstance(res, dict):
            continue
        snap_id = res.get("active_snapshot_id")
        if not isinstance(snap_id, str) or not snap_id:
            continue
        members_set: set[tuple[str, str]] = set()
        members = res.get("snapshot_members")
        if isinstance(members, list):
            for pair in members:
                if isinstance(pair, list) and len(pair) == 2:
                    node_pair = (str(pair[0]), str(pair[1]))
                    snapshot_members.setdefault(snap_id, set()).add(node_pair)
                    members_set.add(node_pair)
        m_id = res.get("method_id")
        r_sha = res.get("revision_sha256")
        if isinstance(m_id, str) and isinstance(r_sha, str):
            node_pair = (m_id, r_sha)
            snapshot_members.setdefault(snap_id, set()).add(node_pair)
            if node_pair not in node_meta or node_meta[node_pair][4] != "DEPRECATE":
                members_set.add(node_pair)
        content_snap = res.get("content_snapshot_id")
        if isinstance(content_snap, str) and content_snap and members_set:
            snapshot_members.setdefault(content_snap, set()).update(members_set)
        if members_set:
            active_only = {
                pair
                for pair in members_set
                if pair not in node_meta or node_meta[pair][4] != "DEPRECATE"
            }
            if active_only:
                c_snap = "snap-" + _sha256_json(sorted(active_only))[:24]
                snapshot_members.setdefault(c_snap, set()).update(active_only)
                historical_member_sets.append(active_only)

    current_active = {
        node
        for node, (_, _, _, st, dk) in node_meta.items()
        if st == "active" and dk != "DEPRECATE"
    }
    if current_active:
        historical_member_sets.append(current_active)

    needed_snaps = {
        str(raw_snap).strip()
        for _, _, _, raw_snap, *_ in delta_rows
        if isinstance(raw_snap, str) and raw_snap.strip()
    } - set(snapshot_members.keys())

    if needed_snaps and historical_member_sets:
        proj_members: dict[str, set[str]] = {}
        for pid, raw_members in db.execute(
            "SELECT project_id, members FROM shared_project WHERE company_id = ?",
            (company_id,),
        ).fetchall():
            try:
                m_list = json.loads(str(raw_members))
            except json.JSONDecodeError:
                continue
            if isinstance(m_list, list):
                proj_members[str(pid)] = {str(m) for m in m_list}

        agent_ids = extra_agents | {
            str(r[0])
            for r in db.execute(
                "SELECT id FROM agent WHERE company_id = ?", (company_id,)
            ).fetchall()
        }
        for (dk_row,) in db.execute(
            "SELECT DISTINCT duty_kind FROM work_items WHERE company_id = ?",
            (company_id,),
        ).fetchall():
            if dk_row:
                duty_kinds.add(str(dk_row))
        for (dk_row,) in db.execute(
            "SELECT DISTINCT duty_kind FROM agent_episode WHERE company_id = ?",
            (company_id,),
        ).fetchall():
            if dk_row:
                duty_kinds.add(str(dk_row))

        for mset in historical_member_sets:
            full_snap = "snap-" + _sha256_json(sorted(mset))[:24]
            if full_snap in needed_snaps:
                snapshot_members.setdefault(full_snap, set()).update(mset)
            for aid in agent_ids:
                agent_mset: list[tuple[str, str]] = []
                for node in mset:
                    meta = node_meta.get(node)
                    if meta is None:
                        continue
                    _trig, sc, tgt, _st, dk_kind = meta
                    if dk_kind == "DEPRECATE":
                        continue
                    if sc == "company":
                        agent_mset.append(node)
                    elif sc == "project" and tgt is not None:
                        if aid in proj_members.get(tgt, ()):
                            agent_mset.append(node)
                    elif sc == "one_agent" and tgt == aid:
                        agent_mset.append(node)
                if not agent_mset:
                    continue
                for dk in duty_kinds:
                    scoped = sorted(
                        node
                        for node in agent_mset
                        if _trigger_matches_duty(node_meta[node][0], dk)
                    )
                    if not scoped:
                        continue
                    sub_snap = "snap-" + _sha256_json(scoped)[:24]
                    if sub_snap in needed_snaps:
                        snapshot_members.setdefault(sub_snap, set()).update(scoped)

    remaining_snaps = needed_snaps - set(snapshot_members.keys())
    if remaining_snaps:
        opaque_to_nodes: dict[str, list[tuple[str, str]]] = {}
        for mid_key, node_list in nodes_by_method.items():
            opaque_to_nodes.setdefault(opaque_one_agent_method_id(mid_key), []).extend(
                node_list
            )
        for (raw_payload,) in db.execute(
            """SELECT payload_json FROM audit_event
               WHERE company_id = ? AND domain = 'learning'
                 AND event_type = 'learning.causal_method_trace'""",
            (company_id,),
        ).fetchall():
            try:
                payload = json.loads(str(raw_payload))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            snap_val = payload.get("active_snapshot_id")
            if isinstance(snap_val, str) and snap_val in remaining_snaps:
                avail = payload.get("available")
                if isinstance(avail, list):
                    for mid in avail:
                        if not isinstance(mid, str):
                            continue
                        matched_nodes = nodes_by_method.get(mid) or opaque_to_nodes.get(
                            mid
                        )
                        if matched_nodes and len(matched_nodes) == 1:
                            snapshot_members.setdefault(snap_val, set()).add(
                                matched_nodes[0]
                            )

    for mid, rev, raw_parents, raw_snap, raw_sup, *_ in delta_rows:
        child = (str(mid), str(rev))
        if isinstance(raw_sup, str) and raw_sup.strip():
            sup_node = (str(mid), raw_sup.strip())
            if sup_node in all_nodes:
                _add_edge(sup_node, child)
        try:
            parent_list = json.loads(str(raw_parents))
        except json.JSONDecodeError:
            parent_list = []
        if isinstance(parent_list, list):
            for p_entry in parent_list:
                if not isinstance(p_entry, str) or not p_entry.strip():
                    continue
                p_ref = p_entry.strip()
                resolved_pair: tuple[str, str] | None = None
                for sep in (":", "@"):
                    if sep in p_ref:
                        pm, pr = p_ref.rsplit(sep, 1)
                        if _SHA256_RE.fullmatch(pr):
                            resolved_pair = (pm, pr)
                            break
                if resolved_pair is not None and resolved_pair in all_nodes:
                    _add_edge(resolved_pair, child)
                else:
                    for p_node in nodes_by_method.get(p_ref, ()):
                        _add_edge(p_node, child)
        if isinstance(raw_snap, str) and raw_snap.strip():
            for p_node in snapshot_members.get(raw_snap.strip(), ()):
                _add_edge(p_node, child)

    return children_by_node, parents_by_node


def _bfs_lineage_descendants(
    children_by_node: Mapping[tuple[str, str], set[tuple[str, str]]],
    method_id: str,
    revision_sha256: str,
) -> tuple[str, ...]:
    root = (str(method_id), str(revision_sha256))
    visited: set[tuple[str, str]] = {root}
    descendants: set[tuple[str, str]] = set()
    queue: deque[tuple[str, str]] = deque([root])
    while queue:
        cur = queue.popleft()
        for child in children_by_node.get(cur, ()):
            if child not in visited:
                visited.add(child)
                descendants.add(child)
                queue.append(child)
    return tuple(sorted(f"{m}:{r}" for m, r in descendants))


def collect_lineage_blast_radius(
    conn: sqlite3.Connection,
    company_id: str,
    method_id: str,
    revision_sha256: str,
) -> tuple[str, ...]:
    """Return sorted delta_ids ('method_id:revision_sha256') of all transitive lineage descendants."""
    if _identity(conn) != company_id:
        raise MethodRefused("Company identity mismatch")
    children_by_node, _ = _all_lineage_edges(conn, company_id)
    return _bfs_lineage_descendants(children_by_node, method_id, revision_sha256)


def collect_lineage_blast_radii(
    conn: sqlite3.Connection,
    company_id: str,
    targets: Sequence[tuple[str, str]],
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Return sorted descendant delta_ids keyed by (method_id, revision_sha256) in one pass."""
    if _identity(conn) != company_id:
        raise MethodRefused("Company identity mismatch")
    if not targets:
        return {}
    children_by_node, _ = _all_lineage_edges(conn, company_id)
    out: dict[tuple[str, str], tuple[str, ...]] = {}
    for mid, rev in targets:
        key = (str(mid), str(rev))
        if key not in out:
            out[key] = _bfs_lineage_descendants(children_by_node, key[0], key[1])
    return out


def _quarantined_nodes(db: sqlite3.Connection, company_id: str) -> set[tuple[str, str]]:
    nodes: set[tuple[str, str]] = set()
    for mid, rev in db.execute(
        "SELECT method_id, revision_sha256 FROM method_delta WHERE company_id = ? AND status = 'quarantined'",
        (company_id,),
    ).fetchall():
        nodes.add((str(mid), str(rev)))
    for mid, rev in db.execute(
        "SELECT method_id, revision_sha256 FROM method_activation WHERE company_id = ? AND quarantined = 1",
        (company_id,),
    ).fetchall():
        nodes.add((str(mid), str(rev)))
    return nodes


def _has_quarantined_ancestor(
    db: sqlite3.Connection,
    company_id: str,
    start_node: tuple[str, str],
    *,
    quarantined: set[tuple[str, str]] | None = None,
    parents_by_node: dict[tuple[str, str], set[tuple[str, str]]] | None = None,
) -> bool:
    q_nodes = _quarantined_nodes(db, company_id) if quarantined is None else quarantined
    if not q_nodes:
        return False
    if start_node in q_nodes:
        return True
    if parents_by_node is None:
        _, parents_by_node = _all_lineage_edges(db, company_id)
    visited: set[tuple[str, str]] = {start_node}
    queue: deque[tuple[str, str]] = deque([start_node])
    while queue:
        cur = queue.popleft()
        for parent in parents_by_node.get(cur, ()):
            if parent in q_nodes:
                return True
            if parent not in visited:
                visited.add(parent)
                queue.append(parent)
    return False


def _node_has_quarantined_parent_or_snapshot(
    db: sqlite3.Connection,
    company_id: str,
    delta: MethodDelta,
) -> bool:
    q_nodes = _quarantined_nodes(db, company_id)
    if not q_nodes:
        return False
    node = (delta.method_id, delta.revision_sha256)
    if _has_quarantined_ancestor(db, company_id, node, quarantined=q_nodes):
        return True
    q_methods = {m for m, _ in q_nodes}
    for p_entry in delta.parent_method_ids:
        for sep in (":", "@"):
            if sep in p_entry:
                pm, pr = p_entry.rsplit(sep, 1)
                if _SHA256_RE.fullmatch(pr) and (pm, pr) in q_nodes:
                    return True
        if p_entry in q_methods:
            return True
    return False


def _collect_transitive_shared_publication_ids(
    db: sqlite3.Connection,
    company_id: str,
    delta: MethodDelta,
) -> set[str] | None:
    """Collect all shared_publication asset IDs cited directly or via ancestor method_deltas."""
    asset_ids: set[str] = set()
    visited: set[tuple[str, str]] = {(delta.method_id, delta.revision_sha256)}
    queue: list[MethodDelta] = [delta]
    _, parents_by_node = _all_lineage_edges(db, company_id)

    def _enqueue_node(pm: str, pr: str) -> None:
        key = (pm, pr)
        if key in visited:
            return
        row = db.execute(
            f"""SELECT {_DELTA_COLUMNS}
                FROM method_delta
                WHERE company_id = ? AND method_id = ? AND revision_sha256 = ?""",
            (company_id, pm, pr),
        ).fetchone()
        if row is not None:
            visited.add(key)
            queue.append(_row_to_delta(row))

    def _enqueue_parent_ref(raw: str) -> None:
        ref = raw.strip()
        if not ref:
            return
        for sep in (":", "@"):
            if sep in ref:
                pm, pr = ref.rsplit(sep, 1)
                if _SHA256_RE.fullmatch(pr):
                    _enqueue_node(pm, pr)
                    return
        # Revisionless parent reference: check all revisions of that method_id in this company.
        for row in db.execute(
            f"""SELECT {_DELTA_COLUMNS}
                FROM method_delta
                WHERE company_id = ? AND method_id = ?""",
            (company_id, ref),
        ).fetchall():
            parent_delta = _row_to_delta(row)
            key = (parent_delta.method_id, parent_delta.revision_sha256)
            if key not in visited:
                visited.add(key)
                queue.append(parent_delta)

    while queue:
        cur = queue.pop(0)
        for raw_ref in cur.source_refs:
            if not isinstance(raw_ref, str) or ":" not in raw_ref:
                continue
            kind, ident = raw_ref.strip().split(":", 1)
            if kind == "shared_publication":
                asset_id = ident.split("@", 1)[0].strip()
                if not asset_id:
                    return None
                asset_ids.add(asset_id)
            elif kind == "method_delta":
                _enqueue_parent_ref(ident)
        for parent_ref in cur.parent_method_ids:
            if isinstance(parent_ref, str):
                _enqueue_parent_ref(parent_ref)
        if cur.superseded_revision:
            _enqueue_node(cur.method_id, cur.superseded_revision)
        for pm, pr in parents_by_node.get((cur.method_id, cur.revision_sha256), ()):
            _enqueue_node(pm, pr)
    return asset_ids


def _agent_authorized_for_delta_sources(
    db: sqlite3.Connection,
    company_id: str,
    agent_id: str,
    delta: MethodDelta,
) -> bool:
    """Revalidate recipient access to all directly or transitively cited shared publications."""
    if delta.audience_scope not in ("project", "company"):
        return True
    asset_ids = _collect_transitive_shared_publication_ids(db, company_id, delta)
    if asset_ids is None:
        return False
    for asset_id in asset_ids:
        pub_row = db.execute(
            """SELECT company_id, project_id, audience
               FROM shared_publication
               WHERE asset_id = ?""",
            (asset_id,),
        ).fetchone()
        if pub_row is None:
            return False
        pub_co, pub_proj, aud_json = pub_row
        if str(pub_co) != company_id:
            return False
        try:
            audience = set(json.loads(str(aud_json)))
        except json.JSONDecodeError:
            audience = set()
        proj_row = db.execute(
            "SELECT members FROM shared_project WHERE company_id = ? AND project_id = ?",
            (company_id, str(pub_proj)),
        ).fetchone()
        try:
            members = set(json.loads(str(proj_row[0]))) if proj_row else set()
        except json.JSONDecodeError:
            members = set()
        if agent_id not in audience or agent_id not in members:
            return False
        if (
            delta.audience_scope == "project"
            and delta.audience_target_id is not None
            and str(pub_proj) != delta.audience_target_id
        ):
            return False
    return True
