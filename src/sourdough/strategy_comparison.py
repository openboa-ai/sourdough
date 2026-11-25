"""StrategyRelease registry, PIT catalog verification, isolated paired arms,
anytime-valid sequential evaluation, and revocable strategy deployment (Flow 09).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from fractions import Fraction
from hashlib import sha256
from importlib import resources
from pathlib import Path
from sqlite3 import Connection
from time import time_ns
from typing import Any
from uuid import uuid4

from .company import FoundingTerms, _identity, found
from .duty_run import employment_cutoff
from .paper_account import (
    BindingRefused,
    has_pending_binding_change,
    read_current_binding,
)
from .runtime.registry import DutyContext, DutyDescriptor, DutyOutcome, DutyRegistry
from .storage.db import StorageRefused, open_reader, open_writer, read_tx, write_tx
from .storage.kernel import (
    CommandConflict,
    admit_command_receipt,
    append_audit_event,
)
from .storage.schema import enable_domain_v1, is_domain_installed
from .writer_lease import WriterLease

STRATEGY_DEPLOY_CONTRACT = "strategy-deploy-v1"
ACTIVATE_STRATEGY_CMD = "activate_strategy_release"
ROLLBACK_STRATEGY_CMD = "rollback_strategy_deployment"
CHALLENGER_EVAL_DUTY_KIND = "quant.challenger_eval"

_MAX_I64 = 2**63 - 1
_MAX_RETURN_BPS = 1e9
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

AuthorizeDeployment = Callable[[Connection, str, str, str], str | None]


class StrategyComparisonRefused(Exception):
    """Strategy registration, PIT verification, paired trial, or deployment refused."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise StrategyComparisonRefused(
            "Payload cannot be encoded as finite canonical JSON"
        ) from error


def _sha256_bytes(data: bytes) -> str:
    return sha256(data).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _require_token(value: str, label: str, *, max_len: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= max_len
        or not value.strip()
        or "\x00" in value
    ):
        raise StrategyComparisonRefused(f"Invalid {label}")
    return value


def _require_safe_id(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or ".." in value
        or _SAFE_ID_RE.fullmatch(value) is None
    ):
        raise StrategyComparisonRefused(
            f"{label} must be a safe identifier (alphanumeric, '.', '_', '-')"
        )
    return value


def _require_sha256(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise StrategyComparisonRefused(f"{label} must be a 64-char hex SHA-256")
    return value


def _require_safe_relative_path(rel_path: str, label: str) -> Path:
    _require_token(rel_path, label, max_len=512)
    if (
        rel_path.startswith("/")
        or rel_path.startswith("\\")
        or "\\" in rel_path
        or rel_path.endswith("/")
    ):
        raise StrategyComparisonRefused(f"{label} must be a canonical relative path")
    parts = Path(rel_path).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise StrategyComparisonRefused(f"{label} must be a canonical relative path")
    if "/".join(parts) != rel_path:
        raise StrategyComparisonRefused(f"{label} must be a canonical relative path")
    return Path(*parts)


def _reject_symlinks_under(root: Path, rel: Path, label: str) -> Path:
    """Walk every path component below root and refuse any symlink or escape."""
    resolved_root = root.resolve()
    current = resolved_root
    for part in rel.parts:
        current = current / part
        try:
            info = current.stat(follow_symlinks=False)
        except OSError as error:
            raise StrategyComparisonRefused(f"{label} is unavailable: {rel}") from error
        if stat.S_ISLNK(info.st_mode):
            raise StrategyComparisonRefused(f"Symlink prohibited inside {label}: {rel}")
    resolved_target = current.resolve()
    if resolved_target != resolved_root and not resolved_target.is_relative_to(
        resolved_root
    ):
        raise StrategyComparisonRefused(f"{label} escapes root directory")
    return current


def _reject_symlinks_in_absolute_path(
    path: Path, label: str, *, allow_missing_leaf: bool = False
) -> Path:
    """Reject symlinks on the leaf (and any relative component from root) of an absolute path."""
    if "\x00" in str(path):
        raise StrategyComparisonRefused(f"Invalid NUL byte in {label}")
    abs_path = path.absolute()
    # Walk from root downward; tolerate macOS system symlinks (/var -> /private/var, /tmp -> /private/tmp)
    # ONLY when checking leaf vs ancestors above existing parents? Better: check every existing
    # component starting from the deepest non-system prefix, and ALWAYS reject symlink on the leaf
    # and its immediate parent directory!
    try:
        leaf_info = abs_path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        if not allow_missing_leaf:
            raise StrategyComparisonRefused(f"{label} is unavailable") from error
    except OSError as error:
        raise StrategyComparisonRefused(f"{label} is unavailable") from error
    else:
        if stat.S_ISLNK(leaf_info.st_mode):
            raise StrategyComparisonRefused(f"Symlink prohibited for {label}")
    try:
        parent_info = abs_path.parent.stat(follow_symlinks=False)
    except OSError as error:
        raise StrategyComparisonRefused(
            f"Parent directory unavailable for {label}"
        ) from error
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise StrategyComparisonRefused(
            f"Parent directory for {label} cannot be a symlink"
        )
    return abs_path


def pinned_nautilus_lock_sha256() -> str:
    """Return the exact SHA-256 of the packaged nautilus_trader==1.231.0 lockfile."""
    lock_bytes = (
        resources.files("sourdough").joinpath("nautilus-macos-arm64.lock").read_bytes()
    )
    return _sha256_bytes(lock_bytes)


def _hash_regular_file(path: Path) -> str:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise StrategyComparisonRefused(f"File unavailable: {path}") from error
    if not stat.S_ISREG(info.st_mode):
        raise StrategyComparisonRefused(f"Expected a regular non-symlink file: {path}")
    digest = sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class PitFileEntry:
    relative_path: str
    sha256: str
    valid_from_ns: int
    valid_through_ns: int
    available_at_ns: int

    def __post_init__(self) -> None:
        _require_safe_relative_path(self.relative_path, "PIT relative_path")
        _require_sha256(self.sha256, "PIT file sha256")
        if (
            type(self.valid_from_ns) is not int
            or type(self.valid_through_ns) is not int
            or type(self.available_at_ns) is not int
            or not 0 < self.valid_from_ns < self.valid_through_ns <= _MAX_I64
            or not self.valid_from_ns <= self.available_at_ns <= _MAX_I64
        ):
            raise StrategyComparisonRefused("Invalid bitemporal PIT file bounds")


@dataclass(frozen=True)
class PitCatalogManifest:
    manifest_id: str
    company_id: str
    dataset_name: str
    valid_from_ns: int
    valid_through_ns: int
    available_at_ns: int
    files: tuple[PitFileEntry, ...]
    manifest_sha256: str
    recorded_at_ns: int


def _compute_pit_manifest_sha256(
    *,
    company_id: str,
    dataset_name: str,
    valid_from_ns: int,
    valid_through_ns: int,
    available_at_ns: int,
    files: tuple[PitFileEntry, ...],
) -> str:
    ordered = tuple(sorted(files, key=lambda item: item.relative_path))
    return _sha256_json(
        {
            "company_id": company_id,
            "dataset_name": dataset_name,
            "valid_from_ns": valid_from_ns,
            "valid_through_ns": valid_through_ns,
            "available_at_ns": available_at_ns,
            "files": [asdict(item) for item in ordered],
        }
    )


def build_pit_file_entries(
    catalog_root: Path,
    entries: tuple[tuple[str, int, int, int], ...],
) -> tuple[PitFileEntry, ...]:
    """Hash regular non-symlink files under catalog_root for immutable PitFileEntry tuples."""
    if not entries:
        raise StrategyComparisonRefused(
            "PIT catalog manifest requires at least one file"
        )
    result: list[PitFileEntry] = []
    for rel_str, valid_from_ns, valid_through_ns, available_at_ns in entries:
        rel = _require_safe_relative_path(rel_str, "PIT relative_path")
        target = _reject_symlinks_under(catalog_root, rel, "PIT catalog")
        result.append(
            PitFileEntry(
                relative_path=rel_str,
                sha256=_hash_regular_file(target),
                valid_from_ns=valid_from_ns,
                valid_through_ns=valid_through_ns,
                available_at_ns=available_at_ns,
            )
        )
    return tuple(sorted(result, key=lambda item: item.relative_path))


def register_pit_catalog_manifest(
    writer: WriterLease,
    *,
    manifest_id: str,
    dataset_name: str,
    files: tuple[PitFileEntry, ...],
) -> PitCatalogManifest:
    writer.verify()
    _require_safe_id(manifest_id, "manifest_id")
    _require_token(dataset_name, "dataset_name")
    if not files:
        raise StrategyComparisonRefused(
            "PIT catalog manifest requires at least one file"
        )
    ordered = tuple(sorted(files, key=lambda item: item.relative_path))
    if len({item.relative_path for item in ordered}) != len(ordered):
        raise StrategyComparisonRefused("Duplicate file path in PIT catalog manifest")
    valid_from_ns = min(item.valid_from_ns for item in ordered)
    valid_through_ns = max(item.valid_through_ns for item in ordered)
    available_at_ns = max(item.available_at_ns for item in ordered)
    files_json = _canonical_json([asdict(item) for item in ordered])
    manifest_sha256 = _compute_pit_manifest_sha256(
        company_id=writer.company_id,
        dataset_name=dataset_name,
        valid_from_ns=valid_from_ns,
        valid_through_ns=valid_through_ns,
        available_at_ns=available_at_ns,
        files=ordered,
    )

    with (
        closing(open_writer(writer.store, lease=writer)) as db,
        write_tx(db),
    ):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise StrategyComparisonRefused("PIT catalog belongs to another company")
        enable_domain_v1(
            db, "paper", company_id=writer.company_id, actor_id="strategy_comparison"
        )
        existing = db.execute(
            """SELECT manifest_id, company_id, dataset_name, valid_from_ns,
                      valid_through_ns, available_at_ns, files_json,
                      manifest_sha256, recorded_at_ns
               FROM paper_pit_catalog_manifest
               WHERE manifest_id = ?""",
            (manifest_id,),
        ).fetchone()
        if existing is not None:
            if existing[7] != manifest_sha256 or existing[1] != writer.company_id:
                raise StrategyComparisonRefused(
                    "PIT catalog manifest ID conflicts with prior registration"
                )
            return PitCatalogManifest(
                manifest_id=existing[0],
                company_id=existing[1],
                dataset_name=existing[2],
                valid_from_ns=existing[3],
                valid_through_ns=existing[4],
                available_at_ns=existing[5],
                files=tuple(PitFileEntry(**row) for row in json.loads(existing[6])),
                manifest_sha256=existing[7],
                recorded_at_ns=existing[8],
            )
        if (
            db.execute(
                "SELECT 1 FROM paper_pit_catalog_manifest WHERE manifest_sha256 = ?",
                (manifest_sha256,),
            ).fetchone()
            is not None
        ):
            raise StrategyComparisonRefused(
                "Identical PIT catalog manifest is already registered under another ID"
            )
        now_ns = time_ns()
        db.execute(
            """INSERT INTO paper_pit_catalog_manifest (
                   manifest_id, company_id, dataset_name, valid_from_ns,
                   valid_through_ns, available_at_ns, files_json,
                   manifest_sha256, recorded_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                manifest_id,
                writer.company_id,
                dataset_name,
                valid_from_ns,
                valid_through_ns,
                available_at_ns,
                files_json,
                manifest_sha256,
                now_ns,
            ),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="paper",
            entity_type="paper_pit_catalog_manifest",
            entity_id=manifest_id,
            event_type="paper.pit_catalog_registered",
            actor_id="strategy_comparison",
            payload={
                "manifest_id": manifest_id,
                "dataset_name": dataset_name,
                "manifest_sha256": manifest_sha256,
                "file_count": len(ordered),
            },
            recorded_at_ns=now_ns,
        )
        writer.verify()
        return PitCatalogManifest(
            manifest_id=manifest_id,
            company_id=writer.company_id,
            dataset_name=dataset_name,
            valid_from_ns=valid_from_ns,
            valid_through_ns=valid_through_ns,
            available_at_ns=available_at_ns,
            files=ordered,
            manifest_sha256=manifest_sha256,
            recorded_at_ns=now_ns,
        )


def verify_pit_manifest(
    manifest: PitCatalogManifest,
    catalog_root: Path,
    *,
    decision_cut_ns: int,
) -> None:
    """Verify manifest digest, catalog file digests on disk, and anti-lookahead bounds."""
    if type(decision_cut_ns) is not int or not 0 < decision_cut_ns <= _MAX_I64:
        raise StrategyComparisonRefused("decision_cut_ns must be a valid positive int")
    if not manifest.files:
        raise StrategyComparisonRefused("PIT catalog manifest cannot be empty")
    expected_sha = _compute_pit_manifest_sha256(
        company_id=manifest.company_id,
        dataset_name=manifest.dataset_name,
        valid_from_ns=manifest.valid_from_ns,
        valid_through_ns=manifest.valid_through_ns,
        available_at_ns=manifest.available_at_ns,
        files=manifest.files,
    )
    if expected_sha != manifest.manifest_sha256:
        raise StrategyComparisonRefused(
            "PIT catalog manifest digest does not match its sealed contents"
        )
    if (
        manifest.available_at_ns > decision_cut_ns
        or manifest.valid_through_ns > decision_cut_ns
    ):
        raise StrategyComparisonRefused(
            "PIT lookahead violation: manifest extends beyond decision cut"
        )
    for entry in manifest.files:
        if entry.available_at_ns > decision_cut_ns:
            raise StrategyComparisonRefused(
                f"PIT lookahead violation: {entry.relative_path} unavailable at decision cut"
            )
        if entry.valid_through_ns > decision_cut_ns:
            raise StrategyComparisonRefused(
                f"PIT lookahead violation: {entry.relative_path} extends past decision cut"
            )
        rel = _require_safe_relative_path(entry.relative_path, "PIT relative_path")
        target = _reject_symlinks_under(catalog_root, rel, "PIT catalog")
        actual = _hash_regular_file(target)
        if actual != entry.sha256:
            raise StrategyComparisonRefused(
                f"PIT catalog artifact digest mismatch for {entry.relative_path}"
            )


@dataclass(frozen=True)
class StrategyRelease:
    release_id: str
    company_id: str
    owner_agent_id: str
    parent_release_id: str | None
    strategy_source_sha256: str
    parameters_json: str
    nautilus_lock_sha256: str
    instrument_rules_sha256: str
    risk_config_sha256: str
    fill_policy_sha256: str
    pit_manifest_id: str
    pit_manifest_sha256: str
    research_trial_count_n: int
    bundle_sha256: str
    recorded_at_ns: int


def register_strategy_release(
    writer: WriterLease,
    *,
    release_id: str,
    owner_agent_id: str,
    strategy_source_sha256: str,
    parameters: dict[str, Any],
    instrument_rules_sha256: str,
    risk_config_sha256: str,
    fill_policy_sha256: str,
    pit_manifest_id: str,
    pit_manifest_sha256: str,
    nautilus_lock_sha256: str | None = None,
    parent_release_id: str | None = None,
) -> StrategyRelease:
    """Register an immutable, content-addressed StrategyRelease with monotonic trial count."""
    writer.verify()
    _require_safe_id(release_id, "release_id")
    _require_token(owner_agent_id, "owner_agent_id")
    _require_sha256(strategy_source_sha256, "strategy_source_sha256")
    _require_sha256(instrument_rules_sha256, "instrument_rules_sha256")
    _require_sha256(risk_config_sha256, "risk_config_sha256")
    _require_sha256(fill_policy_sha256, "fill_policy_sha256")
    _require_safe_id(pit_manifest_id, "pit_manifest_id")
    _require_sha256(pit_manifest_sha256, "pit_manifest_sha256")
    if not isinstance(parameters, dict):
        raise StrategyComparisonRefused("Strategy parameters must be a JSON object")
    if parent_release_id is not None:
        _require_safe_id(parent_release_id, "parent_release_id")
        if parent_release_id == release_id:
            raise StrategyComparisonRefused("Strategy release cannot be its own parent")

    expected_lock = pinned_nautilus_lock_sha256()
    lock_sha = expected_lock if nautilus_lock_sha256 is None else nautilus_lock_sha256
    _require_sha256(lock_sha, "nautilus_lock_sha256")
    if lock_sha != expected_lock:
        raise StrategyComparisonRefused(
            "StrategyRelease nautilus_trader lock hash does not match pinned runtime"
        )
    parameters_json = _canonical_json(parameters)

    with (
        closing(open_writer(writer.store, lease=writer)) as db,
        write_tx(db),
    ):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise StrategyComparisonRefused(
                "Strategy release belongs to another company"
            )
        enable_domain_v1(
            db, "paper", company_id=writer.company_id, actor_id=owner_agent_id
        )
        if (
            db.execute(
                "SELECT 1 FROM agent WHERE id = ? AND company_id = ?",
                (owner_agent_id, writer.company_id),
            ).fetchone()
            is None
        ):
            raise StrategyComparisonRefused(
                "Strategy release owner must be a company agent"
            )
        if employment_cutoff(db, owner_agent_id) is not None:
            raise StrategyComparisonRefused(
                "Retired agent cannot register a new strategy release"
            )
        pit_row = db.execute(
            """SELECT manifest_sha256 FROM paper_pit_catalog_manifest
               WHERE manifest_id = ? AND company_id = ?""",
            (pit_manifest_id, writer.company_id),
        ).fetchone()
        if pit_row is None or pit_row[0] != pit_manifest_sha256:
            raise StrategyComparisonRefused(
                "Strategy release requires a registered matching PIT catalog manifest"
            )
        if parent_release_id is not None:
            if (
                db.execute(
                    """SELECT 1 FROM paper_strategy_release
                       WHERE release_id = ? AND company_id = ?""",
                    (parent_release_id, writer.company_id),
                ).fetchone()
                is None
            ):
                raise StrategyComparisonRefused(
                    "Parent strategy release does not exist in this company"
                )

        existing = db.execute(
            """SELECT release_id, company_id, owner_agent_id, parent_release_id,
                      strategy_source_sha256, parameters_json, nautilus_lock_sha256,
                      instrument_rules_sha256, risk_config_sha256, fill_policy_sha256,
                      pit_manifest_id, pit_manifest_sha256, research_trial_count_n,
                      bundle_sha256, recorded_at_ns
               FROM paper_strategy_release
               WHERE release_id = ?""",
            (release_id,),
        ).fetchone()
        if existing is not None:
            prior = StrategyRelease(*existing)
            if (
                prior.company_id != writer.company_id
                or prior.owner_agent_id != owner_agent_id
                or prior.parent_release_id != parent_release_id
                or prior.strategy_source_sha256 != strategy_source_sha256
                or prior.parameters_json != parameters_json
                or prior.nautilus_lock_sha256 != lock_sha
                or prior.instrument_rules_sha256 != instrument_rules_sha256
                or prior.risk_config_sha256 != risk_config_sha256
                or prior.fill_policy_sha256 != fill_policy_sha256
                or prior.pit_manifest_id != pit_manifest_id
                or prior.pit_manifest_sha256 != pit_manifest_sha256
            ):
                raise StrategyComparisonRefused(
                    "Strategy release ID conflicts with previously sealed terms"
                )
            return prior

        (max_n,) = db.execute(
            """SELECT COALESCE(MAX(research_trial_count_n), 0)
               FROM paper_strategy_release
               WHERE company_id = ?""",
            (writer.company_id,),
        ).fetchone()
        trial_n = int(max_n) + 1
        bundle_sha256 = _sha256_json(
            {
                "release_id": release_id,
                "company_id": writer.company_id,
                "owner_agent_id": owner_agent_id,
                "parent_release_id": parent_release_id,
                "strategy_source_sha256": strategy_source_sha256,
                "parameters_json": parameters_json,
                "nautilus_lock_sha256": lock_sha,
                "instrument_rules_sha256": instrument_rules_sha256,
                "risk_config_sha256": risk_config_sha256,
                "fill_policy_sha256": fill_policy_sha256,
                "pit_manifest_id": pit_manifest_id,
                "pit_manifest_sha256": pit_manifest_sha256,
                "research_trial_count_n": trial_n,
            }
        )
        now_ns = time_ns()
        db.execute(
            """INSERT INTO paper_strategy_release (
                   release_id, company_id, owner_agent_id, parent_release_id,
                   strategy_source_sha256, parameters_json, nautilus_lock_sha256,
                   instrument_rules_sha256, risk_config_sha256, fill_policy_sha256,
                   pit_manifest_id, pit_manifest_sha256, research_trial_count_n,
                   bundle_sha256, recorded_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                release_id,
                writer.company_id,
                owner_agent_id,
                parent_release_id,
                strategy_source_sha256,
                parameters_json,
                lock_sha,
                instrument_rules_sha256,
                risk_config_sha256,
                fill_policy_sha256,
                pit_manifest_id,
                pit_manifest_sha256,
                trial_n,
                bundle_sha256,
                now_ns,
            ),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="paper",
            entity_type="paper_strategy_release",
            entity_id=release_id,
            event_type="paper.strategy_release_registered",
            actor_id=owner_agent_id,
            payload={
                "release_id": release_id,
                "bundle_sha256": bundle_sha256,
                "research_trial_count_n": trial_n,
                "pit_manifest_id": pit_manifest_id,
            },
            recorded_at_ns=now_ns,
        )
        writer.verify()
        return StrategyRelease(
            release_id=release_id,
            company_id=writer.company_id,
            owner_agent_id=owner_agent_id,
            parent_release_id=parent_release_id,
            strategy_source_sha256=strategy_source_sha256,
            parameters_json=parameters_json,
            nautilus_lock_sha256=lock_sha,
            instrument_rules_sha256=instrument_rules_sha256,
            risk_config_sha256=risk_config_sha256,
            fill_policy_sha256=fill_policy_sha256,
            pit_manifest_id=pit_manifest_id,
            pit_manifest_sha256=pit_manifest_sha256,
            research_trial_count_n=trial_n,
            bundle_sha256=bundle_sha256,
            recorded_at_ns=now_ns,
        )


def verify_strategy_release_source(
    release: StrategyRelease, source_bytes: bytes
) -> None:
    """Reject execution or activation if mutable source diverged from sealed release."""
    if not isinstance(source_bytes, bytes):
        raise StrategyComparisonRefused("Strategy source must be exact bytes")
    if _sha256_bytes(source_bytes) != release.strategy_source_sha256:
        raise StrategyComparisonRefused(
            "Strategy source digest no longer matches registered StrategyRelease"
        )


@dataclass(frozen=True)
class IsolatedArmNamespace:
    role: str
    release_id: str
    company_id: str
    store_path: str
    venue_path: str
    desk_path: str
    lease_dir: str
    feed_path: str
    feed_sha256: str
    account_id: str

    def __post_init__(self) -> None:
        if self.role not in ("incumbent", "challenger"):
            raise StrategyComparisonRefused("Arm role must be incumbent or challenger")
        _require_safe_id(self.release_id, "release_id")
        for value, name in (
            (self.company_id, "company_id"),
            (self.store_path, "store_path"),
            (self.venue_path, "venue_path"),
            (self.desk_path, "desk_path"),
            (self.lease_dir, "lease_dir"),
            (self.feed_path, "feed_path"),
            (self.account_id, "account_id"),
        ):
            _require_token(value, name, max_len=512)
        _require_sha256(self.feed_sha256, "feed_sha256")


def provision_isolated_arm_namespace(
    *,
    root_dir: Path,
    role: str,
    release: StrategyRelease,
    charter_revision: str,
    execution_profile: str,
    shared_feed_path: Path,
    baseline_desk_dir: Path | None = None,
    permitted_desk_files: tuple[str, ...] = (),
) -> IsolatedArmNamespace:
    """Create one fresh, physically isolated simulated company-arm namespace."""
    if role not in ("incumbent", "challenger"):
        raise StrategyComparisonRefused("Arm role must be incumbent or challenger")
    _require_safe_id(release.release_id, "release_id")
    feed_checked = _reject_symlinks_in_absolute_path(
        shared_feed_path, "shared_feed_path"
    )
    feed_sha = _hash_regular_file(feed_checked)

    try:
        root_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved_root = root_dir.resolve()
        arm_root = resolved_root / f"arm-{role}-{release.release_id}"
        if not arm_root.resolve().is_relative_to(resolved_root):
            raise StrategyComparisonRefused("Arm namespace escapes root directory")
        arm_root.mkdir(mode=0o700, parents=False, exist_ok=False)
        desk_dir = arm_root / "desk"
        desk_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        lease_dir = arm_root / "leases"
        lease_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    except OSError as error:
        raise StrategyComparisonRefused(
            f"Cannot provision fresh isolated arm namespace: {error}"
        ) from error

    if baseline_desk_dir is not None and permitted_desk_files:
        for rel_str in permitted_desk_files:
            rel = _require_safe_relative_path(rel_str, "permitted_desk_file")
            src = _reject_symlinks_under(baseline_desk_dir, rel, "baseline Desk")
            try:
                info = src.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise StrategyComparisonRefused(
                        "Permitted desk baseline must be a regular file"
                    )
                dest = desk_dir / rel
                dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(
                    dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                try:
                    with src.open("rb") as reader, os.fdopen(fd, "wb") as writer_file:
                        shutil.copyfileobj(reader, writer_file)
                except BaseException:
                    raise
            except OSError as error:
                raise StrategyComparisonRefused(
                    f"Failed copying permitted baseline Desk file {rel_str}"
                ) from error

    store_path = arm_root / "company.sqlite"
    receipt = found(
        store_path,
        FoundingTerms(
            f"trial-{role}-{release.release_id}",
            charter_revision,
            "paper",
            execution_profile,
        ),
    )
    venue_path = arm_root / "venue.sqlite"
    return IsolatedArmNamespace(
        role=role,
        release_id=release.release_id,
        company_id=receipt.company_id,
        store_path=str(store_path),
        venue_path=str(venue_path),
        desk_path=str(desk_dir),
        lease_dir=str(lease_dir),
        feed_path=str(feed_checked.resolve()),
        feed_sha256=feed_sha,
        account_id=f"{role}-{release.release_id}-paper",
    )


def _regular_identity(path: Path, label: str) -> tuple[int, int]:
    checked = _reject_symlinks_in_absolute_path(path, label)
    try:
        info = checked.stat(follow_symlinks=False)
    except OSError as error:
        raise StrategyComparisonRefused(f"{label} is unavailable") from error
    if not stat.S_ISREG(info.st_mode):
        raise StrategyComparisonRefused(f"{label} must be a regular non-symlink file")
    return info.st_dev, info.st_ino


def _dir_identity(path: Path, label: str) -> tuple[int, int]:
    checked = _reject_symlinks_in_absolute_path(path, label)
    try:
        info = checked.stat(follow_symlinks=False)
    except OSError as error:
        raise StrategyComparisonRefused(f"{label} is unavailable") from error
    if not stat.S_ISDIR(info.st_mode):
        raise StrategyComparisonRefused(f"{label} must be a non-symlink directory")
    return info.st_dev, info.st_ino


def _paths_overlap(a: Path, b: Path) -> bool:
    ra, rb = a.resolve(), b.resolve()
    return ra == rb or ra.is_relative_to(rb) or rb.is_relative_to(ra)


def verify_arm_isolation(
    incumbent: IsolatedArmNamespace,
    challenger: IsolatedArmNamespace,
    *,
    live_store: Path,
    live_company_id: str | None = None,
) -> None:
    """Enforce strict physical isolation between arms and the operating company store."""
    if incumbent.role != "incumbent" or challenger.role != "challenger":
        raise StrategyComparisonRefused(
            "Paired trial requires incumbent and challenger"
        )
    if (
        incumbent.company_id == challenger.company_id
        or incumbent.account_id == challenger.account_id
        or incumbent.release_id == challenger.release_id
    ):
        raise StrategyComparisonRefused("Trial arms must have distinct identities")
    if live_company_id is not None and live_company_id in (
        incumbent.company_id,
        challenger.company_id,
    ):
        raise StrategyComparisonRefused(
            "Trial arms cannot reuse the live company identity"
        )
    if (
        incumbent.feed_path != challenger.feed_path
        or incumbent.feed_sha256 != challenger.feed_sha256
    ):
        raise StrategyComparisonRefused(
            "Both arms must consume the identical shared read-only market feed"
        )

    feed_path = _reject_symlinks_in_absolute_path(
        Path(incumbent.feed_path), "shared feed"
    )
    actual_feed_sha = _hash_regular_file(feed_path)
    if actual_feed_sha != incumbent.feed_sha256:
        raise StrategyComparisonRefused("Shared market feed digest changed")

    live_key = _regular_identity(live_store, "live_store")
    inc_store_path = Path(incumbent.store_path)
    chal_store_path = Path(challenger.store_path)
    inc_store_key = _regular_identity(inc_store_path, "incumbent store")
    chal_store_key = _regular_identity(chal_store_path, "challenger store")
    if len({live_key, inc_store_key, chal_store_key}) != 3:
        raise StrategyComparisonRefused(
            "Trial arm stores must be physically isolated from each other and live store"
        )

    # Verify each arm SQLite store belongs to its declared isolated company_id.
    for arm_store, expected_cid, label in (
        (inc_store_path, incumbent.company_id, "incumbent"),
        (chal_store_path, challenger.company_id, "challenger"),
    ):
        try:
            with closing(open_reader(arm_store)) as arm_db, read_tx(arm_db):
                actual_cid = _identity(arm_db)
        except (StorageRefused, sqlite3.Error, OSError) as error:
            raise StrategyComparisonRefused(
                f"Cannot verify {label} isolated store identity"
            ) from error
        if actual_cid != expected_cid:
            raise StrategyComparisonRefused(
                f"{label} store company identity does not match registered arm"
            )

    inc_desk = Path(incumbent.desk_path)
    chal_desk = Path(challenger.desk_path)
    inc_lease = Path(incumbent.lease_dir)
    chal_lease = Path(challenger.lease_dir)
    inc_desk_key = _dir_identity(inc_desk, "incumbent desk")
    chal_desk_key = _dir_identity(chal_desk, "challenger desk")
    inc_lease_key = _dir_identity(inc_lease, "incumbent lease dir")
    chal_lease_key = _dir_identity(chal_lease, "challenger lease dir")
    live_parent_key = _dir_identity(live_store.parent, "live store directory")

    if (
        len(
            {
                inc_desk_key,
                chal_desk_key,
                inc_lease_key,
                chal_lease_key,
                live_parent_key,
            }
        )
        != 5
    ):
        raise StrategyComparisonRefused(
            "Trial arm desks, leases, and live store directory must be physically isolated"
        )
    if (
        _paths_overlap(inc_desk, chal_desk)
        or _paths_overlap(inc_desk, live_store)
        or _paths_overlap(chal_desk, live_store)
        or _paths_overlap(inc_desk, feed_path)
        or _paths_overlap(chal_desk, feed_path)
    ):
        raise StrategyComparisonRefused(
            "Trial arm writable directories cannot overlap sibling arms, live store, or shared feed"
        )

    inc_venue = _reject_symlinks_in_absolute_path(
        Path(incumbent.venue_path), "incumbent venue", allow_missing_leaf=True
    )
    chal_venue = _reject_symlinks_in_absolute_path(
        Path(challenger.venue_path), "challenger venue", allow_missing_leaf=True
    )
    if inc_venue.resolve() == chal_venue.resolve():
        raise StrategyComparisonRefused("Trial arms cannot share a venue journal")
    store_keys = {live_key, inc_store_key, chal_store_key}
    venue_keys: list[tuple[int, int]] = []
    for venue_path, label in (
        (inc_venue, "incumbent venue"),
        (chal_venue, "challenger venue"),
    ):
        if venue_path.exists():
            vkey = _regular_identity(venue_path, label)
            if vkey in store_keys:
                raise StrategyComparisonRefused(f"{label} aliases a company store")
            venue_keys.append(vkey)
    if len(venue_keys) == 2 and venue_keys[0] == venue_keys[1]:
        raise StrategyComparisonRefused(
            "Trial arms cannot hard-link the same venue journal"
        )


@dataclass(frozen=True)
class AnalysisPlan:
    opens_ns: int
    closes_ns: int
    primary_metric: str
    worthwhile_margin_bps: float
    futility_margin_bps: float
    alpha: float
    min_observations: int
    max_drawdown_bps: int
    max_exposure_minor: int
    max_operating_cost_minor: int
    incumbent_cost_share_num: int
    incumbent_cost_share_den: int
    changed_components: tuple[str, ...]
    stopping_rule: str
    variance_floor_bps2: float = 1.0

    def __post_init__(self) -> None:
        if (
            type(self.opens_ns) is not int
            or type(self.closes_ns) is not int
            or not 0 < self.opens_ns < self.closes_ns <= _MAX_I64
            or self.primary_metric != "company_net_return_bps"
            or isinstance(self.worthwhile_margin_bps, bool)
            or not isinstance(self.worthwhile_margin_bps, (int, float))
            or not math.isfinite(self.worthwhile_margin_bps)
            or self.worthwhile_margin_bps <= 0
            or isinstance(self.futility_margin_bps, bool)
            or not isinstance(self.futility_margin_bps, (int, float))
            or not math.isfinite(self.futility_margin_bps)
            or self.futility_margin_bps > 0
            or isinstance(self.alpha, bool)
            or not isinstance(self.alpha, (int, float))
            or not math.isfinite(self.alpha)
            or not 0 < self.alpha < 0.5
            or type(self.min_observations) is not int
            or not 5 <= self.min_observations <= 100_000
            or type(self.max_drawdown_bps) is not int
            or not 0 < self.max_drawdown_bps <= 10_000
            or type(self.max_exposure_minor) is not int
            or not 0 < self.max_exposure_minor <= _MAX_I64
            or type(self.max_operating_cost_minor) is not int
            or not 0 <= self.max_operating_cost_minor <= _MAX_I64
            or type(self.incumbent_cost_share_num) is not int
            or type(self.incumbent_cost_share_den) is not int
            or not 0 < self.incumbent_cost_share_den <= _MAX_I64
            or not 0 <= self.incumbent_cost_share_num <= self.incumbent_cost_share_den
            or not isinstance(self.changed_components, tuple)
            or not self.changed_components
            or any(
                not isinstance(c, str) or not c.strip() for c in self.changed_components
            )
            or not isinstance(self.stopping_rule, str)
            or not self.stopping_rule.strip()
            or isinstance(self.variance_floor_bps2, bool)
            or not isinstance(self.variance_floor_bps2, (int, float))
            or not math.isfinite(self.variance_floor_bps2)
            or self.variance_floor_bps2 <= 0
        ):
            raise StrategyComparisonRefused("Invalid pre-registered analysis plan")

    @property
    def incumbent_cost_share(self) -> Fraction:
        return Fraction(self.incumbent_cost_share_num, self.incumbent_cost_share_den)

    @property
    def challenger_cost_share(self) -> Fraction:
        return Fraction(1, 1) - self.incumbent_cost_share


@dataclass(frozen=True)
class ArmObservation:
    """Observed evidence from one isolated arm using Flow 08's statement contract."""

    statement_report: dict[str, Any]
    interval_returns_bps: tuple[float, ...]
    realized_drawdown_bps: int
    peak_exposure_minor: int
    operating_cost_minor: int
    reconciliation_drift: bool = False
    risk_invariant_breach: bool = False
    unexpected_change_detected: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.statement_report, dict)
            or not isinstance(self.interval_returns_bps, tuple)
            or any(
                isinstance(x, bool)
                or not isinstance(x, (int, float))
                or not math.isfinite(x)
                or abs(x) > _MAX_RETURN_BPS
                for x in self.interval_returns_bps
            )
            or type(self.realized_drawdown_bps) is not int
            or not 0 <= self.realized_drawdown_bps <= 10_000
            or type(self.peak_exposure_minor) is not int
            or not 0 <= self.peak_exposure_minor <= _MAX_I64
            or type(self.operating_cost_minor) is not int
            or not 0 <= self.operating_cost_minor <= _MAX_I64
            or type(self.reconciliation_drift) is not bool
            or type(self.risk_invariant_breach) is not bool
            or type(self.unexpected_change_detected) is not bool
        ):
            raise StrategyComparisonRefused("Invalid arm observation telemetry")


@dataclass(frozen=True)
class SequentialVerdict:
    verdict: str  # PROMOTE | REJECT | INCONCLUSIVE
    reasons: tuple[str, ...]
    observations_n: int
    research_trial_count_n: int
    alpha_effective: float
    mean_diff_bps: float | None
    lower_bound_bps: float | None
    upper_bound_bps: float | None
    incumbent_statement_revision: str
    challenger_statement_revision: str
    multi_component_change: bool
    shared_cost_allocated_minor: tuple[int, int]


@dataclass(frozen=True)
class ChallengerTrial:
    comparison_id: str
    company_id: str
    incumbent_release_id: str
    challenger_release_id: str
    baseline_statement_revision: str
    analysis_plan: AnalysisPlan
    analysis_plan_sha256: str
    incumbent_arm: IsolatedArmNamespace
    challenger_arm: IsolatedArmNamespace
    status: str
    verdict: str | None
    verdict_detail: SequentialVerdict | None
    invalidation_reason: str | None
    registered_at_ns: int
    evaluated_at_ns: int | None


def compute_anytime_valid_bounds(
    paired_diffs_bps: tuple[float, ...],
    *,
    alpha: float,
    research_trial_count_n: int,
    variance_floor_bps2: float = 1.0,
) -> tuple[float, float, float, float]:
    """Predictable self-normalized normal-mixture confidence sequence with Bonferroni N-adjustment.

    Zero peeking bias across arbitrary sequential stopping times.
    Returns (alpha_effective, mean_diff_bps, lower_bound_bps, upper_bound_bps).
    """
    if (
        not isinstance(paired_diffs_bps, tuple)
        or len(paired_diffs_bps) == 0
        or any(
            isinstance(x, bool)
            or not isinstance(x, (int, float))
            or not math.isfinite(x)
            or abs(x) > _MAX_RETURN_BPS
            for x in paired_diffs_bps
        )
        or isinstance(alpha, bool)
        or not isinstance(alpha, (int, float))
        or not math.isfinite(alpha)
        or not 0 < alpha < 0.5
        or type(research_trial_count_n) is not int
        or research_trial_count_n < 1
        or isinstance(variance_floor_bps2, bool)
        or not isinstance(variance_floor_bps2, (int, float))
        or not math.isfinite(variance_floor_bps2)
        or not 0 < variance_floor_bps2 <= _MAX_RETURN_BPS
    ):
        raise StrategyComparisonRefused("Invalid sequential inference inputs")

    n = len(paired_diffs_bps)
    alpha_eff = alpha / research_trial_count_n
    rho = float(variance_floor_bps2)

    running_sum = 0.0
    predictable_v = 0.0
    for idx, value in enumerate(paired_diffs_bps):
        x = float(value)
        pred_mean = running_sum / idx if idx > 0 else 0.0
        predictable_v += (x - pred_mean) ** 2
        running_sum += x

    mean_diff = running_sum / n
    empirical_v = sum((float(x) - mean_diff) ** 2 for x in paired_diffs_bps)
    total_v = max(predictable_v, empirical_v, n * rho)
    df_factor = math.sqrt(n / (n - 2)) if n >= 5 else 3.0
    log_term = 2.0 * math.log(2.0 / alpha_eff) + math.log(1.0 + total_v / rho)
    radius = (df_factor / n) * math.sqrt((total_v + rho) * log_term)
    if not (math.isfinite(mean_diff) and math.isfinite(radius)):
        raise StrategyComparisonRefused("Sequential inference overflowed finite bounds")
    return alpha_eff, mean_diff, mean_diff - radius, mean_diff + radius


def allocate_shared_cost(
    total_shared_cost_minor: int, plan: AnalysisPlan
) -> tuple[int, int]:
    """Split shared trial expense across the two arms without double-counting."""
    if (
        type(total_shared_cost_minor) is not int
        or not 0 <= total_shared_cost_minor <= _MAX_I64
    ):
        raise StrategyComparisonRefused(
            "Shared cost must be a bounded non-negative int"
        )
    inc_cost = int(
        round(Fraction(total_shared_cost_minor, 1) * plan.incumbent_cost_share)
    )
    chal_cost = total_shared_cost_minor - inc_cost
    return inc_cost, chal_cost


def _decode_trial_row(row: tuple[Any, ...]) -> ChallengerTrial:
    plan_raw = json.loads(row[5])
    plan_raw["changed_components"] = tuple(plan_raw["changed_components"])
    verdict_raw = json.loads(row[11]) if row[11] is not None else None
    verdict_detail = (
        SequentialVerdict(
            verdict=verdict_raw["verdict"],
            reasons=tuple(verdict_raw["reasons"]),
            observations_n=verdict_raw["observations_n"],
            research_trial_count_n=verdict_raw["research_trial_count_n"],
            alpha_effective=verdict_raw["alpha_effective"],
            mean_diff_bps=verdict_raw["mean_diff_bps"],
            lower_bound_bps=verdict_raw["lower_bound_bps"],
            upper_bound_bps=verdict_raw["upper_bound_bps"],
            incumbent_statement_revision=verdict_raw["incumbent_statement_revision"],
            challenger_statement_revision=verdict_raw["challenger_statement_revision"],
            multi_component_change=verdict_raw["multi_component_change"],
            shared_cost_allocated_minor=tuple(
                verdict_raw["shared_cost_allocated_minor"]
            ),
        )
        if verdict_raw is not None
        else None
    )
    return ChallengerTrial(
        comparison_id=row[0],
        company_id=row[1],
        incumbent_release_id=row[2],
        challenger_release_id=row[3],
        baseline_statement_revision=row[4],
        analysis_plan=AnalysisPlan(**plan_raw),
        analysis_plan_sha256=row[6],
        incumbent_arm=IsolatedArmNamespace(**json.loads(row[7])),
        challenger_arm=IsolatedArmNamespace(**json.loads(row[8])),
        status=row[9],
        verdict=row[10],
        verdict_detail=verdict_detail,
        invalidation_reason=row[12],
        registered_at_ns=row[13],
        evaluated_at_ns=row[14],
    )


def register_challenger_trial(
    writer: WriterLease,
    *,
    comparison_id: str,
    incumbent_release_id: str,
    challenger_release_id: str,
    baseline_statement_revision: str,
    analysis_plan: AnalysisPlan,
    incumbent_arm: IsolatedArmNamespace,
    challenger_arm: IsolatedArmNamespace,
    now_ns: int | None = None,
) -> ChallengerTrial:
    """Preregister a prospective paired incumbent vs challenger comparison."""
    writer.verify()
    _require_safe_id(comparison_id, "comparison_id")
    _require_safe_id(incumbent_release_id, "incumbent_release_id")
    _require_safe_id(challenger_release_id, "challenger_release_id")
    _require_sha256(baseline_statement_revision, "baseline_statement_revision")
    if incumbent_release_id == challenger_release_id:
        raise StrategyComparisonRefused("Incumbent and challenger releases must differ")
    if (
        incumbent_arm.release_id != incumbent_release_id
        or challenger_arm.release_id != challenger_release_id
    ):
        raise StrategyComparisonRefused("Arm namespaces must match registered releases")
    verify_arm_isolation(
        incumbent_arm,
        challenger_arm,
        live_store=writer.store,
        live_company_id=writer.company_id,
    )

    recorded_ns = time_ns() if now_ns is None else now_ns
    if type(recorded_ns) is not int or not 0 < recorded_ns < analysis_plan.opens_ns:
        raise StrategyComparisonRefused(
            "Paired comparison must be registered before its prospective window opens"
        )

    plan_json = _canonical_json(asdict(analysis_plan))
    plan_sha = _sha256_bytes(plan_json.encode("utf-8"))
    inc_json = _canonical_json(asdict(incumbent_arm))
    chal_json = _canonical_json(asdict(challenger_arm))

    with (
        closing(open_writer(writer.store, lease=writer)) as db,
        write_tx(db),
    ):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise StrategyComparisonRefused("Comparison belongs to another company")
        enable_domain_v1(
            db, "paper", company_id=writer.company_id, actor_id="strategy_comparison"
        )
        for rid in (incumbent_release_id, challenger_release_id):
            if (
                db.execute(
                    """SELECT 1 FROM paper_strategy_release
                       WHERE release_id = ? AND company_id = ?""",
                    (rid, writer.company_id),
                ).fetchone()
                is None
            ):
                raise StrategyComparisonRefused(
                    f"Strategy release {rid} is not registered for this company"
                )
        existing = db.execute(
            """SELECT comparison_id, company_id, incumbent_release_id,
                      challenger_release_id, baseline_statement_revision,
                      analysis_plan_json, analysis_plan_sha256,
                      incumbent_arm_json, challenger_arm_json,
                      status, verdict, verdict_json, invalidation_reason,
                      registered_at_ns, evaluated_at_ns
               FROM paper_challenger_trial
               WHERE comparison_id = ?""",
            (comparison_id,),
        ).fetchone()
        if existing is not None:
            if (
                existing[1] != writer.company_id
                or existing[2] != incumbent_release_id
                or existing[3] != challenger_release_id
                or existing[4] != baseline_statement_revision
                or existing[6] != plan_sha
                or existing[7] != inc_json
                or existing[8] != chal_json
            ):
                raise StrategyComparisonRefused(
                    "Comparison ID conflicts with previously registered trial"
                )
            return _decode_trial_row(existing)

        db.execute(
            """INSERT INTO paper_challenger_trial (
                   comparison_id, company_id, incumbent_release_id,
                   challenger_release_id, baseline_statement_revision,
                   analysis_plan_json, analysis_plan_sha256,
                   incumbent_arm_json, challenger_arm_json,
                   status, verdict, verdict_json, invalidation_reason,
                   registered_at_ns, evaluated_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'registered', NULL, NULL, NULL, ?, NULL)""",
            (
                comparison_id,
                writer.company_id,
                incumbent_release_id,
                challenger_release_id,
                baseline_statement_revision,
                plan_json,
                plan_sha,
                inc_json,
                chal_json,
                recorded_ns,
            ),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="paper",
            entity_type="paper_challenger_trial",
            entity_id=comparison_id,
            event_type="paper.challenger_trial_registered",
            actor_id="strategy_comparison",
            payload={
                "comparison_id": comparison_id,
                "incumbent_release_id": incumbent_release_id,
                "challenger_release_id": challenger_release_id,
                "analysis_plan_sha256": plan_sha,
            },
            recorded_at_ns=recorded_ns,
        )
        writer.verify()
        return ChallengerTrial(
            comparison_id=comparison_id,
            company_id=writer.company_id,
            incumbent_release_id=incumbent_release_id,
            challenger_release_id=challenger_release_id,
            baseline_statement_revision=baseline_statement_revision,
            analysis_plan=analysis_plan,
            analysis_plan_sha256=plan_sha,
            incumbent_arm=incumbent_arm,
            challenger_arm=challenger_arm,
            status="registered",
            verdict=None,
            verdict_detail=None,
            invalidation_reason=None,
            registered_at_ns=recorded_ns,
            evaluated_at_ns=None,
        )


def _verify_flow08_statement(
    report: dict[str, Any], expected_company_id: str
) -> tuple[bool, str]:
    """Verify Flow 08 _seal_report SHA-256 integrity, field types, and arm scope."""
    if not isinstance(report, dict) or not isinstance(report.get("revision"), str):
        raise StrategyComparisonRefused(
            "Arm observation requires a sealed Flow 08 statement report"
        )
    revision = _require_sha256(report["revision"], "statement revision")
    unsealed = {key: value for key, value in report.items() if key != "revision"}
    if _sha256_json(unsealed) != revision:
        raise StrategyComparisonRefused(
            "Arm statement report failed Flow 08 cryptographic seal verification"
        )
    company = report.get("company")
    if not isinstance(company, dict):
        raise StrategyComparisonRefused(
            "Arm statement report is missing company section"
        )
    scope = company.get("scope")
    if isinstance(scope, dict):
        if scope.get("company_id") != expected_company_id:
            raise StrategyComparisonRefused(
                "Arm statement scope company_id does not match isolated arm"
            )
    missing = company.get("missing")
    if not isinstance(missing, (list, tuple)) or any(
        not isinstance(item, str) for item in missing
    ):
        raise StrategyComparisonRefused(
            "Arm statement report has invalid missing coverage field"
        )
    pnl = company.get("pnl_minor")
    twr = company.get("time_weighted_return")
    if len(missing) > 0 or pnl is None or twr is None:
        return False, revision
    if type(pnl) is not int:
        raise StrategyComparisonRefused("Arm statement pnl_minor must be an integer")
    if (
        not isinstance(twr, dict)
        or type(twr.get("numerator")) is not int
        or type(twr.get("denominator")) is not int
        or twr["denominator"] <= 0
    ):
        raise StrategyComparisonRefused(
            "Arm statement time_weighted_return must be an exact rational ratio"
        )
    return True, revision


def invalidate_challenger_trial(
    writer: WriterLease, comparison_id: str, reason: str
) -> ChallengerTrial:
    writer.verify()
    _require_safe_id(comparison_id, "comparison_id")
    _require_token(reason, "invalidation_reason", max_len=512)
    with (
        closing(open_writer(writer.store, lease=writer)) as db,
        write_tx(db),
    ):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise StrategyComparisonRefused("Comparison belongs to another company")
        if not is_domain_installed(db, "paper"):
            raise StrategyComparisonRefused("Comparison trial not found")
        row = db.execute(
            """SELECT comparison_id, company_id, incumbent_release_id,
                      challenger_release_id, baseline_statement_revision,
                      analysis_plan_json, analysis_plan_sha256,
                      incumbent_arm_json, challenger_arm_json,
                      status, verdict, verdict_json, invalidation_reason,
                      registered_at_ns, evaluated_at_ns
               FROM paper_challenger_trial
               WHERE comparison_id = ? AND company_id = ?""",
            (comparison_id, writer.company_id),
        ).fetchone()
        if row is None:
            raise StrategyComparisonRefused("Comparison trial not found")
        if row[9] != "registered":
            raise StrategyComparisonRefused(
                f"Cannot overwrite finalized comparison trial ({row[9]})"
            )
        now_ns = max(time_ns(), int(row[13]))
        db.execute(
            """UPDATE paper_challenger_trial
               SET status = 'invalidated', verdict = NULL, verdict_json = NULL,
                   invalidation_reason = ?, evaluated_at_ns = ?
               WHERE comparison_id = ? AND company_id = ? AND status = 'registered'""",
            (reason, now_ns, comparison_id, writer.company_id),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="paper",
            entity_type="paper_challenger_trial",
            entity_id=comparison_id,
            event_type="paper.challenger_trial_invalidated",
            actor_id="strategy_comparison",
            payload={"comparison_id": comparison_id, "reason": reason},
            recorded_at_ns=now_ns,
        )
        updated = db.execute(
            """SELECT comparison_id, company_id, incumbent_release_id,
                      challenger_release_id, baseline_statement_revision,
                      analysis_plan_json, analysis_plan_sha256,
                      incumbent_arm_json, challenger_arm_json,
                      status, verdict, verdict_json, invalidation_reason,
                      registered_at_ns, evaluated_at_ns
               FROM paper_challenger_trial
               WHERE comparison_id = ? AND company_id = ?""",
            (comparison_id, writer.company_id),
        ).fetchone()
        assert updated is not None
        writer.verify()
        return _decode_trial_row(updated)


def evaluate_challenger_trial(
    writer: WriterLease,
    *,
    comparison_id: str,
    incumbent_obs: ArmObservation,
    challenger_obs: ArmObservation,
    shared_trial_cost_minor: int = 0,
    now_ns: int | None = None,
) -> ChallengerTrial:
    """Evaluate a registered paired trial into PROMOTE, REJECT, INCONCLUSIVE, or invalidated."""
    writer.verify()
    _require_safe_id(comparison_id, "comparison_id")
    if not isinstance(incumbent_obs, ArmObservation) or not isinstance(
        challenger_obs, ArmObservation
    ):
        raise StrategyComparisonRefused("Expected validated ArmObservation inputs")

    with (
        closing(open_writer(writer.store, lease=writer)) as db,
        write_tx(db),
    ):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise StrategyComparisonRefused("Comparison belongs to another company")
        if not is_domain_installed(db, "paper"):
            raise StrategyComparisonRefused("Comparison trial not found")
        row = db.execute(
            """SELECT comparison_id, company_id, incumbent_release_id,
                      challenger_release_id, baseline_statement_revision,
                      analysis_plan_json, analysis_plan_sha256,
                      incumbent_arm_json, challenger_arm_json,
                      status, verdict, verdict_json, invalidation_reason,
                      registered_at_ns, evaluated_at_ns
               FROM paper_challenger_trial
               WHERE comparison_id = ? AND company_id = ?""",
            (comparison_id, writer.company_id),
        ).fetchone()
        if row is None:
            raise StrategyComparisonRefused("Comparison trial not found")
        trial = _decode_trial_row(row)
        if trial.status != "registered":
            raise StrategyComparisonRefused(
                f"Comparison trial is already finalized ({trial.status})"
            )
        plan = trial.analysis_plan
        eval_ns = time_ns() if now_ns is None else now_ns
        if type(eval_ns) is not int or eval_ns < plan.opens_ns:
            raise StrategyComparisonRefused(
                "Comparison trial cannot be evaluated before its prospective window opens"
            )
        eval_ns = max(eval_ns, trial.registered_at_ns)

        # Re-verify physical arm isolation & shared read-only feed digest at evaluation time.
        invalidation_reason: str | None = None
        try:
            verify_arm_isolation(
                trial.incumbent_arm,
                trial.challenger_arm,
                live_store=writer.store,
                live_company_id=writer.company_id,
            )
        except StrategyComparisonRefused:
            invalidation_reason = "arm-isolation-or-feed-violation"

        if invalidation_reason is None and (
            incumbent_obs.unexpected_change_detected
            or challenger_obs.unexpected_change_detected
        ):
            invalidation_reason = "unexpected-mid-window-change"

        if invalidation_reason is not None:
            db.execute(
                """UPDATE paper_challenger_trial
                   SET status = 'invalidated', invalidation_reason = ?, evaluated_at_ns = ?
                   WHERE comparison_id = ? AND company_id = ?""",
                (invalidation_reason, eval_ns, comparison_id, writer.company_id),
            )
            append_audit_event(
                db,
                company_id=writer.company_id,
                domain="paper",
                entity_type="paper_challenger_trial",
                entity_id=comparison_id,
                event_type="paper.challenger_trial_invalidated",
                actor_id="strategy_comparison",
                payload={"comparison_id": comparison_id, "reason": invalidation_reason},
                recorded_at_ns=eval_ns,
            )
            updated = db.execute(
                """SELECT comparison_id, company_id, incumbent_release_id,
                          challenger_release_id, baseline_statement_revision,
                          analysis_plan_json, analysis_plan_sha256,
                          incumbent_arm_json, challenger_arm_json,
                          status, verdict, verdict_json, invalidation_reason,
                          registered_at_ns, evaluated_at_ns
                   FROM paper_challenger_trial
                   WHERE comparison_id = ? AND company_id = ?""",
                (comparison_id, writer.company_id),
            ).fetchone()
            assert updated is not None
            writer.verify()
            return _decode_trial_row(updated)

        inc_complete, inc_rev = _verify_flow08_statement(
            incumbent_obs.statement_report, trial.incumbent_arm.company_id
        )
        chal_complete, chal_rev = _verify_flow08_statement(
            challenger_obs.statement_report, trial.challenger_arm.company_id
        )
        if inc_rev == chal_rev:
            raise StrategyComparisonRefused(
                "Incumbent and challenger arms cannot reuse the identical statement"
            )
        allocated_costs = allocate_shared_cost(shared_trial_cost_minor, plan)

        (trial_count_n,) = db.execute(
            """SELECT COALESCE(MAX(research_trial_count_n), 1)
               FROM paper_strategy_release
               WHERE company_id = ?""",
            (writer.company_id,),
        ).fetchone()
        trial_count_n = max(1, int(trial_count_n))

        # 1. Operational fast-fail hard gates on challenger (cannot be waived by positive return).
        hard_failures: list[str] = []
        if challenger_obs.risk_invariant_breach:
            hard_failures.append("risk-invariant-breach")
        if challenger_obs.reconciliation_drift:
            hard_failures.append("reconciliation-drift")
        if challenger_obs.realized_drawdown_bps > plan.max_drawdown_bps:
            hard_failures.append("drawdown-limit-breach")
        if challenger_obs.peak_exposure_minor > plan.max_exposure_minor:
            hard_failures.append("exposure-limit-breach")
        if (
            challenger_obs.operating_cost_minor + allocated_costs[1]
            > plan.max_operating_cost_minor
        ):
            hard_failures.append("operating-cost-ceiling-breach")

        inc_issues: list[str] = []
        if incumbent_obs.reconciliation_drift:
            inc_issues.append("incumbent-reconciliation-drift")
        if incumbent_obs.risk_invariant_breach:
            inc_issues.append("incumbent-risk-breach")

        inc_series = incumbent_obs.interval_returns_bps
        chal_series = challenger_obs.interval_returns_bps
        paired_n = min(len(inc_series), len(chal_series))
        unaligned = len(inc_series) != len(chal_series)
        mean_diff: float | None = None
        lower: float | None = None
        upper: float | None = None
        alpha_eff = plan.alpha / trial_count_n

        if paired_n > 0 and not unaligned:
            diffs = tuple(
                chal - inc for chal, inc in zip(chal_series, inc_series, strict=True)
            )
            alpha_eff, mean_diff, lower, upper = compute_anytime_valid_bounds(
                diffs,
                alpha=plan.alpha,
                research_trial_count_n=trial_count_n,
                variance_floor_bps2=plan.variance_floor_bps2,
            )

        if hard_failures:
            verdict_label = "REJECT"
            reasons = tuple(hard_failures)
        elif inc_issues:
            verdict_label = "INCONCLUSIVE"
            reasons = tuple(inc_issues)
        elif not (inc_complete and chal_complete) or unaligned:
            verdict_label = "INCONCLUSIVE"
            reasons = tuple(
                r
                for r, flag in (
                    ("incomplete-incumbent-statement", not inc_complete),
                    ("incomplete-challenger-statement", not chal_complete),
                    ("unaligned-paired-series", unaligned),
                )
                if flag
            )
        elif paired_n < plan.min_observations or lower is None or upper is None:
            verdict_label = "INCONCLUSIVE"
            reasons = ("insufficient-observations",)
        elif lower >= plan.worthwhile_margin_bps:
            verdict_label = "PROMOTE"
            reasons = ("anytime-valid-margin-exceeded",)
        elif upper <= plan.futility_margin_bps:
            verdict_label = "REJECT"
            reasons = ("anytime-valid-futility-bound-crossed",)
        else:
            verdict_label = "INCONCLUSIVE"
            reasons = ("confidence-sequence-straddles-threshold",)

        detail = SequentialVerdict(
            verdict=verdict_label,
            reasons=reasons,
            observations_n=paired_n,
            research_trial_count_n=trial_count_n,
            alpha_effective=alpha_eff,
            mean_diff_bps=mean_diff,
            lower_bound_bps=lower,
            upper_bound_bps=upper,
            incumbent_statement_revision=inc_rev,
            challenger_statement_revision=chal_rev,
            multi_component_change=len(plan.changed_components) > 1,
            shared_cost_allocated_minor=allocated_costs,
        )
        verdict_json = _canonical_json(asdict(detail))
        db.execute(
            """UPDATE paper_challenger_trial
               SET status = 'completed', verdict = ?, verdict_json = ?,
                   evaluated_at_ns = ?
               WHERE comparison_id = ? AND company_id = ?""",
            (verdict_label, verdict_json, eval_ns, comparison_id, writer.company_id),
        )
        append_audit_event(
            db,
            company_id=writer.company_id,
            domain="paper",
            entity_type="paper_challenger_trial",
            entity_id=comparison_id,
            event_type="paper.challenger_evaluated",
            actor_id="strategy_comparison",
            payload={
                "comparison_id": comparison_id,
                "verdict": verdict_label,
                "observations_n": paired_n,
                "research_trial_count_n": trial_count_n,
                "reasons": list(reasons),
            },
            recorded_at_ns=eval_ns,
        )
        writer.verify()
        updated = db.execute(
            """SELECT comparison_id, company_id, incumbent_release_id,
                      challenger_release_id, baseline_statement_revision,
                      analysis_plan_json, analysis_plan_sha256,
                      incumbent_arm_json, challenger_arm_json,
                      status, verdict, verdict_json, invalidation_reason,
                      registered_at_ns, evaluated_at_ns
               FROM paper_challenger_trial
               WHERE comparison_id = ? AND company_id = ?""",
            (comparison_id, writer.company_id),
        ).fetchone()
        assert updated is not None
        return _decode_trial_row(updated)


@dataclass(frozen=True)
class StrategyDeployment:
    deployment_id: str
    company_id: str
    release_id: str
    program_identity: str
    binding_revision: str
    comparison_id: str | None
    status: str
    command_id: str
    activated_at_ns: int
    retired_at_ns: int | None


def _require_current_paper_binding(
    db: Connection, company_id: str, expected_revision: str
) -> str:
    try:
        binding = read_current_binding(db, company_id)
    except BindingRefused as error:
        raise StrategyComparisonRefused(
            "Strategy deployment requires an active bound paper account"
        ) from error
    if has_pending_binding_change(db):
        raise StrategyComparisonRefused(
            "Strategy deployment is blocked while a paper binding changeover is pending"
        )
    if binding.revision != expected_revision:
        raise StrategyComparisonRefused(
            "Strategy deployment binding_revision does not match current paper mandate"
        )
    return binding.revision


def activate_strategy_release(
    writer: WriterLease,
    *,
    command_id: str,
    release_id: str,
    binding_revision: str,
    authorize_deploy: AuthorizeDeployment | None,
    comparison_id: str | None = None,
    expected_active_deployment_id: str | None = None,
) -> StrategyDeployment:
    """Activate a sealed StrategyRelease with a fresh revocable program identity."""
    writer.verify()
    _require_token(command_id, "command_id")
    _require_safe_id(release_id, "release_id")
    _require_token(binding_revision, "binding_revision")
    if comparison_id is not None:
        _require_safe_id(comparison_id, "comparison_id")

    payload_sha = _sha256_json(
        {
            "release_id": release_id,
            "binding_revision": binding_revision,
            "comparison_id": comparison_id,
        }
    )
    preconditions = {
        "expected_active_deployment_id": expected_active_deployment_id,
    }

    with (
        closing(open_writer(writer.store, lease=writer)) as db,
        write_tx(db),
    ):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise StrategyComparisonRefused("Deployment belongs to another company")
        enable_domain_v1(
            db, "paper", company_id=writer.company_id, actor_id="strategy_deployment"
        )
        deployer = (
            authorize_deploy(db, writer.company_id, ACTIVATE_STRATEGY_CMD, release_id)
            if authorize_deploy is not None
            else None
        )
        if not isinstance(deployer, str) or not deployer.strip() or len(deployer) > 128:
            raise StrategyComparisonRefused(
                "Explicit strategy deployment grant is required"
            )

        release_row = db.execute(
            """SELECT owner_agent_id FROM paper_strategy_release
               WHERE release_id = ? AND company_id = ?""",
            (release_id, writer.company_id),
        ).fetchone()
        if release_row is None:
            raise StrategyComparisonRefused("Strategy release is not registered")
        if deployer == release_row[0]:
            raise StrategyComparisonRefused(
                "Strategy author grant cannot self-activate a deployment"
            )

        existing_cmd = db.execute(
            """SELECT deployment_id, company_id, release_id, program_identity,
                      binding_revision, comparison_id, status, command_id,
                      activated_at_ns, retired_at_ns
               FROM paper_strategy_deployment
               WHERE company_id = ? AND command_id = ?""",
            (writer.company_id, command_id),
        ).fetchone()
        if existing_cmd is not None:
            try:
                cmd_receipt, replayed = admit_command_receipt(
                    db,
                    company_id=writer.company_id,
                    command_id=command_id,
                    actor_id=deployer,
                    peer_pid=os.getpid(),
                    command_type=ACTIVATE_STRATEGY_CMD,
                    contract_version=STRATEGY_DEPLOY_CONTRACT,
                    payload_sha256=payload_sha,
                    preconditions=preconditions,
                    state="applied",
                )
            except CommandConflict as error:
                raise StrategyComparisonRefused(
                    "Deployment command ID conflicts with prior request"
                ) from error
            if not replayed or cmd_receipt.state != "applied":
                raise StrategyComparisonRefused(
                    "Deployment command receipt is not in applied replay state"
                )
            return StrategyDeployment(*existing_cmd)

        _require_current_paper_binding(db, writer.company_id, binding_revision)

        active_row = db.execute(
            """SELECT deployment_id, release_id, activated_at_ns
               FROM paper_strategy_deployment
               WHERE company_id = ? AND status = 'active'""",
            (writer.company_id,),
        ).fetchone()
        active_id = active_row[0] if active_row is not None else None
        if expected_active_deployment_id != active_id:
            raise StrategyComparisonRefused(
                "Active deployment revision does not match expected precondition"
            )

        if active_row is not None or comparison_id is not None:
            if comparison_id is None:
                raise StrategyComparisonRefused(
                    "Replacing an active strategy requires a completed PROMOTE comparison"
                )
            if (
                db.execute(
                    """SELECT 1 FROM paper_strategy_deployment
                       WHERE company_id = ? AND comparison_id = ?""",
                    (writer.company_id, comparison_id),
                ).fetchone()
                is not None
            ):
                raise StrategyComparisonRefused(
                    "Comparison trial was already consumed by a prior deployment"
                )
            trial_row = db.execute(
                """SELECT incumbent_release_id, challenger_release_id, status, verdict
                   FROM paper_challenger_trial
                   WHERE comparison_id = ? AND company_id = ?""",
                (comparison_id, writer.company_id),
            ).fetchone()
            if (
                trial_row is None
                or trial_row[2] != "completed"
                or trial_row[3] != "PROMOTE"
                or trial_row[1] != release_id
                or (active_row is not None and trial_row[0] != active_row[1])
            ):
                raise StrategyComparisonRefused(
                    "Candidate cannot be promoted without a completed PROMOTE trial for the active incumbent"
                )

        now_ns = (
            max(time_ns(), int(active_row[2])) if active_row is not None else time_ns()
        )
        deployment_id = f"dep-{uuid4().hex[:16]}"
        program_identity = f"prog-{uuid4().hex[:16]}"
        applied_seq = append_audit_event(
            db,
            company_id=writer.company_id,
            domain="paper",
            entity_type="paper_strategy_deployment",
            entity_id=deployment_id,
            event_type="paper.strategy_activated",
            actor_id=deployer,
            command_id=command_id,
            payload={
                "deployment_id": deployment_id,
                "release_id": release_id,
                "program_identity": program_identity,
                "comparison_id": comparison_id,
                "superseded_deployment_id": active_id,
            },
            recorded_at_ns=now_ns,
        )
        try:
            _, replayed = admit_command_receipt(
                db,
                company_id=writer.company_id,
                command_id=command_id,
                actor_id=deployer,
                peer_pid=os.getpid(),
                command_type=ACTIVATE_STRATEGY_CMD,
                contract_version=STRATEGY_DEPLOY_CONTRACT,
                payload_sha256=payload_sha,
                preconditions=preconditions,
                state="applied",
                result={
                    "deployment_id": deployment_id,
                    "release_id": release_id,
                    "program_identity": program_identity,
                },
                applied_seq=applied_seq,
                now_ns=now_ns,
            )
        except CommandConflict as error:
            raise StrategyComparisonRefused(
                "Deployment command ID conflicts with prior request"
            ) from error
        if replayed:
            raise StrategyComparisonRefused(
                "Deployment command ID was already recorded without an applied deployment"
            )

        if active_id is not None:
            db.execute(
                """UPDATE paper_strategy_deployment
                   SET status = 'superseded', retired_at_ns = ?
                   WHERE deployment_id = ? AND company_id = ? AND status = 'active'""",
                (now_ns, active_id, writer.company_id),
            )
        db.execute(
            """INSERT INTO paper_strategy_deployment (
                   deployment_id, company_id, release_id, program_identity,
                   binding_revision, comparison_id, status, command_id,
                   activated_at_ns, retired_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL)""",
            (
                deployment_id,
                writer.company_id,
                release_id,
                program_identity,
                binding_revision,
                comparison_id,
                command_id,
                now_ns,
            ),
        )
        writer.verify()
        return StrategyDeployment(
            deployment_id=deployment_id,
            company_id=writer.company_id,
            release_id=release_id,
            program_identity=program_identity,
            binding_revision=binding_revision,
            comparison_id=comparison_id,
            status="active",
            command_id=command_id,
            activated_at_ns=now_ns,
            retired_at_ns=None,
        )


def rollback_strategy_deployment(
    writer: WriterLease,
    *,
    command_id: str,
    target_release_id: str,
    expected_active_deployment_id: str,
    authorize_deploy: AuthorizeDeployment | None,
) -> StrategyDeployment:
    """Fence and roll back an active deployment to a previously deployed release."""
    writer.verify()
    _require_token(command_id, "command_id")
    _require_safe_id(target_release_id, "target_release_id")
    _require_token(expected_active_deployment_id, "expected_active_deployment_id")
    payload_sha = _sha256_json(
        {
            "target_release_id": target_release_id,
            "expected_active_deployment_id": expected_active_deployment_id,
        }
    )
    preconditions = {
        "expected_active_deployment_id": expected_active_deployment_id,
    }

    with (
        closing(open_writer(writer.store, lease=writer)) as db,
        write_tx(db),
    ):
        writer.verify()
        if _identity(db) != writer.company_id:
            raise StrategyComparisonRefused("Rollback belongs to another company")
        if not is_domain_installed(db, "paper"):
            raise StrategyComparisonRefused("No active deployment to roll back")
        deployer = (
            authorize_deploy(
                db, writer.company_id, ROLLBACK_STRATEGY_CMD, target_release_id
            )
            if authorize_deploy is not None
            else None
        )
        if not isinstance(deployer, str) or not deployer.strip() or len(deployer) > 128:
            raise StrategyComparisonRefused("Explicit rollback grant is required")

        target_row = db.execute(
            """SELECT owner_agent_id FROM paper_strategy_release
               WHERE release_id = ? AND company_id = ?""",
            (target_release_id, writer.company_id),
        ).fetchone()
        if target_row is None:
            raise StrategyComparisonRefused("Rollback target release is not registered")
        if deployer == target_row[0]:
            raise StrategyComparisonRefused(
                "Strategy author grant cannot self-activate via rollback"
            )

        existing_cmd = db.execute(
            """SELECT deployment_id, company_id, release_id, program_identity,
                      binding_revision, comparison_id, status, command_id,
                      activated_at_ns, retired_at_ns
               FROM paper_strategy_deployment
               WHERE company_id = ? AND command_id = ?""",
            (writer.company_id, command_id),
        ).fetchone()
        if existing_cmd is not None:
            try:
                cmd_receipt, replayed = admit_command_receipt(
                    db,
                    company_id=writer.company_id,
                    command_id=command_id,
                    actor_id=deployer,
                    peer_pid=os.getpid(),
                    command_type=ROLLBACK_STRATEGY_CMD,
                    contract_version=STRATEGY_DEPLOY_CONTRACT,
                    payload_sha256=payload_sha,
                    preconditions=preconditions,
                    state="applied",
                )
            except CommandConflict as error:
                raise StrategyComparisonRefused(
                    "Rollback command ID conflicts with prior request"
                ) from error
            if not replayed or cmd_receipt.state != "applied":
                raise StrategyComparisonRefused(
                    "Rollback command receipt is not in applied replay state"
                )
            return StrategyDeployment(*existing_cmd)

        active_row = db.execute(
            """SELECT deployment_id, release_id, binding_revision, activated_at_ns
               FROM paper_strategy_deployment
               WHERE company_id = ? AND status = 'active'""",
            (writer.company_id,),
        ).fetchone()
        if active_row is None or active_row[0] != expected_active_deployment_id:
            raise StrategyComparisonRefused(
                "Active deployment does not match expected rollback target"
            )
        if target_release_id == active_row[1]:
            raise StrategyComparisonRefused(
                "Rollback target release is already the active deployment"
            )
        # Require rollback target to be a previously deployed release (never an untested candidate!).
        if (
            db.execute(
                """SELECT 1 FROM paper_strategy_deployment
                   WHERE company_id = ?
                     AND release_id = ?
                     AND status IN ('superseded', 'rolled_back')
                   LIMIT 1""",
                (writer.company_id, target_release_id),
            ).fetchone()
            is None
        ):
            raise StrategyComparisonRefused(
                "Rollback target must be a previously deployed strategy release"
            )
        current_binding_rev = _require_current_paper_binding(
            db, writer.company_id, active_row[2]
        )

        now_ns = max(time_ns(), int(active_row[3]))
        deployment_id = f"dep-{uuid4().hex[:16]}"
        program_identity = f"prog-{uuid4().hex[:16]}"
        applied_seq = append_audit_event(
            db,
            company_id=writer.company_id,
            domain="paper",
            entity_type="paper_strategy_deployment",
            entity_id=deployment_id,
            event_type="paper.strategy_rolled_back",
            actor_id=deployer,
            command_id=command_id,
            payload={
                "rolled_back_deployment_id": active_row[0],
                "restored_deployment_id": deployment_id,
                "target_release_id": target_release_id,
                "program_identity": program_identity,
            },
            recorded_at_ns=now_ns,
        )
        try:
            _, replayed = admit_command_receipt(
                db,
                company_id=writer.company_id,
                command_id=command_id,
                actor_id=deployer,
                peer_pid=os.getpid(),
                command_type=ROLLBACK_STRATEGY_CMD,
                contract_version=STRATEGY_DEPLOY_CONTRACT,
                payload_sha256=payload_sha,
                preconditions=preconditions,
                state="applied",
                result={
                    "rolled_back_deployment_id": active_row[0],
                    "deployment_id": deployment_id,
                    "target_release_id": target_release_id,
                    "program_identity": program_identity,
                },
                applied_seq=applied_seq,
                now_ns=now_ns,
            )
        except CommandConflict as error:
            raise StrategyComparisonRefused(
                "Rollback command ID conflicts with prior request"
            ) from error
        if replayed:
            raise StrategyComparisonRefused(
                "Rollback command ID was already recorded without an applied deployment"
            )

        db.execute(
            """UPDATE paper_strategy_deployment
               SET status = 'rolled_back', retired_at_ns = ?
               WHERE deployment_id = ? AND company_id = ? AND status = 'active'""",
            (now_ns, active_row[0], writer.company_id),
        )
        db.execute(
            """INSERT INTO paper_strategy_deployment (
                   deployment_id, company_id, release_id, program_identity,
                   binding_revision, comparison_id, status, command_id,
                   activated_at_ns, retired_at_ns
               ) VALUES (?, ?, ?, ?, ?, NULL, 'active', ?, ?, NULL)""",
            (
                deployment_id,
                writer.company_id,
                target_release_id,
                program_identity,
                current_binding_rev,
                command_id,
                now_ns,
            ),
        )
        writer.verify()
        return StrategyDeployment(
            deployment_id=deployment_id,
            company_id=writer.company_id,
            release_id=target_release_id,
            program_identity=program_identity,
            binding_revision=current_binding_rev,
            comparison_id=None,
            status="active",
            command_id=command_id,
            activated_at_ns=now_ns,
            retired_at_ns=None,
        )


def read_active_strategy_deployment(
    store: Path, company_id: str
) -> StrategyDeployment | None:
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise StrategyComparisonRefused("Deployment belongs to another company")
        if not is_domain_installed(db, "paper"):
            return None
        row = db.execute(
            """SELECT deployment_id, company_id, release_id, program_identity,
                      binding_revision, comparison_id, status, command_id,
                      activated_at_ns, retired_at_ns
               FROM paper_strategy_deployment
               WHERE company_id = ? AND status = 'active'""",
            (company_id,),
        ).fetchone()
        return StrategyDeployment(*row) if row is not None else None


def read_challenger_trial(
    store: Path, company_id: str, comparison_id: str
) -> ChallengerTrial | None:
    with closing(open_reader(store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise StrategyComparisonRefused("Comparison belongs to another company")
        if not is_domain_installed(db, "paper"):
            return None
        row = db.execute(
            """SELECT comparison_id, company_id, incumbent_release_id,
                      challenger_release_id, baseline_statement_revision,
                      analysis_plan_json, analysis_plan_sha256,
                      incumbent_arm_json, challenger_arm_json,
                      status, verdict, verdict_json, invalidation_reason,
                      registered_at_ns, evaluated_at_ns
               FROM paper_challenger_trial
               WHERE comparison_id = ? AND company_id = ?""",
            (comparison_id, company_id),
        ).fetchone()
        return _decode_trial_row(row) if row is not None else None


def register_challenger_duties(
    registry: DutyRegistry,
    *,
    observe_arms: Callable[
        [ChallengerTrial], tuple[ArmObservation, ArmObservation, int]
    ],
    required_scope: str | None = None,
) -> None:
    """Register the idempotent quant.challenger_eval DutyRegistry handler."""

    def _run_eval(context: DutyContext) -> DutyOutcome:
        context.writer.verify_for(context.writer.store, context.company_id)
        try:
            payload = json.loads(context.work_item.payload_json)
        except json.JSONDecodeError as error:
            raise StrategyComparisonRefused(
                "Invalid quant.challenger_eval payload"
            ) from error
        if not isinstance(payload, dict) or not isinstance(
            payload.get("comparison_id"), str
        ):
            raise StrategyComparisonRefused(
                "quant.challenger_eval payload requires string comparison_id"
            )
        comparison_id = payload["comparison_id"]
        trial = read_challenger_trial(
            context.writer.store, context.company_id, comparison_id
        )
        if trial is None:
            raise StrategyComparisonRefused("Comparison trial is not registered")
        if trial.status in ("completed", "invalidated"):
            return DutyOutcome(
                disposition="completed",
                result={
                    "comparison_id": trial.comparison_id,
                    "status": trial.status,
                    "verdict": trial.verdict,
                    "invalidation_reason": trial.invalidation_reason,
                },
            )
        inc_obs, chal_obs, shared_cost = observe_arms(trial)
        evaluated = evaluate_challenger_trial(
            context.writer,
            comparison_id=comparison_id,
            incumbent_obs=inc_obs,
            challenger_obs=chal_obs,
            shared_trial_cost_minor=shared_cost,
            now_ns=context.now_ns,
        )
        return DutyOutcome(
            disposition="completed",
            result={
                "comparison_id": evaluated.comparison_id,
                "status": evaluated.status,
                "verdict": evaluated.verdict,
                "invalidation_reason": evaluated.invalidation_reason,
            },
        )

    registry.register(
        DutyDescriptor(
            duty_kind=CHALLENGER_EVAL_DUTY_KIND,
            execute=_run_eval,
            required_scope=required_scope,
        )
    )
