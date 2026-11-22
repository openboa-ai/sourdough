import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from observe_paper import observe


class ConnectedFlow(unittest.TestCase):
    def test_native_sdk_denial_releases_the_original_reservation_without_a_fill(self):
        tape = Path(__file__).with_name("causal-quotes.json")
        with TemporaryDirectory() as scratch:
            result = observe(
                Path(scratch) / "sdk-denial",
                None,
                tape,
                expected_lots=0,
                sdk_risk_denial=True,
            )
        self.assertIsNone(result["posting"])
        self.assertEqual(result["cancellation"]["reserved_atoms"], 0)
        self.assertEqual(
            result["sandbox_account"],
            {"position_lots": 0, "cash_atoms": 100_000_000_000},
        )

    def test_zero_fill_schedules_the_original_agents_follow_up(self):
        tape = Path(__file__).with_name("causal-quotes.json")
        with TemporaryDirectory() as scratch:
            result = observe(Path(scratch) / "zero-fill", None, tape, expected_lots=0)
        self.assertIsNone(result["posting"])
        self.assertEqual(
            (
                result["cancellation"]["cancelled_lots"],
                result["cancellation"]["reserved_atoms"],
            ),
            (20, 0),
        )
        self.assertEqual(
            result["sandbox_account"],
            {"position_lots": 0, "cash_atoms": 100_000_000_000},
        )

    def test_sdk_cancellation_releases_only_the_unfilled_remainder(self):
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            tape = root / "thin-quotes.json"
            scenario = json.loads(
                Path(__file__).with_name("causal-quotes.json").read_text()
            )
            scenario["quotes"][0]["ask_size"] = "0.00008"
            tape.write_text(json.dumps(scenario))
            result = observe(root / "replay", None, tape, expected_lots=8)
        self.assertEqual(
            result["cancellation"],
            {
                "original_order_id": result["posting"]["order_id"],
                "filled_lots": 8,
                "cancelled_lots": 12,
                "reserved_atoms": 400_000_000,
            },
        )
        self.assertTrue(result["verified_after_engine_exit"])

    def test_end_to_end(self):
        tape = Path(__file__).with_name("causal-quotes.json")
        research = [
            {
                "observation_id": identity,
                "price_minor_units": price,
                "observed_at": event,
                "available_at": available,
            }
            for identity, price, event, available in (
                ("previous", 49_000_000, 1699999998, 1699999999),
                ("available", 50_000_000, 1700000000, 1700000000),
                ("later", 70_000_000, 1700000001, 1700000002),
            )
        ]
        with TemporaryDirectory() as scratch:
            results = [
                observe(Path(scratch) / name, None, tape, research_inputs=research)
                for name in ("first", "independent")
            ]
        expected_cut = {
            "tape_revision": sha256(tape.read_bytes()).hexdigest(),
            "price_atoms_per_lot": 50_000_000,
            "event_ns": 1700000000000000000,
            "available_ns": 1700000000100000000,
        }
        for result in results:
            self.assertIsNone(result["market_activation"])
            self.assertEqual(result["connector"], "nautilus-offline-spot-replay-v1")
            self.assertEqual(result["replay_cut"], expected_cut)
            snapshot = result["research_snapshot"]
            self.assertEqual(snapshot["observations"], research[:2])
            self.assertEqual(snapshot["cut_at"], 1700000000)
            self.assertEqual(
                snapshot["replay_cut"],
                {
                    key: item
                    for key, item in expected_cut.items()
                    if key != "price_atoms_per_lot"
                },
            )
            self.assertEqual(result["posting"]["price_minor_units"], 50_000_000)
            self.assertEqual(result["posting"]["fee_minor_units"], 0)
            self.assertEqual(
                result["sandbox_account"],
                {"position_lots": 20, "cash_atoms": 99_000_000_000},
            )
            self.assertTrue(result["verified_after_engine_exit"])
        self.assertNotEqual(
            results[0]["posting"]["order_id"], results[1]["posting"]["order_id"]
        )
