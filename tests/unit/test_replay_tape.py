import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.trading.execution import VenueRefused
from sourdough.trading.replay_tape import ReplayTape


FIXTURE = Path(__file__).with_name("causal-quotes.json")


class ReplayTapeInput(unittest.TestCase):
    def test_exact_source_digest_and_event_availability(self):
        tape = ReplayTape.load(FIXTURE)
        self.assertEqual(tape.revision, sha256(FIXTURE.read_bytes()).hexdigest())
        self.assertEqual(tape.source, "verification-two-quote-cuts")
        self.assertEqual(tape.quotes[0].available_ns, 1700000000100000000)
        self.assertEqual(tape.quotes[0].event_ns, 1700000000000000000)
        self.assertLess(tape.quotes[0].available_ns, tape.quotes[1].available_ns)

    def test_rejects_early_duplicate_and_unaligned_quotes(self):
        with TemporaryDirectory() as scratch:
            altered = Path(scratch) / "altered.json"
            cases = (
                (
                    {"available_ns": 1700000000999999999},
                    "invalid price or availability",
                ),
                (
                    {
                        "event_ns": 1700000000000000000,
                        "available_ns": 1700000000100000000,
                    },
                    "strictly ordered by availability",
                ),
                ({"ask": "70000.001"}, "registered integer units"),
                ({"ask_size": "0.100001"}, "registered integer units"),
            )
            for changes, reason in cases:
                with self.subTest(changes=changes):
                    dataset = json.loads(FIXTURE.read_bytes())
                    dataset["quotes"][1].update(changes)
                    altered.write_text(json.dumps(dataset))
                    with self.assertRaisesRegex(VenueRefused, reason):
                        ReplayTape.load(altered)
