import json
import sys
import unittest
from dataclasses import asdict
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sourdough.company import FoundingTerms, found
from sourdough.trading.engine import (
    LOCK,
    VERSION,
    EngineRefused,
    _inspect_installation,
    prepare_engine,
)
from sourdough.writer_lease import hold_company_writer


class TradingEngineSetup(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        store = root / "company.sqlite"
        company = found(store, FoundingTerms("setup", "charter", "paper", "profile"))
        self.writer = self.enterContext(hold_company_writer(store, company.company_id))
        self.engine = root / "engine"

    def test_distribution_pin_is_packaged_with_hashes(self):
        requirements = files("sourdough").joinpath(LOCK).read_text().splitlines()
        pinned = [line for line in requirements if line.startswith("nautilus_trader==")]
        self.assertEqual(len(pinned), 1)
        self.assertTrue(
            pinned[0].startswith(f"nautilus_trader=={VERSION} --hash=sha256:")
        )
        for requirement in requirements:
            self.assertRegex(requirement, r"^\S+==\S+ --hash=sha256:[0-9a-f]{64}$")

    def test_unsupported_os_is_blocked_before_install(self):
        with (
            patch("sourdough.trading.engine.platform.system", return_value="Darwin"),
            patch("sourdough.trading.engine.platform.machine", return_value="arm64"),
            patch("sourdough.trading.engine.platform.mac_ver", return_value=("15.7",)),
            patch("sourdough.trading.engine._run") as invoke,
            self.assertRaisesRegex(EngineRefused, "macOS 26"),
        ):
            prepare_engine(self.writer, self.engine)
        invoke.assert_not_called()
        self.assertFalse(self.engine.exists())

    def test_install_failure_does_not_publish_an_installation(self):
        with (
            patch("sourdough.trading.engine._require_supported_platform"),
            patch(
                "sourdough.trading.engine._run",
                side_effect=EngineRefused("Wheel preparation failed"),
            ),
            self.assertRaisesRegex(EngineRefused, "Wheel preparation failed"),
        ):
            prepare_engine(self.writer, self.engine)
        self.assertEqual(list(self.engine.iterdir()), [])

    def test_an_installation_from_another_company_does_not_start(self):
        installed = self.engine / f"nautilus-{VERSION}"
        self.engine.mkdir(mode=0o700)
        installed.mkdir(mode=0o700)
        (installed / "installation.json").write_text(
            json.dumps({"company_id": "other", "lock_digest": "another"})
        )
        with (
            patch("sourdough.trading.engine._require_supported_platform"),
            patch("sourdough.trading.engine._run") as invoke,
            self.assertRaisesRegex(EngineRefused, "another scope or pin"),
        ):
            prepare_engine(self.writer, self.engine)
        invoke.assert_not_called()

    def test_engine_receipt_rejects_modified_files_before_import(self):
        self.engine.mkdir(mode=0o700)
        installed = self.engine.resolve() / f"nautilus-{VERSION}"
        path_alias = self.engine.parent / "alias"
        path_alias.symlink_to(self.engine.parent.resolve(), target_is_directory=True)
        requested = path_alias / self.engine.name
        installed.mkdir(mode=0o700)
        (installed / "home").mkdir(mode=0o700)
        runtime = installed / "runtime"
        runtime.mkdir(mode=0o700)
        (runtime / "bin").mkdir()
        (runtime / "bin/python").symlink_to(Path(sys._base_executable).resolve())
        module = runtime / "backtest.py"
        module.write_text("ORIGINAL = True")
        lock = sha256(files("sourdough").joinpath(LOCK).read_bytes()).hexdigest()
        probe = json.dumps(
            {
                "prefix": str(runtime),
                "version": VERSION,
                "record": "unchanged-wheel-record",
                "entrypoints": [
                    "BacktestEngine",
                    "BinanceLiveDataClientFactory",
                    "SandboxLiveExecClientFactory",
                    "TradingNode",
                ],
            }
        )
        with (
            patch("sourdough.trading.engine._require_supported_platform"),
            patch("sourdough.trading.engine._run", return_value=probe) as invoke,
        ):
            receipt = _inspect_installation(installed, self.writer.company_id, lock)
            (installed / "installation.json").write_text(json.dumps(asdict(receipt)))
            self.assertEqual(prepare_engine(self.writer, requested), receipt)
            module.write_text("ORIGINAL = False")
            invoke.reset_mock()
            with self.assertRaisesRegex(EngineRefused, "files differ"):
                prepare_engine(self.writer, requested)
            invoke.assert_not_called()
