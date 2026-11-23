"""Domain table ownership registry and static cross-domain SQL write checker."""

import ast
import re
from pathlib import Path

_SQL_MUTATION_RE = re.compile(
    r"\b(?:"
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+\"?([a-z][a-z0-9_]*)\"?(?:\s*\(|\s+VALUES\b|\s+SELECT\b|\s+DEFAULT\b)"
    r"|UPDATE(?:\s+OR\s+\w+)?\s+\"?([a-z][a-z0-9_]*)\"?\s+SET\b"
    r"|DELETE\s+FROM\s+\"?([a-z][a-z0-9_]*)\"?"
    r"|CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+\"?([a-z][a-z0-9_]*)\"?(?:\s*\(|\s+AS\b)"
    r"|ALTER\s+TABLE\s+\"?([a-z][a-z0-9_]*)\"?\s+(?:ADD|RENAME|DROP)\b"
    r"|DROP\s+TABLE(?:\s+IF\s+EXISTS)?\s+\"?([a-z][a-z0-9_]*)\"?"
    r")",
    re.IGNORECASE,
)

# Maps each authoritative SQLite table to the set of module paths (relative to
# src/sourdough/) authorized to execute SQL mutations against it.
DOMAIN_TABLE_OWNERS: dict[str, frozenset[str]] = {
    # Storage & kernel tables
    "schema_migrations": frozenset({"storage/migrations.py"}),
    "audit_event": frozenset({"storage/kernel.py", "storage/migrations.py"}),
    "command_receipt": frozenset({"storage/kernel.py", "storage/migrations.py"}),
    "external_actions": frozenset({"storage/kernel.py", "storage/migrations.py"}),
    "company_restore_receipt": frozenset(
        {"storage/checkpoint.py", "storage/migrations.py"}
    ),
    "company_store_retired": frozenset(
        {"storage/checkpoint.py", "storage/migrations.py"}
    ),
    # Core company & staffing tables
    "company": frozenset(
        {"company.py", "storage/migrations.py", "storage/checkpoint.py"}
    ),
    "founder": frozenset({"company.py"}),
    "agent": frozenset({"company.py", "staffing.py"}),
    "agent_next": frozenset({"company.py"}),
    "standing_grant": frozenset({"company.py", "staffing.py"}),
    "founding_receipt": frozenset({"company.py"}),
    "staffing_authority": frozenset({"staffing.py"}),
    "staffing_read": frozenset({"staffing.py"}),
    "staffing_choice": frozenset({"staffing.py"}),
    "hire_receipt": frozenset({"staffing.py"}),
    "hire_admission_run": frozenset({"staffing.py"}),
    "staffing_follow_up": frozenset({"staffing.py"}),
    "staffing_assignment": frozenset({"staffing.py"}),
    "hire_onboarding": frozenset({"onboarding.py"}),
    "onboarding_read": frozenset({"onboarding.py"}),
    "onboarding_acceptance": frozenset({"onboarding.py"}),
    "private_desk": frozenset({"onboarding.py"}),
    # Work & assessment tables
    "duty": frozenset(
        {
            "company.py",
            "duty_run.py",
            "first_duty.py",
            "work.py",
            "abandoned_work.py",
            "onboarding.py",
            "staffing.py",
            "paper_research.py",
            "company_assessment.py",
        }
    ),
    "duty_next": frozenset({"company.py"}),
    "duty_run": frozenset(
        {
            "company.py",
            "duty_run.py",
            "first_duty.py",
            "abandoned_work.py",
            "onboarding.py",
            "staffing.py",
        }
    ),
    "duty_run_next": frozenset({"company.py"}),
    "first_duty_claim": frozenset({"company.py"}),
    "work_follow_up": frozenset({"company.py", "work.py", "abandoned_work.py"}),
    "follow_up_next": frozenset({"company.py"}),
    "work_result_read": frozenset({"company.py", "work.py"}),
    "work_result_read_next": frozenset({"company.py"}),
    "work_review_decision": frozenset({"work.py"}),
    "assessment_source": frozenset({"company_assessment.py"}),
    "assessment_read": frozenset({"company_assessment.py"}),
    "assessment_publication": frozenset({"company_assessment.py"}),
    # Quant profile tables (company store)
    "paper_binding": frozenset({"paper_account.py"}),
    "paper_binding_change": frozenset({"paper_account.py"}),
    "paper_order": frozenset({"paper_order.py", "paper_books.py"}),
    "paper_fill": frozenset({"paper_books.py"}),
    "paper_fill_next": frozenset({"paper_books.py"}),
    "paper_fill_fee": frozenset({"paper_books.py"}),
    "paper_cancellation": frozenset({"paper_reconcile.py", "paper_books.py"}),
    "paper_reconciliation": frozenset({"paper_reconcile.py"}),
    "paper_position_observation": frozenset({"paper_decision.py", "paper_books.py"}),
    "paper_decision": frozenset({"paper_decision.py"}),
    "paper_review_quote": frozenset({"paper_decision.py", "paper_review.py"}),
    "paper_research_plan": frozenset({"paper_research.py"}),
    "paper_research_plan_legacy": frozenset({"paper_research.py"}),
    "paper_research_snapshot": frozenset({"paper_research.py"}),
    "paper_research_finding": frozenset({"paper_research.py"}),
    "paper_research_decision": frozenset({"paper_research.py"}),
    "hired_research_order_request": frozenset({"paper_research.py"}),
    "replay_decision_cut": frozenset({"paper_research.py", "nautilus_paper.py"}),
    # Separate venue / market SQLite databases (outside company store)
    "venue_account": frozenset({"paper_venue.py"}),
    "venue_execution": frozenset({"paper_venue.py"}),
    "venue_fee_policy": frozenset({"paper_venue.py"}),
    "venue_fill_fee": frozenset({"paper_venue.py"}),
    "spot_instrument_terms": frozenset(
        {"paper_venue.py", "nautilus_paper.py", "market_worker.py"}
    ),
    "market_scope": frozenset({"market_worker.py"}),
    "market_trade": frozenset({"market_worker.py"}),
    "sandbox_identity": frozenset({"nautilus_paper.py"}),
    "sandbox_config": frozenset({"nautilus_paper.py"}),
    "sandbox_activation": frozenset({"nautilus_paper.py"}),
    "sandbox_quote": frozenset({"nautilus_paper.py"}),
    "sandbox_order": frozenset({"nautilus_paper.py"}),
    "sandbox_fill_observation": frozenset({"nautilus_paper.py"}),
    "sandbox_cancellation": frozenset({"nautilus_paper.py"}),
    "sandbox_account_observation": frozenset({"nautilus_paper.py"}),
}


def _extract_mutated_tables(source: str) -> set[str]:
    tree = ast.parse(source)
    tables: set[str] = set()
    for node in ast.walk(tree):
        text: str | None = None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
        if text is None:
            continue
        for match in _SQL_MUTATION_RE.finditer(text):
            name = next(group for group in match.groups() if group is not None)
            tables.add(name.lower())
    return tables


def audit_domain_write_ownership(src_root: Path) -> list[str]:
    """Return a list of unauthorized or unregistered SQL table write violations."""
    violations: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        rel = path.relative_to(src_root).as_posix()
        if rel == "storage/ownership.py":
            continue
        mutated = _extract_mutated_tables(path.read_text(encoding="utf-8"))
        for table in sorted(mutated):
            owners = DOMAIN_TABLE_OWNERS.get(table)
            if owners is None:
                violations.append(f"{rel}: mutates unregistered table '{table}'")
            elif rel not in owners:
                violations.append(
                    f"{rel}: unauthorized mutation of table '{table}' (owners: {sorted(owners)})"
                )
    return violations
