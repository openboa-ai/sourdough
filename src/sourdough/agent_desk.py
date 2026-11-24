"""Own private draft custody physically isolated under each Agent's Desk directory."""

import json
import os
import re
import stat
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from sqlite3 import Connection
from uuid import uuid4

from .company import _connect
from .duty_run import DutyClaim, DutyRefused, resolve_run
from .writer_lease import WriterLease


_NAME = re.compile(r"[a-z][a-z0-9-]{0,47}")
_AGENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_MAX_DRAFTS = 6
_MAX_CONTENT_BYTES = 800
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


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


def _read_draft_file(path: Path) -> Draft | None:
    if not os.path.lexists(path):
        return None
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise DutyRefused("Private Desk draft must be a regular file")
    fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    try:
        raw = os.read(fd, _MAX_CONTENT_BYTES * 8 + 4096)
    finally:
        os.close(fd)
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
        or len(content.encode("utf-8")) > _MAX_CONTENT_BYTES
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


def require_desk(database: Connection, claim: DutyClaim, purpose: str) -> str:
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
    store: Path, agent_id: str, name: str, revision: str
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
    draft = _read_draft_file(draft_path)
    if draft is None or draft.revision != revision or draft.name != name:
        return None
    return draft


def resolve_desk(
    writer: WriterLease, claim: DutyClaim, purpose: str, *, include_drafts: bool = False
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
                loaded.append(_read_draft_file(entry))
                if len(loaded) > _MAX_DRAFTS:
                    raise DutyRefused(
                        "Private Desk snapshot exceeds its admitted bounds"
                    )
            drafts = tuple(sorted(loaded, key=lambda item: item.name))
    return revision, drafts


def save_draft(
    writer: WriterLease,
    claim: DutyClaim,
    purpose: str,
    *,
    name: str,
    content: str,
    expected_revision: str | None,
) -> Draft:
    """Persist an authorized private draft on disk outside company.sqlite."""
    if (
        not isinstance(name, str)
        or not _NAME.fullmatch(name)
        or not isinstance(content, str)
        or not 0 < len(content.encode("utf-8")) <= _MAX_CONTENT_BYTES
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
    previous = _read_draft_file(draft_path) if os.path.lexists(draft_path) else None
    if previous is not None and (previous.revision, previous.content) == (
        revision,
        content,
    ):
        return draft
    if (previous.revision if previous is not None else None) != expected_revision:
        raise DutyRefused("Private draft revision changed")
    existing_count = sum(
        1
        for entry in drafts_dir.iterdir()
        if not entry.name.startswith(".") and entry.suffix == ".json"
    )
    if previous is None and existing_count >= _MAX_DRAFTS:
        raise DutyRefused("Private Desk draft capacity is exhausted")
    encoded = json.dumps(
        {"name": name, "revision": revision, "content": content},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    _write_file_atomic(drafts_dir, f"{name}.json", encoded)
    _write_file_atomic(drafts_dir, f"{name}.md", content.encode("utf-8"))
    writer.verify()
    return draft
