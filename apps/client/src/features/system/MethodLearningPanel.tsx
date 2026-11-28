import { useMemo } from "react";
import { CopyButton } from "@openboa/yogurt/motion-copy-button";
import {
  Badge,
  Card,
  DataTable,
  EmptyResult,
  Heading,
  Inline,
  Notice,
  Stack,
  Surface,
  Text,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import type {
  MethodActivationProjectionItem,
  MethodDeltaProjectionItem,
  MethodQualificationProjectionItem,
} from "../../api/types";
import { formatNsWithClock } from "../../lib/format";
import { verdictTone } from "../../lib/statusTones";

export interface MethodLearningPanelProps {
  deltas: MethodDeltaProjectionItem[];
  qualifications: MethodQualificationProjectionItem[];
  activations: MethodActivationProjectionItem[];
  query: string;
  onClearQuery: () => void;
}

export function MethodLearningPanel({
  deltas,
  qualifications,
  activations,
  query,
  onClearQuery,
}: MethodLearningPanelProps) {
  const q = query.trim().toLowerCase();

  const filteredDeltas = useMemo(() => {
    if (!q) return deltas;
    return deltas.filter((d) =>
      `${d.method_id} ${d.delta_kind} ${d.item_kind} ${d.audience_scope} ${d.status} ${d.author_agent_id}`
        .toLowerCase()
        .includes(q)
    );
  }, [deltas, q]);

  const filteredQuals = useMemo(() => {
    if (!q) return qualifications;
    return qualifications.filter((item) =>
      `${item.qualification_id} ${item.method_id} ${item.evaluator_revision} ${item.passed ? "passed" : "failed"}`
        .toLowerCase()
        .includes(q)
    );
  }, [qualifications, q]);

  const filteredActs = useMemo(() => {
    if (!q) return activations;
    return activations.filter((a) =>
      `${a.request_id} ${a.method_id} ${a.operation} ${a.audience_scope} ${a.quarantine_reason ?? ""} ${a.blast_radius_descendants.join(" ")}`
        .toLowerCase()
        .includes(q)
    );
  }, [activations, q]);

  const deltaById = useMemo(
    () => new Map(filteredDeltas.map((d) => [d.delta_id, d])),
    [filteredDeltas]
  );
  const deltaCols: DataColumn[] = useMemo(
    () => [
      {
        id: "method",
        label: "Method",
        kind: "identity",
        sortValue: (row) => deltaById.get(row.id)?.method_id,
      },
      {
        id: "kind",
        label: "Delta / Kind",
        kind: "role",
        sortValue: (row) => deltaById.get(row.id)?.delta_kind,
      },
      {
        id: "scope",
        label: "Scope",
        kind: "role",
        sortValue: (row) => deltaById.get(row.id)?.audience_scope,
      },
      {
        id: "status",
        label: "Status",
        kind: "role",
        sortValue: (row) => deltaById.get(row.id)?.status,
      },
      {
        id: "created",
        label: "Recorded",
        kind: "numeric",
        align: "end",
        sortValue: (row) => deltaById.get(row.id)?.created_at_ns,
      },
    ],
    [deltaById]
  );
  const deltaRows: DataRow[] = useMemo(
    () =>
      filteredDeltas.map((d) => ({
        id: d.delta_id,
        cells: {
          method: (
            <Stack gap={2}>
              <Inline gap={6} align="center">
                <Text className="sd-mono">{d.method_id}</Text>
                <CopyButton value={d.delta_id} presentation="quiet" />
              </Inline>
              <Text variant="help" className="sd-mono">
                rev {d.revision_sha256.slice(0, 12)}
                {d.method_id_redacted ? " · one_agent redacted" : ""}
              </Text>
            </Stack>
          ),
          kind: (
            <Text variant="body">
              {d.delta_kind} · {d.item_kind}
            </Text>
          ),
          scope: (
            <Badge
              tone={d.audience_scope === "one_agent" ? "neutral" : "brand"}
              variant="outline"
              dot={false}
            >
              {d.audience_scope === "one_agent" ? "one_agent (redacted)" : d.audience_scope}
            </Badge>
          ),
          status: (
            <Badge tone={verdictTone(d.status)} variant="soft" dot={false}>
              {d.status}
            </Badge>
          ),
          created: (
            <Text className="sd-tabular">{formatNsWithClock(d.created_at_ns)}</Text>
          ),
        },
      })),
    [filteredDeltas]
  );

  const qualById = useMemo(
    () => new Map(filteredQuals.map((item) => [item.qualification_id, item])),
    [filteredQuals]
  );
  const qualCols: DataColumn[] = useMemo(
    () => [
      {
        id: "qual",
        label: "Qualification",
        kind: "identity",
        sortValue: (row) => qualById.get(row.id)?.qualification_id,
      },
      {
        id: "method",
        label: "Method",
        kind: "text",
        sortValue: (row) => qualById.get(row.id)?.method_id,
      },
      {
        id: "stages",
        label: "3-Stage Gate (S1 / S2 / S3)",
        kind: "text",
      },
      {
        id: "trials",
        label: "Trials",
        kind: "numeric",
        align: "end",
        sortValue: (row) => qualById.get(row.id)?.trial_count,
      },
      {
        id: "verdict",
        label: "Verdict",
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
    ],
    [qualById]
  );
  const qualRows: DataRow[] = useMemo(
    () =>
      filteredQuals.map((item) => ({
        id: item.qualification_id,
        cells: {
          qual: <Text className="sd-mono">{item.qualification_id}</Text>,
          method: (
            <Stack gap={2}>
              <Text className="sd-mono">{item.method_id}</Text>
              <Text variant="help" className="sd-mono">
                {item.revision_sha256.slice(0, 12)}
                {item.method_id_redacted ? " · private" : ""}
              </Text>
            </Stack>
          ),
          stages: (
            <Text className="sd-mono">
              S1:{item.stage1_passed ? "PASS" : "FAIL"} · S2:
              {item.stage2_passed ? "PASS" : "FAIL"} · S3:
              {item.stage3_passed ? "PASS" : "FAIL"}
            </Text>
          ),
          trials: <Text className="sd-tabular">{item.trial_count}</Text>,
          verdict: (
            <Badge tone={item.passed ? "success" : "danger"} dot={false}>
              {item.passed ? "Qualified" : "Rejected"}
            </Badge>
          ),
          recorded: (
            <Text className="sd-tabular">{formatNsWithClock(item.recorded_at_ns)}</Text>
          ),
        },
      })),
    [filteredQuals]
  );

  return (
    <Stack gap={16}>
      <Card
        title={`Method Deltas (${filteredDeltas.length})`}
        description="Distilled procedure & helper candidates (private one_agent identities stay opaque)"
        variant="raised"
        className="sd-ledger-card"
      >
        {deltas.length === 0 ? (
          <Text variant="help">No method deltas recorded yet.</Text>
        ) : deltaRows.length === 0 ? (
          <EmptyResult query={query} onClear={onClearQuery} />
        ) : (
          <DataTable caption="Method deltas" columns={deltaCols} rows={deltaRows} />
        )}
      </Card>

      <Card
        title={`3-Stage Method Qualifications (${filteredQuals.length})`}
        description="Stage 1 (schema/redaction), Stage 2 (isolated replay), Stage 3 (paired counterfactual)"
        variant="raised"
        className="sd-ledger-card"
      >
        {qualifications.length === 0 ? (
          <Text variant="help">No method qualifications recorded yet.</Text>
        ) : qualRows.length === 0 ? (
          <EmptyResult query={query} onClear={onClearQuery} />
        ) : (
          <DataTable caption="Method qualifications" columns={qualCols} rows={qualRows} />
        )}
      </Card>

      <Card
        title={`Method Activations & Blast Radius (${filteredActs.length})`}
        description="Active method lineage, transitive quarantine descendants, and fallback revisions"
        variant="raised"
      >
        {activations.length === 0 ? (
          <Text variant="help">No method activations recorded yet.</Text>
        ) : filteredActs.length === 0 ? (
          <EmptyResult query={query} onClear={onClearQuery} />
        ) : (
          <Stack gap={12}>
            {filteredActs.map((act) => (
              <Surface key={act.request_id} variant="surface" density="compact">
                <Stack gap={8}>
                  <Inline justify="between" align="center" gap={8} wrap>
                    <Inline gap={6} align="center">
                      <Heading level={4} className="sd-mono">
                        {act.method_id} · seq #{act.activation_seq}
                      </Heading>
                      <CopyButton value={act.delta_id} presentation="quiet" />
                    </Inline>
                    <Badge tone={act.quarantined ? "danger" : "success"} dot={false}>
                      {act.quarantined ? "Quarantined" : act.operation}
                    </Badge>
                  </Inline>
                  <Text variant="meta">
                    Scope: {act.audience_scope}
                    {act.method_id_redacted ? " (redacted)" : ""} · Rev:{" "}
                    {act.revision_sha256.slice(0, 12)} · Qual: {act.qualification_id} · Recorded:{" "}
                    {formatNsWithClock(act.recorded_at_ns)}
                  </Text>
                  {act.quarantined || act.quarantine_reason ? (
                    <Notice title="Quarantine Status" tone="danger">
                      {act.method_id_redacted
                        ? "Quarantined (private one_agent reason redacted)"
                        : (act.quarantine_reason ?? "Quarantined without recorded reason")}
                    </Notice>
                  ) : null}
                  <Text variant="meta">
                    Blast-radius descendants:{" "}
                    {act.blast_radius_descendants.length > 0
                      ? act.blast_radius_descendants.join(", ")
                      : "None"}
                  </Text>
                  <Text variant="meta">
                    Fallback revision:{" "}
                    {act.fallback_revision
                      ? act.fallback_revision.slice(0, 12)
                      : "None (base harness)"}
                  </Text>
                </Stack>
              </Surface>
            ))}
          </Stack>
        )}
      </Card>
    </Stack>
  );
}
