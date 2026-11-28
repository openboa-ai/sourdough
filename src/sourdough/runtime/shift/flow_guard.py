"""Shift-level information-flow guard and memoized provenance lattice."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from ...agent_runtime.protocol import ToolHandler, ToolSpec
from ...duty_run import DutyRefused
from ...storage.db import scoped_reader
from .policy import (
    is_external_write_sink,
    is_peer_input,
    is_peer_outbound,
    is_private_desk_read,
    is_private_desk_tool,
)


def _intersect_audience(cur: set[str] | None, aud: set[str] | None) -> set[str] | None:
    if aud is None:
        return cur
    return set(aud) if cur is None else (cur & aud)


class ProvenanceIndex:
    """Shift-scoped memoized lookup cache over shared projects, assets, messages, peer requests, and handoffs."""

    def __init__(
        self,
        store: Path,
        company_id: str,
        agent_id: str,
        work_item_id: str,
    ) -> None:
        self._store = store
        self._company_id = company_id
        self._agent_id = agent_id
        self._work_item_id = work_item_id
        self._project_members: dict[str, set[str]] = {}
        self._assets: dict[str, tuple[str | None, set[str] | None]] = {}
        self._messages: dict[str, tuple[str | None, set[str] | None]] = {}
        self._handoffs: dict[str, tuple[str | None, str | None]] = {}
        self._peer_requester_loaded: bool = False
        self._peer_requester: str | None = None

    def _run_read(
        self,
        db: sqlite3.Connection | None,
        fn: Callable[[sqlite3.Connection], Any],
    ) -> Any:
        if db is not None:
            return fn(db)
        with scoped_reader(self._store) as ro_db:
            return fn(ro_db)

    def project_members(
        self,
        project_id: str,
        *,
        db: sqlite3.Connection | None = None,
        refresh: bool = False,
    ) -> set[str] | None:
        if not isinstance(project_id, str) or not project_id.strip():
            return None
        pid = project_id.strip()
        if not refresh:
            cached = self._project_members.get(pid)
            if cached is not None:
                return set(cached)

        def _query(ro_db: sqlite3.Connection) -> set[str] | None:
            row = ro_db.execute(
                "SELECT members FROM shared_project WHERE company_id = ? AND project_id = ?",
                (self._company_id, pid),
            ).fetchone()
            if row is None:
                self._project_members.pop(pid, None)
                return None
            try:
                parsed = json.loads(str(row[0]))
            except json.JSONDecodeError:
                return None
            if not isinstance(parsed, list):
                return None
            members = {str(m) for m in parsed if isinstance(m, str) and m}
            self._project_members[pid] = set(members)
            return members

        return self._run_read(db, _query)

    def asset_provenance(
        self, asset_id: str, *, db: sqlite3.Connection | None = None
    ) -> tuple[str | None, set[str] | None]:
        if not isinstance(asset_id, str) or not asset_id.strip():
            return None, None
        aid = asset_id.strip()
        if aid in self._assets:
            pid, aud = self._assets[aid]
            return pid, (None if aud is None else set(aud))

        def _query(ro_db: sqlite3.Connection) -> tuple[str | None, set[str] | None]:
            row = ro_db.execute(
                """SELECT project_id, audience FROM shared_publication
                   WHERE company_id = ? AND asset_id = ?""",
                (self._company_id, aid),
            ).fetchone()
            if row is None:
                return None, None
            proj_id = str(row[0])
            try:
                aud_list = json.loads(str(row[1]))
            except json.JSONDecodeError:
                return proj_id, None
            if not isinstance(aud_list, list):
                return proj_id, None
            aud_set = {str(a) for a in aud_list if isinstance(a, str) and a}
            proj_members = self.project_members(proj_id, db=ro_db)
            if proj_members is not None:
                aud_set &= proj_members
            self._assets[aid] = (proj_id, set(aud_set))
            return proj_id, aud_set

        return self._run_read(db, _query)

    def message_audience(
        self, message_id: str, *, db: sqlite3.Connection | None = None
    ) -> tuple[str | None, set[str] | None]:
        if not isinstance(message_id, str) or not message_id.strip():
            return None, None
        mid = message_id.strip()
        if mid in self._messages:
            pid, aud = self._messages[mid]
            return pid, (None if aud is None else set(aud))

        def _query(ro_db: sqlite3.Connection) -> tuple[str | None, set[str] | None]:
            row = ro_db.execute(
                """SELECT project_id, audience, asset_id
                   FROM project_message
                   WHERE company_id = ? AND message_id = ?""",
                (self._company_id, mid),
            ).fetchone()
            if row is None:
                return None, None
            proj_id = str(row[0])
            try:
                aud_list = json.loads(str(row[1]))
            except json.JSONDecodeError:
                return proj_id, None
            if not isinstance(aud_list, list):
                return proj_id, None
            aud_set = {str(a) for a in aud_list if isinstance(a, str) and a}
            proj_members = self.project_members(proj_id, db=ro_db)
            if proj_members is not None:
                aud_set &= proj_members
            msg_asset_id = row[2]
            if isinstance(msg_asset_id, str) and msg_asset_id.strip():
                _, asset_aud = self.asset_provenance(msg_asset_id.strip(), db=ro_db)
                if asset_aud is not None:
                    aud_set &= asset_aud
                else:
                    aud_set &= {self._agent_id}
            self._messages[mid] = (proj_id, set(aud_set))
            return proj_id, aud_set

        return self._run_read(db, _query)

    def peer_request_provenance(
        self, request_id: str | None, *, db: sqlite3.Connection | None = None
    ) -> tuple[str | None, str | None, str | None, list[str]] | None:
        def _query(
            ro_db: sqlite3.Connection,
        ) -> tuple[str | None, str | None, str | None, list[str]] | None:
            if isinstance(request_id, str) and request_id.strip():
                row = ro_db.execute(
                    """SELECT project_id, requester_agent_id, recipient_agent_id,
                              evidence_refs_json, response_json
                       FROM work_peer_requests
                       WHERE company_id = ? AND request_id = ?""",
                    (self._company_id, request_id.strip()),
                ).fetchone()
            else:
                row = ro_db.execute(
                    """SELECT project_id, requester_agent_id, recipient_agent_id,
                              evidence_refs_json, response_json
                       FROM work_peer_requests
                       WHERE company_id = ? AND requester_work_item_id = ?
                         AND resumed_run_id IS NOT NULL
                       ORDER BY rowid DESC LIMIT 1""",
                    (self._company_id, self._work_item_id),
                ).fetchone()
            if row is None:
                return None
            proj_id = None if row[0] is None else str(row[0])
            req_agent = None if row[1] is None else str(row[1])
            recip_agent = None if row[2] is None else str(row[2])
            refs: list[str] = []
            for raw_json, key in ((row[3], None), (row[4], "evidence_refs")):
                if not isinstance(raw_json, str) or not raw_json:
                    continue
                try:
                    parsed = json.loads(raw_json)
                except json.JSONDecodeError:
                    continue
                seq = (
                    parsed.get(key)
                    if (key is not None and isinstance(parsed, Mapping))
                    else parsed
                )
                if isinstance(seq, list):
                    for item in seq:
                        if (
                            isinstance(item, str)
                            and item.strip()
                            and item.strip() not in refs
                        ):
                            refs.append(item.strip())
            return proj_id, req_agent, recip_agent, refs

        return self._run_read(db, _query)

    def handoff_offer_provenance(
        self, offer_id: str, *, db: sqlite3.Connection | None = None
    ) -> tuple[str | None, str | None]:
        if not isinstance(offer_id, str) or not offer_id.strip():
            return None, None
        oid = offer_id.strip()
        if oid in self._handoffs:
            return self._handoffs[oid]

        def _query(ro_db: sqlite3.Connection) -> tuple[str | None, str | None]:
            row = ro_db.execute(
                """SELECT sender_id, asset_id FROM work_handoff
                   WHERE company_id = ? AND offer_id = ?""",
                (self._company_id, oid),
            ).fetchone()
            if row is None:
                return None, None
            res = (str(row[0]), None if row[1] is None else str(row[1]))
            self._handoffs[oid] = res
            return res

        return self._run_read(db, _query)

    def peer_request_requester(
        self, *, db: sqlite3.Connection | None = None
    ) -> str | None:
        if self._peer_requester_loaded:
            return self._peer_requester

        def _query(ro_db: sqlite3.Connection) -> str | None:
            row = ro_db.execute(
                """SELECT requester_agent_id FROM work_peer_requests
                   WHERE company_id = ? AND response_work_item_id = ?""",
                (self._company_id, self._work_item_id),
            ).fetchone()
            val = None if row is None else str(row[0])
            self._peer_requester_loaded = True
            self._peer_requester = val
            return val

        return self._run_read(db, _query)


class ShiftFlowGuard:
    """Enforces shift-scoped information-flow isolation and audience-intersection lattice rules."""

    def __init__(
        self,
        *,
        agent_id: str,
        provenance: ProvenanceIndex,
        specs_by_name: Mapping[str, ToolSpec],
        public_offloaded_paths: set[str],
        read_private_desk: bool = False,
        read_peer_input: bool = False,
        allowed_audience: set[str] | None = None,
        tainted_projects: set[str] | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.provenance = provenance
        self._specs_by_name = dict(specs_by_name)
        self._public_offloaded_paths = public_offloaded_paths
        self.read_private_desk = bool(read_private_desk)
        self.read_peer_input = bool(read_peer_input)
        self.allowed_audience: set[str] | None = (
            None if allowed_audience is None else set(allowed_audience)
        )
        self.tainted_projects: set[str] = (
            set(tainted_projects) if tainted_projects is not None else set()
        )

    def _narrow_audience(self, aud: set[str] | None) -> None:
        self.allowed_audience = _intersect_audience(self.allowed_audience, aud)

    def _narrow_from_asset(
        self, asset_id: str, *, db: sqlite3.Connection | None = None
    ) -> None:
        pid, aud = self.provenance.asset_provenance(asset_id, db=db)
        if pid:
            self.tainted_projects.add(pid)
        self._narrow_audience(aud if aud is not None else {self.agent_id})

    def _narrow_from_peer_request(
        self, receipt_obj: Mapping[str, Any], *, db: sqlite3.Connection | None = None
    ) -> None:
        raw_req_id = receipt_obj.get("request_id")
        prov = self.provenance.peer_request_provenance(
            raw_req_id if isinstance(raw_req_id, str) else None,
            db=db,
        )
        if prov is not None:
            pr_proj_id, pr_req_agent, pr_recip_agent, pr_ev_refs = prov
        else:
            raw_pid = receipt_obj.get("project_id")
            pr_proj_id = (
                raw_pid.strip()
                if isinstance(raw_pid, str) and raw_pid.strip()
                else None
            )
            raw_req = receipt_obj.get("requester_agent_id")
            pr_req_agent = (
                raw_req.strip()
                if isinstance(raw_req, str) and raw_req.strip()
                else None
            )
            raw_recip = receipt_obj.get("recipient_agent_id")
            pr_recip_agent = (
                raw_recip.strip()
                if isinstance(raw_recip, str) and raw_recip.strip()
                else None
            )
            raw_refs = receipt_obj.get("evidence_refs")
            pr_ev_refs = (
                [str(x).strip() for x in raw_refs if isinstance(x, str) and x.strip()]
                if isinstance(raw_refs, list)
                else []
            )
        if pr_proj_id:
            self.tainted_projects.add(pr_proj_id)
            p_members = self.provenance.project_members(pr_proj_id, db=db)
            self._narrow_audience(
                p_members if p_members is not None else {self.agent_id}
            )
        else:
            pair = {
                a.strip()
                for a in (self.agent_id, pr_req_agent, pr_recip_agent)
                if isinstance(a, str) and a.strip()
            }
            self._narrow_audience(pair if pair else {self.agent_id})
        for ref in pr_ev_refs:
            if ref.startswith("shared_publication:"):
                ev_aid = (
                    ref.removeprefix("shared_publication:").split("@", 1)[0].strip()
                )
                if ev_aid:
                    self._narrow_from_asset(ev_aid, db=db)

    def seed_from_shift_context(
        self,
        *,
        work_item_source_kind: str,
        work_item_payload_json: str | None,
        open_receipts: Sequence[Mapping[str, Any]],
        prior_had_peer_input: bool,
        prior_peer_rows: Sequence[tuple[Any, Any, Any]],
        db: sqlite3.Connection | None = None,
    ) -> None:
        """Initialize audience bounds and tainted projects from hydrated work item and prior receipts."""
        with (
            scoped_reader(self.provenance._store) if db is None else nullcontext(db)
        ) as ro_db:
            self._seed_on_db(
                ro_db,
                work_item_source_kind=work_item_source_kind,
                work_item_payload_json=work_item_payload_json,
                open_receipts=open_receipts,
                prior_had_peer_input=prior_had_peer_input,
                prior_peer_rows=prior_peer_rows,
            )

    def _seed_on_db(
        self,
        ro_db: sqlite3.Connection,
        *,
        work_item_source_kind: str,
        work_item_payload_json: str | None,
        open_receipts: Sequence[Mapping[str, Any]],
        prior_had_peer_input: bool,
        prior_peer_rows: Sequence[tuple[Any, Any, Any]],
    ) -> None:
        try:
            item_payload_obj = json.loads(work_item_payload_json or "{}")
        except json.JSONDecodeError:
            item_payload_obj = {}
        if isinstance(item_payload_obj, Mapping):
            payload_asset_id = item_payload_obj.get("asset_id")
            if isinstance(payload_asset_id, str) and payload_asset_id.strip():
                p_pid, p_aud = self.provenance.asset_provenance(
                    payload_asset_id, db=ro_db
                )
                if p_pid:
                    self.tainted_projects.add(p_pid)
                if p_aud is not None:
                    self._narrow_audience(p_aud)
            payload_msg_id = item_payload_obj.get("message_id")
            if isinstance(payload_msg_id, str) and payload_msg_id.strip():
                m_pid, m_aud = self.provenance.message_audience(
                    payload_msg_id, db=ro_db
                )
                if m_pid:
                    self.tainted_projects.add(m_pid)
                self._narrow_audience(m_aud if m_aud is not None else {self.agent_id})
            payload_proj_id = item_payload_obj.get("project_id")
            if (
                isinstance(payload_proj_id, str)
                and payload_proj_id.strip()
                and (
                    work_item_source_kind
                    in ("project_message", "peer_request", "handoff_offer")
                    or item_payload_obj.get("trigger_kind")
                    in ("project_message", "peer_request", "handoff_offer")
                )
            ):
                self.tainted_projects.add(payload_proj_id.strip())
                p_members = self.provenance.project_members(
                    payload_proj_id.strip(), db=ro_db
                )
                if p_members is not None:
                    self._narrow_audience(p_members)

        for r in open_receipts:
            if not isinstance(r, Mapping):
                continue
            r_kind = r.get("kind")
            if r_kind == "peer_outcome" and r.get("outcome") != "unavailable":
                self._narrow_from_peer_request(r, db=ro_db)
            elif r_kind == "peer_request":
                self._narrow_from_peer_request(r, db=ro_db)
                raw_ev = r.get("evidence")
                if isinstance(raw_ev, list):
                    for ev in raw_ev:
                        if isinstance(ev, Mapping):
                            ev_aid = ev.get("asset_id")
                            if isinstance(ev_aid, str) and ev_aid.strip():
                                self._narrow_from_asset(ev_aid.strip(), db=ro_db)
            elif r_kind == "accepted_handoff":
                h_aid = r.get("asset_id")
                h_pid = r.get("project_id")
                h_sender = r.get("sender_id")
                if isinstance(h_aid, str) and h_aid.strip():
                    self._narrow_from_asset(h_aid.strip(), db=ro_db)
                elif isinstance(h_pid, str) and h_pid.strip():
                    self.tainted_projects.add(h_pid.strip())
                    p_members = self.provenance.project_members(h_pid.strip(), db=ro_db)
                    self._narrow_audience(
                        p_members if p_members is not None else {self.agent_id}
                    )
                elif isinstance(h_sender, str) and h_sender.strip():
                    self._narrow_audience({self.agent_id, h_sender.strip()})
                else:
                    self._narrow_audience({self.agent_id})
            elif r_kind == "shared_asset" and r.get("author_id") != self.agent_id:
                s_aid = r.get("asset_id")
                s_pid = r.get("project_id")
                if isinstance(s_aid, str) and s_aid.strip():
                    self._narrow_from_asset(s_aid.strip(), db=ro_db)
                elif isinstance(s_pid, str) and s_pid.strip():
                    self.tainted_projects.add(s_pid.strip())
                    p_members = self.provenance.project_members(s_pid.strip(), db=ro_db)
                    self._narrow_audience(
                        p_members if p_members is not None else {self.agent_id}
                    )
                else:
                    self._narrow_audience({self.agent_id})

        if prior_had_peer_input:
            self._seed_from_prior_peer_receipts(prior_peer_rows, db=ro_db)

    def _seed_from_prior_peer_receipts(
        self,
        prior_peer_rows: Sequence[tuple[Any, Any, Any]],
        *,
        db: sqlite3.Connection,
    ) -> None:
        resolved_prior_peer = False
        unresolved_prior_peer = False
        for p_tname, p_res_json, p_off in prior_peer_rows:
            if p_off is not None or not isinstance(p_res_json, str):
                unresolved_prior_peer = True
                continue
            try:
                p_obj = json.loads(p_res_json)
            except json.JSONDecodeError:
                unresolved_prior_peer = True
                continue
            if not isinstance(p_obj, Mapping):
                unresolved_prior_peer = True
                continue
            if p_tname == "read_shared_asset":
                p_aid = p_obj.get("asset_id")
                if isinstance(p_aid, str) and p_aid.strip():
                    pid, aud = self.provenance.asset_provenance(p_aid, db=db)
                    if pid:
                        self.tainted_projects.add(pid)
                    if aud is not None:
                        self._narrow_audience(aud)
                        resolved_prior_peer = True
                    else:
                        unresolved_prior_peer = True
                else:
                    unresolved_prior_peer = True
            elif p_tname == "read_messages":
                msgs = p_obj.get("messages")
                if isinstance(msgs, list):
                    for m in msgs:
                        if not isinstance(m, Mapping):
                            unresolved_prior_peer = True
                            continue
                        m_mid = m.get("message_id")
                        if isinstance(m_mid, str) and m_mid.strip():
                            pid, aud = self.provenance.message_audience(
                                m_mid.strip(), db=db
                            )
                            if pid:
                                self.tainted_projects.add(pid)
                            if aud is not None:
                                self._narrow_audience(aud)
                                resolved_prior_peer = True
                            else:
                                unresolved_prior_peer = True
                        else:
                            unresolved_prior_peer = True
                else:
                    unresolved_prior_peer = True
            elif p_tname == "read_inbox":
                delivs = p_obj.get("deliveries")
                if isinstance(delivs, list):
                    for d in delivs:
                        if not isinstance(d, Mapping):
                            unresolved_prior_peer = True
                            continue
                        d_mid = d.get("message_id")
                        if isinstance(d_mid, str) and d_mid.strip():
                            pid, aud = self.provenance.message_audience(
                                d_mid.strip(), db=db
                            )
                            if pid:
                                self.tainted_projects.add(pid)
                            if aud is not None:
                                self._narrow_audience(aud)
                                resolved_prior_peer = True
                            else:
                                unresolved_prior_peer = True
                        else:
                            unresolved_prior_peer = True
                else:
                    unresolved_prior_peer = True
            elif p_tname == "discover_handoffs":
                offers = p_obj.get("offers")
                if isinstance(offers, list):
                    for o in offers:
                        if isinstance(o, Mapping):
                            o_aid = o.get("asset_id")
                            if isinstance(o_aid, str) and o_aid.strip():
                                pid, aud = self.provenance.asset_provenance(
                                    o_aid, db=db
                                )
                                if pid:
                                    self.tainted_projects.add(pid)
                                if aud is not None:
                                    self._narrow_audience(aud)
                                    resolved_prior_peer = True
                else:
                    unresolved_prior_peer = True
            else:
                unresolved_prior_peer = True
        if unresolved_prior_peer or not resolved_prior_peer:
            self._narrow_audience({self.agent_id})

    def _is_public_offload_read(self, name: str, args: Mapping[str, Any] | Any) -> bool:
        return (
            name == "desk_read_offloaded_log"
            and isinstance(args, Mapping)
            and isinstance(args.get("log_path"), str)
            and args["log_path"] in self._public_offloaded_paths
        )

    def check_before_call(self, name: str, args: Any) -> None:
        """Raise DutyRefused if invoking `name` violates shift taint or audience-lattice rules."""
        spec = self._specs_by_name.get(name)
        public_offload = self._is_public_offload_read(name, args)
        outbound = is_peer_outbound(name, spec)
        ext_sink = is_external_write_sink(name, spec)
        priv_tool = is_private_desk_tool(name, spec)
        peer_in = is_peer_input(name, spec)

        if (outbound or ext_sink) and self.read_private_desk:
            raise DutyRefused(
                "Cannot write free-form peer messages, peer requests, or external write actions in the same shift "
                "after reading private Desk data; publish explicitly via desk_publish_selection"
            )
        if ext_sink and self.read_peer_input:
            raise DutyRefused(
                "Cannot execute external write actions in the same shift "
                "after reading peer-authored messages, handoffs, or shared assets"
            )
        if priv_tool and not public_offload and self.read_peer_input:
            raise DutyRefused(
                "Cannot access private Desk drafts, publications, or skills in the same shift "
                "after reading peer-authored messages, handoffs, or shared assets"
            )
        if peer_in and self.read_private_desk:
            raise DutyRefused(
                "Cannot read peer-authored messages, handoffs, or shared assets "
                "in the same shift after reading private Desk data"
            )

        if outbound and self.allowed_audience is not None:
            cur_allowed = self.allowed_audience
            args_map: Mapping[str, Any] = args if isinstance(args, Mapping) else {}
            if name == "append_message":
                dest_pid = args_map.get("project_id")
                dest_members = (
                    self.provenance.project_members(dest_pid, refresh=True)
                    if isinstance(dest_pid, str)
                    else None
                )
                if dest_members is None or not dest_members.issubset(cur_allowed):
                    raise DutyRefused(
                        "Cannot post message to a project whose members extend beyond "
                        "the audience of previously read peer messages or shared assets"
                    )
            elif name in ("request_peer_response", "offer_handoff"):
                dest_agent = args_map.get("recipient_id")
                if (
                    not isinstance(dest_agent, str)
                    or dest_agent.strip() not in cur_allowed
                ):
                    raise DutyRefused(
                        "Cannot send peer request or handoff to a recipient outside "
                        "the audience of previously read peer messages or shared assets"
                    )
            elif name == "respond_peer_request":
                req_agent = self.provenance.peer_request_requester()
                if req_agent is None or req_agent not in cur_allowed:
                    raise DutyRefused(
                        "Cannot respond to a peer requester outside the audience "
                        "of previously read peer messages or shared assets"
                    )
            elif name == "decline_handoff":
                raw_oid = args_map.get("offer_id")
                offerer_id, _ = (
                    self.provenance.handoff_offer_provenance(raw_oid)
                    if isinstance(raw_oid, str)
                    else (None, None)
                )
                if offerer_id is None or offerer_id not in cur_allowed:
                    raise DutyRefused(
                        "Cannot decline handoff with a message to an offerer outside "
                        "the audience of previously read peer messages or shared assets"
                    )
            else:
                raise DutyRefused(
                    "Cannot invoke custom outbound peer tool while shift is restricted "
                    "to a narrowed peer audience"
                )

    def record_after_call(self, name: str, args: Any, out: Any) -> Any:
        """Update taint/audience state after tool execution and filter tainted discovery outputs."""
        spec = self._specs_by_name.get(name)
        public_offload = self._is_public_offload_read(name, args)
        if is_private_desk_read(name, spec) and not public_offload:
            self.read_private_desk = True
        if is_peer_input(name, spec):
            self.read_peer_input = True
            self._record_peer_input_provenance(name, args, out)
        return self.filter_discovery_output(name, out)

    def _record_peer_input_provenance(self, name: str, args: Any, out: Any) -> None:
        if name == "read_messages" and isinstance(args, Mapping):
            pid = args.get("project_id")
            if isinstance(pid, str) and pid.strip():
                self.tainted_projects.add(pid.strip())
            msgs = out.get("messages") if isinstance(out, Mapping) else None
            with scoped_reader(self.provenance._store) as ro_db:
                if isinstance(msgs, list) and msgs:
                    for m in msgs:
                        m_mid = m.get("message_id") if isinstance(m, Mapping) else None
                        if isinstance(m_mid, str) and m_mid.strip():
                            m_pid, m_aud = self.provenance.message_audience(
                                m_mid.strip(), db=ro_db
                            )
                            if m_pid:
                                self.tainted_projects.add(m_pid)
                            self._narrow_audience(
                                m_aud if m_aud is not None else {self.agent_id}
                            )
                        else:
                            self._narrow_audience({self.agent_id})
                elif isinstance(pid, str) and pid.strip():
                    p_members = self.provenance.project_members(pid.strip(), db=ro_db)
                    self._narrow_audience(
                        p_members if p_members is not None else {self.agent_id}
                    )
                else:
                    self._narrow_audience({self.agent_id})
        elif name == "read_shared_asset" and isinstance(args, Mapping):
            aid = args.get("asset_id")
            if isinstance(aid, str) and aid.strip():
                pid, aud = self.provenance.asset_provenance(aid.strip())
                if pid:
                    self.tainted_projects.add(pid)
                self._narrow_audience(aud if aud is not None else {self.agent_id})
            else:
                self._narrow_audience({self.agent_id})
        elif name == "read_inbox" and isinstance(out, Mapping):
            delivs = out.get("deliveries")
            if isinstance(delivs, list):
                with scoped_reader(self.provenance._store) as ro_db:
                    for d in delivs:
                        if not isinstance(d, Mapping):
                            self._narrow_audience({self.agent_id})
                            continue
                        d_pid = d.get("project_id")
                        if isinstance(d_pid, str) and d_pid.strip():
                            self.tainted_projects.add(d_pid.strip())
                        d_mid = d.get("message_id")
                        if isinstance(d_mid, str) and d_mid.strip():
                            m_pid, m_aud = self.provenance.message_audience(
                                d_mid.strip(), db=ro_db
                            )
                            if m_pid:
                                self.tainted_projects.add(m_pid)
                            self._narrow_audience(
                                m_aud if m_aud is not None else {self.agent_id}
                            )
                        else:
                            self._narrow_audience({self.agent_id})
            else:
                self._narrow_audience({self.agent_id})
        elif name == "discover_handoffs" and isinstance(out, Mapping):
            offers = out.get("offers")
            if isinstance(offers, list):
                with scoped_reader(self.provenance._store) as ro_db:
                    for o in offers:
                        if not isinstance(o, Mapping):
                            self._narrow_audience({self.agent_id})
                            continue
                        o_aid = o.get("asset_id")
                        if isinstance(o_aid, str) and o_aid.strip():
                            pid, aud = self.provenance.asset_provenance(
                                o_aid.strip(), db=ro_db
                            )
                            if pid:
                                self.tainted_projects.add(pid)
                            self._narrow_audience(
                                aud if aud is not None else {self.agent_id}
                            )
                        else:
                            sender = o.get("sender_id")
                            if isinstance(sender, str) and sender.strip():
                                self._narrow_audience({self.agent_id, sender.strip()})
                            else:
                                self._narrow_audience({self.agent_id})
            else:
                self._narrow_audience({self.agent_id})
        else:
            # Fail closed for custom peer_input tools whose provenance audience is not resolved.
            self._narrow_audience({self.agent_id})

    def filter_discovery_output(self, name: str, out: Any) -> Any:
        if (
            name != "discover_project_references"
            or not self.read_peer_input
            or not isinstance(out, Mapping)
        ):
            return out
        allowed_projs: set[str] = set(self.tainted_projects)
        allowed_aud_now = self.allowed_audience
        filtered_projects = []
        for p_entry in out.get("projects") or ():
            if not isinstance(p_entry, Mapping):
                continue
            p_id = str(p_entry.get("project_id") or "")
            p_members_raw = p_entry.get("members") or ()
            p_members_set = {str(m) for m in p_members_raw if isinstance(m, str)}
            if p_id in allowed_projs or (
                allowed_aud_now is not None
                and p_members_set
                and p_members_set.issubset(allowed_aud_now)
            ):
                filtered_projects.append(p_entry)
                allowed_projs.add(p_id)
        filtered = dict(out)
        filtered["projects"] = filtered_projects
        filtered["shared_assets"] = [
            a
            for a in (out.get("shared_assets") or ())
            if isinstance(a, Mapping)
            and str(a.get("project_id") or "") in allowed_projs
        ]
        filtered["publication_receipts"] = [
            r
            for r in (out.get("publication_receipts") or ())
            if isinstance(r, Mapping)
            and str(r.get("project_id") or "") in allowed_projs
        ]
        return filtered

    def wrap_handler(
        self,
        name: str,
        fn: ToolHandler,
        *,
        lock_factory: Callable[[], Any] | None = None,
    ) -> ToolHandler:
        def _wrapped(args: dict[str, Any]) -> dict[str, Any]:
            self.check_before_call(name, args)
            if lock_factory is not None:
                with lock_factory():
                    out = fn(args)
            else:
                out = fn(args)
            return self.record_after_call(name, args, out)

        return _wrapped
