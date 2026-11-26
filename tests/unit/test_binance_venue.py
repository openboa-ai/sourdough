import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.trading.binance_emulator import BinanceWireEmulator
from sourdough.trading.binance_venue import (
    MARKET_SPOT,
    MARKET_USDM_FUTURES,
    PINNED_BINANCE_ENDPOINTS,
    BinanceVenueAdapter,
    BinanceVenueConfig,
)
from sourdough.company import FoundingTerms, found
from sourdough.trading.credentials import InMemoryTestCredentialProvider
from sourdough.trading.domain import ENV_EXTERNAL_TEST, ENV_LIVE, ENV_PAPER
from sourdough.trading.external_venue import (
    ExternalEnvironmentMismatch,
    ExternalOrderRequest,
    ExternalVenueRefused,
)
from sourdough.trading.venue_qualification import run_binance_testnet_qualification
from sourdough.writer_lease import hold_company_writer


class BinanceVenueTests(unittest.TestCase):
    def test_hard_endpoint_pinning_and_loopback_guard(self):
        spot_test = BinanceVenueConfig(
            environment=ENV_EXTERNAL_TEST,
            market_kind=MARKET_SPOT,
            account_id="acct-t1",
            destination_id="binance-spot-testnet",
            credential_handle_ref="envref:BINANCE_TESTNET_SPOT",
        )
        self.assertEqual(
            spot_test.base_url,
            PINNED_BINANCE_ENDPOINTS[(ENV_EXTERNAL_TEST, MARKET_SPOT)],
        )

        fut_live = BinanceVenueConfig(
            environment=ENV_LIVE,
            market_kind=MARKET_USDM_FUTURES,
            account_id="acct-l1",
            destination_id="binance-usdm-prod",
            credential_handle_ref="keychain:openboa/binance-prod-usdm",
        )
        self.assertEqual(
            fut_live.base_url,
            PINNED_BINANCE_ENDPOINTS[(ENV_LIVE, MARKET_USDM_FUTURES)],
        )

        # Paper environment is unconditionally blocked.
        with self.assertRaises(ExternalEnvironmentMismatch):
            BinanceVenueConfig(
                environment=ENV_PAPER,
                market_kind=MARKET_SPOT,
                account_id="acct-p",
                destination_id="dest-p",
                credential_handle_ref="envref:BINANCE_TESTNET_SPOT",
            )

        # Live environment can NEVER override base_url, even to loopback.
        with self.assertRaises(ExternalEnvironmentMismatch):
            BinanceVenueConfig(
                environment=ENV_LIVE,
                market_kind=MARKET_SPOT,
                account_id="acct-l2",
                destination_id="dest-l2",
                credential_handle_ref="keychain:openboa/binance-prod-spot",
                base_url_override="http://127.0.0.1:9999",
                allow_loopback_override=True,
            )

        # external_test cannot override to non-loopback production host!
        with self.assertRaises(ExternalEnvironmentMismatch):
            BinanceVenueConfig(
                environment=ENV_EXTERNAL_TEST,
                market_kind=MARKET_SPOT,
                account_id="acct-t2",
                destination_id="dest-t2",
                credential_handle_ref="envref:BINANCE_TESTNET_SPOT",
                base_url_override="https://api.binance.com",
                allow_loopback_override=True,
            )

    def test_spot_and_usdm_futures_qualification_against_emulator(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        store = root / "c.sqlite"
        leases = root / "l"
        founding = found(
            store, FoundingTerms("binance-qual", "charter-v1", "paper", "profile-v1")
        )
        creds = InMemoryTestCredentialProvider(
            {
                "envref:BINANCE_TESTNET_SPOT": ("emul-spot-key", "emul-spot-secret"),
                "envref:BINANCE_TESTNET_FUT": ("emul-fut-key", "emul-fut-secret"),
                "envref:BINANCE_TESTNET_BAD": ("wrong-key", "wrong-secret"),
            }
        )

        for market, handle, dest in (
            (MARKET_SPOT, "envref:BINANCE_TESTNET_SPOT", "binance-spot-testnet"),
            (MARKET_USDM_FUTURES, "envref:BINANCE_TESTNET_FUT", "binance-usdm-testnet"),
        ):
            api_key, api_secret = (
                ("emul-spot-key", "emul-spot-secret")
                if market == MARKET_SPOT
                else ("emul-fut-key", "emul-fut-secret")
            )
            with BinanceWireEmulator(api_key=api_key, api_secret=api_secret) as emul:
                adapter = BinanceVenueAdapter(
                    BinanceVenueConfig(
                        environment=ENV_EXTERNAL_TEST,
                        market_kind=market,
                        account_id=f"acct-{market}",
                        destination_id=dest,
                        credential_handle_ref=handle,
                        base_url_override=emul.base_url,
                        allow_loopback_override=True,
                    ),
                    creds,
                )
                # Verify account observation works cleanly
                snap = adapter.observe_account()
                self.assertEqual(snap.balances["USDT"], Decimal("100000"))

                # Wrong credentials fail closed with 401 -> ExternalVenueRefused
                bad_adapter = BinanceVenueAdapter(
                    BinanceVenueConfig(
                        environment=ENV_EXTERNAL_TEST,
                        market_kind=market,
                        account_id=f"acct-bad-{market}",
                        destination_id=dest,
                        credential_handle_ref="envref:BINANCE_TESTNET_BAD",
                        base_url_override=emul.base_url,
                        allow_loopback_override=True,
                    ),
                    creds,
                )
                with self.assertRaisesRegex(ExternalVenueRefused, "authentication"):
                    bad_adapter.observe_account()

                with hold_company_writer(
                    store, founding.company_id, lease_directory=leases
                ) as writer:
                    report = run_binance_testnet_qualification(
                        writer,
                        adapter=adapter,
                        emulator=emul,
                        instrument_rules_digest=f"{market}-btcusdt-v1",
                    )
                self.assertEqual(report.receipt.verdict, "PASS")
                self.assertEqual(report.receipt.cases_passed, 6)
                self.assertEqual(report.receipt.cases_failed, 0)
                self.assertTrue(all(c.passed for c in report.cases))

                # Spot check submit + deterministic clientOrderId idempotence
                req = ExternalOrderRequest(
                    action_id=f"extra-{market}",
                    symbol="BTCUSDT",
                    side="SELL",
                    quantity=Decimal("0.01"),
                    limit_price=Decimal("50000"),
                )
                st_a = adapter.submit_order(req)
                st_b = adapter.submit_order(req)
                self.assertEqual(st_a.venue_order_id, st_b.venue_order_id)


if __name__ == "__main__":
    unittest.main()
