"""Prompt-injection screening, private Desk redaction, and source reference verification."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from time import time_ns
from typing import Any

from ..agent_desk import desk_directory
from ..company import _identity
from ..duty_run import ExecutionClaim, employment_cutoff
from ..storage.db import open_writer, write_tx
from ..storage.kernel import append_audit_event
from ..writer_lease import WriterLease
from .models import (
    _INJECTION_PATTERNS,
    _PRIVATE_DESK_PATH_RE,
    _SECRET_TOKEN_RE,
    MethodRefused,
    RedactionResult,
    _require_nonempty_str,
    _require_safe_id,
    _sha256_json,
    _validate_claim,
)


def _iter_strings(value: Any) -> list[str]:
    strings: list[str] = []
    if isinstance(value, str):
        strings.append(value)
    elif isinstance(value, Mapping):
        for k, v in value.items():
            if isinstance(k, str):
                strings.append(k)
            else:
                strings.extend(_iter_strings(k))
            strings.extend(_iter_strings(v))
    elif isinstance(value, (Sequence, set, frozenset)) and not isinstance(
        value, (bytes, bytearray)
    ):
        for item in value:
            strings.extend(_iter_strings(item))
    return strings


def _check_no_prompt_injection(text: str, *, label: str) -> None:
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            raise MethodRefused(
                f"Prompt-injection or authority-override content rejected in {label}"
            )


def _find_unredacted_shared_leaks(text: str) -> list[str]:
    findings: list[str] = []
    for match in _PRIVATE_DESK_PATH_RE.finditer(text):
        findings.append(f"private_path:{match.group(0)}")
    for match in _SECRET_TOKEN_RE.finditer(text):
        findings.append("secret_token")
    return findings


def _opaque_private_ref(kind: str, agent_id: str, draft_stem: str) -> str:
    """Return a durable opaque digest reference so private Desk filenames never enter company.sqlite."""
    digest = sha256(f"{agent_id}:{draft_stem}".encode("utf-8")).hexdigest()[:24]
    return f"{kind}:opaque:{digest}"


def _opaque_missing_ref(kind: str, company_id: str, agent_id: str, ident: str) -> str:
    """Return a durable opaque digest for an unverified/missing source_ref so arbitrary text never enters SQLite."""
    digest = sha256(
        f"sourdough:missing-ref:v1\x1f{company_id}\x1f{agent_id}\x1f{kind}\x1f{ident}".encode(
            "utf-8"
        )
    ).hexdigest()[:24]
    return f"{kind}:missing:{digest}"


def _parse_private_desk_ref(ident: str, *, agent_id: str, ref: str) -> str:
    if "/" in ident:
        owner, draft_name = ident.split("/", 1)
    else:
        owner, draft_name = agent_id, ident
    _require_safe_id(owner, "private Desk owner")
    if owner != agent_id:
        raise MethodRefused(f"Cannot cite another Agent's private Desk resource: {ref}")
    _require_safe_id(draft_name, "private Desk draft name")
    draft_stem = draft_name.removesuffix(".json").removesuffix(".md")
    _require_safe_id(draft_stem, "private Desk draft name")
    return draft_stem


def _verify_source_refs(
    db: sqlite3.Connection,
    *,
    store_path: Path | None,
    company_id: str,
    agent_id: str,
    audience_scope: str,
    audience_target_id: str | None,
    source_refs: Sequence[str],
    allow_private_refs: bool,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Validate access to all cited source_refs; return (normalized_refs, missing_refs)."""
    if (
        isinstance(source_refs, (str, bytes))
        or not isinstance(source_refs, Sequence)
        or not source_refs
    ):
        raise MethodRefused(
            "source_refs must be a nonempty sequence of source citations"
        )

    normalized: list[str] = []
    missing: list[str] = []
    for raw_ref in source_refs:
        if not isinstance(raw_ref, str) or ":" not in raw_ref or not raw_ref.strip():
            raise MethodRefused(f"Invalid source reference format: {raw_ref!r}")
        ref = raw_ref.strip()
        kind, ident = ref.split(":", 1)
        if not ident.strip():
            raise MethodRefused(f"Invalid source reference identifier: {raw_ref!r}")

        if kind in ("private_desk", "private_draft"):
            draft_stem = _parse_private_desk_ref(ident, agent_id=agent_id, ref=ref)
            if not allow_private_refs:
                raise MethodRefused(
                    f"Private Desk reference '{ref}' must be redacted before entering '{audience_scope}' scope"
                )
            opaque_ref = _opaque_private_ref(kind, agent_id, draft_stem)
            if store_path is not None:
                draft_file = (
                    desk_directory(store_path, agent_id)
                    / ".desk"
                    / "drafts"
                    / f"{draft_stem}.json"
                )
                if draft_file.is_symlink() or not draft_file.is_file():
                    missing.append(opaque_ref)
            ref = opaque_ref

        elif kind == "shared_publication":
            asset_id, *rev_parts = ident.split("@", 1)
            pub_row = db.execute(
                """SELECT company_id, project_id, audience, content
                   FROM shared_publication
                   WHERE asset_id = ?""",
                (asset_id,),
            ).fetchone()
            if pub_row is None:
                ref = _opaque_missing_ref(kind, company_id, agent_id, ident)
                missing.append(ref)
            else:
                pub_co, pub_proj, aud_json, content = pub_row
                if str(pub_co) != company_id:
                    raise MethodRefused(
                        "Cited shared_publication belongs to another company"
                    )
                try:
                    audience = set(json.loads(str(aud_json)))
                except json.JSONDecodeError:
                    audience = set()
                proj_row = db.execute(
                    "SELECT members FROM shared_project WHERE company_id = ? AND project_id = ?",
                    (company_id, str(pub_proj)),
                ).fetchone()
                try:
                    members = set(json.loads(str(proj_row[0]))) if proj_row else set()
                except json.JSONDecodeError:
                    members = set()
                if agent_id not in audience or agent_id not in members:
                    raise MethodRefused(
                        f"Agent '{agent_id}' is not authorized to cite shared_publication '{asset_id}'"
                    )
                if audience_scope == "project":
                    if (
                        audience_target_id is not None
                        and str(pub_proj) != audience_target_id
                    ):
                        raise MethodRefused(
                            f"Cited shared_publication '{asset_id}' belongs to another project"
                        )
                    active_proj_members = {
                        m for m in members if employment_cutoff(db, m) is None
                    }
                    if not active_proj_members.issubset(audience):
                        raise MethodRefused(
                            f"Cited shared_publication '{asset_id}' is restricted to a subset "
                            f"of project '{pub_proj}' members and cannot be shared across the project"
                        )
                elif audience_scope == "company":
                    active_company_agents = {
                        str(r[0])
                        for r in db.execute(
                            "SELECT id FROM agent WHERE company_id = ?", (company_id,)
                        ).fetchall()
                        if employment_cutoff(db, str(r[0])) is None
                    }
                    if not (
                        active_company_agents.issubset(audience)
                        and active_company_agents.issubset(members)
                    ):
                        raise MethodRefused(
                            f"Cited shared_publication '{asset_id}' is not readable by all active "
                            "company agents and cannot be distilled into a company-scoped method"
                        )
                if rev_parts:
                    expected_rev = sha256(str(content).encode("utf-8")).hexdigest()
                    if rev_parts[0] != expected_rev:
                        ref = _opaque_missing_ref(kind, company_id, agent_id, ident)
                        missing.append(ref)

        elif kind == "work_run":
            run_row = db.execute(
                "SELECT company_id, agent_id FROM work_runs WHERE run_id = ?",
                (ident,),
            ).fetchone()
            if run_row is None:
                ref = _opaque_missing_ref(kind, company_id, agent_id, ident)
                missing.append(ref)
            elif str(run_row[0]) != company_id or str(run_row[1]) != agent_id:
                raise MethodRefused(
                    f"Agent '{agent_id}' is not authorized to cite work_run '{ident}'"
                )

        elif kind == "work_decision":
            dec_row = db.execute(
                "SELECT company_id, agent_id FROM work_decision WHERE decision_id = ?",
                (ident,),
            ).fetchone()
            if dec_row is None:
                ref = _opaque_missing_ref(kind, company_id, agent_id, ident)
                missing.append(ref)
            elif str(dec_row[0]) != company_id or str(dec_row[1]) != agent_id:
                raise MethodRefused(
                    f"Agent '{agent_id}' is not authorized to cite work_decision '{ident}'"
                )

        elif kind == "tool_call_receipt":
            rec_row = db.execute(
                """SELECT 1 FROM tool_call_receipt
                   WHERE company_id = ? AND agent_id = ? AND call_id = ?
                   LIMIT 1""",
                (company_id, agent_id, ident),
            ).fetchone()
            if rec_row is None:
                any_row = db.execute(
                    """SELECT 1 FROM tool_call_receipt
                       WHERE company_id = ? AND call_id = ?
                       LIMIT 1""",
                    (company_id, ident),
                ).fetchone()
                if any_row is None:
                    ref = _opaque_missing_ref(kind, company_id, agent_id, ident)
                    missing.append(ref)
                else:
                    raise MethodRefused(
                        f"Agent '{agent_id}' is not authorized to cite tool_call_receipt '{ident}'"
                    )

        elif kind == "agent_episode":
            ep_row = db.execute(
                "SELECT company_id, agent_id FROM agent_episode WHERE episode_id = ?",
                (ident,),
            ).fetchone()
            if ep_row is None:
                ref = _opaque_missing_ref(kind, company_id, agent_id, ident)
                missing.append(ref)
            elif str(ep_row[0]) != company_id or str(ep_row[1]) != agent_id:
                raise MethodRefused(
                    f"Agent '{agent_id}' is not authorized to cite agent_episode '{ident}'"
                )

        elif kind == "peer_request":
            pr_row = db.execute(
                """SELECT company_id, requester_agent_id, recipient_agent_id
                   FROM work_peer_requests WHERE request_id = ?""",
                (ident,),
            ).fetchone()
            if pr_row is None:
                ref = _opaque_missing_ref(kind, company_id, agent_id, ident)
                missing.append(ref)
            elif str(pr_row[0]) != company_id or agent_id not in (
                str(pr_row[1]),
                str(pr_row[2]),
            ):
                raise MethodRefused(
                    f"Agent '{agent_id}' is not authorized to cite peer_request '{ident}'"
                )

        else:
            raise MethodRefused(f"Unsupported source reference kind: {kind!r}")

        normalized.append(ref)

    return tuple(normalized), tuple(missing)


def redact_for_shared_scope(
    writer_or_conn: WriterLease | sqlite3.Connection,
    claim: ExecutionClaim,
    *,
    raw_body: str,
    source_refs: Sequence[str],
    target_scope: str,
    now_ns: int | None = None,
) -> RedactionResult:
    """Screen candidate content for injection/secrets/private Desk paths before shared evaluation."""
    if target_scope not in ("project", "company"):
        raise MethodRefused(
            f"redact_for_shared_scope targets 'project' or 'company' (got {target_scope!r})"
        )
    body = _require_nonempty_str(raw_body, "raw_body")
    _check_no_prompt_injection(body, label="raw_body")

    def _run(db: sqlite3.Connection, store_path: Path | None) -> RedactionResult:
        company_id = _identity(db)
        _validate_claim(db, claim, company_id=company_id)

        if (
            isinstance(source_refs, (str, bytes))
            or not isinstance(source_refs, Sequence)
            or not source_refs
        ):
            raise MethodRefused("source_refs must be a nonempty sequence")

        stripped_items: list[str] = []
        retained_refs: list[str] = []
        for raw_ref in source_refs:
            if (
                not isinstance(raw_ref, str)
                or ":" not in raw_ref
                or not raw_ref.strip()
            ):
                raise MethodRefused(f"Invalid source reference: {raw_ref!r}")
            ref = raw_ref.strip()
            kind, ident = ref.split(":", 1)
            if kind in ("private_desk", "private_draft"):
                if ident.startswith("opaque:"):
                    stripped_items.append(f"source_ref:{ref}")
                else:
                    draft_stem = _parse_private_desk_ref(
                        ident, agent_id=claim.founder_id, ref=ref
                    )
                    stripped_items.append(
                        f"source_ref:{_opaque_private_ref(kind, claim.founder_id, draft_stem)}"
                    )
            else:
                retained_refs.append(ref)

        if not retained_refs:
            raise MethodRefused(
                "No authorized shared/verifiable source references remain after redaction"
            )

        verified_refs, missing_refs = _verify_source_refs(
            db,
            store_path=store_path,
            company_id=company_id,
            agent_id=claim.founder_id,
            audience_scope=target_scope,
            audience_target_id=None,
            source_refs=retained_refs,
            allow_private_refs=False,
        )
        if missing_refs:
            raise MethodRefused(
                f"Shared redaction requires verifiable non-missing source_refs (missing: {missing_refs})"
            )

        def _strip_private(match: re.Match[str]) -> str:
            path_digest = sha256(match.group(0).encode("utf-8")).hexdigest()[:16]
            stripped_items.append(f"body_path:opaque:{path_digest}")
            return "[redacted-private-ref]"

        def _strip_secret(match: re.Match[str]) -> str:
            stripped_items.append("body_secret")
            return "[redacted-secret]"

        cleaned = _PRIVATE_DESK_PATH_RE.sub(_strip_private, body)
        cleaned = _SECRET_TOKEN_RE.sub(_strip_secret, cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

        meaningful = re.sub(
            r"\[redacted-(?:private-ref|secret)\]|[\s.,;:!?-]+", "", cleaned
        )
        if not meaningful:
            raise MethodRefused(
                "Redaction left no substantive content safe for shared scope"
            )

        ts = time_ns() if now_ns is None else int(now_ns)
        receipt_id = (
            "redact-"
            + _sha256_json(
                {
                    "company_id": company_id,
                    "agent_id": claim.founder_id,
                    "target_scope": target_scope,
                    "redacted_body": cleaned,
                    "retained_refs": list(verified_refs),
                    "stripped_items": stripped_items,
                }
            )[:24]
        )
        append_audit_event(
            db,
            company_id=company_id,
            domain="learning",
            entity_type="method_redaction",
            entity_id=receipt_id,
            event_type="learning.redaction_screened",
            actor_id=claim.founder_id,
            run_id=claim.run_id,
            payload={
                "target_scope": target_scope,
                "stripped_count": len(stripped_items),
                "retained_refs_count": len(verified_refs),
            },
            recorded_at_ns=ts,
        )
        return RedactionResult(
            redacted_body=cleaned,
            redaction_receipt_id=receipt_id,
            stripped_items=tuple(stripped_items),
            retained_source_refs=verified_refs,
        )

    if isinstance(writer_or_conn, WriterLease):
        writer_or_conn.verify()
        with (
            closing(open_writer(writer_or_conn.store, lease=writer_or_conn)) as db,
            write_tx(db),
        ):
            res = _run(db, writer_or_conn.store)
            writer_or_conn.verify()
            return res
    if isinstance(writer_or_conn, sqlite3.Connection):
        return _run(writer_or_conn, None)
    raise MethodRefused("Expected WriterLease or sqlite3.Connection")
