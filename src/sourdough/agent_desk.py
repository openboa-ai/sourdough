"""Own private draft custody physically isolated under each Agent's Desk directory."""

import json
import os
import re
import shutil
import stat
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from sqlite3 import Connection
from uuid import uuid4

from .company import _connect, _identity
from .duty_run import (
    DutyClaim,
    DutyRefused,
    WorkRunClaim,
    employment_cutoff,
    resolve_run,
)
from .storage.db import open_reader, read_tx
from .writer_lease import WriterLease


_NAME = re.compile(r"[a-z][a-z0-9-]{0,47}")
_AGENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_MAX_DRAFTS = 6
_MAX_CONTENT_BYTES = 800
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
EMPTY_DESK_DIGEST = sha256(b"[]").hexdigest()


@dataclass(frozen=True)
class DeskLimits:
    max_drafts: int = 64
    max_content_bytes: int = 65_536
    max_total_bytes: int = 4_194_304
    max_scratch_bytes: int = 16_777_216


LEGACY_DESK_LIMITS = DeskLimits(
    max_drafts=_MAX_DRAFTS,
    max_content_bytes=_MAX_CONTENT_BYTES,
    max_total_bytes=_MAX_DRAFTS * _MAX_CONTENT_BYTES * 2,
    max_scratch_bytes=16_777_216,
)
PRODUCTION_DESK_LIMITS = DeskLimits()


@dataclass(frozen=True)
class Draft:
    name: str
    revision: str
    content: str


def _validate_agent_id(agent_id: str) -> str:
    if (
        not isinstance(agent_id, str)
        or not _AGENT_ID.fullmatch(agent_id)
        or ".." in agent_id
    ):
        raise DutyRefused("Invalid Agent identity for private Desk")
    return agent_id


def desks_root(store: Path) -> Path:
    """Return the company's private desks directory sibling to the company store."""
    return store.resolve(strict=False).parent / "desks"


def desk_directory(store: Path, agent_id: str) -> Path:
    """Return the physically isolated per-Agent Desk path (/desks/<agent_id>)."""
    safe_agent = _validate_agent_id(agent_id)
    return desks_root(store) / safe_agent


def _drafts_directory(store: Path, agent_id: str) -> Path:
    return desk_directory(store, agent_id) / ".desk" / "drafts"


def logs_directory(store: Path, agent_id: str) -> Path:
    """Return the offloaded tool logs directory inside an Agent's Desk."""
    return desk_directory(store, agent_id) / ".desk" / "logs"


def scratch_directory(store: Path, agent_id: str, run_id: str) -> Path:
    """Return the ephemeral per-run scratch directory inside an Agent's Desk."""
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or ".." in run_id:
        raise DutyRefused("Invalid run_id for Desk scratch directory")
    return desk_directory(store, agent_id) / ".desk" / "scratch" / run_id


def _dir_bytes(directory: Path) -> int:
    total = 0
    if not os.path.lexists(directory):
        return 0
    for root_str, _dirs, files in os.walk(directory, followlinks=False):
        root_path = Path(root_str)
        for fname in files:
            fpath = root_path / fname
            st = os.lstat(fpath)
            if stat.S_ISLNK(st.st_mode):
                raise DutyRefused("Symlinks are forbidden inside private Desk storage")
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total


def allocate_run_scratch(
    store: Path,
    agent_id: str,
    run_id: str,
    *,
    limits: DeskLimits = PRODUCTION_DESK_LIMITS,
) -> Path:
    """Create an isolated ephemeral scratch directory for one run under .desk/scratch/<run_id>/."""
    root = store.resolve(strict=False).parent
    scratch_root = desk_directory(store, agent_id) / ".desk" / "scratch"
    target = scratch_directory(store, agent_id, run_id)
    _ensure_private_dir(target, root)
    if _dir_bytes(scratch_root) > limits.max_scratch_bytes:
        raise DutyRefused("Private Desk scratch byte quota exceeded")
    return target


def cleanup_run_scratch(store: Path, agent_id: str, run_id: str) -> None:
    """Remove an ephemeral per-run scratch directory safely without following symlinks."""
    root = store.resolve(strict=False).parent
    target = scratch_directory(store, agent_id, run_id)
    if not _check_no_symlinks(target, root):
        return
    shutil.rmtree(target, ignore_errors=False)


def compute_agent_desk_digest(store: Path, agent_id: str) -> str:
    """Compute canonical SHA-256 over sorted (filename, file_sha256) of .desk/drafts/*.json."""
    root = store.resolve(strict=False).parent
    drafts_dir = _drafts_directory(store, agent_id)
    if not _check_no_symlinks(drafts_dir, root):
        return EMPTY_DESK_DIGEST
    st = os.lstat(drafts_dir)
    if not stat.S_ISDIR(st.st_mode):
        raise DutyRefused("Private Desk drafts path must be a regular directory")
    entries: list[tuple[str, str]] = []
    for entry in sorted(drafts_dir.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or entry.suffix != ".json":
            continue
        est = os.lstat(entry)
        if stat.S_ISLNK(est.st_mode) or not stat.S_ISREG(est.st_mode):
            raise DutyRefused("Private Desk draft must be a regular file")
        if est.st_nlink != 1:
            raise DutyRefused("Hard-link aliases are forbidden inside private Desk")
        fd = os.open(entry, os.O_RDONLY | _O_NOFOLLOW)
        try:
            fst = os.fstat(fd)
            if not stat.S_ISREG(fst.st_mode) or fst.st_nlink != 1:
                raise DutyRefused("Private Desk draft file failed inode validation")
            chunks: list[bytes] = []
            while chunk := os.read(fd, 65_536):
                chunks.append(chunk)
        finally:
            os.close(fd)
        entries.append((entry.name, sha256(b"".join(chunks)).hexdigest()))
    entries.sort()
    return sha256(
        json.dumps(entries, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _ensure_private_dir(path: Path, stop_at: Path) -> None:
    """Create directories from stop_at down to path with mode 0o700 and refuse symlinks."""
    rel = path.relative_to(stop_at)
    current = stop_at
    for part in rel.parts:
        current = current / part
        if os.path.lexists(current):
            st = os.lstat(current)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise DutyRefused("Private Desk path must be a regular directory")
        else:
            os.mkdir(current, 0o700)
        os.chmod(current, 0o700)


def _check_no_symlinks(path: Path, stop_at: Path) -> bool:
    """Verify no component between stop_at and path is a symlink; return False if absent."""
    if not os.path.lexists(path):
        return False
    rel = path.relative_to(stop_at)
    current = stop_at
    for part in rel.parts:
        current = current / part
        if not os.path.lexists(current):
            return False
        st = os.lstat(current)
        if stat.S_ISLNK(st.st_mode):
            raise DutyRefused("Symlinks are forbidden inside private Desk storage")
    return True


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_draft_file(
    path: Path, *, limits: DeskLimits = LEGACY_DESK_LIMITS
) -> Draft | None:
    if not os.path.lexists(path):
        return None
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise DutyRefused("Private Desk draft must be a regular file")
    if st.st_nlink != 1:
        raise DutyRefused("Hard-link aliases are forbidden inside private Desk")
    fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    try:
        max_read = limits.max_content_bytes * 8 + 4096
        raw = os.read(fd, max_read + 1)
    finally:
        os.close(fd)
    if len(raw) > max_read:
        raise DutyRefused("Private Desk snapshot exceeds its admitted bounds")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DutyRefused("Private Desk draft file is corrupted") from error
    if not isinstance(payload, dict) or set(payload) != {
        "name",
        "revision",
        "content",
    }:
        raise DutyRefused("Private Desk draft file has invalid metadata")
    name = payload["name"]
    revision = payload["revision"]
    content = payload["content"]
    if (
        not isinstance(name, str)
        or not isinstance(revision, str)
        or not isinstance(content, str)
        or not _NAME.fullmatch(name)
        or len(content.encode("utf-8")) > limits.max_content_bytes
    ):
        raise DutyRefused("Private Desk snapshot exceeds its admitted bounds")
    return Draft(name=name, revision=revision, content=content)


def _write_file_atomic(directory: Path, filename: str, data: bytes) -> None:
    tmp_path = directory / f".tmp-{uuid4().hex}-{filename}"
    fd = os.open(
        tmp_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    os.close(fd)
    target = directory / filename
    if os.path.lexists(target) and stat.S_ISLNK(os.lstat(target).st_mode):
        tmp_path.unlink()
        raise DutyRefused("Symlinks are forbidden inside private Desk storage")
    os.replace(tmp_path, target)
    os.chmod(target, 0o600)
    _fsync_dir(directory)


def ensure_private_desk(
    database: Connection, company_id: str, agent_id: str, execution_profile: str
) -> None:
    revision = sha256(
        f"{company_id}:{agent_id}:{execution_profile}".encode()
    ).hexdigest()
    database.execute(
        "INSERT INTO private_desk VALUES (?,?) ON CONFLICT(agent_id) DO NOTHING",
        (agent_id, revision),
    )


def require_desk(
    database: Connection, claim: DutyClaim | WorkRunClaim, purpose: str
) -> str:
    authority = resolve_run(database, claim)
    authority.require_current(purpose)
    if authority.duty_state != "running":
        raise DutyRefused("Scoped workflow requires the current running duty")
    row = database.execute(
        "SELECT manifest_revision FROM private_desk WHERE agent_id=?",
        (claim.founder_id,),
    ).fetchone()
    if not row:
        raise DutyRefused("The current Agent has no private Desk")
    return row[0]


def draft_at_revision(
    store: Path,
    agent_id: str,
    name: str,
    revision: str,
    *,
    limits: DeskLimits = PRODUCTION_DESK_LIMITS,
) -> Draft | None:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        return None
    root = store.resolve(strict=False).parent
    drafts_dir = _drafts_directory(store, agent_id)
    if not _check_no_symlinks(drafts_dir, root):
        return None
    draft_path = drafts_dir / f"{name}.json"
    if not os.path.lexists(draft_path):
        return None
    draft = _read_draft_file(draft_path, limits=limits)
    if draft is None or draft.revision != revision or draft.name != name:
        return None
    return draft


def resolve_desk(
    writer: WriterLease,
    claim: DutyClaim | WorkRunClaim,
    purpose: str,
    *,
    include_drafts: bool = False,
    limits: DeskLimits = LEGACY_DESK_LIMITS,
) -> tuple[str, tuple[Draft, ...]]:
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        revision = require_desk(database, claim, purpose)
    drafts: tuple[Draft, ...] = ()
    if include_drafts:
        root = writer.store.resolve(strict=False).parent
        drafts_dir = _drafts_directory(writer.store, claim.founder_id)
        if _check_no_symlinks(drafts_dir, root):
            loaded: list[Draft] = []
            for entry in sorted(drafts_dir.iterdir(), key=lambda p: p.name):
                if entry.name.startswith(".") or entry.suffix != ".json":
                    continue
                draft = _read_draft_file(entry, limits=limits)
                if draft is not None:
                    loaded.append(draft)
                if len(loaded) > limits.max_drafts:
                    raise DutyRefused(
                        "Private Desk snapshot exceeds its admitted bounds"
                    )
            drafts = tuple(sorted(loaded, key=lambda item: item.name))
    return revision, drafts


def save_draft(
    writer: WriterLease,
    claim: DutyClaim | WorkRunClaim,
    purpose: str,
    *,
    name: str,
    content: str,
    expected_revision: str | None,
    limits: DeskLimits = LEGACY_DESK_LIMITS,
) -> Draft:
    """Persist an authorized private draft on disk outside company.sqlite."""
    if (
        not isinstance(name, str)
        or not _NAME.fullmatch(name)
        or not isinstance(content, str)
        or not 0 < len(content.encode("utf-8")) <= limits.max_content_bytes
    ):
        raise DutyRefused("Invalid private draft name or content")
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        desk = require_desk(database, claim, purpose)
    root = writer.store.resolve(strict=False).parent
    drafts_dir = _drafts_directory(writer.store, claim.founder_id)
    _ensure_private_dir(drafts_dir, root)
    revision = sha256(
        json.dumps(
            (desk, name, expected_revision, content), separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    draft = Draft(name, revision, content)
    draft_path = drafts_dir / f"{name}.json"
    previous = (
        _read_draft_file(draft_path, limits=limits)
        if os.path.lexists(draft_path)
        else None
    )
    if (
        previous is not None
        and previous.content == content
        and expected_revision in (None, previous.revision)
    ):
        return previous
    if (previous.revision if previous is not None else None) != expected_revision:
        raise DutyRefused("Private draft revision changed")
    existing_count = sum(
        1
        for entry in drafts_dir.iterdir()
        if not entry.name.startswith(".") and entry.suffix == ".json"
    )
    if previous is None and existing_count >= limits.max_drafts:
        raise DutyRefused("Private Desk draft capacity is exhausted")
    encoded = json.dumps(
        {"name": name, "revision": revision, "content": content},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    existing_total = sum(
        os.lstat(entry).st_size
        for entry in drafts_dir.iterdir()
        if not entry.name.startswith(".")
        and entry.suffix == ".json"
        and entry.name != f"{name}.json"
        and stat.S_ISREG(os.lstat(entry).st_mode)
    )
    if existing_total + len(encoded) > limits.max_total_bytes:
        raise DutyRefused("Private Desk total draft capacity is exhausted")
    _write_file_atomic(drafts_dir, f"{name}.json", encoded)
    _write_file_atomic(drafts_dir, f"{name}.md", content.encode("utf-8"))
    writer.verify()
    return draft


def delete_draft(
    writer: WriterLease,
    claim: DutyClaim | WorkRunClaim,
    purpose: str,
    *,
    name: str,
    expected_revision: str | None = None,
    limits: DeskLimits = PRODUCTION_DESK_LIMITS,
) -> str:
    """Remove an authorized private draft from disk and return the updated Desk digest."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise DutyRefused("Invalid private draft name")
    writer.verify_for(writer.store, claim.company_id)
    with closing(_connect(writer.store, "ro")) as database:
        database.execute("BEGIN")
        require_desk(database, claim, purpose)
    root = writer.store.resolve(strict=False).parent
    drafts_dir = _drafts_directory(writer.store, claim.founder_id)
    if not _check_no_symlinks(drafts_dir, root):
        raise DutyRefused("Private draft does not exist")
    draft_path = drafts_dir / f"{name}.json"
    if not _check_no_symlinks(draft_path, root):
        raise DutyRefused("Private draft does not exist")
    existing = _read_draft_file(draft_path, limits=limits)
    if existing is None:
        raise DutyRefused("Private draft does not exist")
    if expected_revision is not None and existing.revision != expected_revision:
        raise DutyRefused("Private draft revision changed")
    draft_path.unlink()
    md_path = drafts_dir / f"{name}.md"
    if _check_no_symlinks(md_path, root):
        st = os.lstat(md_path)
        if not stat.S_ISREG(st.st_mode):
            raise DutyRefused("Private Desk draft mirror must be a regular file")
        md_path.unlink()
    _fsync_dir(drafts_dir)
    writer.verify()
    return compute_agent_desk_digest(writer.store, claim.founder_id)


def clone_desk_snapshot(
    source_store: Path,
    company_id: str,
    agent_ids: Sequence[str],
    target_store: Path,
    *,
    expected_revisions: Mapping[str, str] | None = None,
    limits: DeskLimits = PRODUCTION_DESK_LIMITS,
) -> dict[str, str]:
    """Clone only authorized active Agents' .desk/drafts/ into an isolated target store."""
    unique_agents = tuple(dict.fromkeys(agent_ids))
    for aid in unique_agents:
        _validate_agent_id(aid)

    source_root = source_store.resolve(strict=False).parent
    target_root = target_store.resolve(strict=False).parent
    cloned: dict[str, str] = {}

    with closing(open_reader(source_store)) as db, read_tx(db):
        if _identity(db) != company_id:
            raise DutyRefused("Company identity mismatch during Desk snapshot clone")
        for aid in unique_agents:
            agent_row = db.execute(
                "SELECT 1 FROM agent WHERE id = ? AND company_id = ?",
                (aid, company_id),
            ).fetchone()
            if agent_row is None:
                continue
            if employment_cutoff(db, aid) is not None:
                continue
            grant_row = db.execute(
                """SELECT scope FROM standing_grant
                   WHERE agent_id = ? AND trim(scope) != '' AND lower(trim(scope)) != 'revoked'""",
                (aid,),
            ).fetchone()
            if grant_row is None:
                continue
            desk_row = db.execute(
                "SELECT manifest_revision FROM private_desk WHERE agent_id = ?",
                (aid,),
            ).fetchone()
            if desk_row is None:
                continue
            manifest_rev = str(desk_row[0])
            src_digest = compute_agent_desk_digest(source_store, aid)
            if expected_revisions is not None and aid in expected_revisions:
                expected = expected_revisions[aid]
                if expected != manifest_rev and expected != src_digest:
                    raise DutyRefused(
                        f"Private Desk revision mismatch for Agent '{aid}'"
                    )

            target_desk = desk_directory(target_store, aid)
            target_drafts = target_desk / ".desk" / "drafts"
            target_scratch = target_desk / ".desk" / "scratch"
            target_logs = target_desk / ".desk" / "logs"
            _ensure_private_dir(target_drafts, target_root)
            _ensure_private_dir(target_scratch, target_root)
            _ensure_private_dir(target_logs, target_root)

            src_drafts = _drafts_directory(source_store, aid)
            if _check_no_symlinks(src_drafts, source_root):
                draft_count = 0
                total_bytes = 0
                for entry in sorted(src_drafts.iterdir(), key=lambda p: p.name):
                    if entry.name.startswith(".") or entry.suffix != ".json":
                        continue
                    draft = _read_draft_file(entry, limits=limits)
                    if draft is None:
                        continue
                    draft_count += 1
                    if draft_count > limits.max_drafts:
                        raise DutyRefused(
                            "Private Desk snapshot exceeds its admitted bounds"
                        )
                    fd = os.open(entry, os.O_RDONLY | _O_NOFOLLOW)
                    try:
                        fst = os.fstat(fd)
                        if not stat.S_ISREG(fst.st_mode) or fst.st_nlink != 1:
                            raise DutyRefused(
                                "Private Desk draft failed regular-file validation"
                            )
                        data = os.read(fd, limits.max_content_bytes * 8 + 4096)
                    finally:
                        os.close(fd)
                    total_bytes += len(data)
                    if total_bytes > limits.max_total_bytes:
                        raise DutyRefused(
                            "Private Desk total draft capacity is exhausted"
                        )
                    _write_file_atomic(target_drafts, entry.name, data)
                    _write_file_atomic(
                        target_drafts,
                        f"{draft.name}.md",
                        draft.content.encode("utf-8"),
                    )

            dst_digest = compute_agent_desk_digest(target_store, aid)
            if dst_digest != src_digest:
                raise DutyRefused(f"Cloned Desk digest mismatch for Agent '{aid}'")
            cloned[aid] = dst_digest
    return cloned
