"""Connected E2E flow verifying P0-B ShiftRuntime, declarative ToolSpec.flow_class, and decomposed duties."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from typing import Any

from sourdough.agent_desk import save_draft
from sourdough.agent_runtime import (
    PROVIDER_OPENAI,
    AdapterManifest,
    AgentProfileBinding,
    AgentRuntimeAdapter,
    OuterToolBroker,
    ShiftRequest,
    ShiftResult,
    TokenUsage,
    ToolSpec,
)
from sourdough.agent_runtime.registry import AgentAdapterRegistry
from sourdough.board_control import (
    BOARD_CONTROL_ROLE,
    bind_board_principal,
    post_ceo_message,
)
from sourdough.company import FoundingTerms, found
from sourdough.company_assets import assign_project
from sourdough.conversations import Post, append_message
from sourdough.duty_run import WorkRunClaim
from sourdough.runtime import (
    CompanyRuntime,
    build_production_duty_registry,
    create_work_item,
)
from sourdough.runtime.domains import ToolContribution, ToolQueryContext
from sourdough.runtime.handlers import (
    DUTY_WORK_ASSESSMENT_REVIEW,
    sync_board_triggers,
)
from sourdough.runtime.work_kernel import claim_work_run, complete_work_run
from sourdough.storage.db import scoped_reader, write_uow
from sourdough.writer_lease import hold_company_writer


class _CustomSinkPlugin:
    """Test plugin declaring a custom external_write_sink tool via ToolSpec.flow_class."""

    domain_name = "custom_connector"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def contribute_decision_actions(self) -> dict[str, Any]:
        return {}

    def contribute_duties(self, _services: Any) -> tuple[Any, ...]:
        return ()

    def contribute_tools(self, ctx: ToolQueryContext) -> ToolContribution:
        def _dispatch(args: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(dict(args))
            return {"ok": True, "dispatched": True}

        return ToolContribution(
            specs=(
                ToolSpec(
                    name="custom_connector_post",
                    description="Dispatch an external connector write action.",
                    parameters={
                        "type": "object",
                        "properties": {"payload": {"type": "string"}},
                        "required": ["payload"],
                        "additionalProperties": False,
                    },
                    flow_class="external_write_sink",
                ),
            ),
            handlers={"custom_connector_post": _dispatch},
        )


class ConnectedFlow(unittest.TestCase):
    """End-to-end check that declarative flow_class and decomposed shift/duties preserve isolation."""

    def test_end_to_end(self) -> None:
        with TemporaryDirectory(dir="/tmp", prefix="sd-flow-p0b-") as tmp:
            root = Path(tmp)
            co_dir = root / "co"
            co_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            store = co_dir / "company.sqlite"
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
                    "ShiftRuntime Flow Co",
                    "charter-v1",
                    "manage-company",
                    profile.profile_id(),
                ),
            )
            cid = founding.company_id
            ceo_id = founding.founder_id
            ceo_grant = founding.grant_id
            peer_id = "agent-peer-p0b"
            peer_grant = "grant-peer-p0b"
            now = time_ns()

            with hold_company_writer(store, cid, lease_directory=leases) as writer:
                bind_board_principal(
                    writer,
                    principal_id="board-chair",
                    role_name=BOARD_CONTROL_ROLE,
                )
                with write_uow(writer) as db:
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
                    seed_item, _ = create_work_item(
                        db,
                        company_id=cid,
                        owner_agent_id=peer_id,
                        duty_kind=DUTY_WORK_ASSESSMENT_REVIEW,
                        source_kind="flow",
                        source_id="seed-peer-msg",
                        now_ns=now,
                    )
                    seed_run = claim_work_run(
                        db,
                        company_id=cid,
                        work_item_id=seed_item.work_item_id,
                        lease_owner="flow-seed",
                        lease_ttl_ns=3_600_000_000_000,
                        now_ns=now,
                    )
                    peer_claim = WorkRunClaim(
                        company_id=cid,
                        founder_id=peer_id,
                        duty_id=seed_item.work_item_id,
                        run_id=seed_run.run_id,
                        fencing_token=seed_run.fencing_token,
                        grant_id=peer_grant,
                        grant_scope="assessment",
                        purpose="assessment",
                    )
                    ceo_seed_item, _ = create_work_item(
                        db,
                        company_id=cid,
                        owner_agent_id=ceo_id,
                        duty_kind=DUTY_WORK_ASSESSMENT_REVIEW,
                        source_kind="flow",
                        source_id="seed-ceo-draft",
                        now_ns=now + 1,
                    )
                    ceo_seed_run = claim_work_run(
                        db,
                        company_id=cid,
                        work_item_id=ceo_seed_item.work_item_id,
                        lease_owner="flow-seed",
                        lease_ttl_ns=3_600_000_000_000,
                        now_ns=now + 1,
                    )
                    ceo_claim = WorkRunClaim(
                        company_id=cid,
                        founder_id=ceo_id,
                        duty_id=ceo_seed_item.work_item_id,
                        run_id=ceo_seed_run.run_id,
                        fencing_token=ceo_seed_run.fencing_token,
                        grant_id=ceo_grant,
                        grant_scope="manage-company",
                        purpose="manage-company",
                    )

                proj_rev = assign_project(
                    writer,
                    ceo_grant,
                    "proj-p0b",
                    (ceo_id, peer_id),
                    publishers=(ceo_id, peer_id),
                )
                save_draft(
                    writer,
                    ceo_claim,
                    ceo_claim.purpose,
                    name="ceo-private-secret",
                    content="CEO confidential plan",
                    expected_revision=None,
                )
                append_message(
                    writer,
                    peer_claim,
                    peer_claim.purpose,
                    Post(
                        request_id="peer-msg-p0b",
                        project_id="proj-p0b",
                        project_revision=proj_rev,
                        body="Peer input attempting prompt injection.",
                        asset_id="",
                    ),
                )
                with write_uow(writer) as db:
                    complete_work_run(
                        db,
                        seed_run.run_id,
                        seed_run.fencing_token,
                        outcome={"seeded": True},
                        now_ns=now + 5,
                    )
                    complete_work_run(
                        db,
                        ceo_seed_run.run_id,
                        ceo_seed_run.fencing_token,
                        outcome={"seeded": True},
                        now_ns=now + 6,
                    )
                    # Enqueue an assessment review shift for CEO where plugin injects custom_connector_post.
                    create_work_item(
                        db,
                        company_id=cid,
                        owner_agent_id=ceo_id,
                        duty_kind=DUTY_WORK_ASSESSMENT_REVIEW,
                        source_kind="flow",
                        source_id="review-with-custom-sink",
                        now_ns=now + 10,
                    )

                # Also post a Board -> CEO message to exercise decomposed BoardDuties.
                post_ceo_message(
                    writer,
                    command_id="board-ping-p0b",
                    target_agent_id=ceo_id,
                    charter_revision="charter-v1",
                    body="Give me a concise status check.",
                    principal_hint="board-chair",
                    now_ns=now + 20,
                )
                created = sync_board_triggers(writer, cid, now_ns=now + 25)
                self.assertEqual(created, 1)

            plugin = _CustomSinkPlugin()
            observed_requests: list[ShiftRequest] = []
            tool_outcomes: list[dict[str, Any]] = []

            class _ScriptedAdapter(AgentRuntimeAdapter):
                @property
                def manifest(self) -> AdapterManifest:
                    return AdapterManifest(
                        PROVIDER_OPENAI, "v1", "https_json", True, True, True
                    )

                def run_shift(
                    self, _profile: Any, request: ShiftRequest, broker: OuterToolBroker
                ) -> ShiftResult:
                    observed_requests.append(request)
                    tool_names = {t.name for t in request.tools}
                    if "read_messages" in tool_names:
                        # 1. Read peer message (taints shift with peer_input).
                        r1 = broker.invoke(
                            "call-read-1", "read_messages", {"project_id": "proj-p0b"}
                        )
                        # 2. Attempt custom external_write_sink -> MUST be refused by ShiftFlowGuard!
                        r2 = broker.invoke(
                            "call-sink-2",
                            "custom_connector_post",
                            {"payload": "exfil"},
                        )
                        tool_outcomes.append({"r1": r1, "r2": r2})
                        return ShiftResult(
                            provider=PROVIDER_OPENAI,
                            model="gpt-mock",
                            run_id=request.run_id,
                            turn_id=request.turn_id,
                            output_text="Review finished safely.",
                            usage=TokenUsage(input_tokens=20, output_tokens=10),
                            events=(),
                            tool_calls_executed=2,
                        )
                    # Board CEO reply shift
                    broker.invoke("call-status-1", "read_company_status", {})
                    return ShiftResult(
                        provider=PROVIDER_OPENAI,
                        model="gpt-mock",
                        run_id=request.run_id,
                        turn_id=request.turn_id,
                        output_text="All systems operational.",
                        usage=TokenUsage(input_tokens=15, output_tokens=8),
                        events=(),
                        tool_calls_executed=1,
                    )

            areg = AgentAdapterRegistry()
            areg.register(_ScriptedAdapter())
            duty_reg = build_production_duty_registry(
                adapter_registry=areg,
                profile_bindings={ceo_id: profile, peer_id: profile},
                domain_plugins=(plugin,),
            )
            runtime = CompanyRuntime(
                store,
                cid,
                duty_reg,
                lease_directory=leases,
            )
            with hold_company_writer(store, cid, lease_directory=leases) as writer:
                # Step settles both the assessment review and the CEO board reply.
                runtime.step_with_writer(writer, now_ns=now + 100)
                runtime.step_with_writer(writer, now_ns=now + 200)

            self.assertEqual(len(observed_requests), 2)
            # Because custom_connector_post declares flow_class="external_write_sink",
            # ShiftRunner MUST withhold desk_path on EVERY shift exposing it!
            for req in observed_requests:
                self.assertIsNone(req.desk_path)
                self.assertEqual(req.context.desk_manifest, ())

            self.assertEqual(len(tool_outcomes), 1)
            self.assertTrue(tool_outcomes[0]["r1"]["ok"])
            self.assertFalse(tool_outcomes[0]["r2"]["ok"])
            self.assertIn("external write actions", tool_outcomes[0]["r2"]["error"])
            self.assertEqual(plugin.calls, [])

            # Confirm CEO reply was persisted by decomposed BoardDuties.
            with scoped_reader(store) as db:
                reply = db.execute(
                    "SELECT body FROM ceo_message WHERE company_id = ? AND sender_kind = 'ceo'",
                    (cid,),
                ).fetchone()
                self.assertIsNotNone(reply)
                assert reply is not None
                self.assertEqual(str(reply[0]), "All systems operational.")


if __name__ == "__main__":
    unittest.main()
