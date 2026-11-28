import { useMemo, useState } from "react";
import { CopyButton } from "@openboa/yogurt/motion-copy-button";
import {
  Badge,
  Button,
  Card,
  EmptyResult,
  FilterBar,
  Inline,
  RecordFacts,
  Select,
  Stack,
  Table,
  Text,
} from "@openboa/yogurt";
import type { StrategiesProjection } from "../../../api/types";
import { formatMinorCurrency, formatNsWithClock } from "../../../lib/format";
import { useOutbox } from "../../../state/store";
import {
  renderSortableTableHead,
  sortItems,
  type SortState,
  type SortableColumn,
} from "../../../views/tableUtils";
import type { StrategyConfirmTarget } from "./StrategyReleaseList";

type TrialItem = StrategiesProjection["trials"][number];

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

export interface ChallengerTrialsPanelProps {
  data: StrategiesProjection;
  consumedComparisonIds: Set<string>;
  isSelfOwned: (releaseId: string) => boolean;
  onSelectConfirm: (target: StrategyConfirmTarget) => void;
}

export function ChallengerTrialsPanel({
  data,
  consumedComparisonIds,
  isSelfOwned,
  onSelectConfirm,
}: ChallengerTrialsPanelProps) {
  const { canWrite } = useOutbox();
  const [trialQuery, setTrialQuery] = useState("");
  const [verdictFilter, setVerdictFilter] = useState("all");
  const [trialSort, setTrialSort] = useState<SortState>(null);

  const filteredTrials = useMemo(() => {
    const q = trialQuery.trim().toLowerCase();
    const list = data.trials.filter((t) => {
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
  }, [data.trials, trialQuery, verdictFilter, trialSort]);

  return (
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
                            onSelectConfirm({
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
  );
}

export function ExecutionAdapterPanel({ data }: { data: StrategiesProjection }) {
  return (
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
                    <CopyButton value={data.adapter.binding_id} presentation="quiet" />
                  </Inline>
                ),
              },
              {
                id: "bindrev",
                label: "Binding Revision",
                value: (
                  <Inline gap={6} align="center">
                    <Text className="sd-mono">{data.adapter.binding_revision}</Text>
                    <CopyButton value={data.adapter.binding_revision} presentation="quiet" />
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
  );
}
