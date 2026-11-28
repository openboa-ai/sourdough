import { Fragment, useMemo, useState } from "react";
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
  RecordFacts,
  Stack,
  Table,
  Text,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import type { StrategiesProjection } from "../../../api/types";
import { ControlledInlineConfirm } from "../../../components/ControlledInlineConfirm";
import {
  formatNsWithClock,
  formatRelativeNs,
  newCommandId,
  normalizeToNs,
} from "../../../lib/format";
import { useControlPlane, useOutbox } from "../../../state/store";
import {
  renderSortableTableHead,
  sortItems,
  type SortState,
  type SortableColumn,
} from "../../../views/tableUtils";

export interface StrategyConfirmTarget {
  kind: "activate" | "rollback";
  releaseId: string;
  comparisonId?: string | null;
}

type ReleaseItem = StrategiesProjection["releases"][number];
type DeploymentItem = StrategiesProjection["deployments"][number];

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

export interface StrategyActivationConfirmProps {
  data: StrategiesProjection;
  confirmTarget: StrategyConfirmTarget;
  isSelfOwned: (releaseId: string) => boolean;
  onClose: () => void;
}

export function StrategyActivationConfirm({
  data,
  confirmTarget,
  isSelfOwned,
  onClose,
}: StrategyActivationConfirmProps) {
  const { meta } = useControlPlane();
  const { canWrite, outbox, dispatchCommand } = useOutbox();

  if (!meta || !data.adapter) return null;

  const adapter = data.adapter;
  const isStrategyPending =
    (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
    (outbox.intent?.command_type === "activate_strategy_release" ||
      outbox.intent?.command_type === "rollback_strategy_deployment");
  const targetSelfOwned = isSelfOwned(confirmTarget.releaseId);

  return (
    <ControlledInlineConfirm
      open
      pending={isStrategyPending}
      onCancel={onClose}
      decision={{
        id: `${confirmTarget.kind}-${confirmTarget.releaseId}`,
        revision: `${data.active_deployment?.deployment_id ?? "none"}:${adapter.binding_revision}`,
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
          { label: "Environment", value: adapter.environment },
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
          { label: "binding_revision", value: adapter.binding_revision },
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
              binding_revision: adapter.binding_revision,
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
            onClose();
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
            onClose();
          }
        }
      }}
    />
  );
}

export interface StrategyReleaseListProps {
  data: StrategiesProjection;
  consumedComparisonIds: Set<string>;
  isSelfOwned: (releaseId: string) => boolean;
  onSelectConfirm: (target: StrategyConfirmTarget) => void;
}

export function StrategyReleaseList({
  data,
  consumedComparisonIds,
  isSelfOwned,
  onSelectConfirm,
}: StrategyReleaseListProps) {
  const { canWrite } = useOutbox();
  const [releaseQuery, setReleaseQuery] = useState("");
  const [releaseSort, setReleaseSort] = useState<SortState>({
    id: "recorded",
    descending: true,
  });
  const [expandedReleases, setExpandedReleases] = useState<Set<string>>(() => new Set());

  const filteredReleases = useMemo(() => {
    const q = releaseQuery.trim().toLowerCase();
    const list = data.releases.filter((r) => {
      if (!q) return true;
      return `${r.release_id} ${r.owner_name} ${r.owner_agent_id} ${r.pit_manifest_id} ${r.parent_release_id ?? ""} ${JSON.stringify(r.parameters)}`
        .toLowerCase()
        .includes(q);
    });
    return sortItems(list, releaseSort, RELEASE_COLUMNS);
  }, [data.releases, releaseQuery, releaseSort]);

  const toggleReleaseExpand = (releaseId: string) => {
    setExpandedReleases((prev) => {
      const next = new Set(prev);
      if (next.has(releaseId)) next.delete(releaseId);
      else next.add(releaseId);
      return next;
    });
  };

  return (
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
                  data.adapter && !isActive && (!data.active_deployment || matchingTrial)
                );
                const paramEntries = Object.entries(r.parameters);
                const paramSummary =
                  paramEntries.length === 0
                    ? "None"
                    : paramEntries
                        .slice(0, 3)
                        .map(([k, v]) => `${k}=${String(v)}`)
                        .join(", ") +
                      (paramEntries.length > 3 ? ` (+${paramEntries.length - 3})` : "");

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
                              onSelectConfirm({
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
                                          <Text className="sd-mono">{r.bundle_sha256}</Text>
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
  );
}

export interface StrategyDeploymentHistoryPanelProps {
  data: StrategiesProjection;
  isSelfOwned: (releaseId: string) => boolean;
  onSelectConfirm: (target: StrategyConfirmTarget) => void;
}

export function StrategyDeploymentHistoryPanel({
  data,
  isSelfOwned,
  onSelectConfirm,
}: StrategyDeploymentHistoryPanelProps) {
  const { canWrite } = useOutbox();
  const [deployQuery, setDeployQuery] = useState("");

  const filteredDeployments = useMemo(() => {
    const q = deployQuery.trim().toLowerCase();
    return data.deployments.filter((d) => {
      if (!q) return true;
      return `${d.deployment_id} ${d.release_id} ${d.program_identity} ${d.status} ${d.command_id} ${d.comparison_id ?? ""}`
        .toLowerCase()
        .includes(q);
    });
  }, [data.deployments, deployQuery]);

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
              onSelectConfirm({
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
  );
}
