import { Fragment, useEffect, useMemo, useState } from "react";
import { CopyButton } from "@openboa/yogurt/motion-copy-button";
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  DataTable,
  EmptyResult,
  FilterBar,
  Inline,
  Notice,
  PageHeader,
  RecordFacts,
  Result,
  Select,
  Skeleton,
  Stack,
  Table,
  Tabs,
  Text,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import {
  fetchProjection,
  formatMinorCurrency,
  formatRelativeNs,
  newCommandId,
} from "../../api/client";
import type { StrategiesProjection } from "../../api/types";
import {
  ContextualApprovalList,
  usePendingApprovals,
} from "../../components/ContextualApprovalCard";
import { ControlledInlineConfirm } from "../../components/ControlledInlineConfirm";
import { useApp } from "../../state/store";
import {
  formatNsWithClock,
  normalizeToNs,
  renderSortableTableHead,
  sortItems,
  type SortState,
  type SortableColumn,
} from "../tableUtils";

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

type ReleaseItem = StrategiesProjection["releases"][number];
type DeploymentItem = StrategiesProjection["deployments"][number];
type TrialItem = StrategiesProjection["trials"][number];

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

  const strategyApprovals = usePendingApprovals(
    (q) =>
      q.kind === "strategy_promotion" ||
      q.kind === "strategy_release" ||
      q.kind === "challenger_promotion"
  );

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

  if (!data.trading_enabled || (meta !== null && !meta.active_domains.includes("trading"))) {
    return (
      <Stack gap={16}>
        <PageHeader title="Strategies & Trials" detail="Trading domain not installed" />
        <Result title="Trading strategies inactive">
          Activate the Trading domain module to register releases and run paired challenger trials.
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
        title="Strategies"
        detail="Release versions, paired challenger trials, forward rollbacks, and historical performance lineage"
      />

      <ContextualApprovalList questions={strategyApprovals} />

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
                ? "Activates the promoted challenger strategy as the sole active strategy deployment."
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
              { label: "Environment", value: data.adapter.environment },
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
            panel:
              activeTab === "active" ? (
                <Card variant="raised" title="Active Strategy Deployment">
                  {data.active_deployment ? (
                    <div className="sd-facts-wrap">
                      <RecordFacts
                        source="trading_strategy_deployment"
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
            panel:
              activeTab === "releases" ? (
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
                                              source="trading_strategy_release"
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
            panel:
              activeTab === "deployments" ? (
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
            panel:
              activeTab === "trials" ? (
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
            label: "Execution Adapter",
            panel:
              activeTab === "adapter" ? (
                <Card variant="raised" title="Swappable Execution Connector">
                  {data.adapter ? (
                    <div className="sd-facts-wrap">
                      <RecordFacts
                        source="execution_environment_binding"
                        asOf={`seq #${data.event_seq}`}
                        facts={[
                          { id: "env", label: "Environment", value: data.adapter.environment },
                          {
                            id: "bindid",
                            label: "Binding ID",
                            value: (
                              <Inline gap={6} align="center">
                                <Text className="sd-mono">{data.adapter.binding_id}</Text>
                                <CopyButton
                                  value={data.adapter.binding_id}
                                  presentation="quiet"
                                />
                              </Inline>
                            ),
                          },
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
                          { id: "acct", label: "Execution Account", value: data.adapter.account_id },
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
                    <Text variant="help">No execution adapter bound.</Text>
                  )}
                </Card>
              ) : null,
          },
        ]}
      />
    </Stack>
  );
}
