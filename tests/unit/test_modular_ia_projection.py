"""Unit tests for modular IA conditional per-Agent sandbox projection gating."""

from __future__ import annotations

import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.board_control import upsert_agent_profile
from sourdough.company import FoundingTerms, found
from sourdough.execution_tiers import AgentSandboxTools
from sourdough.ipc.projections import (
    read_agent_detail_projection,
    read_agents_graph_projection,
)
from sourdough.storage.db import open_writer, write_tx
from sourdough.writer_lease import hold_company_writer


class TestModularIaSandboxGating(unittest.TestCase):
    def test_conditional_sandbox_visibility_by_grant_and_activity(self) -> None:
        with TemporaryDirectory(prefix="sd-mod-ia-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"

            founding = found(
                store,
                FoundingTerms("sourdough-mod", "charter-v1", "paper", "profile-v1"),
            )
            company_id = founding.company_id
            ceo_id = founding.founder_id
            active_quant = "agent-active-quant"
            idle_quant = "agent-idle-quant"
            analyst = "agent-assessment-only"

            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    for aid, scope in [
                        (active_quant, "paper"),
                        (idle_quant, "paper"),
                        (analyst, "assessment"),
                    ]:
                        db.execute(
                            "INSERT INTO agent (id, company_id, execution_profile) VALUES (?, ?, ?)",
                            (aid, company_id, "profile-v1"),
                        )
                        db.execute(
                            "INSERT INTO standing_grant (id, agent_id, scope) VALUES (?, ?, ?)",
                            (f"grant-{aid}", aid, scope),
                        )

                upsert_agent_profile(
                    writer,
                    agent_id=active_quant,
                    display_name="Atlas Quant",
                    role_label="Quant Researcher",
                    monthly_budget_minor=500_000,
                )
                upsert_agent_profile(
                    writer,
                    agent_id=idle_quant,
                    display_name="Idle Quant",
                    role_label="Quant Researcher",
                    monthly_budget_minor=200_000,
                )
                upsert_agent_profile(
                    writer,
                    agent_id=analyst,
                    display_name="Mira Analyst",
                    role_label="Research Analyst",
                    monthly_budget_minor=200_000,
                )

                # Active quant opens a sandbox AND executes a trade.
                active_tools = AgentSandboxTools(writer=writer, agent_id=active_quant)
                active_tools.invoke(
                    "open_agent_sandbox",
                    {
                        "account_id": "atlas-sbx-1",
                        "starting_minor_units": 10_000_000,
                        "fee_bps": 4,
                        "slippage_bps": 2,
                    },
                )
                active_tools.invoke(
                    "submit_agent_sandbox_order",
                    {
                        "action_id": "act-mod-1",
                        "instrument_id": "BTC-USDT",
                        "side": "buy",
                        "quantity": 1,
                        "limit_minor_units": 6_600_000,
                        "market_price_minor_units": 6_500_000,
                    },
                )

                # Idle quant opens a sandbox with zero orders/fills.
                idle_tools = AgentSandboxTools(writer=writer, agent_id=idle_quant)
                idle_tools.invoke(
                    "open_agent_sandbox",
                    {
                        "account_id": "idle-sbx-1",
                        "starting_minor_units": 5_000_000,
                    },
                )

            graph, _ = read_agents_graph_projection(store, company_id)
            by_id = {n["agent_id"]: n for n in graph["nodes"]}

            # Active trading quant: authorized AND active sandbox badge populated.
            self.assertTrue(by_id[active_quant]["sandbox_authorized"])
            self.assertIsNotNone(by_id[active_quant]["sandbox_activity"])
            self.assertEqual(by_id[active_quant]["sandbox_activity"]["fill_count"], 1)
            self.assertEqual(
                by_id[active_quant]["sandbox_activity"]["open_positions_count"], 1
            )

            # Idle trading quant: authorized, but zero fills/orders -> badge hidden (None).
            self.assertTrue(by_id[idle_quant]["sandbox_authorized"])
            self.assertIsNone(by_id[idle_quant]["sandbox_activity"])

            # Non-trading assessment analyst: unauthorized & hidden.
            self.assertFalse(by_id[analyst]["sandbox_authorized"])
            self.assertIsNone(by_id[analyst]["sandbox_activity"])

            # Founder CEO with no sandbox activity: badge hidden.
            self.assertIsNone(by_id[ceo_id]["sandbox_activity"])

            # Agent detail also propagates sandbox_authorized accurately.
            active_detail, _ = read_agent_detail_projection(
                store, company_id, active_quant
            )
            analyst_detail, _ = read_agent_detail_projection(store, company_id, analyst)
            self.assertTrue(active_detail["sandbox_authorized"])
            self.assertIsNotNone(active_detail["agent_sandbox"])
            self.assertFalse(analyst_detail["sandbox_authorized"])
            self.assertIsNone(analyst_detail["agent_sandbox"])


if __name__ == "__main__":
    unittest.main()
