import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from observe_paper import observe


class OfflineReplay(unittest.TestCase):
    def test_reproducible_first_decision_as_of_quote_availability(self):
        tape = Path(__file__).with_name("causal-quotes.json")
        with TemporaryDirectory() as scratch:
            results = [
                observe(Path(scratch) / name, None, tape)
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
