"""Unit tests for execution-tiers UI projection enrichment (promotion hash & agent names)."""

from __future__ import annotations

import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.board_control import upsert_agent_profile
from sourdough.company import FoundingTerms, found
from sourdough.trading.accounts import (
    open_agent_sandbox,
    record_venue_qualification_receipt,
    register_live_candidate_binding,
)
from sourdough.trading.domain import (
    ENV_EXTERNAL_TEST,
    ENV_LIVE,
    build_promotion_payload_hash,
    compute_trading_core_digest,
)
from sourdough.trading.projections import read_trading_desk_projection
from sourdough.storage.db import open_writer, write_tx
from sourdough.writer_lease import hold_company_writer


class TierUiProjectionTests(unittest.TestCase):
    def test_projection_enriches_candidate_promotion_hash_and_sandbox_names(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="sd-tier-ui-u-") as tmp:
            root = Path(tmp)
            store = root / "c.sqlite"
            leases = root / "l"
            founding = found(
                store,
                FoundingTerms("sourdough-fund", "charter-v1", "paper", "profile-v1"),
            )
            company_id = founding.company_id
            quant_id = "agent-atlas"

            with hold_company_writer(
                store, company_id, lease_directory=leases
            ) as writer:
                with closing(open_writer(store, lease=writer)) as db, write_tx(db):
                    db.execute(
                        "INSERT INTO agent (id, company_id, execution_profile) VALUES (?, ?, ?)",
                        (quant_id, company_id, "profile-v1"),
                    )
                    db.execute(
                        "INSERT INTO standing_grant (id, agent_id, scope) VALUES (?, ?, ?)",
                        ("grant-atlas", quant_id, "paper"),
                    )
                upsert_agent_profile(
                    writer,
                    agent_id=quant_id,
                    display_name="Atlas Quant",
                    role_label="Quant Researcher",
                    monthly_budget_minor=500_000,
                )
                open_agent_sandbox(
                    writer,
                    agent_id=quant_id,
                    account_id="atlas-sandbox-01",
                    starting_minor_units=5_000_000,
                )
                cand = register_live_candidate_binding(
                    writer,
                    binding_id="binance-live-01",
                    account_id="binance-prod-01",
                    destination_id="binance-spot-prod",
                    connector_revision="binance-spot-v1",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    currency="USD",
                    starting_minor_units=25_000_000,
                    exposure_limit_minor_units=10_000_000,
                    credential_handle_ref="keychain:openboa/binance/prod-01",
                )

                # Before qualification: matching receipt & promotion hash must be None.
                before_desk, _ = read_trading_desk_projection(
                    store, company_id, environment=ENV_LIVE
                )
                before = before_desk["governance"]
                self.assertEqual(before_desk["charter_revision"], "charter-v1")
                self.assertEqual(len(before["live_candidates"]), 1)
                self.assertIsNone(
                    before["live_candidates"][0]["matching_qualification_receipt_id"]
                )
                self.assertIsNone(
                    before["live_candidates"][0]["promotion_payload_hash"]
                )

                # Stale core digest receipt does not qualify.
                record_venue_qualification_receipt(
                    writer,
                    receipt_id="qual-stale-core",
                    adapter_revision="binance-spot-v1",
                    environment=ENV_EXTERNAL_TEST,
                    destination_id="binance-spot-testnet",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    core_digest="0" * 64,
                    verdict="PASS",
                    cases_passed=6,
                    cases_failed=0,
                    cases_unrun=0,
                    evidence_sha256="a" * 64,
                )
                stale_proj, _ = read_trading_desk_projection(
                    store, company_id, environment=ENV_LIVE
                )
                self.assertIsNone(
                    stale_proj["governance"]["live_candidates"][0][
                        "matching_qualification_receipt_id"
                    ]
                )

                # Passing qualification matching current core populates exact server payload hash.
                record_venue_qualification_receipt(
                    writer,
                    receipt_id="qual-current-core",
                    adapter_revision="binance-spot-v1",
                    environment=ENV_EXTERNAL_TEST,
                    destination_id="binance-spot-testnet",
                    instrument_rules_digest="btc-usdt-spot-v1",
                    core_digest=compute_trading_core_digest(),
                    verdict="PASS",
                    cases_passed=6,
                    cases_failed=0,
                    cases_unrun=0,
                    evidence_sha256="b" * 64,
                )
                ready_proj, _ = read_trading_desk_projection(
                    store, company_id, environment=ENV_LIVE
                )
                expected_hash = build_promotion_payload_hash(
                    company_id=company_id,
                    charter_revision="charter-v1",
                    target_binding=cand,
                    qualification_receipt_id="qual-current-core",
                )
                self.assertEqual(
                    ready_proj["governance"]["live_candidates"][0][
                        "matching_qualification_receipt_id"
                    ],
                    "qual-current-core",
                )
                self.assertEqual(
                    ready_proj["governance"]["live_candidates"][0][
                        "promotion_payload_hash"
                    ],
                    expected_hash,
                )


if __name__ == "__main__":
    unittest.main()
