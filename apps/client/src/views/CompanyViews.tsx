import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { CopyButton } from "@openboa/yogurt/motion-copy-button";
import {
  AgentRow,
  Badge,
  Button,
  Card,
  CodeBlock,
  DataTable,
  DisclosureRow,
  EmptyResult,
  FilterBar,
  Heading,
  Inline,
  List,
  Notice,
  PageHeader,
  RecordFacts,
  Result,
  Select,
  Skeleton,
  Stack,
  Surface,
  Switch,
  Table,
  Tabs,
  Text,
  WorkEvent,
  type DataColumn,
  type DataRow,
  type Fact,
} from "@openboa/yogurt";
import {
  fetchProjection,
  formatMinorCurrency,
  formatNsClock,
  formatRelativeNs,
  newCommandId,
} from "../api/client";
import type {
  AgentsGraphProjection,
  FinanceProjection,
  OrgProjection,
  RestrictionItem,
  RestrictionsProjection,
  StrategiesProjection,
} from "../api/types";
import { ControlledInlineConfirm } from "../components/ControlledInlineConfirm";
import { useApp } from "../state/store";

type SortState = { id: string; descending: boolean } | null;

interface SortableColumn<T> {
  id: string;
  label: string;
  kind?: "identity" | "text" | "numeric" | "role" | "actions";
  align?: "start" | "end";
  sortValue?: (item: T) => string | number | null | undefined;
}

function normalizeToNs(epoch: number | null | undefined): number | null {
  if (!epoch || !Number.isFinite(epoch)) return null;
  if (epoch < 1e12) return epoch * 1_000_000_000;
  if (epoch < 1e15) return epoch * 1_000_000;
  return epoch;
}

function formatNsWithClock(ns: number | null | undefined, emptyLabel = "No reading"): string {
  const normalized = normalizeToNs(ns);
  if (!normalized) return emptyLabel;
  return `${formatRelativeNs(normalized)} · ${formatNsClock(normalized)}`;
}

function sortItems<T>(items: T[], sort: SortState, cols: SortableColumn<T>[]): T[] {
  if (!sort) return items;
  const col = cols.find((c) => c.id === sort.id);
  if (!col?.sortValue) return items;
  const getVal = col.sortValue;
  return [...items].sort((a, b) => {
    const av = getVal(a);
    const bv = getVal(b);
    let cmp = 0;
    if (av === null || av === undefined) {
      cmp = bv === null || bv === undefined ? 0 : 1;
    } else if (bv === null || bv === undefined) {
      cmp = -1;
    } else if (typeof av === "number" && typeof bv === "number") {
      cmp = av - bv;
    } else {
      cmp = String(av).localeCompare(String(bv), undefined, { numeric: true });
    }
    return sort.descending ? -cmp : cmp;
  });
}

function renderSortableTableHead<T>(
  cols: SortableColumn<T>[],
  sort: SortState,
  setSort: (updater: (prev: SortState) => SortState) => void
) {
  return (
    <thead>
      <tr>
        {cols.map((col) => {
          const isNumeric = col.kind === "numeric";
          const isSorted = sort?.id === col.id && Boolean(col.sortValue);
          return (
            <th
              key={col.id}
              scope="col"
              data-kind={col.kind}
              data-align={col.align}
              data-numeric={isNumeric || undefined}
              aria-sort={
                isSorted ? (sort!.descending ? "descending" : "ascending") : undefined
              }
            >
              {col.sortValue ? (
                <button
                  type="button"
                  className="yg-table-sort"
                  onClick={() =>
                    setSort((prev) => ({
                      id: col.id,
                      descending: prev?.id === col.id && !prev.descending,
                    }))
                  }
                >
                  {col.label}
                  <span aria-hidden="true">
                    {isSorted ? (sort!.descending ? "↓" : "↑") : "↕"}
                  </span>
                </button>
              ) : (
                col.label
              )}
            </th>
          );
        })}
      </tr>
    </thead>
  );
}

function formatSharedCost(detail: Record<string, unknown>): string | null {
  const raw = detail.shared_cost_allocated_minor ?? detail.shared_cost_split;
  if (Array.isArray(raw) && raw.length >= 2) {
    const inc = typeof raw[0] === "number" ? formatMinorCurrency(raw[0]) : String(raw[0]);
    const chal = typeof raw[1] === "number" ? formatMinorCurrency(raw[1]) : String(raw[1]);
    return `Shared cost: inc ${inc} / chal ${chal}`;
  }
  if (typeof raw === "number" && Number.isFinite(raw)) {
    return `Shared cost: ${formatMinorCurrency(raw)}`;
  }
  if (raw && typeof raw === "object") {
    return `Shared cost: ${JSON.stringify(raw)}`;
  }
  return null;
}

function formatTrialVerdictMetrics(detail: Record<string, unknown> | null): {
  summary: string[];
  reasons: string[];
} {
  if (!detail) return { summary: [], reasons: [] };
  const summary: string[] = [];
  if (typeof detail.mean_diff_bps === "number") {
    const sign = detail.mean_diff_bps > 0 ? "+" : "";
    summary.push(`Mean diff: ${sign}${detail.mean_diff_bps} bps`);
  }
  if (typeof detail.lower_bound_bps === "number" && typeof detail.upper_bound_bps === "number") {
    summary.push(`CI: [${detail.lower_bound_bps}, ${detail.upper_bound_bps}] bps`);
  }
  if (typeof detail.alpha_effective === "number") {
    summary.push(`alpha: ${detail.alpha_effective}`);
  }
  if (typeof detail.observations_n === "number") {
    summary.push(`N=${detail.observations_n}`);
  }
  const shared = formatSharedCost(detail);
  if (shared) summary.push(shared);

  const reasons = Array.isArray(detail.reasons)
    ? detail.reasons.map((r) => String(r)).filter(Boolean)
    : [];
  return { summary, reasons };
}

function formatIssueLine(issue: Record<string, unknown>, idx: number): string {
  const code = typeof issue.code === "string" ? issue.code : null;
  const reference = typeof issue.reference === "string" ? issue.reference : null;
  const owner = typeof issue.owner === "string" ? issue.owner : null;
  const detail =
    typeof issue.reason === "string"
      ? issue.reason
      : typeof issue.detail === "string"
        ? issue.detail
        : null;
  const parts = [code, reference, owner, detail].filter(Boolean);
  return parts.length > 0 ? parts.join(" · ") : `Issue #${idx + 1}: ${JSON.stringify(issue)}`;
}

function formatStatementPayloadFacts(
  requestId: string,
  payload: Record<string, unknown>
): Fact[] {
  const entries = Object.entries(payload);
  if (entries.length === 0) {
    return [{ id: `${requestId}-empty`, label: "Payload", value: "No statement metrics" }];
  }
  const currency = typeof payload.currency === "string" ? payload.currency : "USD";
  return entries.map(([key, val]) => {
    const label = key.replace(/_/g, " ");
    let formatted: string;
    if (val === null || val === undefined) {
      formatted = "Unavailable";
    } else if (typeof val === "number" && /minor/i.test(key)) {
      formatted = formatMinorCurrency(val, currency);
    } else if (typeof val === "number" && /_ns$/i.test(key)) {
      formatted = formatNsWithClock(val);
    } else if (typeof val === "object") {
      formatted = JSON.stringify(val);
    } else {
      formatted = String(val);
    }
    return { id: `${requestId}-${key}`, label, value: formatted };
  });
}

type ReleaseItem = StrategiesProjection["releases"][number];
type DeploymentItem = StrategiesProjection["deployments"][number];
type TrialItem = StrategiesProjection["trials"][number];
type CloseItem = FinanceProjection["accounting_closes"][number];
type JournalItem = FinanceProjection["journals"][number];

const RELEASE_COLUMNS: SortableColumn<ReleaseItem>[] = [
  { id: "release", label: "Release", kind: "identity", sortValue: (r) => r.release_id },
  { id: "owner", label: "Owner", kind: "text", sortValue: (r) => r.owner_name },
  { id: "pit", label: "PIT Manifest", kind: "text", sortValue: (r) => r.pit_manifest_id },
  {
    id: "trials",
    label: "Trials (N)",
    kind: "numeric",
    align: "end",
    sortValue: (r) => r.research_trial_count_n,
  },
  { id: "params", label: "Parameters", kind: "text" },
  {
    id: "recorded",
    label: "Recorded",
    kind: "numeric",
    align: "end",
    sortValue: (r) => r.recorded_at_ns,
  },
  { id: "action", label: "Action", kind: "actions", align: "end" },
  { id: "details", label: "Details", kind: "actions", align: "end" },
];

const TRIAL_COLUMNS: SortableColumn<TrialItem>[] = [
  { id: "id", label: "Comparison", kind: "identity", sortValue: (t) => t.comparison_id },
  {
    id: "pair",
    label: "Incumbent vs Challenger",
    kind: "text",
    sortValue: (t) => `${t.incumbent_release_id}:${t.challenger_release_id}`,
  },
  {
    id: "verdict",
    label: "Verdict",
    kind: "role",
    sortValue: (t) => t.verdict ?? t.status,
  },
  { id: "detail", label: "Evaluation Metrics & Reasons", kind: "text" },
  {
    id: "evaluated",
    label: "Evaluated",
    kind: "numeric",
    align: "end",
    sortValue: (t) => t.evaluated_at_ns ?? t.registered_at_ns,
  },
  { id: "action", label: "Action", kind: "actions", align: "end" },
];

export function StrategiesView() {
  const { refreshSeq, meta, canWrite, outbox, dispatchCommand, reportApiError } = useApp();
  const [data, setData] = useState<StrategiesProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [activeTab, setActiveTab] = useState("active");
  const [releaseQuery, setReleaseQuery] = useState("");
  const [releaseSort, setReleaseSort] = useState<SortState>({ id: "recorded", descending: true });
  const [expandedReleases, setExpandedReleases] = useState<Set<string>>(() => new Set());
  const [deployQuery, setDeployQuery] = useState("");
  const [trialQuery, setTrialQuery] = useState("");
  const [verdictFilter, setVerdictFilter] = useState("all");
  const [trialSort, setTrialSort] = useState<SortState>(null);
  const [confirmTarget, setConfirmTarget] = useState<{
    kind: "activate" | "rollback";
    releaseId: string;
    comparisonId?: string | null;
  } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const res = await fetchProjection<StrategiesProjection>("strategies", {
          signal: controller.signal,
        });
        if (!active || controller.signal.aborted) return;
        if (res.data) setData(res.data);
        setError(null);
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setError(err instanceof Error ? err.message : "Could not load strategies.");
      } finally {
        if (active && !controller.signal.aborted) setLoading(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [refreshSeq, retrySeq, reportApiError]);

  const principalId = meta?.board_access?.principal_id ?? null;

  const releaseById = useMemo(
    () => new Map((data?.releases ?? []).map((r) => [r.release_id, r])),
    [data?.releases]
  );

  const consumedComparisonIds = useMemo(() => {
    const set = new Set<string>();
    for (const d of data?.deployments ?? []) {
      if (d.comparison_id) set.add(d.comparison_id);
    }
    return set;
  }, [data?.deployments]);

  const filteredReleases = useMemo(() => {
    const q = releaseQuery.trim().toLowerCase();
    const list = (data?.releases ?? []).filter((r) => {
      if (!q) return true;
      return `${r.release_id} ${r.owner_name} ${r.owner_agent_id} ${r.pit_manifest_id} ${r.parent_release_id ?? ""} ${JSON.stringify(r.parameters)}`
        .toLowerCase()
        .includes(q);
    });
    return sortItems(list, releaseSort, RELEASE_COLUMNS);
  }, [data?.releases, releaseQuery, releaseSort]);

  const filteredDeployments = useMemo(() => {
    const q = deployQuery.trim().toLowerCase();
    return (data?.deployments ?? []).filter((d) => {
      if (!q) return true;
      return `${d.deployment_id} ${d.release_id} ${d.program_identity} ${d.status} ${d.command_id} ${d.comparison_id ?? ""}`
        .toLowerCase()
        .includes(q);
    });
  }, [data?.deployments, deployQuery]);

  const filteredTrials = useMemo(() => {
    const q = trialQuery.trim().toLowerCase();
    const list = (data?.trials ?? []).filter((t) => {
      if (verdictFilter !== "all") {
        if (verdictFilter === "invalidated") {
          if (t.status !== "invalidated" && !t.invalidation_reason) return false;
        } else if (t.verdict !== verdictFilter) {
          return false;
        }
      }
      if (!q) return true;
      return `${t.comparison_id} ${t.incumbent_release_id} ${t.challenger_release_id} ${t.verdict ?? ""} ${t.status} ${t.invalidation_reason ?? ""} ${JSON.stringify(t.verdict_detail ?? {})}`
        .toLowerCase()
        .includes(q);
    });
    return sortItems(list, trialSort, TRIAL_COLUMNS);
  }, [data?.trials, trialQuery, verdictFilter, trialSort]);

  if (loading && !data) return <Skeleton label="Loading strategy trials" lines={5} />;
  if (!data) {
    return (
      <Result
        title="Strategies unavailable"
        action={
          <Button
            emphasis="soft"
            shape="pill"
            onClick={() => {
              setLoading(true);
              setError(null);
              setRetrySeq((n) => n + 1);
            }}
          >
            Retry
          </Button>
        }
      >
        {error ?? "Could not load strategies."}
      </Result>
    );
  }

  if (!data.paper_enabled || (meta !== null && !meta.active_domains.includes("paper"))) {
    return (
      <Stack gap={16}>
        <PageHeader title="Strategies & Trials" detail="Paper domain not installed" />
        <Result title="Paper trading strategies inactive">
          Activate the opt-in paper domain to register releases and run paired challenger trials.
        </Result>
      </Stack>
    );
  }

  const selfOwnedReleases = data.releases.filter((r) =>
    Boolean(principalId && r.owner_agent_id === principalId)
  );
  const isSelfOwned = (releaseId: string) => {
    if (!principalId) return false;
    return releaseById.get(releaseId)?.owner_agent_id === principalId;
  };

  const isStrategyPending =
    (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
    (outbox.intent?.command_type === "activate_strategy_release" ||
      outbox.intent?.command_type === "rollback_strategy_deployment");
  const targetSelfOwned = confirmTarget ? isSelfOwned(confirmTarget.releaseId) : false;

  const toggleReleaseExpand = (releaseId: string) => {
    setExpandedReleases((prev) => {
      const next = new Set(prev);
      if (next.has(releaseId)) next.delete(releaseId);
      else next.add(releaseId);
      return next;
    });
  };

  const deployById = new Map<string, DeploymentItem>(
    filteredDeployments.map((d) => [d.deployment_id, d])
  );
  const deployCols: DataColumn[] = [
    {
      id: "deployment",
      label: "Deployment",
      kind: "identity",
      sortValue: (row) => deployById.get(row.id)?.deployment_id,
    },
    {
      id: "release",
      label: "Release",
      kind: "text",
      sortValue: (row) => deployById.get(row.id)?.release_id,
    },
    {
      id: "program",
      label: "Program",
      kind: "text",
      sortValue: (row) => deployById.get(row.id)?.program_identity,
    },
    {
      id: "status",
      label: "Status",
      kind: "role",
      sortValue: (row) => deployById.get(row.id)?.status,
    },
    {
      id: "activated",
      label: "Activated",
      kind: "numeric",
      align: "end",
      sortValue: (row) => deployById.get(row.id)?.activated_at_ns,
    },
    {
      id: "retired",
      label: "Retired",
      kind: "numeric",
      align: "end",
      sortValue: (row) => deployById.get(row.id)?.retired_at_ns ?? null,
    },
    {
      id: "command",
      label: "Command ID",
      kind: "text",
      sortValue: (row) => deployById.get(row.id)?.command_id,
    },
    { id: "action", label: "Action", kind: "actions", align: "end" },
  ];

  const deployRows: DataRow[] = filteredDeployments.map((d) => {
    const isActive = data.active_deployment?.release_id === d.release_id;
    const eligibleRollback = Boolean(
      data.active_deployment &&
        data.adapter &&
        (d.status === "superseded" || d.status === "rolled_back") &&
        d.release_id !== data.active_deployment.release_id
    );
    const selfOwned = isSelfOwned(d.release_id);
    return {
      id: d.deployment_id,
      cells: {
        deployment: <Text className="sd-mono">{d.deployment_id}</Text>,
        release: <Text className="sd-mono">{d.release_id}</Text>,
        program: <Text className="sd-mono">{d.program_identity}</Text>,
        status: (
          <Badge
            tone={
              d.status === "active"
                ? "success"
                : d.status === "rolled_back"
                  ? "warning"
                  : "neutral"
            }
            dot={false}
          >
            {d.status}
          </Badge>
        ),
        activated: (
          <Text className="sd-tabular">{formatRelativeNs(normalizeToNs(d.activated_at_ns))}</Text>
        ),
        retired: (
          <Text className="sd-tabular">
            {d.retired_at_ns ? formatRelativeNs(normalizeToNs(d.retired_at_ns)) : "Active"}
          </Text>
        ),
        command: (
          <Inline gap={6} align="center">
            <Text className="sd-mono">{d.command_id}</Text>
            <CopyButton value={d.command_id} presentation="quiet" />
          </Inline>
        ),
        action: isActive ? (
          <Badge tone="success" dot={false}>
            Active
          </Badge>
        ) : eligibleRollback ? (
          <Button
            emphasis="soft"
            tone="danger"
            size="compact"
            shape="pill"
            disabled={!canWrite || selfOwned}
            onClick={() =>
              setConfirmTarget({
                kind: "rollback",
                releaseId: d.release_id,
                comparisonId: d.comparison_id,
              })
            }
          >
            Roll Back
          </Button>
        ) : (
          <Text variant="meta">{selfOwned ? "Self-owned" : "—"}</Text>
        ),
      },
    };
  });

  return (
    <Stack gap={16}>
      <PageHeader
        title="Strategies & Challenger Trials"
        detail="Paired incumbent-vs-challenger evaluation, release lineage, forward rollbacks, and PaperExecution adapter"
      />

      {selfOwnedReleases.length > 0 ? (
        <Notice title="Self-activation guard active" tone="neutral">
          Self-activation guard: release author ({principalId}) cannot activate or roll back their
          own release ({selfOwnedReleases.map((r) => r.release_id).join(", ")}). A separate Board
          Control principal must promote or roll back these releases.
        </Notice>
      ) : null}

      {confirmTarget && meta && data.adapter ? (
        <ControlledInlineConfirm
          open
          pending={isStrategyPending}
          onCancel={() => setConfirmTarget(null)}
          decision={{
            id: `${confirmTarget.kind}-${confirmTarget.releaseId}`,
            revision: `${data.active_deployment?.deployment_id ?? "none"}:${data.adapter.binding_revision}`,
            title:
              confirmTarget.kind === "activate"
                ? `Promote Strategy Release ${confirmTarget.releaseId}`
                : `Roll Back to Strategy Release ${confirmTarget.releaseId}`,
            consequence:
              confirmTarget.kind === "activate"
                ? "Activates the promoted challenger strategy as the sole active paper deployment."
                : "Creates a forward rollback deployment returning to the selected prior release.",
            confirmLabel:
              confirmTarget.kind === "activate"
                ? `Promote ${confirmTarget.releaseId}`
                : `Roll Back to ${confirmTarget.releaseId}`,
            destructive: confirmTarget.kind === "rollback",
            available: (canWrite || isStrategyPending) && !targetSelfOwned,
            whatChanges: [
              { label: "Active strategy", value: confirmTarget.releaseId },
              {
                label: "Previous deployment",
                value: data.active_deployment?.deployment_id ?? "None",
              },
            ],
            whatStaysUntouched: [
              {
                label: "Open orders, fills & costs",
                value: "Preserved unchanged across forward rollback",
              },
              { label: "Environment", value: "Strictly Paper (no live funds)" },
              { label: "Historical trials", value: "Immutable audit records preserved" },
            ],
            technicalDetails: [
              {
                label: "command_type",
                value:
                  confirmTarget.kind === "activate"
                    ? "activate_strategy_release"
                    : "rollback_strategy_deployment",
              },
              { label: "binding_revision", value: data.adapter.binding_revision },
              {
                label: "comparison_id",
                value: confirmTarget.comparisonId ?? "none",
              },
            ],
          }}
          onConfirm={async () => {
            if (confirmTarget.kind === "activate") {
              const lifecycle = await dispatchCommand({
                company_id: meta.company_id,
                command_id: newCommandId("strat-act"),
                command_type: "activate_strategy_release",
                contract_version: "strategy-command-v1",
                preconditions: {
                  binding_revision: data.adapter!.binding_revision,
                  ...(data.active_deployment
                    ? { expected_active_deployment_id: data.active_deployment.deployment_id }
                    : {}),
                },
                payload: {
                  release_id: confirmTarget.releaseId,
                  comparison_id: confirmTarget.comparisonId ?? null,
                },
                label: `Promote strategy ${confirmTarget.releaseId}`,
                sent_ns: Date.now() * 1_000_000,
              });
              if (
                lifecycle === "applied" ||
                lifecycle === "accepted" ||
                lifecycle === "unknown"
              ) {
                setConfirmTarget(null);
              }
            } else if (data.active_deployment) {
              const lifecycle = await dispatchCommand({
                company_id: meta.company_id,
                command_id: newCommandId("strat-rb"),
                command_type: "rollback_strategy_deployment",
                contract_version: "strategy-command-v1",
                preconditions: {
                  expected_active_deployment_id: data.active_deployment.deployment_id,
                },
                payload: { target_release_id: confirmTarget.releaseId },
                label: `Roll back to strategy ${confirmTarget.releaseId}`,
                sent_ns: Date.now() * 1_000_000,
              });
              if (
                lifecycle === "applied" ||
                lifecycle === "accepted" ||
                lifecycle === "unknown"
              ) {
                setConfirmTarget(null);
              }
            }
          }}
        />
      ) : null}

      <Tabs
        label="Strategy sections"
        presentation="pill"
        value={activeTab}
        onValueChange={setActiveTab}
        items={[
          {
            id: "active",
            label: "Active Deployment",
            panel: activeTab === "active" ? (
              <Card variant="raised" title="Active Paper Strategy Deployment">
                {data.active_deployment ? (
                  <div className="sd-facts-wrap">
                    <RecordFacts
                      source="paper_strategy_deployment"
                      asOf={`seq #${data.event_seq}`}
                      facts={[
                        {
                          id: "dep",
                          label: "Deployment ID",
                          value: (
                            <Inline gap={6} align="center">
                              <Text className="sd-mono">{data.active_deployment.deployment_id}</Text>
                              <CopyButton
                                value={data.active_deployment.deployment_id}
                                presentation="quiet"
                              />
                            </Inline>
                          ),
                        },
                        {
                          id: "rel",
                          label: "Active Release",
                          value: (
                            <Text className="sd-mono">{data.active_deployment.release_id}</Text>
                          ),
                        },
                        {
                          id: "prog",
                          label: "Program Identity",
                          value: (
                            <Text className="sd-mono">
                              {data.active_deployment.program_identity}
                            </Text>
                          ),
                        },
                        {
                          id: "bind",
                          label: "Binding Revision",
                          value: (
                            <Inline gap={6} align="center">
                              <Text className="sd-mono">
                                {data.active_deployment.binding_revision}
                              </Text>
                              <CopyButton
                                value={data.active_deployment.binding_revision}
                                presentation="quiet"
                              />
                            </Inline>
                          ),
                        },
                        {
                          id: "cmp",
                          label: "Qualifying Trial",
                          value: data.active_deployment.comparison_id ?? "Initial baseline",
                        },
                        {
                          id: "status",
                          label: "Status",
                          value: (
                            <Badge tone="success" dot={false}>
                              {data.active_deployment.status}
                            </Badge>
                          ),
                        },
                        {
                          id: "act",
                          label: "Activated At",
                          value: formatNsWithClock(data.active_deployment.activated_at_ns),
                        },
                        {
                          id: "cmd",
                          label: "Command ID",
                          value: (
                            <Inline gap={6} align="center">
                              <Text className="sd-mono">{data.active_deployment.command_id}</Text>
                              <CopyButton
                                value={data.active_deployment.command_id}
                                presentation="quiet"
                              />
                            </Inline>
                          ),
                        },
                      ]}
                    />
                  </div>
                ) : (
                  <Text variant="help">No active strategy deployment recorded yet.</Text>
                )}
              </Card>
            ) : null,
          },
          {
            id: "releases",
            label: `Releases (${data.releases.length})`,
            panel: activeTab === "releases" ? (
              <Card
                variant="raised"
                className="sd-ledger-card"
                title="Registered Strategy Releases"
                description="Versioned strategy parameters, PIT data manifests, and SHA-256 artifact bundles"
              >
                <Stack gap={12}>
                  <FilterBar
                    label="Filter releases"
                    hiddenLabel
                    placeholder="Filter by release ID, owner, PIT manifest, or parameter..."
                    size="compact"
                    query={releaseQuery}
                    onQueryChange={setReleaseQuery}
                  />
                  {data.releases.length === 0 ? (
                    <Text variant="help">No strategy releases registered yet.</Text>
                  ) : filteredReleases.length === 0 ? (
                    <EmptyResult query={releaseQuery} onClear={() => setReleaseQuery("")} />
                  ) : (
                    <Table caption="Registered strategy releases">
                      {renderSortableTableHead(RELEASE_COLUMNS, releaseSort, setReleaseSort)}
                      <tbody>
                        {filteredReleases.map((r) => {
                          const isExpanded = expandedReleases.has(r.release_id);
                          const isActive = data.active_deployment?.release_id === r.release_id;
                          const selfOwned = isSelfOwned(r.release_id);
                          const matchingTrial = data.active_deployment
                            ? data.trials.find(
                                (t) =>
                                  t.status === "completed" &&
                                  t.verdict === "PROMOTE" &&
                                  !consumedComparisonIds.has(t.comparison_id) &&
                                  t.incumbent_release_id === data.active_deployment?.release_id &&
                                  t.challenger_release_id === r.release_id
                              )
                            : undefined;
                          const canPromote = Boolean(
                            data.adapter &&
                              !isActive &&
                              (!data.active_deployment || matchingTrial)
                          );
                          const paramEntries = Object.entries(r.parameters);
                          const paramSummary =
                            paramEntries.length === 0
                              ? "None"
                              : paramEntries
                                  .slice(0, 3)
                                  .map(([k, v]) => `${k}=${String(v)}`)
                                  .join(", ") +
                                (paramEntries.length > 3
                                  ? ` (+${paramEntries.length - 3})`
                                  : "");

                          return (
                            <Fragment key={r.release_id}>
                              <tr>
                                <td data-kind="identity" data-align="start">
                                  <Stack gap={2}>
                                    <Text className="sd-mono">{r.release_id}</Text>
                                    {r.parent_release_id ? (
                                      <Text variant="meta">from {r.parent_release_id}</Text>
                                    ) : null}
                                  </Stack>
                                </td>
                                <td data-kind="text" data-align="start">
                                  <Stack gap={2}>
                                    <Text variant="body">{r.owner_name}</Text>
                                    <Text variant="meta" className="sd-mono">
                                      {r.owner_agent_id}
                                    </Text>
                                  </Stack>
                                </td>
                                <td data-kind="text" data-align="start">
                                  <Text className="sd-mono">{r.pit_manifest_id}</Text>
                                </td>
                                <td data-kind="numeric" data-align="end" data-numeric="true">
                                  <Text className="sd-tabular">{r.research_trial_count_n}</Text>
                                </td>
                                <td data-kind="text" data-align="start">
                                  <Text variant="meta" className="sd-mono">
                                    {paramSummary}
                                  </Text>
                                </td>
                                <td data-kind="numeric" data-align="end" data-numeric="true">
                                  <Text className="sd-tabular">
                                    {formatRelativeNs(normalizeToNs(r.recorded_at_ns))}
                                  </Text>
                                </td>
                                <td data-kind="actions" data-align="end">
                                  {isActive ? (
                                    <Badge tone="success" dot={false}>
                                      Active
                                    </Badge>
                                  ) : canPromote ? (
                                    <Button
                                      emphasis="soft"
                                      tone="brand"
                                      size="compact"
                                      shape="pill"
                                      disabled={!canWrite || selfOwned}
                                      onClick={() =>
                                        setConfirmTarget({
                                          kind: "activate",
                                          releaseId: r.release_id,
                                          comparisonId: matchingTrial?.comparison_id ?? null,
                                        })
                                      }
                                    >
                                      Promote
                                    </Button>
                                  ) : (
                                    <Text variant="meta">
                                      {selfOwned ? "Self-owned" : "Needs PROMOTE trial"}
                                    </Text>
                                  )}
                                </td>
                                <td data-kind="actions" data-align="end">
                                  <Button
                                    emphasis="quiet"
                                    size="compact"
                                    shape="pill"
                                    aria-expanded={isExpanded}
                                    onClick={() => toggleReleaseExpand(r.release_id)}
                                  >
                                    {isExpanded ? "Hide" : "Details"}
                                  </Button>
                                </td>
                              </tr>
                              {isExpanded ? (
                                <tr className="sd-tape-detail-row">
                                  <td colSpan={RELEASE_COLUMNS.length}>
                                    <div className="sd-ledger-detail-sticky">
                                      <Stack gap={12}>
                                        <div className="sd-facts-wrap">
                                          <RecordFacts
                                            source="paper_strategy_release"
                                            asOf={formatNsWithClock(r.recorded_at_ns)}
                                            facts={[
                                              {
                                                id: "sha",
                                                label: "Bundle SHA-256",
                                                value: (
                                                  <Inline gap={6} align="center">
                                                    <Text className="sd-mono">
                                                      {r.bundle_sha256}
                                                    </Text>
                                                    <CopyButton
                                                      value={r.bundle_sha256}
                                                      presentation="quiet"
                                                    />
                                                  </Inline>
                                                ),
                                              },
                                              {
                                                id: "parent",
                                                label: "Parent Release",
                                                value: r.parent_release_id ?? "Root release",
                                              },
                                              {
                                                id: "owner",
                                                label: "Author Principal",
                                                value: `${r.owner_name} (${r.owner_agent_id})`,
                                              },
                                            ]}
                                          />
                                        </div>
                                        <CodeBlock
                                          language="json"
                                          code={JSON.stringify(r.parameters, null, 2)}
                                        />
                                      </Stack>
                                    </div>
                                  </td>
                                </tr>
                              ) : null}
                            </Fragment>
                          );
                        })}
                      </tbody>
                    </Table>
                  )}
                </Stack>
              </Card>
            ) : null,
          },
          {
            id: "deployments",
            label: `Deployments (${data.deployments.length})`,
            panel: activeTab === "deployments" ? (
              <Card
                variant="raised"
                className="sd-ledger-card"
                title="Strategy Deployment History"
                description="Forward-only deployment lineage; Roll Back records a new active deployment without erasing open orders, fills, or costs"
              >
                <Stack gap={12}>
                  <FilterBar
                    label="Filter deployments"
                    hiddenLabel
                    placeholder="Filter by deployment ID, release, status, or command ID..."
                    size="compact"
                    query={deployQuery}
                    onQueryChange={setDeployQuery}
                  />
                  {data.deployments.length === 0 ? (
                    <Text variant="help">No strategy deployments recorded yet.</Text>
                  ) : deployRows.length === 0 ? (
                    <EmptyResult query={deployQuery} onClear={() => setDeployQuery("")} />
                  ) : (
                    <DataTable
                      caption="Strategy deployment history"
                      columns={deployCols}
                      rows={deployRows}
                    />
                  )}
                </Stack>
              </Card>
            ) : null,
          },
          {
            id: "trials",
            label: `Challenger Trials (${data.trials.length})`,
            panel: activeTab === "trials" ? (
              <Card
                variant="raised"
                className="sd-ledger-card"
                title="Paired Incumbent vs Challenger Trials"
                description="Matched-window challenger evaluations, confidence bounds, and invalidation reasons"
              >
                <Stack gap={12}>
                  <Inline gap={8} align="center" wrap>
                    <FilterBar
                      label="Filter trials"
                      hiddenLabel
                      placeholder="Filter by comparison ID, release, reason, or verdict..."
                      size="compact"
                      query={trialQuery}
                      onQueryChange={setTrialQuery}
                    />
                    <Select
                      label="Verdict filter"
                      hiddenLabel
                      size="compact"
                      value={verdictFilter}
                      onValueChange={setVerdictFilter}
                      options={[
                        { id: "all", label: "All verdicts" },
                        { id: "PROMOTE", label: "PROMOTE" },
                        { id: "REJECT", label: "REJECT" },
                        { id: "INCONCLUSIVE", label: "INCONCLUSIVE" },
                        { id: "invalidated", label: "Invalidated" },
                      ]}
                    />
                  </Inline>
                  {data.trials.length === 0 ? (
                    <Text variant="help">No challenger trials registered yet.</Text>
                  ) : filteredTrials.length === 0 ? (
                    <EmptyResult
                      query={trialQuery || verdictFilter}
                      onClear={() => {
                        setTrialQuery("");
                        setVerdictFilter("all");
                      }}
                    />
                  ) : (
                    <Table caption="Challenger trials">
                      {renderSortableTableHead(TRIAL_COLUMNS, trialSort, setTrialSort)}
                      <tbody>
                        {filteredTrials.map((t) => {
                          const metrics = formatTrialVerdictMetrics(t.verdict_detail);
                          const selfOwned = isSelfOwned(t.challenger_release_id);
                          const alreadyConsumed = consumedComparisonIds.has(t.comparison_id);
                          const matchesIncumbent =
                            !data.active_deployment ||
                            t.incumbent_release_id === data.active_deployment.release_id;
                          const canPromote = Boolean(
                            data.adapter &&
                              t.status === "completed" &&
                              t.verdict === "PROMOTE" &&
                              !alreadyConsumed &&
                              matchesIncumbent
                          );

                          return (
                            <tr key={t.comparison_id}>
                              <td data-kind="identity" data-align="start">
                                <Inline gap={6} align="center">
                                  <Text className="sd-mono">{t.comparison_id}</Text>
                                  <CopyButton value={t.comparison_id} presentation="quiet" />
                                </Inline>
                              </td>
                              <td data-kind="text" data-align="start">
                                <Stack gap={2}>
                                  <Text variant="body">
                                    {t.incumbent_release_id} → {t.challenger_release_id}
                                  </Text>
                                  <Text variant="meta" className="sd-mono">
                                    Baseline: {t.baseline_statement_revision}
                                  </Text>
                                </Stack>
                              </td>
                              <td data-kind="role" data-align="start">
                                <Stack gap={4}>
                                  <Badge
                                    tone={
                                      t.verdict === "PROMOTE"
                                        ? "success"
                                        : t.verdict === "REJECT" || t.status === "invalidated"
                                          ? "danger"
                                          : "neutral"
                                    }
                                    dot={false}
                                  >
                                    {t.verdict ?? t.status}
                                  </Badge>
                                  <Text variant="meta">{t.status}</Text>
                                </Stack>
                              </td>
                              <td data-kind="text" data-align="start">
                                <Stack gap={4}>
                                  {metrics.summary.length > 0 ? (
                                    <Text variant="meta" className="sd-tabular">
                                      {metrics.summary.join(" · ")}
                                    </Text>
                                  ) : null}
                                  {metrics.reasons.length > 0 ? (
                                    <Text variant="help">{metrics.reasons.join("; ")}</Text>
                                  ) : null}
                                  {t.invalidation_reason ? (
                                    <Text variant="meta" className="sd-tone-neg">
                                      Invalidation: {t.invalidation_reason}
                                    </Text>
                                  ) : null}
                                  {metrics.summary.length === 0 &&
                                  metrics.reasons.length === 0 &&
                                  !t.invalidation_reason ? (
                                    <Text variant="meta">Awaiting paired evaluation</Text>
                                  ) : null}
                                </Stack>
                              </td>
                              <td data-kind="numeric" data-align="end" data-numeric="true">
                                <Text className="sd-tabular">
                                  {t.evaluated_at_ns
                                    ? formatNsWithClock(t.evaluated_at_ns)
                                    : "Not evaluated"}
                                </Text>
                              </td>
                              <td data-kind="actions" data-align="end">
                                {canPromote ? (
                                  <Button
                                    emphasis="soft"
                                    tone="brand"
                                    size="compact"
                                    shape="pill"
                                    disabled={!canWrite || selfOwned}
                                    onClick={() =>
                                      setConfirmTarget({
                                        kind: "activate",
                                        releaseId: t.challenger_release_id,
                                        comparisonId: t.comparison_id,
                                      })
                                    }
                                  >
                                    Promote
                                  </Button>
                                ) : alreadyConsumed ? (
                                  <Text variant="meta">Promoted</Text>
                                ) : (
                                  <Text variant="meta">
                                    {selfOwned && t.verdict === "PROMOTE" ? "Self-owned" : "—"}
                                  </Text>
                                )}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </Table>
                  )}
                </Stack>
              </Card>
            ) : null,
          },
          {
            id: "adapter",
            label: "PaperExecution Adapter",
            panel: activeTab === "adapter" ? (
              <Card variant="raised" title="Swappable PaperExecution Connector">
                {data.adapter ? (
                  <div className="sd-facts-wrap">
                    <RecordFacts
                      source="paper_binding"
                      asOf={`seq #${data.event_seq}`}
                      facts={[
                        { id: "env", label: "Environment", value: data.adapter.environment },
                        {
                          id: "bindrev",
                          label: "Binding Revision",
                          value: (
                            <Inline gap={6} align="center">
                              <Text className="sd-mono">{data.adapter.binding_revision}</Text>
                              <CopyButton
                                value={data.adapter.binding_revision}
                                presentation="quiet"
                              />
                            </Inline>
                          ),
                        },
                        { id: "conn", label: "Connector", value: data.adapter.connector_revision },
                        { id: "acct", label: "Paper Account", value: data.adapter.account_id },
                        {
                          id: "start",
                          label: "Starting Capital",
                          value: formatMinorCurrency(
                            data.adapter.starting_minor_units,
                            data.adapter.currency
                          ),
                        },
                        {
                          id: "cap",
                          label: "Exposure Cap",
                          value: formatMinorCurrency(
                            data.adapter.exposure_limit_minor_units,
                            data.adapter.currency
                          ),
                        },
                        { id: "dest", label: "Destination ID", value: data.adapter.destination_id },
                      ]}
                    />
                  </div>
                ) : (
                  <Text variant="help">No paper adapter bound.</Text>
                )}
              </Card>
            ) : null,
          },
        ]}
      />
    </Stack>
  );
}

const CLOSE_COLUMNS: SortableColumn<CloseItem>[] = [
  { id: "req", label: "Close Request", kind: "identity", sortValue: (c) => c.request_id },
  { id: "period", label: "Period", kind: "text", sortValue: (c) => c.period ?? "" },
  { id: "scope", label: "Scope", kind: "text", sortValue: (c) => c.scope },
  { id: "status", label: "Status", kind: "role", sortValue: (c) => c.status },
  {
    id: "supersedes",
    label: "Supersedes",
    kind: "text",
    sortValue: (c) => c.supersedes ?? "",
  },
  {
    id: "assessed",
    label: "Assessed",
    kind: "numeric",
    align: "end",
    sortValue: (c) => c.assessed_ns ?? null,
  },
  {
    id: "issues",
    label: "Open Issues",
    kind: "numeric",
    align: "end",
    sortValue: (c) => c.issues.length,
  },
  { id: "details", label: "Details", kind: "actions", align: "end" },
];

const JOURNAL_COLUMNS: SortableColumn<JournalItem>[] = [
  { id: "entry", label: "Entry ID", kind: "identity", sortValue: (j) => j.entry_id },
  {
    id: "source",
    label: "Source / Event",
    kind: "text",
    sortValue: (j) => `${j.source}:${j.source_event}`,
  },
  {
    id: "date",
    label: "Effective Date",
    kind: "numeric",
    align: "end",
    sortValue: (j) => j.effective_date,
  },
  { id: "currency", label: "Currency", kind: "role", sortValue: (j) => j.currency },
  { id: "lines", label: "Debit / Credit Lines", kind: "text" },
  { id: "details", label: "Details", kind: "actions", align: "end" },
];

export function FinanceView() {
  const { refreshSeq, reportApiError } = useApp();
  const [data, setData] = useState<FinanceProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [closeQuery, setCloseQuery] = useState("");
  const [closeSort, setCloseSort] = useState<SortState>(null);
  const [expandedCloses, setExpandedCloses] = useState<Set<string>>(() => new Set());
  const [journalQuery, setJournalQuery] = useState("");
  const [journalSort, setJournalSort] = useState<SortState>({ id: "date", descending: true });
  const [expandedJournals, setExpandedJournals] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const res = await fetchProjection<FinanceProjection>("finance", {
          signal: controller.signal,
        });
        if (!active || controller.signal.aborted) return;
        if (res.data) setData(res.data);
        setError(null);
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setError(err instanceof Error ? err.message : "Could not load finance data.");
      } finally {
        if (active && !controller.signal.aborted) setLoading(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [refreshSeq, retrySeq, reportApiError]);

  const filteredCloses = useMemo(() => {
    const q = closeQuery.trim().toLowerCase();
    const list = (data?.accounting_closes ?? []).filter((c) => {
      if (!q) return true;
      return `${c.request_id} ${c.period ?? ""} ${c.scope} ${c.status} ${c.supersedes ?? ""} ${JSON.stringify(c.issues)}`
        .toLowerCase()
        .includes(q);
    });
    return sortItems(list, closeSort, CLOSE_COLUMNS);
  }, [data?.accounting_closes, closeQuery, closeSort]);

  const filteredJournals = useMemo(() => {
    const q = journalQuery.trim().toLowerCase();
    const list = (data?.journals ?? []).filter((j) => {
      if (!q) return true;
      const accounts = j.lines.map((l) => l.account).join(" ");
      return `${j.entry_id} ${j.source} ${j.source_event} ${j.effective_date} ${j.currency} ${accounts}`
        .toLowerCase()
        .includes(q);
    });
    return sortItems(list, journalSort, JOURNAL_COLUMNS);
  }, [data?.journals, journalQuery, journalSort]);

  if (loading && !data) return <Skeleton label="Loading finance ledger" lines={5} />;
  if (!data) {
    return (
      <Result
        title="Finance & Costs unavailable"
        action={
          <Button
            emphasis="soft"
            shape="pill"
            onClick={() => {
              setLoading(true);
              setError(null);
              setRetrySeq((n) => n + 1);
            }}
          >
            Retry
          </Button>
        }
      >
        {error ?? "Could not load finance data."}
      </Result>
    );
  }

  const balById = new Map(data.account_balances.map((b) => [b.account, b]));
  const balCols: DataColumn[] = [
    {
      id: "acct",
      label: "GL Account",
      kind: "identity",
      sortValue: (row) => balById.get(row.id)?.account,
    },
    {
      id: "bal",
      label: "Balance",
      kind: "numeric",
      align: "end",
      sortValue: (row) => balById.get(row.id)?.balance_minor,
    },
  ];
  const balRows: DataRow[] = data.account_balances.map((b) => ({
    id: b.account,
    cells: {
      acct: b.account,
      bal: <Text className="sd-tabular">{formatMinorCurrency(b.balance_minor)}</Text>,
    },
  }));

  const toggleClose = (requestId: string) => {
    setExpandedCloses((prev) => {
      const next = new Set(prev);
      if (next.has(requestId)) next.delete(requestId);
      else next.add(requestId);
      return next;
    });
  };

  const toggleJournal = (entryId: string) => {
    setExpandedJournals((prev) => {
      const next = new Set(prev);
      if (next.has(entryId)) next.delete(entryId);
      else next.add(entryId);
      return next;
    });
  };

  return (
    <Stack gap={16}>
      <PageHeader
        title="Finance & Operating Costs"
        detail="Double-entry general ledger, period accounting closes, retained statements, and 8h funding settlements"
      />

      {data.open_close_issues.length > 0 ? (
        <Notice title="Accounting close contains open issues" tone="warning" urgent>
          <Stack gap={6}>
            <Text variant="body">
              {data.open_close_issues.length} unresolved cost or valuation item(s). Company Net
              P&amp;L stays explicitly Unavailable until closed cleanly.
            </Text>
            <List label="Open close issues" divided>
              {data.open_close_issues.map((issue, idx) => (
                <li key={idx} className="yg-list-item sd-list-row-padded">
                  <Text variant="meta" className="sd-mono">
                    {formatIssueLine(issue, idx)}
                  </Text>
                </li>
              ))}
            </List>
          </Stack>
        </Notice>
      ) : null}

      <div className="sd-split-equal">
        <Card
          title="General Ledger Account Balances"
          variant="raised"
          className="sd-ledger-card"
        >
          {balRows.length === 0 ? (
            <Text variant="help">No journal entries posted yet.</Text>
          ) : (
            <DataTable caption="GL account balances" columns={balCols} rows={balRows} />
          )}
        </Card>

        <Card
          title={`Retained Paper Statements (${data.retained_statements.length})`}
          description="Sealed window statement snapshots retained for reconciliation and strategy evaluation"
          variant="raised"
        >
          {data.retained_statements.length === 0 ? (
            <Text variant="help">
              {!data.paper_enabled
                ? "Paper domain not enabled — no retained statements."
                : "No retained paper statements sealed yet."}
            </Text>
          ) : (
            <Stack gap={12}>
              {data.retained_statements.map((stmt) => (
                <Surface key={stmt.request_id} variant="surface" density="compact">
                  <Stack gap={8}>
                    <Inline justify="between" align="center" gap={8} wrap>
                      <Inline gap={8} align="center">
                        <Heading level={4} className="sd-mono">
                          {stmt.request_id}
                        </Heading>
                        <Badge tone="neutral" variant="outline" dot={false}>
                          {stmt.window_id}
                        </Badge>
                      </Inline>
                      <Inline gap={6} align="center">
                        <Text variant="meta" className="sd-mono">
                          Rev: {stmt.revision}
                        </Text>
                        <CopyButton value={stmt.revision} presentation="quiet" />
                      </Inline>
                    </Inline>
                    <div className="sd-facts-wrap">
                      <RecordFacts
                        source={`statement:${stmt.window_id}`}
                        asOf={stmt.revision}
                        facts={formatStatementPayloadFacts(stmt.request_id, stmt.payload)}
                      />
                    </div>
                  </Stack>
                </Surface>
              ))}
            </Stack>
          )}
        </Card>
      </div>

      <Card title="Period Accounting Closes" variant="raised" className="sd-ledger-card">
          <Stack gap={12}>
            <FilterBar
              label="Filter accounting closes"
              hiddenLabel
              placeholder="Filter by request ID, period, scope, or issue..."
              size="compact"
              query={closeQuery}
              onQueryChange={setCloseQuery}
            />
            {data.accounting_closes.length === 0 ? (
              <Text variant="help">No accounting closes recorded yet.</Text>
            ) : filteredCloses.length === 0 ? (
              <EmptyResult query={closeQuery} onClear={() => setCloseQuery("")} />
            ) : (
              <Table caption="Accounting closes">
                {renderSortableTableHead(CLOSE_COLUMNS, closeSort, setCloseSort)}
                <tbody>
                  {filteredCloses.map((c) => {
                    const isExpanded = expandedCloses.has(c.request_id);
                    return (
                      <Fragment key={c.request_id}>
                        <tr>
                          <td data-kind="identity" data-align="start">
                            <Text className="sd-mono">{c.request_id}</Text>
                          </td>
                          <td data-kind="text" data-align="start">
                            <Text className="sd-tabular">{c.period ?? "Unspecified"}</Text>
                          </td>
                          <td data-kind="text" data-align="start">
                            {c.scope}
                          </td>
                          <td data-kind="role" data-align="start">
                            <Badge tone={c.issues.length > 0 ? "warning" : "success"} dot={false}>
                              {c.status}
                            </Badge>
                          </td>
                          <td data-kind="text" data-align="start">
                            <Text className="sd-mono">{c.supersedes ?? "—"}</Text>
                          </td>
                          <td data-kind="numeric" data-align="end" data-numeric="true">
                            <Text className="sd-tabular">
                              {c.assessed_ns
                                ? formatRelativeNs(normalizeToNs(c.assessed_ns))
                                : "Not assessed"}
                            </Text>
                          </td>
                          <td data-kind="numeric" data-align="end" data-numeric="true">
                            <Text className="sd-tabular">{c.issues.length}</Text>
                          </td>
                          <td data-kind="actions" data-align="end">
                            <Button
                              emphasis="quiet"
                              size="compact"
                              shape="pill"
                              aria-expanded={isExpanded}
                              onClick={() => toggleClose(c.request_id)}
                            >
                              {isExpanded ? "Hide" : "Details"}
                            </Button>
                          </td>
                        </tr>
                        {isExpanded ? (
                          <tr className="sd-tape-detail-row">
                            <td colSpan={CLOSE_COLUMNS.length}>
                              <div className="sd-ledger-detail-sticky">
                                <Stack gap={8}>
                                  <Text variant="meta">
                                    Assessed:{" "}
                                    {c.assessed_ns
                                      ? formatNsWithClock(c.assessed_ns)
                                      : "Not assessed"}
                                    {c.supersedes ? ` · Supersedes: ${c.supersedes}` : ""}
                                  </Text>
                                  {c.issues.length === 0 ? (
                                    <Text variant="help">
                                      No open close issues on this request.
                                    </Text>
                                  ) : (
                                    <List label={`Issues for ${c.request_id}`} divided>
                                      {c.issues.map((issue, idx) => (
                                        <li key={idx} className="yg-list-item sd-list-row-padded">
                                          <Text variant="meta" className="sd-mono">
                                            {formatIssueLine(issue, idx)}
                                          </Text>
                                        </li>
                                      ))}
                                    </List>
                                  )}
                                </Stack>
                              </div>
                            </td>
                          </tr>
                        ) : null}
                      </Fragment>
                    );
                  })}
                </tbody>
              </Table>
            )}
          </Stack>
        </Card>

      <Card
        title={`General Ledger Journal Entries (${data.journals.length})`}
        description="Double-entry postings with ordered debit and credit lines"
        variant="raised"
        className="sd-ledger-card"
      >
        <Stack gap={12}>
          <FilterBar
            label="Filter journal entries"
            hiddenLabel
            placeholder="Filter by entry ID, source, event, date, or account..."
            size="compact"
            query={journalQuery}
            onQueryChange={setJournalQuery}
          />
          {data.journals.length === 0 ? (
            <Text variant="help">No general ledger journal entries recorded yet.</Text>
          ) : filteredJournals.length === 0 ? (
            <EmptyResult query={journalQuery} onClear={() => setJournalQuery("")} />
          ) : (
            <Table caption="General ledger journal entries">
              {renderSortableTableHead(JOURNAL_COLUMNS, journalSort, setJournalSort)}
              <tbody>
                {filteredJournals.map((j) => {
                  const isExpanded = expandedJournals.has(j.entry_id);
                  const orderedLines = [...j.lines].sort((a, b) => a.ordinal - b.ordinal);
                  const lineSummary = orderedLines
                    .map(
                      (l) =>
                        `${l.amount_minor >= 0 ? "DR" : "CR"} ${l.account} ${formatMinorCurrency(Math.abs(l.amount_minor), j.currency)}`
                    )
                    .join(" · ");

                  return (
                    <Fragment key={j.entry_id}>
                      <tr>
                        <td data-kind="identity" data-align="start">
                          <Inline gap={6} align="center">
                            <Text className="sd-mono">{j.entry_id}</Text>
                            <CopyButton value={j.entry_id} presentation="quiet" />
                          </Inline>
                        </td>
                        <td data-kind="text" data-align="start">
                          <Stack gap={2}>
                            <Text variant="body">{j.source}</Text>
                            <Text variant="meta" className="sd-mono">
                              {j.source_event}
                            </Text>
                          </Stack>
                        </td>
                        <td data-kind="numeric" data-align="end" data-numeric="true">
                          <Text className="sd-tabular">{j.effective_date}</Text>
                        </td>
                        <td data-kind="role" data-align="start">
                          <Badge tone="neutral" variant="outline" dot={false}>
                            {j.currency}
                          </Badge>
                        </td>
                        <td data-kind="text" data-align="start">
                          <Text variant="meta" className="sd-tabular">
                            {lineSummary || "No lines"}
                          </Text>
                        </td>
                        <td data-kind="actions" data-align="end">
                          <Button
                            emphasis="quiet"
                            size="compact"
                            shape="pill"
                            aria-expanded={isExpanded}
                            onClick={() => toggleJournal(j.entry_id)}
                          >
                            {isExpanded ? "Hide" : "Lines"}
                          </Button>
                        </td>
                      </tr>
                      {isExpanded ? (
                        <tr className="sd-tape-detail-row">
                          <td colSpan={JOURNAL_COLUMNS.length}>
                            <div className="sd-ledger-detail-sticky">
                              <List label={`Journal lines for ${j.entry_id}`} divided>
                                {orderedLines.map((line) => (
                                  <li key={line.ordinal} className="yg-list-item sd-list-row-padded">
                                    <Inline
                                      justify="between"
                                      align="center"
                                      gap={8}
                                      wrap
                                    >
                                      <Inline gap={8} align="center">
                                        <Text variant="meta" className="sd-tabular">
                                          #{line.ordinal}
                                        </Text>
                                        <Badge
                                          tone={line.amount_minor >= 0 ? "success" : "neutral"}
                                          dot={false}
                                        >
                                          {line.amount_minor >= 0 ? "Debit" : "Credit"}
                                        </Badge>
                                        <Text className="sd-mono">{line.account}</Text>
                                      </Inline>
                                      <Text className="sd-tabular">
                                        {formatMinorCurrency(Math.abs(line.amount_minor), j.currency)}
                                      </Text>
                                    </Inline>
                                  </li>
                                ))}
                              </List>
                            </div>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </Table>
          )}
        </Stack>
      </Card>

      <DisclosureRow
        label={`8h Funding Settlements (${data.funding_settlements.length})`}
        initiallyOpen={false}
      >
        {data.funding_settlements.length === 0 ? (
          <Text variant="help">No funding intervals recorded.</Text>
        ) : (
          <List label="8h funding settlements" divided>
            {data.funding_settlements.map((f) => (
              <li key={f.settlement_id} className="yg-list-item">
                <WorkEvent
                  event={{
                    id: f.settlement_id,
                    kind: "routine",
                    title: `${f.instrument_id} · pos ${f.signed_position_lots}`,
                    detail:
                      f.status === "unavailable"
                        ? `Unavailable (${f.unavailable_reason ?? "missing"})`
                        : formatMinorCurrency(f.funding_cashflow_atoms),
                    time: formatNsWithClock(f.interval_end_ns),
                    state: f.status === "unavailable" ? "waiting" : "done",
                  }}
                />
              </li>
            ))}
          </List>
        )}
      </DisclosureRow>
    </Stack>
  );
}

export function OrgReportsView() {
  const { navigate, askCeo, refreshSeq, reportApiError, meta } = useApp();
  const [data, setData] = useState<OrgProjection | null>(null);
  const [graph, setGraph] = useState<AgentsGraphProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [tab, setTab] = useState("roster");
  const [rosterQuery, setRosterQuery] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [reportsQuery, setReportsQuery] = useState("");
  const [expandedPubs, setExpandedPubs] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const [orgRes, graphRes] = await Promise.all([
          fetchProjection<OrgProjection>("org", { signal: controller.signal }),
          fetchProjection<AgentsGraphProjection>("agents-graph", {
            signal: controller.signal,
          }).catch(() => null),
        ]);
        if (!active || controller.signal.aborted) return;
        if (orgRes.data) setData(orgRes.data);
        if (graphRes?.data) setGraph(graphRes.data);
        setError(null);
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setError(err instanceof Error ? err.message : "Could not load organization data.");
      } finally {
        if (active && !controller.signal.aborted) setLoading(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [refreshSeq, retrySeq, reportApiError]);

  const graphById = useMemo(
    () => new Map((graph?.nodes ?? []).map((n) => [n.agent_id, n])),
    [graph?.nodes]
  );

  const agentNameById = useMemo(() => {
    const map = new Map<string, string>();
    for (const a of data?.roster ?? []) {
      if (a.agent_id && a.display_name) map.set(a.agent_id, a.display_name);
    }
    for (const n of graph?.nodes ?? []) {
      if (n.agent_id && n.display_name) map.set(n.agent_id, n.display_name);
    }
    for (const p of data?.publications ?? []) {
      if (p.author_id && p.author_name && !map.has(p.author_id)) {
        map.set(p.author_id, p.author_name);
      }
    }
    if (meta?.founder_id) {
      map.set(meta.founder_id, "Founder · CEO");
    }
    return map;
  }, [data?.roster, data?.publications, graph?.nodes, meta?.founder_id]);

  const displayNameFor = useCallback(
    (id: string): string => agentNameById.get(id) ?? id,
    [agentNameById]
  );

  const filteredRoster = useMemo(() => {
    const q = rosterQuery.trim().toLowerCase();
    return (data?.roster ?? []).filter((agent) => {
      if (!showArchived && agent.archived) return false;
      if (!q) return true;
      const g = graphById.get(agent.agent_id);
      const liveStatus = g?.live_status ?? agent.live_status ?? agent.onboarding_state ?? "active";
      return `${agent.display_name} ${agent.agent_id} ${agent.role_label} ${agent.grant_scope} ${agent.onboarding_state ?? ""} ${liveStatus}`
        .toLowerCase()
        .includes(q);
    });
  }, [data?.roster, showArchived, rosterQuery, graphById]);

  const filteredPublications = useMemo(() => {
    const q = reportsQuery.trim().toLowerCase();
    return (data?.publications ?? []).filter((p) => {
      if (!q) return true;
      const audienceNames = p.audience.map(displayNameFor).join(" ");
      return `${p.asset_id} ${p.author_name} ${p.author_role} ${p.project_id} ${p.work_item_id} ${p.audience.join(" ")} ${audienceNames} ${p.content}`
        .toLowerCase()
        .includes(q);
    });
  }, [data?.publications, reportsQuery, displayNameFor]);

  if (loading && !data) return <Skeleton label="Loading organization and reports" lines={5} />;
  if (!data) {
    return (
      <Result
        title="Org & Reports unavailable"
        action={
          <Button
            emphasis="soft"
            shape="pill"
            onClick={() => {
              setLoading(true);
              setError(null);
              setRetrySeq((n) => n + 1);
            }}
          >
            Retry
          </Button>
        }
      >
        {error ?? "Could not load organization data."}
      </Result>
    );
  }

  const custodyById = new Map(
    data.delegated_hr_custody.archived_employees.map((a) => [`${a.cutoff}-${a.agent_id}`, a])
  );
  const custodyCols: DataColumn[] = [
    {
      id: "cutoff",
      label: "Cutoff",
      kind: "numeric",
      align: "end",
      sortValue: (row) => custodyById.get(row.id)?.cutoff,
    },
    {
      id: "agent",
      label: "Archived Agent",
      kind: "identity",
      sortValue: (row) => custodyById.get(row.id)?.agent_name,
    },
    {
      id: "manager",
      label: "Custody Manager",
      kind: "text",
      sortValue: (row) => custodyById.get(row.id)?.manager_name,
    },
    {
      id: "employment_revision",
      label: "Employment Rev",
      kind: "text",
      sortValue: (row) => custodyById.get(row.id)?.employment_revision,
    },
    {
      id: "scope",
      label: "Prior Scope",
      kind: "role",
      sortValue: (row) => custodyById.get(row.id)?.previous_scope,
    },
    {
      id: "reason",
      label: "Reason",
      kind: "text",
      sortValue: (row) => custodyById.get(row.id)?.reason,
    },
  ];
  const custodyRows: DataRow[] = data.delegated_hr_custody.archived_employees.map((a) => ({
    id: `${a.cutoff}-${a.agent_id}`,
    cells: {
      cutoff: <Text className="sd-tabular">#{a.cutoff}</Text>,
      agent: (
        <Stack gap={2}>
          <Text variant="body">{a.agent_name}</Text>
          <Text variant="meta" className="sd-mono">
            {a.agent_id}
          </Text>
        </Stack>
      ),
      manager: `${a.manager_name} (${a.manager_id})`,
      employment_revision: (
        <Inline gap={6} align="center">
          <Text className="sd-mono">{a.employment_revision}</Text>
          <CopyButton value={a.employment_revision} presentation="quiet" />
        </Inline>
      ),
      scope: a.previous_scope,
      reason: a.reason,
    },
  }));

  const togglePub = (assetId: string) => {
    setExpandedPubs((prev) => {
      const next = new Set(prev);
      if (next.has(assetId)) next.delete(assetId);
      else next.add(assetId);
      return next;
    });
  };

  return (
    <Stack gap={16}>
      <PageHeader
        title="Organization & Published Reports"
        detail="Company roster, onboarding status, selective publications, and CEO-delegated HR custody audit"
      />

      <Tabs
        label="Organization sections"
        presentation="pill"
        value={tab}
        onValueChange={setTab}
        items={[
          {
            id: "roster",
            label: `Roster (${filteredRoster.length})`,
            panel: tab === "roster" ? (
              <Card title="Company Roster & Onboarding" variant="raised">
                <Stack gap={12}>
                  <Stack gap={8}>
                    <FilterBar
                      label="Filter roster"
                      hiddenLabel
                      placeholder="Filter by agent, role, grant scope, or onboarding state..."
                      size="compact"
                      query={rosterQuery}
                      onQueryChange={setRosterQuery}
                    />
                    <Inline justify="end">
                      <Switch
                        label="Show archived"
                        checked={showArchived}
                        onCheckedChange={setShowArchived}
                      />
                    </Inline>
                  </Stack>
                  {filteredRoster.length === 0 ? (
                    <EmptyResult
                      query={rosterQuery}
                      onClear={() => setRosterQuery("")}
                    />
                  ) : (
                    <List label="Company agents" divided>
                      {filteredRoster.map((agent) => {
                        const graphNode = graphById.get(agent.agent_id);
                        const derivedAvatarState =
                          graphNode?.avatar_state ??
                          agent.avatar_state ??
                          (agent.archived
                            ? "done"
                            : agent.onboarding_state === "blocked"
                              ? "blocked"
                              : "ready");
                        const derivedLiveStatus =
                          graphNode?.live_status ??
                          agent.live_status ??
                          (agent.archived
                            ? "archived"
                            : agent.onboarding_state === "blocked"
                              ? "blocked"
                              : agent.onboarding_state ?? "active");
                        const onboardingLabel = agent.onboarding_state ?? "none";

                        return (
                          <li key={agent.agent_id} className="yg-list-item sd-list-row-padded">
                            <div data-focus-id={`org-agent-${agent.agent_id}`}>
                              <Stack gap={6}>
                                <AgentRow
                                  agent={{
                                    id: agent.agent_id,
                                    name: agent.display_name,
                                    role: `${agent.role_label} · ${derivedLiveStatus}`,
                                    state: derivedAvatarState,
                                    art: agent.avatar_variant,
                                    preview: `${agent.grant_scope || "No grant"} · ${agent.private_draft_count} private drafts`,
                                  }}
                                  onSelect={() =>
                                    navigate(`/office/agents/${encodeURIComponent(agent.agent_id)}`, {
                                      focusReturnId: `org-agent-${agent.agent_id}`,
                                    })
                                  }
                                />
                                <Inline gap={8} align="center" wrap>
                                  <Badge
                                    tone={
                                      agent.archived
                                        ? "neutral"
                                        : derivedLiveStatus === "blocked"
                                          ? "danger"
                                          : "brand"
                                    }
                                    dot={false}
                                  >
                                    {agent.archived ? "Archived" : derivedLiveStatus}
                                  </Badge>
                                  <Text variant="meta">
                                    Onboarding: {onboardingLabel} ({agent.onboarding_attempts}/3) · Budget:{" "}
                                    {formatMinorCurrency(agent.monthly_budget_minor)}
                                  </Text>
                                </Inline>
                              </Stack>
                            </div>
                          </li>
                        );
                      })}
                    </List>
                  )}
                </Stack>
              </Card>
            ) : null,
          },
          {
            id: "reports",
            label: `Published Reports (${data.publications.length})`,
            panel: tab === "reports" ? (
              <Card title="Selective Company Publications" variant="raised">
                <Stack gap={12}>
                  <FilterBar
                    label="Filter published reports"
                    hiddenLabel
                    placeholder="Filter by asset ID, author, project, work item, or content..."
                    size="compact"
                    query={reportsQuery}
                    onQueryChange={setReportsQuery}
                  />
                  {data.publications.length === 0 ? (
                    <Text variant="help">No published company reports yet.</Text>
                  ) : filteredPublications.length === 0 ? (
                    <EmptyResult query={reportsQuery} onClear={() => setReportsQuery("")} />
                  ) : (
                    <List label="Published company reports" divided>
                      {filteredPublications.map((p) => {
                        const isLong = p.content.length > 220;
                        const isExpanded = expandedPubs.has(p.asset_id);
                        const bodyText =
                          isLong && !isExpanded ? `${p.content.slice(0, 220)}…` : p.content;
                        const audienceList =
                          p.audience.length > 0 ? p.audience : ["internal"];

                        return (
                          <li key={p.asset_id} className="yg-list-item sd-list-row-padded">
                            <Surface variant="surface" density="compact">
                              <Stack gap={8}>
                                <Inline justify="between" align="center" gap={8} wrap>
                                  <Inline gap={6} align="center" wrap>
                                    <Heading level={4}>Report {p.asset_id}</Heading>
                                    <CopyButton value={p.asset_id} presentation="quiet" />
                                    <Text variant="meta">
                                      {p.author_name} ({p.author_role}) · #{p.project_id} · Work item:{" "}
                                      {p.work_item_id}
                                    </Text>
                                  </Inline>
                                  <Inline gap={6} align="center" wrap>
                                    {audienceList.map((aud) => (
                                      <Badge
                                        key={aud}
                                        tone="neutral"
                                        variant="outline"
                                        dot={false}
                                      >
                                        {displayNameFor(aud)}
                                      </Badge>
                                    ))}
                                  </Inline>
                                </Inline>
                                <Text variant="body">{bodyText}</Text>
                                <Inline gap={8} align="center" wrap>
                                  <Button
                                    emphasis="soft"
                                    size="compact"
                                    shape="pill"
                                    onClick={() =>
                                      askCeo({
                                        kind: "shared_publication",
                                        id: p.asset_id,
                                        label: `Report ${p.asset_id} by ${p.author_name}`,
                                      })
                                    }
                                  >
                                    Ask CEO about this report
                                  </Button>
                                  <Button
                                    emphasis="quiet"
                                    size="compact"
                                    shape="pill"
                                    onClick={() =>
                                      navigate(`/office/agents/${encodeURIComponent(p.author_id)}`)
                                    }
                                  >
                                    View author
                                  </Button>
                                  {isLong ? (
                                    <Button
                                      emphasis="quiet"
                                      size="compact"
                                      shape="pill"
                                      aria-expanded={isExpanded}
                                      onClick={() => togglePub(p.asset_id)}
                                    >
                                      {isExpanded ? "Collapse" : "Show full report"}
                                    </Button>
                                  ) : null}
                                </Inline>
                              </Stack>
                            </Surface>
                          </li>
                        );
                      })}
                    </List>
                  )}
                </Stack>
              </Card>
            ) : null,
          },
          {
            id: "custody",
            label: `Delegated HR Custody (${custodyRows.length})`,
            panel: tab === "custody" ? (
              <Stack gap={12}>
                <Notice title={data.delegated_hr_custody.policy_notice} tone="neutral">
                  Routine onboarding and offboarding custody transfers are delegated to Founder ·
                  CEO under standing authority. Board members inspect offboarding custody transfers
                  here read-only.
                </Notice>
                <Card
                  title="Archived Employees & Custody Ledger"
                  variant="raised"
                  className="sd-ledger-card"
                >
                  {custodyRows.length === 0 ? (
                    <Text variant="help">No archived employees recorded.</Text>
                  ) : (
                    <DataTable
                      caption="Delegated HR custody ledger"
                      columns={custodyCols}
                      rows={custodyRows}
                    />
                  )}
                </Card>
              </Stack>
            ) : null,
          },
        ]}
      />
    </Stack>
  );
}

export function RestrictionsView() {
  const { refreshSeq, meta, canWrite, outbox, dispatchCommand, reportApiError } = useApp();
  const [data, setData] = useState<RestrictionsProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [tab, setTab] = useState("active");
  const [historyQuery, setHistoryQuery] = useState("");
  const [clearingCause, setClearingCause] = useState<RestrictionItem | null>(null);
  const [addPauseOpen, setAddPauseOpen] = useState(false);
  const [paperRiskConfirmOpen, setPaperRiskConfirmOpen] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const res = await fetchProjection<RestrictionsProjection>("restrictions", {
          signal: controller.signal,
        });
        if (!active || controller.signal.aborted) return;
        if (res.data) setData(res.data);
        setError(null);
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setError(err instanceof Error ? err.message : "Could not load restrictions.");
      } finally {
        if (active && !controller.signal.aborted) setLoading(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [refreshSeq, retrySeq, reportApiError]);

  const filteredHistory = useMemo(() => {
    const q = historyQuery.trim().toLowerCase();
    return (data?.history ?? []).filter((h) => {
      if (!q) return true;
      return `${h.restriction_id} ${h.cause_category} ${h.scope_ref} ${h.reason} ${h.last_command_id ?? ""}`
        .toLowerCase()
        .includes(q);
    });
  }, [data?.history, historyQuery]);

  if (loading && !data) return <Skeleton label="Loading active restrictions" lines={5} />;
  if (!data) {
    return (
      <Result
        title="Restrictions unavailable"
        action={
          <Button
            emphasis="soft"
            shape="pill"
            onClick={() => {
              setLoading(true);
              setError(null);
              setRetrySeq((n) => n + 1);
            }}
          >
            Retry
          </Button>
        }
      >
        {error ?? "Could not load restrictions."}
      </Result>
    );
  }

  const canResumePaper = Boolean(
    meta?.supported_commands?.resume_new_risk?.includes("paper-risk-command-v1")
  );
  const isPausePending =
    (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
    outbox.intent?.command_type === "pause_company";
  const isPaperRiskPending =
    (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
    (outbox.intent?.command_type === "suspend_new_risk" ||
      outbox.intent?.command_type === "resume_new_risk");

  const historyById = new Map(filteredHistory.map((h) => [h.restriction_id, h]));
  const historyCols: DataColumn[] = [
    {
      id: "id",
      label: "Restriction ID",
      kind: "identity",
      sortValue: (row) => historyById.get(row.id)?.restriction_id,
    },
    {
      id: "category",
      label: "Category",
      kind: "role",
      sortValue: (row) => historyById.get(row.id)?.cause_category,
    },
    {
      id: "scope",
      label: "Scope",
      kind: "text",
      sortValue: (row) => historyById.get(row.id)?.scope_ref,
    },
    {
      id: "reason",
      label: "Reason",
      kind: "text",
      sortValue: (row) => historyById.get(row.id)?.reason,
    },
    {
      id: "opened",
      label: "Opened",
      kind: "numeric",
      align: "end",
      sortValue: (row) => historyById.get(row.id)?.recorded_at_ns,
    },
    {
      id: "cleared",
      label: "Cleared",
      kind: "numeric",
      align: "end",
      sortValue: (row) => historyById.get(row.id)?.cleared_at_ns ?? null,
    },
    {
      id: "command",
      label: "Command ID",
      kind: "text",
      sortValue: (row) => historyById.get(row.id)?.last_command_id ?? "",
    },
  ];

  const historyRows: DataRow[] = filteredHistory.map((h) => ({
    id: h.restriction_id,
    cells: {
      id: <Text className="sd-mono">{h.restriction_id}</Text>,
      category: (
        <Badge tone="neutral" variant="outline" dot={false}>
          {h.cause_category}
        </Badge>
      ),
      scope: h.scope_ref,
      reason: h.reason,
      opened: <Text className="sd-tabular">{formatRelativeNs(normalizeToNs(h.recorded_at_ns))}</Text>,
      cleared: (
        <Text className="sd-tabular">
          {h.cleared_at_ns ? formatRelativeNs(normalizeToNs(h.cleared_at_ns)) : "Not cleared"}
        </Text>
      ),
      command: h.last_command_id ? (
        <Inline gap={6} align="center">
          <Text className="sd-mono">{h.last_command_id}</Text>
          <CopyButton value={h.last_command_id} presentation="quiet" />
        </Inline>
      ) : (
        <Text variant="meta">None</Text>
      ),
    },
  }));

  return (
    <Stack gap={16}>
      <PageHeader
        title="Active Restrictions & Risk Gates"
        detail="Each restriction cause is tracked independently; clearing one never clears another active stop"
        action={
          <Inline gap={8} align="center">
            <Button
              emphasis="soft"
              tone="danger"
              size="compact"
              shape="pill"
              disabled={!canWrite && !isPausePending}
              onClick={() => setAddPauseOpen((v) => !v)}
            >
              Add Operational Stop
            </Button>
          </Inline>
        }
      />

      {meta ? (
        <ControlledInlineConfirm
          open={addPauseOpen}
          pending={isPausePending}
          onCancel={() => setAddPauseOpen(false)}
          decision={{
            id: `pause-company-${data.charter_revision}`,
            revision: `${data.charter_revision}:${data.active_causes.length}`,
            title: "Apply Cause-Scoped Company Pause",
            consequence:
              "Adds a distinct operational restriction cause without disturbing existing stops or open positions.",
            confirmLabel: "Apply Operational Stop",
            destructive: true,
            available: canWrite || isPausePending,
            requireReason: true,
            defaultReason: "Board operational hold pending review",
            whatChanges: [
              { label: "New restriction cause", value: "Added as active board_pause" },
              { label: "Existing stops", value: "Tracked independently" },
            ],
            whatStaysUntouched: [
              { label: "Open paper positions", value: "Never auto-liquidated" },
              { label: "Historical records", value: "Append-only audit preserved" },
            ],
            technicalDetails: [
              { label: "command_type", value: "pause_company" },
              { label: "contract_version", value: "restriction-command-v1" },
              { label: "charter_revision", value: data.charter_revision },
            ],
          }}
          onConfirm={async ({ reason }) => {
            const rid = `rest-board-${Date.now()}-${Math.random().toString(36).slice(2, 5)}`;
            const lifecycle = await dispatchCommand({
              company_id: meta.company_id,
              command_id: newCommandId("rest-add"),
              command_type: "pause_company",
              contract_version: "restriction-command-v1",
              preconditions: {
                charter_revision: data.charter_revision,
                restriction_id: rid,
                expected_restriction_revision: 0,
              },
              payload: {
                cause_category: "board_pause",
                scope_ref: "company:all",
                reason,
              },
              label: `Apply operational stop (${rid})`,
              sent_ns: Date.now() * 1_000_000,
            });
            if (
              lifecycle === "applied" ||
              lifecycle === "accepted" ||
              lifecycle === "unknown"
            ) {
              setAddPauseOpen(false);
            }
          }}
        />
      ) : null}

      {data.paper_risk ? (
        <Card
          title="Paper Risk Gate Readiness (Non-Binding Telemetry)"
          variant="raised"
          headerAction={
            data.paper_risk.suspended ? (
              <Button
                emphasis="filled"
                tone="brand"
                size="compact"
                shape="pill"
                disabled={(!canWrite || !canResumePaper) && !isPaperRiskPending}
                onClick={() => setPaperRiskConfirmOpen((v) => !v)}
              >
                Resume Paper Trading
              </Button>
            ) : (
              <Button
                emphasis="soft"
                tone="danger"
                size="compact"
                shape="pill"
                disabled={!canWrite && !isPaperRiskPending}
                onClick={() => setPaperRiskConfirmOpen((v) => !v)}
              >
                Pause New Paper Orders
              </Button>
            )
          }
        >
          <Stack gap={12}>
            <div className="sd-facts-wrap">
              <RecordFacts
                source="paper_risk_state + paper_order"
                asOf={`seq #${data.event_seq}`}
                facts={[
                  {
                    id: "state",
                    label: "Paper Risk Gate",
                    value: data.paper_risk.suspended ? "Suspended" : "Open",
                  },
                  {
                    id: "rev",
                    label: "Effective Revision",
                    value: String(data.paper_risk.effective_revision),
                  },
                  {
                    id: "unres",
                    label: "Unresolved Paper Orders",
                    value: String(data.paper_risk.unresolved_orders_count),
                  },
                  {
                    id: "settled",
                    label: "Settled Company Check",
                    value: data.paper_risk.settled_company_ok ? "Ready" : "Pending settlement",
                  },
                  {
                    id: "quote",
                    label: "Last Quote Observed",
                    value:
                      data.paper_risk.last_quote_observed_at !== null
                        ? formatNsWithClock(data.paper_risk.last_quote_observed_at)
                        : "No reading",
                  },
                  {
                    id: "bindchg",
                    label: "Pending Binding Change",
                    value: data.paper_risk.pending_binding_change
                      ? "Pending changeover"
                      : "None",
                  },
                ]}
              />
            </div>
            <Text variant="meta">{data.paper_risk.server_freshness_note}</Text>
            {data.paper_risk.suspended && !canResumePaper ? (
              <Text variant="help">
                Server-side engine directory required to resume paper trading.
              </Text>
            ) : null}
            {meta ? (
              <ControlledInlineConfirm
                open={paperRiskConfirmOpen}
                surface="inline"
                pending={isPaperRiskPending}
                onCancel={() => setPaperRiskConfirmOpen(false)}
                decision={{
                  id: `paper-risk-${data.paper_risk.effective_revision}`,
                  revision: `${data.charter_revision}:${data.paper_risk.effective_revision}`,
                  title: data.paper_risk.suspended
                    ? "Resume Paper Risk Gate"
                    : "Pause New Paper Orders",
                  consequence: data.paper_risk.suspended
                    ? "Re-opens new paper order placement under current risk limits and charter."
                    : "Suspends new risk-increasing paper orders without auto-liquidating existing positions.",
                  confirmLabel: data.paper_risk.suspended
                    ? "Confirm Resume Paper Trading"
                    : "Confirm Pause New Paper Orders",
                  destructive: !data.paper_risk.suspended,
                  available:
                    (canWrite || isPaperRiskPending) &&
                    (!data.paper_risk.suspended || canResumePaper),
                  requireReason: true,
                  defaultReason: data.paper_risk.suspended
                    ? "Board verified settled paper balances and resumed trading"
                    : "Board paused new paper orders pending risk review",
                  whatChanges: [
                    {
                      label: "Paper risk gate",
                      value: data.paper_risk.suspended ? "Open" : "Suspended",
                    },
                    {
                      label: "Effective revision",
                      value: `${data.paper_risk.effective_revision} -> ${data.paper_risk.effective_revision + 1}`,
                    },
                  ],
                  whatStaysUntouched: [
                    {
                      label: "Open positions",
                      value: "Preserved unchanged (no forced liquidation)",
                    },
                    {
                      label: "Cause-scoped stops",
                      value: "Independent active restrictions remain unchanged",
                    },
                  ],
                  technicalDetails: [
                    {
                      label: "command_type",
                      value: data.paper_risk.suspended ? "resume_new_risk" : "suspend_new_risk",
                    },
                    { label: "contract_version", value: "paper-risk-command-v1" },
                    { label: "charter_revision", value: data.charter_revision },
                    {
                      label: "expected_revision",
                      value: String(data.paper_risk.effective_revision),
                    },
                  ],
                }}
                onConfirm={async ({ reason }) => {
                  const commandType = data.paper_risk!.suspended
                    ? "resume_new_risk"
                    : "suspend_new_risk";
                  const lifecycle = await dispatchCommand({
                    company_id: meta.company_id,
                    command_id: newCommandId("prisk"),
                    command_type: commandType,
                    contract_version: "paper-risk-command-v1",
                    preconditions: {
                      charter_revision: data.charter_revision,
                      expected_revision: data.paper_risk!.effective_revision,
                    },
                    payload: { reason },
                    label: data.paper_risk!.suspended
                      ? "Resume paper trading"
                      : "Pause new paper orders",
                    sent_ns: Date.now() * 1_000_000,
                  });
                  if (
                    lifecycle === "applied" ||
                    lifecycle === "accepted" ||
                    lifecycle === "unknown"
                  ) {
                    setPaperRiskConfirmOpen(false);
                  }
                }}
              />
            ) : null}
          </Stack>
        </Card>
      ) : null}

      <Tabs
        label="Restriction views"
        presentation="pill"
        value={tab}
        onValueChange={setTab}
        items={[
          {
            id: "active",
            label: `Active (${data.active_causes.length})`,
            panel: tab === "active" ? (
              <Surface variant="raised">
                <Stack gap={12}>
                  <Stack gap={4}>
                    <Heading level={3}>
                      Active Restriction Causes ({data.active_causes.length})
                    </Heading>
                    <Text variant="help">
                      Each active cause is tracked and cleared independently without releasing competing stops.
                    </Text>
                  </Stack>
                  {data.active_causes.length === 0 ? (
                    <Text variant="help">No active operational restriction causes.</Text>
                  ) : (
                    <Stack gap={12}>
                      {data.active_causes.map((cause) => {
                        const isThisClearPending =
                          (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
                          outbox.intent?.command_type === "resume_company" &&
                          outbox.intent?.preconditions?.restriction_id === cause.restriction_id;

                        return (
                          <Card
                            key={cause.restriction_id}
                            variant="outlined"
                            density="compact"
                            title={`${cause.cause_category} · ${cause.restriction_id}`}
                            description={`Scope: ${cause.scope_ref} · Rev ${cause.restriction_revision} · ${formatRelativeNs(normalizeToNs(cause.recorded_at_ns))}`}
                            headerAction={
                              cause.operator_releasable ? (
                                <Button
                                  emphasis="soft"
                                  tone="brand"
                                  size="compact"
                                  shape="pill"
                                  disabled={!canWrite && !isThisClearPending}
                                  onClick={() =>
                                    setClearingCause(
                                      clearingCause?.restriction_id === cause.restriction_id
                                        ? null
                                        : cause
                                    )
                                  }
                                >
                                  Clear Cause
                                </Button>
                              ) : (
                                <Badge tone="warning" dot={false}>
                                  System-Held
                                </Badge>
                              )
                            }
                          >
                            <Stack gap={12}>
                              <Text variant="body">{cause.reason}</Text>
                              {clearingCause?.restriction_id === cause.restriction_id && meta ? (
                                <ControlledInlineConfirm
                                  open
                                  surface="inline"
                                  pending={isThisClearPending}
                                  onCancel={() => setClearingCause(null)}
                                  decision={{
                                    id: cause.restriction_id,
                                    revision: `${cause.restriction_revision}:${data.charter_revision}`,
                                    title: `Clear Restriction ${cause.restriction_id}`,
                                    consequence:
                                      "Clears strictly this restriction cause. Any other active competing stops remain in effect.",
                                    confirmLabel: `Clear ${cause.restriction_id}`,
                                    destructive: false,
                                    available:
                                      (canWrite || isThisClearPending) && cause.operator_releasable,
                                    requireReason: true,
                                    defaultReason: `Board cleared ${cause.restriction_id} after verification`,
                                    whatChanges: [
                                      {
                                        label: "Target cause",
                                        value: `${cause.restriction_id} marked cleared`,
                                      },
                                    ],
                                    whatStaysUntouched: [
                                      {
                                        label: "Other active stops",
                                        value: `${Math.max(data.active_causes.length - 1, 0)} competing cause(s) untouched`,
                                      },
                                    ],
                                    technicalDetails: [
                                      { label: "command_type", value: "resume_company" },
                                      { label: "restriction_id", value: cause.restriction_id },
                                      {
                                        label: "expected_restriction_revision",
                                        value: String(cause.restriction_revision),
                                      },
                                    ],
                                  }}
                                  onConfirm={async ({ reason }) => {
                                    const lifecycle = await dispatchCommand({
                                      company_id: meta.company_id,
                                      command_id: newCommandId("rest-clr"),
                                      command_type: "resume_company",
                                      contract_version: "restriction-command-v1",
                                      preconditions: {
                                        charter_revision: data.charter_revision,
                                        restriction_id: cause.restriction_id,
                                        expected_restriction_revision: cause.restriction_revision,
                                      },
                                      payload: { reason },
                                      label: `Clear restriction ${cause.restriction_id}`,
                                      sent_ns: Date.now() * 1_000_000,
                                    });
                                    if (
                                      lifecycle === "applied" ||
                                      lifecycle === "accepted" ||
                                      lifecycle === "unknown"
                                    ) {
                                      setClearingCause(null);
                                    }
                                  }}
                                />
                              ) : null}
                            </Stack>
                          </Card>
                        );
                      })}
                    </Stack>
                  )}
                </Stack>
              </Surface>
            ) : null,
          },
          {
            id: "history",
            label: `History (${data.history.length})`,
            panel: tab === "history" ? (
              <Card
                title="Cleared Restriction History"
                variant="raised"
                className="sd-ledger-card"
              >
                <Stack gap={12}>
                  <FilterBar
                    label="Filter restriction history"
                    hiddenLabel
                    placeholder="Filter by restriction ID, category, scope, reason, or command..."
                    size="compact"
                    query={historyQuery}
                    onQueryChange={setHistoryQuery}
                  />
                  {data.history.length === 0 ? (
                    <Text variant="help">No cleared restriction history recorded yet.</Text>
                  ) : historyRows.length === 0 ? (
                    <EmptyResult query={historyQuery} onClear={() => setHistoryQuery("")} />
                  ) : (
                    <DataTable
                      caption="Cleared restriction history"
                      columns={historyCols}
                      rows={historyRows}
                    />
                  )}
                </Stack>
              </Card>
            ) : null,
          },
        ]}
      />
    </Stack>
  );
}
