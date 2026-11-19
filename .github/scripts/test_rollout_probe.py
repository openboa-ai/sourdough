import unittest


class RolloutProbe(unittest.TestCase):
    @unittest.skip("Verify that a required test skip blocks this rollout PR")
    def test_required(self):
        self.fail("The rollout probe should be skipped")
