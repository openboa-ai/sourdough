"""Unit tests for unified Core SecretVault, masking, isolation, and compatibility wrappers."""

from __future__ import annotations

import base64
import unittest
import urllib.parse
from unittest.mock import patch

from sourdough.agent_runtime.protocol import (
    AgentRuntimeRefused,
    EnvSecretResolver,
    InMemorySecretResolver,
    MaskedSecret as RuntimeMaskedSecret,
)
from sourdough.secret_vault import (
    CredentialRefused,
    HostSecretVault,
    InMemorySecretVault,
    MaskedSecret,
    MaskedSecretBundle,
    assert_handle_environment_isolation,
    parse_secret_handle,
    redact_secret_structure,
    redact_secret_values,
)
from sourdough.trading.credentials import (
    EnvRefCredentialProvider,
    InMemoryTestCredentialProvider,
)


class UnifiedSecretVaultTests(unittest.TestCase):
    def test_masked_secret_and_bundle_never_expose_raw_values_in_repr_or_str(
        self,
    ) -> None:
        raw = "sk-top-secret-canary-123456"
        s = MaskedSecret(value=raw, handle_ref="envref:OPENAI_KEY")
        self.assertNotIn(raw, repr(s))
        self.assertNotIn(raw, str(s))

        b = MaskedSecretBundle(
            values={"api_key": "k-secret-111", "api_secret": "s-secret-222"},
            handle_ref="envref:VENUE_CRED",
        )
        self.assertNotIn("k-secret-111", repr(b))
        self.assertNotIn("s-secret-222", str(b))
        self.assertEqual(b.get("api_key"), "k-secret-111")
        with self.assertRaises(CredentialRefused):
            b.get("missing")

    def test_handle_validation_and_environment_isolation(self) -> None:
        parsed = parse_secret_handle("envref:OPENAI_TESTNET_KEY")
        self.assertEqual(parsed.scheme, "envref")
        self.assertEqual(parsed.target, "OPENAI_TESTNET_KEY")

        with self.assertRaises(CredentialRefused):
            parse_secret_handle("sk-raw-secret-not-a-handle")

        # Live rejects testnet/paper/mock handles
        with self.assertRaises(CredentialRefused):
            assert_handle_environment_isolation("envref:BINANCE_TESTNET", "live")
        # External test rejects live handles
        with self.assertRaises(CredentialRefused):
            assert_handle_environment_isolation(
                "envref:BINANCE_LIVE_MAIN", "external_test"
            )
        # Paper rejects external venue handles by default
        with self.assertRaises(CredentialRefused):
            assert_handle_environment_isolation("envref:BINANCE_DEMO", "paper")
        # Non-venue connectors can opt into allow_paper=True (still rejecting 'live')
        assert_handle_environment_isolation(
            "envref:OPENAI_DEV_KEY", "paper", allow_paper=True
        )
        with self.assertRaises(CredentialRefused):
            assert_handle_environment_isolation(
                "envref:OPENAI_LIVE_KEY", "paper", allow_paper=True
            )

    def test_host_secret_vault_env_and_keychain(self) -> None:
        vault = HostSecretVault(
            {
                "OPENAI_API_KEY": "sk-unit-env-999",
                "VENUE_DEMO_API_KEY": "vk-123",
                "VENUE_DEMO_API_SECRET": "vs-456",
            }
        )
        sec = vault.resolve_secret("envref:OPENAI_API_KEY")
        self.assertEqual(sec.value, "sk-unit-env-999")
        bundle = vault.resolve_bundle(
            "envref:VENUE_DEMO",
            ("api_key", "api_secret"),
            expected_environment="external_test",
        )
        self.assertEqual(bundle.get("api_key"), "vk-123")
        self.assertEqual(bundle.get("api_secret"), "vs-456")

        class _FakeProc:
            returncode = 0
            stdout = "kc-secret-value\n"

        with patch("sourdough.secret_vault.subprocess.run", return_value=_FakeProc()):
            kc = vault.resolve_secret("keychain:sourdough.openai/bot")
            self.assertEqual(kc.value, "kc-secret-value")

    def test_secret_redaction_scrubs_raw_urlencoded_and_base64(self) -> None:
        raw = "sk-secret/token+with=chars!99"
        url_enc = urllib.parse.quote(raw, safe="")
        b64 = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        sample = f"echo raw={raw} url={url_enc} b64={b64}"
        scrubbed = redact_secret_values(sample, [raw])
        self.assertNotIn(raw, scrubbed)
        self.assertNotIn(url_enc, scrubbed)
        self.assertNotIn(b64, scrubbed)
        self.assertEqual(scrubbed.count("[REDACTED_SECRET]"), 3)

        nested = {"nested": [raw, {"k": b64}], raw: "key"}
        clean_nested = redact_secret_structure(nested, [raw])
        self.assertNotIn(raw, str(clean_nested))
        self.assertNotIn(b64, str(clean_nested))

    def test_backward_compatible_runtime_and_trading_wrappers(self) -> None:
        r_mem = InMemorySecretResolver({"envref:TEST_LLM": "sk-compat-1"})
        sec = r_mem.resolve_secret("envref:TEST_LLM")
        self.assertIsInstance(sec, RuntimeMaskedSecret)
        self.assertEqual(sec.value, "sk-compat-1")
        with self.assertRaises(AgentRuntimeRefused):
            r_mem.resolve_secret("envref:MISSING_LLM")

        r_env = EnvSecretResolver({"MY_LLM_KEY": "sk-compat-2"})
        self.assertEqual(r_env.resolve_secret("envref:MY_LLM_KEY").value, "sk-compat-2")

        t_env = EnvRefCredentialProvider(
            {"BINANCE_TEST_API_KEY": "tk-1", "BINANCE_TEST_API_SECRET": "ts-1"}
        )
        pair = t_env.resolve(
            "envref:BINANCE_TEST", expected_environment="external_test"
        )
        self.assertEqual(pair.api_key, "tk-1")
        self.assertEqual(pair.api_secret, "ts-1")

        t_mem = InMemoryTestCredentialProvider(
            {"envref:BINANCE_TESTNET": ("tk-2", "ts-2")}
        )
        pair2 = t_mem.resolve(
            "envref:BINANCE_TESTNET", expected_environment="external_test"
        )
        self.assertEqual(pair2.api_key, "tk-2")
        with self.assertRaises(CredentialRefused):
            t_mem.resolve("envref:BINANCE_TESTNET", expected_environment="live")

    def test_in_memory_secret_vault(self) -> None:
        vault = InMemorySecretVault(
            {
                "envref:DEV_LLM": "sk-dev-123",
                "envref:TEST_EX": {"api_key": "k1", "api_secret": "s1"},
            }
        )
        self.assertEqual(
            vault.resolve_secret("envref:DEV_LLM", expected_environment="paper").value,
            "sk-dev-123",
        )
        self.assertEqual(
            vault.resolve_bundle(
                "envref:TEST_EX",
                ("api_key", "api_secret"),
                expected_environment="external_test",
            ).get("api_secret"),
            "s1",
        )


if __name__ == "__main__":
    unittest.main()
