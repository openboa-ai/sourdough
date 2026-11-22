"""Provision an isolated, pinned NautilusTrader installation for a company."""

import json
import os
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass
from hashlib import file_digest, sha256
from importlib.resources import as_file, files
from pathlib import Path
from tempfile import TemporaryDirectory

from .writer_lease import WriterLease


VERSION = "1.231.0"
LOCK = "nautilus-macos-arm64.lock"
_PROBE = """
import hashlib, importlib.metadata, json, sys
from nautilus_trader.adapters.binance import BinanceLiveDataClientFactory
from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.live.node import TradingNode

distribution = importlib.metadata.distribution("nautilus-trader")
print(json.dumps({
    "prefix": sys.prefix,
    "version": distribution.version,
    "record": hashlib.sha256(distribution.read_text("RECORD").encode()).hexdigest(),
    "entrypoints": [component.__name__ for component in (
        BinanceLiveDataClientFactory, SandboxLiveExecClientFactory, BacktestEngine, TradingNode
    )],
}))
"""


class EngineRefused(Exception):
    """Trading runtime setup is blocked or its installation cannot be verified."""


@dataclass(frozen=True)
class EngineInstallation:
    company_id: str
    distribution_version: str
    lock_digest: str
    distribution_record_digest: str
    installed_content_digest: str


def _require_supported_platform() -> None:
    mac_version = platform.mac_ver()[0].split(".")[0]
    if (
        platform.system() != "Darwin"
        or platform.machine() != "arm64"
        or not mac_version.isdecimal()
        or int(mac_version) < 26
        or sys.version_info[:2] != (3, 14)
    ):
        raise EngineRefused("Pinned engine requires macOS 26+, ARM64, and CPython 3.14")


def _environment(home: Path) -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), "PIP_CONFIG_FILE": os.devnull}


def _run(command: list[str], home: Path, *, timeout: int) -> str:
    try:
        completed = subprocess.run(
            command,
            env=_environment(home),
            cwd=home,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (TimeoutError, subprocess.TimeoutExpired, OSError) as error:
        raise EngineRefused("Pinned engine setup command could not finish") from error
    if completed.returncode != 0:
        raise EngineRefused(
            f"Pinned engine setup command failed (exit {completed.returncode})"
        )
    return completed.stdout


def _check_private_directory(directory: Path) -> None:
    info = directory.stat(follow_symlinks=False)
    if (
        directory.is_symlink()
        or not directory.is_dir()
        or (info.st_uid != os.getuid() or info.st_mode & 0o077)
    ):
        raise EngineRefused("Engine directory must be an owned, private directory")


def _installed_content_digest(runtime: Path) -> str:
    digest = sha256()
    for item in sorted(runtime.rglob("*")):
        relative = os.fsencode(item.relative_to(runtime))
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        if item.is_symlink():
            target = os.fsencode(item.readlink())
            digest.update(b"l" + len(target).to_bytes(8, "big") + target)
        elif item.is_dir():
            digest.update(b"d")
        elif item.is_file():
            with item.open("rb") as file:
                digest.update(b"f" + file_digest(file, "sha256").digest())
        else:
            raise EngineRefused("Unexpected occupied path in the installed engine")
    return digest.hexdigest()


def _inspect_installation(
    root: Path,
    company_id: str,
    lock_digest: str,
    *,
    expected_content: str | None = None,
) -> EngineInstallation:
    _check_private_directory(root)
    home, runtime = root / "home", root / "runtime"
    _check_private_directory(home)
    interpreter = runtime / "bin" / "python"
    if (
        runtime.is_symlink()
        or interpreter.parent.is_symlink()
        or interpreter.resolve(strict=True) != Path(sys._base_executable).resolve()
    ):
        raise EngineRefused("Engine interpreter is not the pinned application Python")
    content = _installed_content_digest(runtime)
    if expected_content is not None and expected_content != content:
        raise EngineRefused("Installed engine files differ from the verified receipt")
    result = _run([str(interpreter), "-IB", "-c", _PROBE], home, timeout=30)
    try:
        probe = json.loads(result)
        if probe["prefix"] != str(runtime) or probe["version"] != VERSION:
            raise EngineRefused("Engine package or environment differs from its pin")
        entrypoints = {
            "BinanceLiveDataClientFactory",
            "SandboxLiveExecClientFactory",
            "BacktestEngine",
            "TradingNode",
        }
        if set(probe["entrypoints"]) != entrypoints:
            raise EngineRefused("Pinned engine entry points are unavailable")
        return EngineInstallation(
            company_id, VERSION, lock_digest, probe["record"], content
        )
    except (KeyError, TypeError, ValueError) as error:
        raise EngineRefused(
            "The engine installation did not identify itself"
        ) from error


def prepare_engine(
    writer: WriterLease,
    directory: Path,
    *,
    wheelhouse: Path | None = None,
    allow_install: bool = True,
) -> EngineInstallation:
    writer.verify()
    _require_supported_platform()
    if wheelhouse is not None:
        wheelhouse = wheelhouse.resolve(strict=True)
        if not wheelhouse.is_dir():
            raise EngineRefused("Expected an offline wheel directory")
    directory = directory.absolute()
    if allow_install:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    elif not directory.is_dir():
        raise EngineRefused("Prepare the pinned engine before launching it")
    _check_private_directory(directory)
    directory = directory.resolve()
    selected = directory / f"nautilus-{VERSION}"
    lock = files("sourdough").joinpath(LOCK)
    lock_digest = sha256(lock.read_bytes()).hexdigest()
    if selected.exists() or selected.is_symlink():
        _check_private_directory(selected)
        try:
            receipt = selected / "installation.json"
            if receipt.is_symlink():
                raise EngineRefused("Engine receipt must not be redirected")
            saved = json.loads(receipt.read_text())
        except (OSError, ValueError) as error:
            raise EngineRefused(
                "Expected a sealed engine installation receipt"
            ) from error
        if not isinstance(saved, dict) or (
            saved.get("company_id"),
            saved.get("lock_digest"),
        ) != (writer.company_id, lock_digest):
            raise EngineRefused(
                "Existing engine installation belongs to another scope or pin"
            )
        expected = saved.get("installed_content_digest")
        if not isinstance(expected, str) or len(expected) != 64:
            raise EngineRefused(
                "Existing engine receipt has no installed content fingerprint"
            )
        checked = _inspect_installation(
            selected, writer.company_id, lock_digest, expected_content=expected
        )
        if saved != asdict(checked):
            raise EngineRefused("Existing engine installation differs from its receipt")
        return checked
    if not allow_install:
        raise EngineRefused("Prepare the pinned engine before launching it")

    with TemporaryDirectory(prefix=".engine-stage-", dir=directory) as scratch:
        candidate = Path(scratch) / "candidate"
        candidate.mkdir(mode=0o700)
        home, runtime = candidate / "home", candidate / "runtime"
        home.mkdir(mode=0o700)
        _run([sys.executable, "-I", "-m", "venv", str(runtime)], home, timeout=60)
        source = (
            ["--no-index", "--find-links", str(wheelhouse)]
            if wheelhouse is not None
            else ["--index-url", "https://pypi.org/simple"]
        )
        with as_file(lock) as requirement:
            _run(
                [
                    str(runtime / "bin" / "python"),
                    "-I",
                    "-m",
                    "pip",
                    "--isolated",
                    "--disable-pip-version-check",
                    "install",
                    "--no-input",
                    "--require-hashes",
                    "--only-binary=:all:",
                    *source,
                    "-r",
                    str(requirement),
                ],
                home,
                timeout=300,
            )
        installed = _inspect_installation(candidate, writer.company_id, lock_digest)
        (candidate / "installation.json").write_text(json.dumps(asdict(installed)))
        writer.verify()
        candidate.rename(selected)
    return installed
