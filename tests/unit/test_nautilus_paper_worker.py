import unittest
from unittest.mock import AsyncMock, patch

from sourdough.trading.nautilus_paper_worker import _drain_checkpoint
from sourdough.trading.execution import VenueRefused


class SandboxShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_the_original_checkpoint(self):
        checkpoint = AsyncMock(side_effect=(False, False, True))
        with (
            patch("sourdough.trading.nautilus_paper_worker.monotonic", return_value=10),
            patch("sourdough.trading.nautilus_paper_worker.asyncio.sleep") as wait,
        ):
            await _drain_checkpoint(checkpoint)
        self.assertEqual(checkpoint.await_count, 3)
        self.assertEqual(wait.await_count, 2)

    async def test_never_reports_a_clean_shutdown_without_a_checkpoint(self):
        checkpoint = AsyncMock(return_value=False)
        with (
            patch(
                "sourdough.trading.nautilus_paper_worker.monotonic",
                side_effect=(10, 10, 10.2, 15),
            ),
            patch("sourdough.trading.nautilus_paper_worker.asyncio.sleep") as wait,
            self.assertRaisesRegex(VenueRefused, "unresolved account checkpoint"),
        ):
            await _drain_checkpoint(checkpoint)
        checkpoint.assert_awaited_once()
        wait.assert_awaited_once()
