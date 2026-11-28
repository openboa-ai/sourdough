"""Typed Core Company OS <-> Domain Module extension contracts (9 seams)."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import pkgutil
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ..writer_lease import WriterLease
from .registry import DutyDescriptor, RuntimeRefused

if TYPE_CHECKING:
    from ..agent_runtime.protocol import AgentProfileBinding, ToolHandler, ToolSpec
    from ..agent_runtime.registry import AgentAdapterRegistry
    from ..board_control import AuthorizeBoard, BoardCommandOutcome
    from .shift_runner import ShiftRunner

    ProfileBindingLookup = (
        Mapping[str, AgentProfileBinding] | Callable[[str], AgentProfileBinding]
    )
else:
    ProfileBindingLookup = Any


class DomainActionRefused(Exception):
    """A domain-registered Board decision action handler refused to apply."""


class DomainCliRefused(Exception):
    """A domain-registered CLI command handler refused execution."""


# ---------------------------------------------------------------------------
# Seam 2: ToolContributor (OuterToolBroker)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolQueryContext:
    store: Path
    company_id: str
    writer: WriterLease
    agent_id: str
    duty_kind: str
    grant_scope: str
    installed_domains: frozenset[str]


@dataclass(frozen=True)
class ToolContribution:
    specs: tuple[ToolSpec, ...] = ()
    handlers: Mapping[str, ToolHandler] = field(default_factory=dict)
    recoverable_exceptions: tuple[type[BaseException], ...] = ()


@runtime_checkable
class ToolContributor(Protocol):
    @property
    def domain_name(self) -> str: ...

    def contribute_tools(self, ctx: ToolQueryContext) -> ToolContribution: ...


def merge_tool_contributions(
    base_specs: Sequence[ToolSpec],
    base_handlers: Mapping[str, ToolHandler],
    contributions: Sequence[ToolContribution],
) -> tuple[
    tuple[ToolSpec, ...], dict[str, ToolHandler], tuple[type[BaseException], ...]
]:
    specs: list[ToolSpec] = list(base_specs)
    handlers: dict[str, ToolHandler] = dict(base_handlers)
    recoverable: list[type[BaseException]] = []
    seen = {spec.name for spec in specs} | set(handlers)
    for contrib in contributions:
        for spec in contrib.specs:
            if spec.name in seen:
                raise RuntimeRefused(f"Duplicate tool registration: {spec.name}")
            seen.add(spec.name)
            specs.append(spec)
        spec_names = {s.name for s in contrib.specs}
        for name, fn in contrib.handlers.items():
            if name in handlers and name not in spec_names:
                raise RuntimeRefused(f"Duplicate tool handler registration: {name}")
            handlers[name] = fn
        for exc_type in contrib.recoverable_exceptions:
            if exc_type not in recoverable:
                recoverable.append(exc_type)
    return tuple(specs), handlers, tuple(recoverable)


# ---------------------------------------------------------------------------
# Seam 1: DutyRegistry & Services
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DutyRuntimeServices:
    adapter_registry: AgentAdapterRegistry
    profile_bindings: ProfileBindingLookup
    default_budget_ceiling_minor: int
    read_agent_state: Callable[[Path, str, str], tuple[str, str]]
    resolve_profile: Callable[[ProfileBindingLookup, str], AgentProfileBinding]
    read_company_status: Callable[[Path, str], dict[str, Any]]
    contribute_tools: Callable[[ToolQueryContext], ToolContribution]
    shift_runner: ShiftRunner | None = None


@runtime_checkable
class DutyContributor(Protocol):
    @property
    def domain_name(self) -> str: ...

    def contribute_duties(
        self, services: DutyRuntimeServices
    ) -> tuple[DutyDescriptor, ...]: ...


# ---------------------------------------------------------------------------
# Seam 3: DecisionActionRegistry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionActionContext:
    writer: WriterLease
    company_id: str
    question_id: str
    kind: str
    revision: int
    impact: Mapping[str, Any]
    now_ns: int


DecisionActionHandler = Callable[[DecisionActionContext], str]


class DecisionActionRegistry:
    """Registry mapping decision_question impact['action'] to fail-closed handlers."""

    def __init__(self) -> None:
        self._actions: dict[str, DecisionActionHandler] = {}

    def register(self, action: str, handler: DecisionActionHandler) -> None:
        if not isinstance(action, str) or not action.strip():
            raise RuntimeRefused("Decision action name must be nonempty")
        if action in self._actions:
            raise RuntimeRefused(f"Duplicate decision action registration: {action}")
        self._actions[action] = handler

    def get(self, action: str) -> DecisionActionHandler | None:
        return self._actions.get(action)

    def registered_actions(self) -> tuple[str, ...]:
        return tuple(sorted(self._actions))


# ---------------------------------------------------------------------------
# Seam 4: RestoreDomainGuard
# ---------------------------------------------------------------------------


@runtime_checkable
class RestoreDomainGuard(Protocol):
    @property
    def domain_name(self) -> str: ...

    def check_original(self, connection: sqlite3.Connection) -> None: ...

    def reconcile(self, shadow_store: Path) -> None: ...

    def external_scope(self) -> tuple[object, ...] | None: ...


# ---------------------------------------------------------------------------
# Seam 5: CommittedResultPredicate
# ---------------------------------------------------------------------------

CommittedResultPredicate = Callable[[sqlite3.Connection, set[str], str], bool]


# ---------------------------------------------------------------------------
# Seam 6: WorkDomainPolicy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkDomainPolicy:
    domain_name: str
    purpose_scopes: Mapping[str, str] = field(default_factory=dict)
    follow_up_purposes: frozenset[str] = frozenset()
    review_purposes: frozenset[str] = frozenset()
    allowed_transitions: frozenset[tuple[str, str]] = frozenset()
    result_replacement_purposes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class MergedWorkPolicy:
    purpose_scopes: Mapping[str, str]
    follow_up_purposes: frozenset[str]
    review_purposes: frozenset[str]
    allowed_transitions: frozenset[tuple[str, str]]
    result_replacement_purposes: frozenset[str]


def merge_work_policies(*policies: WorkDomainPolicy) -> MergedWorkPolicy:
    scopes: dict[str, str] = {}
    follow_ups: set[str] = set()
    reviews: set[str] = set()
    transitions: set[tuple[str, str]] = set()
    replacements: set[str] = set()
    for policy in policies:
        for purpose, scope in policy.purpose_scopes.items():
            if purpose in scopes and scopes[purpose] != scope:
                raise RuntimeRefused(
                    f"Conflicting scope mapping for duty purpose '{purpose}'"
                )
            scopes[purpose] = scope
        follow_ups.update(policy.follow_up_purposes)
        reviews.update(policy.review_purposes)
        transitions.update(policy.allowed_transitions)
        replacements.update(policy.result_replacement_purposes)
    return MergedWorkPolicy(
        purpose_scopes=scopes,
        follow_up_purposes=frozenset(follow_ups),
        review_purposes=frozenset(reviews),
        allowed_transitions=frozenset(transitions),
        result_replacement_purposes=frozenset(replacements),
    )


# ---------------------------------------------------------------------------
# Seam 7: IndependentProcessGuard & Command Contributions
# ---------------------------------------------------------------------------


@runtime_checkable
class IndependentProcessGuard(Protocol):
    """Verify an external companion store/process is independent from company_store."""

    def require_independent_from(self, company_store: Path) -> None: ...


@dataclass(frozen=True)
class CommandDispatchContext:
    writer: WriterLease
    company_id: str
    command_id: str
    command_type: str
    contract_version: str
    preconditions: Mapping[str, Any]
    payload: Mapping[str, Any]
    authorize_board: AuthorizeBoard | None
    principal_hint: str | None
    peer_pid: int


@dataclass(frozen=True)
class CommandDescriptor:
    command_type: str
    contract_version: str
    execute: Callable[[CommandDispatchContext], BoardCommandOutcome]
    precondition_keys: frozenset[str] | None = None
    requires_domain: str | None = None


# ---------------------------------------------------------------------------
# Seam 8: WorkTimeline, IPC Projections, CLI Contributions & DomainPlugin
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkTimelineContribution:
    decisions_by_duty_id: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    domain_payload: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ProjectionDescriptor:
    name: str
    route_prefix: str
    read: Callable[
        [Path, str, str, Mapping[str, list[str]], AuthorizeBoard | None, str | None],
        tuple[dict[str, Any] | None, int],
    ]
    requires_domain: str | None = None


@dataclass(frozen=True)
class RestrictionClearContext:
    connection: sqlite3.Connection
    company_id: str
    restriction_id: str
    cause_category: str
    scope_ref: str
    engine_directory: Path | None = None
    now_ns: int = 0


RestrictionClearHook = Callable[[RestrictionClearContext], None]


@runtime_checkable
class RestrictionGuard(Protocol):
    def handles_scope(self, scope_ref: str) -> bool: ...

    def validate_apply(
        self,
        db: sqlite3.Connection,
        *,
        company_id: str,
        scope_ref: str,
        cause_category: str,
    ) -> None: ...

    def validate_clear(
        self,
        db: sqlite3.Connection,
        *,
        company_id: str,
        scope_ref: str,
        cause_category: str,
        now_ns: int,
        engine_directory: Path | None = None,
    ) -> None: ...


@runtime_checkable
class DomainPlugin(Protocol):
    @property
    def domain_name(self) -> str: ...

    def work_policy(self) -> WorkDomainPolicy | None: ...

    def contribute_duties(
        self, services: DutyRuntimeServices
    ) -> tuple[DutyDescriptor, ...]: ...

    def contribute_tools(self, ctx: ToolQueryContext) -> ToolContribution: ...

    def contribute_decision_actions(self) -> Mapping[str, DecisionActionHandler]: ...

    def restore_guard(self) -> RestoreDomainGuard | None: ...

    def committed_result_predicate(self) -> CommittedResultPredicate | None: ...

    def restriction_guard(self) -> RestrictionGuard | None: ...

    def restriction_clear_hook(self) -> RestrictionClearHook | None: ...

    def enrich_core_projection(
        self,
        projection_name: str,
        db: sqlite3.Connection,
        company_id: str,
        payload: dict[str, Any],
        *,
        context: Mapping[str, Any] | None = None,
    ) -> None: ...

    def contribute_work_timeline(
        self, connection: sqlite3.Connection, company_id: str
    ) -> WorkTimelineContribution | None: ...

    def contribute_commands(self) -> tuple[CommandDescriptor, ...]: ...

    def contribute_projections(self) -> tuple[ProjectionDescriptor, ...]: ...

    def register_cli_subcommands(
        self, subparsers: argparse._SubParsersAction[argparse.ArgumentParser]
    ) -> None: ...

    def handle_cli_command(self, args: argparse.Namespace) -> int | None: ...

    def validate_decision_evidence(
        self,
        connection: sqlite3.Connection,
        company_id: str,
        agent_id: str,
        decision_at_ns: int,
        evidence: Any,
    ) -> bool | None: ...

    def contribute_connectors(self) -> tuple[Any, ...]: ...


class BaseDomainPlugin:
    """Default no-op implementations for optional DomainPlugin hooks."""

    domain_name: str = ""

    def work_policy(self) -> WorkDomainPolicy | None:
        return None

    def contribute_duties(
        self, services: DutyRuntimeServices
    ) -> tuple[DutyDescriptor, ...]:
        return ()

    def contribute_tools(self, ctx: ToolQueryContext) -> ToolContribution:
        return ToolContribution()

    def contribute_decision_actions(self) -> Mapping[str, DecisionActionHandler]:
        return {}

    def restore_guard(self) -> RestoreDomainGuard | None:
        return None

    def committed_result_predicate(self) -> CommittedResultPredicate | None:
        return None

    def restriction_guard(self) -> RestrictionGuard | None:
        return None

    def restriction_clear_hook(self) -> RestrictionClearHook | None:
        return None

    def enrich_core_projection(
        self,
        projection_name: str,
        db: sqlite3.Connection,
        company_id: str,
        payload: dict[str, Any],
        *,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        return None

    def contribute_work_timeline(
        self, connection: sqlite3.Connection, company_id: str
    ) -> WorkTimelineContribution | None:
        return None

    def contribute_commands(self) -> tuple[CommandDescriptor, ...]:
        return ()

    def contribute_projections(self) -> tuple[ProjectionDescriptor, ...]:
        return ()

    def register_cli_subcommands(
        self, subparsers: argparse._SubParsersAction[argparse.ArgumentParser]
    ) -> None:
        return None

    def handle_cli_command(self, args: argparse.Namespace) -> int | None:
        return None

    def validate_decision_evidence(
        self,
        connection: sqlite3.Connection,
        company_id: str,
        agent_id: str,
        decision_at_ns: int,
        evidence: Any,
    ) -> bool:
        return False

    def contribute_connectors(self) -> tuple[Any, ...]:
        return ()


class DomainPluginRegistry:
    """Validated registry of active DomainPlugin instances at a composition root."""

    def __init__(self, plugins: Sequence[DomainPlugin] = ()) -> None:
        self._plugins: dict[str, DomainPlugin] = {}
        for plugin in plugins:
            self.register(plugin)

    def register(self, plugin: DomainPlugin) -> None:
        raw_name = getattr(plugin, "domain_name", None)
        if not isinstance(raw_name, str):
            raise RuntimeRefused(f"Invalid domain plugin name: {raw_name!r}")
        name = raw_name.strip()
        if not name or name == "core":
            raise RuntimeRefused(f"Invalid domain plugin name: {raw_name!r}")
        if name in self._plugins:
            raise RuntimeRefused(f"Duplicate domain plugin registration: {name}")
        self._plugins[name] = plugin

    @property
    def plugins(self) -> tuple[DomainPlugin, ...]:
        return tuple(self._plugins.values())

    def get(self, domain_name: str) -> DomainPlugin | None:
        return self._plugins.get(domain_name)

    def restore_guards(self) -> dict[str, RestoreDomainGuard]:
        guards: dict[str, RestoreDomainGuard] = {}
        for plugin in self._plugins.values():
            guard = plugin.restore_guard()
            if guard is not None:
                guards[guard.domain_name] = guard
        return guards

    def committed_predicates(self) -> tuple[CommittedResultPredicate, ...]:
        preds: list[CommittedResultPredicate] = []
        for plugin in self._plugins.values():
            pred = plugin.committed_result_predicate()
            if pred is not None:
                preds.append(pred)
        return tuple(preds)

    def restriction_guards(self) -> tuple[RestrictionGuard, ...]:
        guards: list[RestrictionGuard] = []
        for plugin in self._plugins.values():
            guard = plugin.restriction_guard()
            if guard is not None:
                guards.append(guard)
        return tuple(guards)

    def restriction_clear_hooks(self) -> tuple[RestrictionClearHook, ...]:
        hooks: list[RestrictionClearHook] = []
        for plugin in self._plugins.values():
            hook = plugin.restriction_clear_hook()
            if hook is not None:
                hooks.append(hook)
        return tuple(hooks)

    def connector_adapters(self) -> tuple[Any, ...]:
        """Collect Seam #9 domain-contributed ConnectorAdapter instances."""
        adapters: list[Any] = []
        for plugin in self._plugins.values():
            adapters.extend(plugin.contribute_connectors())
        return tuple(adapters)


def load_domain_plugins(
    specs: Sequence[str] | None = None,
) -> tuple[DomainPlugin, ...]:
    """Load DomainPlugin instances via entry points or subpackage discovery without static Core imports."""
    loaded: list[DomainPlugin] = []
    seen_names: set[str] = set()

    def _add(obj: Any) -> None:
        plugin = obj() if callable(obj) else obj
        name = getattr(plugin, "domain_name", "")
        if not isinstance(name, str) or not name.strip() or name in seen_names:
            return
        seen_names.add(name)
        loaded.append(plugin)

    if specs is not None:
        for spec in specs:
            if not isinstance(spec, str) or ":" not in spec:
                raise RuntimeRefused(f"Invalid domain plugin spec: {spec!r}")
            module_path, attr_name = spec.split(":", 1)
            if not module_path.strip() or not attr_name.strip():
                raise RuntimeRefused(f"Invalid domain plugin spec: {spec!r}")
            module = importlib.import_module(module_path)
            if not hasattr(module, attr_name):
                raise RuntimeRefused(
                    f"Domain plugin spec attribute not found: {spec!r}"
                )
            _add(getattr(module, attr_name))
        return tuple(loaded)

    for ep in importlib.metadata.entry_points(group="sourdough.domains"):
        _add(ep.load())

    root_pkg = importlib.import_module("sourdough")
    pkg_paths = getattr(root_pkg, "__path__", None)
    if pkg_paths is not None:
        for info in sorted(
            pkgutil.iter_modules(pkg_paths, prefix="sourdough."),
            key=lambda item: item.name,
        ):
            if not info.ispkg:
                continue
            plugin_mod_name = f"{info.name}.plugin"
            try:
                plugin_mod = importlib.import_module(plugin_mod_name)
            except ModuleNotFoundError as exc:
                if exc.name == plugin_mod_name:
                    continue
                raise
            factory = getattr(plugin_mod, "create_domain_plugin", None)
            if factory is not None:
                _add(factory)
    return tuple(loaded)
