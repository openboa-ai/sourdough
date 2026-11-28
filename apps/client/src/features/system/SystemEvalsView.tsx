import { useMemo, useState } from "react";
import { CopyButton } from "@openboa/yogurt/motion-copy-button";
import {
  Badge,
  Button,
  Card,
  DataTable,
  EmptyResult,
  FilterBar,
  Heading,
  Inline,
  Notice,
  PageHeader,
  Result,
  Skeleton,
  Stack,
  Surface,
  Tabs,
  Text,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import type { SystemEvalsProjection } from "../../api/types";
import { useProjection } from "../../api/useProjection";
import { formatNsWithClock } from "../../lib/format";
import { MethodLearningPanel } from "./MethodLearningPanel";
import { RsiTransferPanel } from "./RsiTransferPanel";

export function SystemEvalsView({ embedded = false }: { embedded?: boolean } = {}) {
  const { data, loading, error, refetch } =
    useProjection<SystemEvalsProjection>("system-evals");
  const [activeTab, setActiveTab] = useState("qualifications");
  const [evalQuery, setEvalQuery] = useState("");

  const filteredQuals = useMemo(() => {
    const q = evalQuery.trim().toLowerCase();
    const list = data?.qualifications ?? [];
    if (!q) return list;
    return list.filter((item) =>
      `${item.receipt_id} ${item.candidate_asset_id} ${item.baseline_asset_id ?? ""} ${item.parent_asset_id ?? ""} ${item.passed ? "passed" : "rejected"}`
        .toLowerCase()
        .includes(q)
    );
  }, [data?.qualifications, evalQuery]);

  const filteredActs = useMemo(() => {
    const q = evalQuery.trim().toLowerCase();
    const list = data?.activations ?? [];
    if (!q) return list;
    return list.filter((act) =>
      `${act.request_id} ${act.candidate_id} ${act.parent_asset_id ?? ""} ${act.qualification_id} ${act.operation} ${act.quarantine_reason ?? ""} ${act.blast_radius_descendants.join(" ")} ${act.fallback_candidate_id ?? ""}`
        .toLowerCase()
        .includes(q)
    );
  }, [data?.activations, evalQuery]);

  const filteredSuites = useMemo(() => {
    const q = evalQuery.trim().toLowerCase();
    const list = data?.offline_evaluator.suites ?? [];
    if (!q) return list;
    return list.filter((s) => s.revision.toLowerCase().includes(q));
  }, [data?.offline_evaluator.suites, evalQuery]);

  if (loading && !data) return <Skeleton label="Loading evaluations and tool lineage" lines={5} />;
  if (!data) {
    return (
      <Result
        title="Evaluations unavailable"
        action={
          <Button emphasis="soft" shape="pill" onClick={refetch}>
            Retry
          </Button>
        }
      >
        {error ?? "Could not load evaluation state."}
      </Result>
    );
  }

  const qualById = new Map(filteredQuals.map((q) => [q.receipt_id, q]));
  const qualCols: DataColumn[] = [
    {
      id: "receipt",
      label: "Qualification",
      kind: "identity",
      sortValue: (row) => qualById.get(row.id)?.receipt_id,
    },
    {
      id: "candidate",
      label: "Candidate",
      kind: "text",
      sortValue: (row) => qualById.get(row.id)?.candidate_asset_id,
    },
    {
      id: "baseline",
      label: "Baseline",
      kind: "text",
      sortValue: (row) => qualById.get(row.id)?.baseline_asset_id ?? "",
    },
    {
      id: "parent",
      label: "Parent Asset",
      kind: "text",
      sortValue: (row) => qualById.get(row.id)?.parent_asset_id ?? "",
    },
    {
      id: "trials",
      label: "Pass^k",
      kind: "numeric",
      align: "end",
      sortValue: (row) => qualById.get(row.id)?.trial_count,
    },
    {
      id: "verdict",
      label: "Result",
      kind: "role",
      sortValue: (row) => (qualById.get(row.id)?.passed ? 1 : 0),
    },
    {
      id: "recorded",
      label: "Recorded",
      kind: "numeric",
      align: "end",
      sortValue: (row) => qualById.get(row.id)?.recorded_at_ns,
    },
  ];
  const qualRows: DataRow[] = filteredQuals.map((q) => ({
    id: q.receipt_id,
    cells: {
      receipt: (
        <Inline gap={6} align="center">
          <Text className="sd-mono">{q.receipt_id}</Text>
          <CopyButton value={q.receipt_id} presentation="quiet" />
        </Inline>
      ),
      candidate: (
        <Inline gap={6} align="center">
          <Text className="sd-mono">{q.candidate_asset_id}</Text>
          <CopyButton value={q.candidate_asset_id} presentation="quiet" />
        </Inline>
      ),
      baseline: <Text className="sd-mono">{q.baseline_asset_id ?? "None"}</Text>,
      parent: <Text className="sd-mono">{q.parent_asset_id ?? "root"}</Text>,
      trials: <Text className="sd-tabular">pass^{q.trial_count}</Text>,
      verdict: (
        <Badge tone={q.passed ? "success" : "danger"} dot={false}>
          {q.passed ? "Passed" : "Rejected"}
        </Badge>
      ),
      recorded: <Text className="sd-tabular">{formatNsWithClock(q.recorded_at_ns)}</Text>,
    },
  }));

  const suiteById = new Map(filteredSuites.map((s) => [s.revision, s]));
  const suiteCols: DataColumn[] = [
    {
      id: "rev",
      label: "Suite Revision",
      kind: "identity",
      sortValue: (row) => suiteById.get(row.id)?.revision,
    },
    {
      id: "sealed",
      label: "Sealed At",
      kind: "numeric",
      align: "end",
      sortValue: (row) => suiteById.get(row.id)?.sealed_at_ns,
    },
    {
      id: "obs",
      label: "Observations",
      kind: "numeric",
      align: "end",
      sortValue: (row) => suiteById.get(row.id)?.observation_count,
    },
    {
      id: "grades",
      label: "Blind Grades",
      kind: "numeric",
      align: "end",
      sortValue: (row) => suiteById.get(row.id)?.grade_count,
    },
    {
      id: "pairs",
      label: "Pairwise Swaps",
      kind: "numeric",
      align: "end",
      sortValue: (row) => suiteById.get(row.id)?.pairwise_grade_count,
    },
  ];
  const suiteRows: DataRow[] = filteredSuites.map((s) => ({
    id: s.revision,
    cells: {
      rev: (
        <Inline gap={6} align="center">
          <Text className="sd-mono">{s.revision}</Text>
          <CopyButton value={s.revision} presentation="quiet" />
        </Inline>
      ),
      sealed: <Text className="sd-tabular">{formatNsWithClock(s.sealed_at_ns)}</Text>,
      obs: <Text className="sd-tabular">{s.observation_count}</Text>,
      grades: <Text className="sd-tabular">{s.grade_count}</Text>,
      pairs: <Text className="sd-tabular">{s.pairwise_grade_count}</Text>,
    },
  }));

  return (
    <Stack gap={16}>
      {!embedded ? (
        <PageHeader
          title="Evaluations"
          detail="Non-self-evaluated pass^3 tool qualifications, DAG blast-radius lineage, and isolated offline suites"
        />
      ) : null}

      <Notice title="Isolated Offline Evaluator Boundary (offline-trial-registry-v1)" tone="neutral">
        <Stack gap={4}>
          <Text variant="body">{data.offline_evaluator.isolated_notice}</Text>
          {!data.offline_evaluator.connected || data.offline_evaluator.suites.length === 0 ? (
            <Text variant="help">
              No offline evaluator store attached — pass --evaluator-store &lt;offline.sqlite&gt;
              to serve-company-daemon to inspect isolated blind evaluation suites.
            </Text>
          ) : null}
        </Stack>
      </Notice>

      <FilterBar
        label="Filter evaluations"
        hiddenLabel
        placeholder="Filter qualifications, tool activations, quarantine reasons, or offline suites..."
        size="compact"
        query={evalQuery}
        onQueryChange={setEvalQuery}
      />

      <Tabs
        label="Evaluations and tool lineage"
        presentation="pill"
        value={activeTab}
        onValueChange={setActiveTab}
        items={[
          {
            id: "qualifications",
            label: `Tool Qualifications (${filteredQuals.length})`,
            panel: activeTab === "qualifications" ? (
              <Card
                title="Review Tool Qualifications (pass^k)"
                variant="raised"
                className="sd-ledger-card"
              >
                {data.qualifications.length === 0 ? (
                  <Text variant="help">No review tool qualifications recorded yet.</Text>
                ) : qualRows.length === 0 ? (
                  <EmptyResult query={evalQuery} onClear={() => setEvalQuery("")} />
                ) : (
                  <DataTable caption="Review qualifications" columns={qualCols} rows={qualRows} />
                )}
              </Card>
            ) : null,
          },
          {
            id: "dag",
            label: `Activation DAG (${filteredActs.length})`,
            panel: activeTab === "dag" ? (
              <Card title="Tool Activation DAG & Quarantine Blast Radius" variant="raised">
                {data.activations.length === 0 ? (
                  <Text variant="help">No review tool activations recorded yet.</Text>
                ) : filteredActs.length === 0 ? (
                  <EmptyResult query={evalQuery} onClear={() => setEvalQuery("")} />
                ) : (
                  <Stack gap={12}>
                    {filteredActs.map((act) => (
                      <Surface key={act.request_id} variant="surface" density="compact">
                        <Stack gap={8}>
                          <Inline justify="between" align="center" gap={8} wrap>
                            <Inline gap={6} align="center">
                              <Heading level={4} className="sd-mono">
                                {act.candidate_id} (rev {act.revision})
                              </Heading>
                              <CopyButton value={act.candidate_id} presentation="quiet" />
                            </Inline>
                            <Badge tone={act.quarantined ? "danger" : "success"} dot={false}>
                              {act.quarantined ? "Quarantined" : "Active"}
                            </Badge>
                          </Inline>
                          <Text variant="meta">
                            {act.parent_asset_id ?? "root"} → {act.candidate_id} · Operation:{" "}
                            {act.operation} · Qual: {act.qualification_id} · Recorded:{" "}
                            {formatNsWithClock(act.recorded_at_ns)}
                          </Text>
                          {act.quarantined || act.quarantine_reason ? (
                            <Notice title="Quarantine reason" tone="danger">
                              {act.quarantine_reason ?? "Quarantined without recorded reason"}
                            </Notice>
                          ) : null}
                          <Text variant="meta">
                            Blast-radius descendants:{" "}
                            {act.blast_radius_descendants.length > 0
                              ? act.blast_radius_descendants.join(", ")
                              : "None"}
                          </Text>
                          <Inline gap={6} align="center">
                            <Text variant="meta">
                              Fallback candidate:{" "}
                              {act.fallback_candidate_id ?? "No change to active tool"}
                            </Text>
                            {act.fallback_candidate_id ? (
                              <CopyButton
                                value={act.fallback_candidate_id}
                                presentation="quiet"
                              />
                            ) : null}
                          </Inline>
                        </Stack>
                      </Surface>
                    ))}
                  </Stack>
                )}
              </Card>
            ) : null,
          },
          {
            id: "method-learning",
            label: `Method Learning (${(data.method_deltas?.length ?? 0) + (data.method_activations?.length ?? 0)})`,
            panel: activeTab === "method-learning" ? (
              <MethodLearningPanel
                deltas={data.method_deltas ?? []}
                qualifications={data.method_qualifications ?? []}
                activations={data.method_activations ?? []}
                query={evalQuery}
                onClearQuery={() => setEvalQuery("")}
              />
            ) : null,
          },
          {
            id: "rsi-transfer",
            label: `RSI Transfer (${data.rsi_transfer_summaries?.length ?? 0})`,
            panel: activeTab === "rsi-transfer" ? (
              <RsiTransferPanel
                summaries={data.rsi_transfer_summaries ?? []}
                query={evalQuery}
                onClearQuery={() => setEvalQuery("")}
              />
            ) : null,
          },
          {
            id: "suites",
            label: `Offline Suites (${filteredSuites.length})`,
            panel: activeTab === "suites" ? (
              <Card
                title="Offline Blind Trial Suites (offline-trial-registry-v1)"
                description={
                  data.offline_evaluator.connected
                    ? "Connected read-only to external offline evaluator store (offline-trial-registry-v1)"
                    : "No offline evaluator store attached to this daemon"
                }
                variant="raised"
                className="sd-ledger-card"
              >
                {data.offline_evaluator.suites.length === 0 ? (
                  <Text variant="help">
                    Pass --evaluator-store &lt;offline.sqlite&gt; to serve-company-daemon to
                    inspect isolated blind evaluation suites here.
                  </Text>
                ) : suiteRows.length === 0 ? (
                  <EmptyResult query={evalQuery} onClear={() => setEvalQuery("")} />
                ) : (
                  <DataTable caption="Offline trial suites" columns={suiteCols} rows={suiteRows} />
                )}
              </Card>
            ) : null,
          },
        ]}
      />
    </Stack>
  );
}
