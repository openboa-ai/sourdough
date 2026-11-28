"""Domain table ownership registry and static cross-domain SQL write checker."""

import ast
import re
from pathlib import Path

_SQL_MUTATION_RE = re.compile(
    r"\b(?:"
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+\"?([a-z][a-z0-9_]*)\"?(?:\s*\(|\s+VALUES\b|\s+SELECT\b|\s+DEFAULT\b)"
    r"|UPDATE(?:\s+OR\s+\w+)?\s+\"?([a-z][a-z0-9_]*)\"?\s+SET\b"
    r"|DELETE\s+FROM\s+\"?([a-z][a-z0-9_]*)\"?"
    r"|CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+(?!IF\b)\"?([a-z][a-z0-9_]*)\"?(?:\s*\(|\s+AS\b)"
    r"|ALTER\s+TABLE\s+\"?([a-z][a-z0-9_]*)\"?\s+(?:ADD|RENAME|DROP)\b"
    r"|DROP\s+TABLE(?:\s+IF\s+EXISTS)?\s+(?!IF\b)\"?([a-z][a-z0-9_]*)\"?"
    r")",
    re.IGNORECASE,
)

_SQL_DDL_RE = re.compile(
    r"\b(?:"
    r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+(?!IF\b)\"?([a-z][a-z0-9_]*)\"?(?:\s*\(|\s+AS\b)"
    r"|ALTER\s+TABLE\s+\"?([a-z][a-z0-9_]*)\"?\s+(?:ADD|RENAME|DROP)\b"
    r"|DROP\s+TABLE(?:\s+IF\s+EXISTS)?\s+(?!IF\b)\"?([a-z][a-z0-9_]*)\"?"
    r")",
    re.IGNORECASE,
)

# Modules that manage physically separate SQLite databases outside company.sqlite.
SEPARATE_DB_MODULES: frozenset[str] = frozenset(
    {
        "trading/venue.py",
        "trading/market_worker.py",
        "trading/nautilus_paper.py",
        "trial_registry.py",
        "episode_index.py",
    }
)

# Dedicated schema migration modules permitted to execute transactional table rebuild DDL.
SCHEMA_MIGRATION_MODULES: frozenset[str] = frozenset({"storage/migration.py"})

# Maps each authoritative SQLite table to the set of module paths (relative to
# src/sourdough/) authorized to execute SQL mutations against it.
DOMAIN_TABLE_OWNERS: dict[str, frozenset[str]] = {
    # Storage & kernel tables
    "installed_domains": frozenset({"storage/schema.py", "storage/migration.py"}),
    "audit_event": frozenset({"storage/kernel.py"}),
    "command_receipt": frozenset({"storage/kernel.py"}),
    "external_actions": frozenset({"storage/kernel.py"}),
    "company_restore_receipt": frozenset({"storage/checkpoint.py"}),
    "company_store_retired": frozenset({"storage/checkpoint.py"}),
    # Three-entity Work kernel tables
    "work_items": frozenset(
        {
            "runtime/kernel/items.py",
            "runtime/kernel/peer_auth.py",
            "runtime/kernel/conditions.py",
            "runtime/kernel/runs.py",
        }
    ),
    "work_runs": frozenset({"runtime/kernel/items.py", "runtime/kernel/runs.py"}),
    "work_responsibilities": frozenset(
        {"runtime/kernel/responsibilities.py", "runtime/kernel/runs.py"}
    ),
    "work_conditions": frozenset(
        {
            "runtime/kernel/responsibilities.py",
            "runtime/kernel/items.py",
            "runtime/kernel/conditions.py",
            "runtime/kernel/runs.py",
        }
    ),
    "work_condition_occurrences": frozenset({"runtime/kernel/conditions.py"}),
    "work_peer_requests": frozenset(
        {
            "runtime/kernel/peer_auth.py",
            "runtime/kernel/items.py",
            "runtime/kernel/runs.py",
            "runtime/kernel/peer_requests.py",
        }
    ),
    "work_decision": frozenset({"runtime/kernel/decisions.py"}),
    "work_event_cursor": frozenset({"runtime/kernel/conditions.py"}),
    "work_discovery_state": frozenset({"runtime/kernel/responsibilities.py"}),
    # Core company & staffing tables
    "company": frozenset({"company.py", "storage/checkpoint.py"}),
    "agent": frozenset({"company.py", "staffing.py"}),
    "agent_runtime_binding": frozenset(
        {"company.py", "staffing.py", "board_control.py"}
    ),
    "standing_grant": frozenset({"company.py", "staffing.py", "retirement.py"}),
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
    "private_desk": frozenset({"onboarding.py", "agent_desk.py"}),
    # Collaboration, custody, handoff & retirement tables
    "shared_project": frozenset({"company_assets.py"}),
    "shared_publication": frozenset({"company_assets.py"}),
    "project_review_brief": frozenset({"company_assets.py"}),
    "project_message": frozenset({"conversations.py"}),
    "project_delivery": frozenset({"conversation_delivery.py"}),
    "agent_archive": frozenset({"retirement.py"}),
    "work_handoff": frozenset({"work_handoff.py"}),
    # Work & assessment tables
    "duty": frozenset(
        {
            "company.py",
            "legacy_duty.py",
            "first_duty.py",
            "work.py",
            "abandoned_work.py",
            "onboarding.py",
            "staffing.py",
            "company_assessment.py",
            "retirement.py",
            "storage/migration.py",
            "work_handoff.py",
            "work_peer_response.py",
        }
    ),
    "duty_run": frozenset(
        {
            "legacy_duty.py",
            "first_duty.py",
            "abandoned_work.py",
            "onboarding.py",
            "staffing.py",
            "retirement.py",
            "work_handoff.py",
            "work_peer_response.py",
        }
    ),
    "work_follow_up": frozenset(
        {"work.py", "abandoned_work.py", "storage/migration.py", "work_handoff.py"}
    ),
    "work_result_read": frozenset({"work.py"}),
    "work_review_decision": frozenset({"work.py"}),
    "assessment_source": frozenset({"company_assessment.py"}),
    "assessment_read": frozenset({"company_assessment.py"}),
    "assessment_publication": frozenset({"company_assessment.py"}),
    # Evidence review qualification, activation & method learning tables
    "review_qualification": frozenset({"review_qualification_store.py"}),
    "review_tool_activation": frozenset({"review_tool_activation.py"}),
    "method_delta": frozenset({"learning/lifecycle.py"}),
    "method_qualification": frozenset({"learning/lifecycle.py"}),
    "method_activation": frozenset({"learning/lifecycle.py"}),
    # Agent runtime tool receipts & episodic memory tables
    "tool_call_receipt": frozenset({"runtime/shift/receipts.py"}),
    "agent_episode": frozenset({"memory.py"}),
    # Core scoped connector registry & actor grant tables
    "connector_binding": frozenset({"connectors/registry.py"}),
    "connector_grant": frozenset({"connectors/registry.py"}),
    # Separate derived episode FTS5 search index (outside company store)
    "episodes_fts": frozenset({"episode_index.py"}),
    "episodes_fts_meta": frozenset({"episode_index.py"}),
    # Core general ledger and accounting close tables
    "journal_entry": frozenset({"general_ledger.py"}),
    "journal_line": frozenset({"general_ledger.py"}),
    "accounting_close": frozenset({"accounting_close.py"}),
    # Board Client governance, messaging, restriction & profile tables
    "board_binding": frozenset({"board_control.py"}),
    "ceo_message": frozenset({"board_control.py"}),
    "decision_question": frozenset({"board_control.py", "storage/migration.py"}),
    "restriction_cause": frozenset({"board_control.py", "storage/migration.py"}),
    "agent_profile": frozenset({"board_control.py"}),
    # Canonical opt-in Trading domain tables (company store)
    "execution_environment_binding": frozenset(
        {"trading/accounts.py", "trading/migration.py", "storage/migration.py"}
    ),
    "trading_book_order": frozenset({"trading/ledger.py", "storage/migration.py"}),
    "trading_book_fill": frozenset({"trading/ledger.py", "storage/migration.py"}),
    "trading_book_position": frozenset({"trading/ledger.py", "storage/migration.py"}),
    "trading_book_valuation": frozenset(
        {"trading/ledger.py", "trading/valuation.py", "storage/migration.py"}
    ),
    "venue_qualification_receipt": frozenset(
        {"trading/accounts.py", "storage/migration.py"}
    ),
    "environment_promotion_record": frozenset(
        {"trading/accounts.py", "storage/migration.py"}
    ),
    "trading_live_migration_record": frozenset(
        {"trading/migration.py", "storage/migration.py"}
    ),
    "trading_operating_window": frozenset(
        {"trading/window.py", "storage/migration.py"}
    ),
    "trading_statement": frozenset(
        {"trading/statement_store.py", "storage/migration.py"}
    ),
    "trading_funding_settlement": frozenset(
        {"trading/funding.py", "storage/migration.py"}
    ),
    "trading_fee_delivery": frozenset(
        {"trading/fee_bridge.py", "storage/migration.py"}
    ),
    "trading_pit_catalog_manifest": frozenset(
        {"trading/strategy_comparison.py", "storage/migration.py"}
    ),
    "trading_strategy_release": frozenset(
        {"trading/strategy_comparison.py", "storage/migration.py"}
    ),
    "trading_challenger_trial": frozenset(
        {"trading/strategy_comparison.py", "storage/migration.py"}
    ),
    "trading_strategy_deployment": frozenset(
        {"trading/strategy_comparison.py", "storage/migration.py"}
    ),
    # Separate venue / market SQLite databases (outside company store)
    "replay_decision_cut": frozenset({"trading/nautilus_paper.py"}),
    "venue_account": frozenset({"trading/venue.py"}),
    "venue_execution": frozenset({"trading/venue.py"}),
    "venue_fee_policy": frozenset({"trading/venue.py"}),
    "venue_fill_fee": frozenset({"trading/venue.py"}),
    "spot_instrument_terms": frozenset(
        {
            "trading/venue.py",
            "trading/nautilus_paper.py",
            "trading/market_worker.py",
        }
    ),
    "market_scope": frozenset({"trading/market_worker.py"}),
    "market_trade": frozenset({"trading/market_worker.py"}),
    "sandbox_identity": frozenset({"trading/nautilus_paper.py"}),
    "sandbox_config": frozenset({"trading/nautilus_paper.py"}),
    "sandbox_activation": frozenset({"trading/nautilus_paper.py"}),
    "sandbox_quote": frozenset({"trading/nautilus_paper.py"}),
    "sandbox_order": frozenset({"trading/nautilus_paper.py"}),
    "sandbox_fill_observation": frozenset({"trading/nautilus_paper.py"}),
    "sandbox_fill_time": frozenset({"trading/nautilus_paper.py"}),
    "sandbox_cancellation": frozenset({"trading/nautilus_paper.py"}),
    "sandbox_account_observation": frozenset({"trading/nautilus_paper.py"}),
    # Separate offline evaluator SQLite database (strictly outside company store)
    "trial_suite": frozenset({"trial_registry.py"}),
    "trial_reservation": frozenset({"trial_registry.py"}),
    "trial_observation": frozenset({"trial_registry.py"}),
    "trial_invalidation": frozenset({"trial_registry.py"}),
    "trial_grade": frozenset({"trial_registry.py"}),
    "trial_pairwise_grade": frozenset({"trial_registry.py"}),
}


def _extract_tables(source: str, pattern: re.Pattern[str]) -> set[str]:
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
        for match in pattern.finditer(text):
            name = next(group for group in match.groups() if group is not None)
            tables.add(name.lower())
    return tables


def audit_domain_write_ownership(src_root: Path) -> list[str]:
    """Return unauthorized table mutations or prohibited company-store inline DDL."""
    violations: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        rel = path.relative_to(src_root).as_posix()
        if rel == "storage/ownership.py":
            continue
        source = path.read_text(encoding="utf-8")
        mutated = _extract_tables(source, _SQL_MUTATION_RE)
        for table in sorted(mutated):
            owners = DOMAIN_TABLE_OWNERS.get(table)
            if owners is None:
                violations.append(f"{rel}: mutates unregistered table '{table}'")
            elif rel not in owners:
                violations.append(
                    f"{rel}: unauthorized mutation of table '{table}' (owners: {sorted(owners)})"
                )
        if rel not in SEPARATE_DB_MODULES and rel not in SCHEMA_MIGRATION_MODULES:
            for ddl_table in sorted(_extract_tables(source, _SQL_DDL_RE)):
                violations.append(
                    f"{rel}: inline company-store DDL prohibited for '{ddl_table}' (declare in storage/schema/v1/)"
                )
    return violations
