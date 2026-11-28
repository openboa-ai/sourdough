"""Connected E2E flow verifying P0 privacy redaction, 5-state handoffs, and event stream parity."""

from __future__ import annotations

import json
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sourdough.agent_desk import save_draft
from sourdough.agent_runtime import PROVIDER_OPENAI, AgentProfileBinding
from sourdough.board_control import BOARD_CONTROL_ROLE, bind_board_principal
from sourdough.company import FoundingTerms, found
from sourdough.company_assets import Selection, assign_project, publish_selection
from sourdough.duty_run import WorkRunClaim
from sourdough.ipc.server import CompanyDaemon
from sourdough.ipc.web_bridge import ClientWebBridge
from sourdough.method_learning import (
    distill_method_delta,
    opaque_one_agent_delta_id,
    opaque_one_agent_method_id,
)
from sourdough.runtime.work_kernel import (
    claim_work_run,
    complete_work_run,
    create_work_item,
)
from sourdough.storage.db import open_writer, write_tx
from sourdough.work_handoff import (
    HandoffOffer,
    accept_handoff,
    decline_handoff,
    expire_handoffs,
    offer_handoff,
    withdraw_handoff,
)
from sourdough.writer_lease import hold_company_writer


def _bridge_get_ndjson(url: str, token: str) -> tuple[int, list[dict[str, Any]]]:
    req = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/x-ndjson",
        },
        method="GET",
    )
    with urlopen(req, timeout=5.0) as resp:
        raw = resp.read().decode("utf-8")
        return resp.status, [
            json.loads(line) for line in raw.splitlines() if line.strip()
        ]


def _bridge_get_json(url: str, token: str) -> tuple[int, Any]:
    req = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=5.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, json.loads(body) if body else {}


def _open_claim(
    db: Any,
    *,
    company_id: str,
    agent_id: str,
    grant_id: str,
    grant_scope: str,
    purpose: str = "assessment_review",
    source_id: str,
    now_ns: int,
) -> tuple[Any, WorkRunClaim]:
    item, _ = create_work_item(
        db,
        company_id=company_id,
        owner_agent_id=agent_id,
        duty_kind="work.assessment_review",
        source_kind="flow",
        source_id=source_id,
        now_ns=now_ns,
    )
    run = claim_work_run(
        db,
        company_id=company_id,
        work_item_id=item.work_item_id,
        lease_owner="flow-worker",
        lease_ttl_ns=3_600_000_000_000,
        now_ns=now_ns,
    )
    claim = WorkRunClaim(
        company_id=company_id,
        founder_id=agent_id,
        duty_id=item.work_item_id,
        run_id=run.run_id,
        fencing_token=run.fencing_token,
        grant_id=grant_id,
        grant_scope=grant_scope,
        purpose=purpose,
    )
    return run, claim


class ConnectedFlow(unittest.TestCase):
    """End-to-end verification of P0 Board privacy redaction and 5-state handoff projections."""

    def test_end_to_end(self) -> None:
        secret_mid = "private-flow-canary-method-xyz"
        shared_mid = "company-flow-public-method"
        secret_draft_name = "confidential-flow-draft-canary"
        now_ns = time_ns()
        now_sec = now_ns // 1_000_000_000

        with TemporaryDirectory(dir="/tmp", prefix="sd-flow-p0-") as tmp:
            root = Path(tmp)
            co_dir = root / "co"
            co_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            store = co_dir / "company.sqlite"
            sock_dir = root / "sock"
            sock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            pending_dir = root / "pending"
            pending_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            leases = root / "leases"
            leases.mkdir(mode=0o700, parents=True, exist_ok=True)

            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-mock",
                revision="rev-1",
                credential_handle_ref="envref:OPENAI_API_KEY",
            )
            founding = found(
                store,
                FoundingTerms(
                    "Hardening Flow Co",
                    "charter-v1",
                    "manage-company",
                    profile.profile_id(),
                ),
            )
            cid = founding.company_id
            ceo_id = founding.founder_id
            ceo_grant = founding.grant_id
            peer_id = "agent-peer-p0"
            peer_grant = "grant-peer-p0"

            with hold_company_writer(store, cid, lease_directory=leases) as writer:
                bind_board_principal(
                    writer,
                    principal_id="board-chair",
                    role_name=BOARD_CONTROL_ROLE,
                )
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    db.execute(
                        "INSERT INTO agent VALUES (?, ?, ?)",
                        (peer_id, cid, profile.profile_id()),
                    )
                    db.execute(
                        "INSERT INTO standing_grant VALUES (?, ?, ?)",
                        (peer_grant, peer_id, "assessment"),
                    )
                    db.execute(
                        "INSERT INTO private_desk VALUES (?, ?)",
                        (peer_id, f"desk-{peer_id}"),
                    )
                    ceo_run, ceo_claim = _open_claim(
                        db,
                        company_id=cid,
                        agent_id=ceo_id,
                        grant_id=ceo_grant,
                        grant_scope="manage-company",
                        purpose="manage-company",
                        source_id="src-ceo-1",
                        now_ns=now_ns,
                    )
                    _, peer_claim = _open_claim(
                        db,
                        company_id=cid,
                        agent_id=peer_id,
                        grant_id=peer_grant,
                        grant_scope="assessment",
                        purpose="assessment",
                        source_id="src-peer-1",
                        now_ns=now_ns + 1,
                    )
                    priv_delta = distill_method_delta(
                        db,
                        ceo_claim,
                        delta_kind="ADD",
                        item_kind="work_method",
                        method_id=secret_mid,
                        trigger_spec={"duty_kind": "work.assessment_review"},
                        body="Private agent method body.",
                        audience_scope="one_agent",
                        audience_target_id=ceo_id,
                        source_refs=[f"work_run:{ceo_run.run_id}"],
                        now_ns=now_ns + 2,
                    )
                    pub_delta = distill_method_delta(
                        db,
                        ceo_claim,
                        delta_kind="ADD",
                        item_kind="work_method",
                        method_id=shared_mid,
                        trigger_spec={"duty_kind": "work.assessment_review"},
                        body="Shared company method body.",
                        audience_scope="company",
                        source_refs=[f"work_run:{ceo_run.run_id}"],
                        now_ns=now_ns + 3,
                    )

                proj_rev = assign_project(
                    writer,
                    ceo_grant,
                    "proj-p0-flow",
                    (ceo_id, peer_id),
                    publishers=(ceo_id,),
                )
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    complete_work_run(
                        db,
                        ceo_run.run_id,
                        ceo_run.fencing_token,
                        outcome={
                            "causal_method_trace": {
                                "available": [secret_mid, shared_mid],
                                "retrieved": [secret_mid],
                                "invoked": [
                                    f"{secret_mid}:{priv_delta.revision_sha256}"
                                ],
                            },
                            "proposals": [
                                {
                                    "delta_id": priv_delta.delta_id,
                                    "method_id": secret_mid,
                                    "revision_sha256": priv_delta.revision_sha256,
                                    "delta_kind": "ADD",
                                    "status": "draft",
                                }
                            ],
                            "patterns": [
                                {
                                    "kind": "stale_or_contradicted_skill",
                                    "method_id": secret_mid,
                                }
                            ],
                        },
                        now_ns=now_ns + 50,
                    )
                    _, ceo_handoff_claim = _open_claim(
                        db,
                        company_id=cid,
                        agent_id=ceo_id,
                        grant_id=ceo_grant,
                        grant_scope="manage-company",
                        purpose="manage-company",
                        source_id="src-ceo-handoff",
                        now_ns=now_ns + 60,
                    )

                draft = save_draft(
                    writer,
                    ceo_handoff_claim,
                    ceo_handoff_claim.purpose,
                    name=secret_draft_name,
                    content="Private draft content.",
                    expected_revision=None,
                )
                pub = publish_selection(
                    writer,
                    ceo_handoff_claim,
                    ceo_handoff_claim.purpose,
                    Selection(
                        request_id="pub-p0-flow",
                        draft_name=draft.name,
                        draft_revision=draft.revision,
                        project_id="proj-p0-flow",
                        project_revision=proj_rev,
                        audience=(ceo_id, peer_id),
                        start=0,
                        end=len(draft.content),
                    ),
                )

                offer_ids: dict[str, str] = {}
                for label, ttl in (
                    ("offered", 3600),
                    ("accepted", 3600),
                    ("declined", 3600),
                    ("withdrawn", 3600),
                    ("expired", 10),
                ):
                    offer_ids[label] = offer_handoff(
                        writer,
                        ceo_handoff_claim,
                        HandoffOffer(
                            request_id=f"flow-req-{label}",
                            recipient_id=peer_id,
                            asset_id=pub.asset_id,
                            expires_at=now_sec + ttl,
                            expected_result=f"Handoff {label}",
                        ),
                        now=now_sec,
                    )
                withdraw_handoff(
                    writer,
                    ceo_handoff_claim,
                    offer_ids["withdrawn"],
                    now=now_sec + 1,
                )
                decline_handoff(
                    writer,
                    peer_claim,
                    "assessment",
                    offer_ids["declined"],
                    now=now_sec + 2,
                    reason="busy",
                )
                accept_handoff(
                    writer,
                    peer_claim,
                    "assessment",
                    offer_ids["accepted"],
                    now=now_sec + 3,
                )
                expire_handoffs(writer, now=now_sec + 20)

            with (
                CompanyDaemon(
                    store,
                    cid,
                    socket_dir=sock_dir,
                    lease_directory=leases,
                ) as daemon,
                ClientWebBridge(
                    daemon.socket_path,
                    port=0,
                    pending_dir=pending_dir,
                    principal="board-chair",
                ) as bridge,
            ):
                base = bridge.info.url.rstrip("/")
                tok = bridge.info.session_token

                st_g, graph = _bridge_get_json(
                    f"{base}/api/v1/projections/agents-graph", tok
                )
                self.assertEqual(st_g, 200)
                st_d, detail = _bridge_get_json(
                    f"{base}/api/v1/projections/agents/{ceo_id}", tok
                )
                self.assertEqual(st_d, 200)
                st_a, audit = _bridge_get_json(
                    f"{base}/api/v1/projections/system-audit", tok
                )
                self.assertEqual(st_a, 200)
                st_e, events = _bridge_get_ndjson(
                    f"{base}/api/v1/events?after_seq=0&limit=100&wait_ms=0", tok
                )
                self.assertEqual(st_e, 200)
                self.assertTrue(any(e["entity_type"] == "method_delta" for e in events))

                combined_wire = json.dumps(
                    {
                        "graph": graph,
                        "detail": detail,
                        "audit": audit,
                        "events": events,
                    }
                )
                self.assertNotIn(secret_mid, combined_wire)
                self.assertNotIn(secret_draft_name, combined_wire)
                self.assertIn(opaque_one_agent_method_id(secret_mid), combined_wire)
                self.assertIn(
                    opaque_one_agent_delta_id(secret_mid, priv_delta.revision_sha256),
                    combined_wire,
                )
                self.assertIn(pub_delta.delta_id, combined_wire)

                # Verify 5-state handoffs across both graph and agent-detail HTTP projections
                graph_handoffs = {
                    e["edge_id"].removeprefix("handoff-"): e
                    for e in graph["edges"]
                    if e["kind"] == "handoff"
                }
                detail_handoffs = {h["offer_id"]: h for h in detail["tabs"]["handoffs"]}
                for label, oid in offer_ids.items():
                    self.assertEqual(graph_handoffs[oid]["state"], label)
                    self.assertEqual(graph_handoffs[oid]["active"], label == "offered")
                    self.assertEqual(detail_handoffs[oid]["state"], label)
                    self.assertEqual(
                        detail_handoffs[oid]["accepted"], label == "accepted"
                    )

                # Verify opaque private draft count surfaced accurately
                self.assertEqual(detail["desk_workspace"]["private_draft_count"], 1)


if __name__ == "__main__":
    unittest.main()
