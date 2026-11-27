import unittest

from sourdough.trading.credentials import (
    ApiCredentialPair,
    CompositeCredentialProvider,
    CredentialRefused,
    EnvRefCredentialProvider,
    InMemoryTestCredentialProvider,
    assert_handle_matches_environment,
    parse_credential_handle,
)
from sourdough.trading.domain import ENV_EXTERNAL_TEST, ENV_LIVE, ENV_PAPER


class CredentialBoundaryTests(unittest.TestCase):
    def test_api_credential_pair_always_masks_secrets_in_repr_and_str(self):
        pair = ApiCredentialPair(
            api_key="emul-key-visible-check",
            api_secret="emul-secret-visible-check",
            handle_ref="envref:BINANCE_TESTNET_DEMO",
        )
        self.assertNotIn("emul-key-visible-check", repr(pair))
        self.assertNotIn("emul-secret-visible-check", repr(pair))
        self.assertNotIn("emul-secret-visible-check", str(pair))
        self.assertIn("envref:BINANCE_TESTNET_DEMO", repr(pair))

    def test_handle_parsing_and_environment_isolation(self):
        parsed = parse_credential_handle("keychain:openboa/binance-prod")
        self.assertEqual(
            (parsed.scheme, parsed.target), ("keychain", "openboa/binance-prod")
        )

        # Paper bindings can never resolve external credentials.
        with self.assertRaises(CredentialRefused):
            assert_handle_matches_environment("envref:BINANCE_TESTNET_01", ENV_PAPER)

        # Live bindings cannot resolve testnet/paper/sandbox handles.
        with self.assertRaises(CredentialRefused):
            assert_handle_matches_environment("envref:BINANCE_TESTNET_01", ENV_LIVE)
        with self.assertRaises(CredentialRefused):
            assert_handle_matches_environment("keychain:openboa/paper-acct", ENV_LIVE)

        # Testnet bindings cannot resolve live handles.
        with self.assertRaises(CredentialRefused):
            assert_handle_matches_environment(
                "envref:BINANCE_LIVE_MAIN", ENV_EXTERNAL_TEST
            )

        # Raw non-handle values are rejected.
        with self.assertRaises(CredentialRefused):
            parse_credential_handle("raw-unprefixed-value")

    def test_envref_and_composite_resolution(self):
        env_provider = EnvRefCredentialProvider(
            {
                "BINANCE_TESTNET_01_API_KEY": "emul-k-1",
                "BINANCE_TESTNET_01_API_SECRET": "emul-s-1",
            }
        )
        comp = CompositeCredentialProvider(env_provider=env_provider)
        resolved = comp.resolve(
            "envref:BINANCE_TESTNET_01", expected_environment=ENV_EXTERNAL_TEST
        )
        self.assertEqual(resolved.api_key, "emul-k-1")
        self.assertEqual(resolved.api_secret, "emul-s-1")

        with self.assertRaises(CredentialRefused):
            comp.resolve(
                "envref:MISSING_HANDLE", expected_environment=ENV_EXTERNAL_TEST
            )

    def test_in_memory_provider_enforces_environment_scope(self):
        provider = InMemoryTestCredentialProvider(
            {"envref:BINANCE_TESTNET_UNIT": ("k1", "s1")}
        )
        resolved = provider.resolve(
            "envref:BINANCE_TESTNET_UNIT", expected_environment=ENV_EXTERNAL_TEST
        )
        self.assertEqual(resolved.api_key, "k1")
        with self.assertRaises(CredentialRefused):
            provider.resolve(
                "envref:BINANCE_TESTNET_UNIT", expected_environment=ENV_LIVE
            )


if __name__ == "__main__":
    unittest.main()
