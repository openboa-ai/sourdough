import { useEffect, useMemo, useRef, useState } from "react";
import { CopyButton } from "@openboa/yogurt/motion-copy-button";
import { TaskList } from "@openboa/yogurt/motion-task-list";
import {
  Badge,
  Button,
  Card,
  DataTable,
  DisclosureRow,
  EmptyResult,
  FilterBar,
  Heading,
  Inline,
  List,
  Navbar,
  Notice,
  PageHeader,
  RecordFacts,
  Result,
  Skeleton,
  Stack,
  Surface,
  Tabs,
  Text,
  TextField,
  WorkEvent,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import {
  fetchProjection,
  formatDeadlineNs,
  formatNsClock,
  formatRelativeNs,
  newCommandId,
} from "../api/client";
import type {
  SystemAuditProjection,
  SystemEvalsProjection,
  SystemSafetyProjection,
} from "../api/types";
import { ControlledInlineConfirm } from "../components/ControlledInlineConfirm";
import { SpaBreadcrumb } from "../components/SpaBreadcrumb";
import { useApp } from "../state/store";

function normalizeToNs(epoch: number | null | undefined): number | null {
  if (!epoch || !Number.isFinite(epoch)) return null;
  if (epoch < 1e12) return epoch * 1_000_000_000;
  if (epoch < 1e15) return epoch * 1_000_000;
  return epoch;
}

function formatNsTimestamp(ns: number | null | undefined, emptyLabel = "No reading"): string {
  const normalized = normalizeToNs(ns);
  if (!normalized) return emptyLabel;
  return `${formatNsClock(normalized)} (${formatRelativeNs(normalized)})`;
}

function formatWatermark(wm: Record<string, unknown> | null | undefined): string {
  if (!wm || Object.keys(wm).length === 0) return "None";
  return Object.entries(wm)
    .map(([k, v]) => `${k}: ${typeof v === "object" && v !== null ? JSON.stringify(v) : String(v)}`)
    .join(" · ");
}

export function SystemSafetyView() {
  const { navigate, refreshSeq, meta, canWrite, outbox, dispatchCommand, reportApiError } =
    useApp();
  const [data, setData] = useState<SystemSafetyProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [cutConfirmOpen, setCutConfirmOpen] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const res = await fetchProjection<SystemSafetyProjection>("system-safety", {
          signal: controller.signal,
        });
        if (!active || controller.signal.aborted) return;
        if (res.data) setData(res.data);
        setError(null);
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setError(err instanceof Error ? err.message : "Could not load safety state.");
      } finally {
        if (active && !controller.signal.aborted) setLoading(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [refreshSeq, retrySeq, reportApiError]);

  if (loading && !data) return <Skeleton label="Loading safety and checkpoints" lines={5} />;
  if (!data) {
    return (
      <Result
        title="Safety & Restore unavailable"
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
        {error ?? "Could not load safety state."}
      </Result>
    );
  }

  const isPendingCut =
    (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
    outbox.intent?.command_type === "cut_checkpoint";

  const tasks = data.safe_custody_hold.map((step) => ({
    id: step.id,
    label: step.label,
    state: (step.status === "done"
      ? "done"
      : step.status === "active"
        ? "running"
        : "queued") as "done" | "running" | "queued",
  }));

  const cpById = new Map(data.checkpoints.map((cp) => [cp.archive_name, cp]));
  const cpCols: DataColumn[] = [
    {
      id: "name",
      label: "Archive",
      kind: "identity",
      sortValue: (row) => cpById.get(row.id)?.archive_name,
    },
    {
      id: "size",
      label: "Size",
      kind: "numeric",
      align: "end",
      sortValue: (row) => cpById.get(row.id)?.size_bytes,
    },
    {
      id: "modified",
      label: "Recorded",
      kind: "numeric",
      align: "end",
      sortValue: (row) => cpById.get(row.id)?.modified_ns,
    },
  ];
  const cpRows: DataRow[] = data.checkpoints.map((cp) => ({
    id: cp.archive_name,
    cells: {
      name: (
        <Inline gap={6} align="center">
          <Text className="sd-mono">{cp.archive_name}</Text>
          <CopyButton value={cp.archive_name} presentation="quiet" />
        </Inline>
      ),
      size: <Text className="sd-tabular">{(cp.size_bytes / 1024).toFixed(1)} KB</Text>,
      modified: <Text className="sd-tabular">{formatRelativeNs(normalizeToNs(cp.modified_ns))}</Text>,
    },
  }));

  const restoreById = new Map(
    data.restore_receipts.map((r) => [`${r.request_revision}:${r.restored_at_ns}`, r])
  );
  const restoreCols: DataColumn[] = [
    {
      id: "revision",
      label: "Request Revision",
      kind: "identity",
      sortValue: (row) => restoreById.get(row.id)?.request_revision,
    },
    {
      id: "sha256",
      label: "Checkpoint SHA-256",
      kind: "text",
      sortValue: (row) => restoreById.get(row.id)?.checkpoint_sha256,
    },
    {
      id: "watermark",
      label: "Watermark",
      kind: "text",
      sortValue: (row) => formatWatermark(restoreById.get(row.id)?.watermark),
    },
    {
      id: "restored",
      label: "Restored",
      kind: "numeric",
      align: "end",
      sortValue: (row) => restoreById.get(row.id)?.restored_at_ns,
    },
  ];
  const restoreRows: DataRow[] = data.restore_receipts.map((r) => ({
    id: `${r.request_revision}:${r.restored_at_ns}`,
    cells: {
      revision: <Text className="sd-mono">{r.request_revision}</Text>,
      sha256: (
        <Inline gap={6} align="center">
          <Text className="sd-mono">{r.checkpoint_sha256.slice(0, 16)}…</Text>
          <CopyButton value={r.checkpoint_sha256} presentation="quiet" />
        </Inline>
      ),
      watermark: <Text variant="meta">{formatWatermark(r.watermark)}</Text>,
      restored: <Text className="sd-tabular">{formatNsTimestamp(r.restored_at_ns)}</Text>,
    },
  }));

  return (
    <Stack gap={16}>
      <PageHeader
        title="Backups"
        detail="Verified SQLite checkpoint archives, 5-step Safe Custody Hold, and restore receipts"
        action={
          <Inline gap={8} align="center">
            <Button
              emphasis="soft"
              size="compact"
              shape="pill"
              onClick={() => navigate("/controls")}
            >
              Controls ({data.active_restrictions.length})
            </Button>
            <Button
              emphasis="filled"
              tone="brand"
              size="compact"
              shape="pill"
              disabled={!canWrite && !isPendingCut}
              onClick={() => setCutConfirmOpen((v) => !v)}
            >
              Cut Verified Checkpoint
            </Button>
          </Inline>
        }
      />

      {meta ? (
        <ControlledInlineConfirm
          open={cutConfirmOpen}
          pending={isPendingCut}
          onCancel={() => setCutConfirmOpen(false)}
          decision={{
            id: `cut-checkpoint-${data.company_id}`,
            revision: data.charter_revision,
            title: "Cut Verified Company Checkpoint",
            consequence:
              "Creates a checksummed sourdough-checkpoint-v2 archive under the company's private checkpoints directory without interrupting read projections.",
            confirmLabel: "Cut Verified Checkpoint",
            destructive: false,
            available: canWrite || isPendingCut,
            requireReason: true,
            defaultReason: "Scheduled Board backup checkpoint",
            whatChanges: [
              { label: "Archive directory", value: "New verified .zip checkpoint created" },
              { label: "Audit log", value: "Records ops.checkpoint_cut with SHA-256" },
            ],
            whatStaysUntouched: [
              { label: "Live company store", value: "Zero mutations to positions or agents" },
              { label: "Active restrictions", value: "Unchanged" },
            ],
            technicalDetails: [
              { label: "command_type", value: "cut_checkpoint" },
              { label: "contract_version", value: "checkpoint-command-v1" },
              { label: "charter_revision", value: data.charter_revision },
            ],
          }}
          onConfirm={async ({ reason }) => {
            const lifecycle = await dispatchCommand({
              company_id: meta.company_id,
              command_id: newCommandId("ckpt"),
              command_type: "cut_checkpoint",
              contract_version: "checkpoint-command-v1",
              preconditions: { charter_revision: data.charter_revision },
              payload: { reason },
              label: "Cut verified company checkpoint",
              sent_ns: Date.now() * 1_000_000,
            });
            if (
              lifecycle === "applied" ||
              lifecycle === "accepted" ||
              lifecycle === "unknown"
            ) {
              setCutConfirmOpen(false);
            }
          }}
        />
      ) : null}

      {data.store_retired ? (
        <Card title="Store Retirement Marker" variant="raised">
          <Stack gap={8}>
            <Notice title="Retired Store Marker Present" tone="warning">
              This store carries a retirement marker pointing to a replacement SQLite store.
            </Notice>
            <div className="sd-facts-wrap">
              <RecordFacts
                source="company_store_retired"
                asOf={`seq #${data.event_seq}`}
                facts={[
                  {
                    id: "rev",
                    label: "Request Revision",
                    value: <Text className="sd-mono">{data.store_retired.request_revision}</Text>,
                  },
                  {
                    id: "sha",
                    label: "Replacement SHA-256",
                    value: (
                      <Inline gap={6} align="center">
                        <Text className="sd-mono">{data.store_retired.replacement_sha256}</Text>
                        <CopyButton
                          value={data.store_retired.replacement_sha256}
                          presentation="quiet"
                        />
                      </Inline>
                    ),
                  },
                  {
                    id: "retired",
                    label: "Retired At",
                    value: formatNsTimestamp(data.store_retired.retired_at_ns),
                  },
                ]}
              />
            </div>
          </Stack>
        </Card>
      ) : null}

      <div className="sd-split-equal">
        <Card title="Safe Custody Hold (5-Step Readiness)" variant="raised">
          <Stack gap={12}>
            <TaskList label="Safe Custody Hold checklist" tasks={tasks} />
            {!data.store_retired ? (
              <Text variant="help">Primary store active (no retired store marker).</Text>
            ) : null}
          </Stack>
        </Card>

        <Card
          title="Verified Checkpoint Archives"
          variant="raised"
          className="sd-ledger-card"
        >
          {cpRows.length === 0 ? (
            <Text variant="help">No checkpoints cut yet. Cut one before major charter changes.</Text>
          ) : (
            <DataTable caption="Company checkpoints" columns={cpCols} rows={cpRows} />
          )}
        </Card>
      </div>

      <Card
        title={`Restore Receipts (${data.restore_receipts.length})`}
        description="Read-only historical restore ledger · Restore execution and quarantine release are offline CLI operations"
        variant="raised"
        className="sd-ledger-card"
      >
        {restoreRows.length === 0 ? (
          <Text variant="help">No store restore receipts recorded.</Text>
        ) : (
          <DataTable caption="Restore receipts" columns={restoreCols} rows={restoreRows} />
        )}
      </Card>

      <Card
        title={`Active Restrictions (${data.active_restrictions.length})`}
        variant="raised"
        headerAction={
          <Button
            emphasis="soft"
            size="compact"
            shape="pill"
            onClick={() => navigate("/controls")}
          >
            Manage in Controls
          </Button>
        }
      >
        {data.active_restrictions.length === 0 ? (
          <Text variant="help">No active restrictions blocking company operations.</Text>
        ) : (
          <List label="Active restriction causes" divided>
            {data.active_restrictions.map((c) => (
              <li key={c.restriction_id} className="yg-list-item sd-list-row-padded">
                <Surface variant="surface" density="compact">
                  <Stack gap={4}>
                    <Inline justify="between" align="center" gap={8} wrap>
                      <Inline gap={6} align="center">
                        <Text className="sd-mono">{c.restriction_id}</Text>
                        <Badge tone="warning" dot={false}>
                          {c.cause_category}
                        </Badge>
                        <Text variant="meta">{c.scope_ref}</Text>
                      </Inline>
                      <Badge tone={c.operator_releasable ? "neutral" : "warning"} dot={false}>
                        {c.operator_releasable ? "Operator-releasable" : "Auto-gated"}
                      </Badge>
                    </Inline>
                    <Text variant="body">{c.reason}</Text>
                    <Text variant="meta" className="sd-tabular">
                      Recorded {formatRelativeNs(normalizeToNs(c.recorded_at_ns))} · Rev{" "}
                      {c.restriction_revision}
                    </Text>
                  </Stack>
                </Surface>
              </li>
            ))}
          </List>
        )}
      </Card>
    </Stack>
  );
}

export function SystemEvalsView({ embedded = false }: { embedded?: boolean } = {}) {
  const { refreshSeq, reportApiError } = useApp();
  const [data, setData] = useState<SystemEvalsProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [activeTab, setActiveTab] = useState("qualifications");
  const [evalQuery, setEvalQuery] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const res = await fetchProjection<SystemEvalsProjection>("system-evals", {
          signal: controller.signal,
        });
        if (!active || controller.signal.aborted) return;
        if (res.data) setData(res.data);
        setError(null);
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setError(err instanceof Error ? err.message : "Could not load evaluation state.");
      } finally {
        if (active && !controller.signal.aborted) setLoading(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [refreshSeq, retrySeq, reportApiError]);

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
      recorded: <Text className="sd-tabular">{formatNsTimestamp(q.recorded_at_ns)}</Text>,
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
      sealed: <Text className="sd-tabular">{formatNsTimestamp(s.sealed_at_ns)}</Text>,
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
                            {formatNsTimestamp(act.recorded_at_ns)}
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

const AUDIT_DOMAINS = [
  "all",
  "trading",
  "accounting",
  "assets",
  "org",
  "work",
  "ceo",
  "ops",
  "governance",
  "messages",
] as const;

export function SystemAuditView({
  embedded = false,
  mode,
}: {
  embedded?: boolean;
  mode?: "audit" | "daemon";
} = {}) {
  const { refreshSeq, meta, navigate, reportApiError, registerEscapeBack } = useApp();
  const effectiveDaemon = mode === "daemon";
  const [data, setData] = useState<SystemAuditProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [auditQuery, setAuditQuery] = useState("");
  const [selectedDomain, setSelectedDomain] = useState<string>("all");

  const tradingActive = meta?.active_domains.includes("trading") ?? false;
  const visibleAuditDomains = useMemo(
    () => AUDIT_DOMAINS.filter((d) => d !== "trading" || tradingActive),
    [tradingActive]
  );

  useEffect(() => {
    if (!tradingActive && selectedDomain === "trading") {
      setSelectedDomain("all");
    }
  }, [tradingActive, selectedDomain]);

  useEffect(() => {
    if (!effectiveDaemon || embedded) return;
    return registerEscapeBack("/audit", "system-daemon-entry");
  }, [effectiveDaemon, embedded, registerEscapeBack]);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const res = await fetchProjection<SystemAuditProjection>("system-audit", {
          signal: controller.signal,
        });
        if (!active || controller.signal.aborted) return;
        if (res.data) setData(res.data);
        setError(null);
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setError(err instanceof Error ? err.message : "Could not load audit trail.");
      } finally {
        if (active && !controller.signal.aborted) setLoading(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [refreshSeq, retrySeq, reportApiError]);

  const filteredEvents = useMemo(() => {
    const events = data?.events ?? [];
    const q = auditQuery.trim().toLowerCase();
    return events.filter((e) => {
      if (selectedDomain !== "all" && e.domain !== selectedDomain) return false;
      if (!q) return true;
      return `${e.event_seq} ${e.domain} ${e.event_type} ${e.entity_type} ${e.entity_id}`
        .toLowerCase()
        .includes(q);
    });
  }, [data?.events, selectedDomain, auditQuery]);

  if (loading && !data) return <Skeleton label="Loading audit log and leases" lines={5} />;
  if (!data) {
    return (
      <Result
        title="Audit Log unavailable"
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
        {error ?? "Could not load audit trail."}
      </Result>
    );
  }

  const evById = new Map(filteredEvents.map((e) => [String(e.event_seq), e]));
  const evCols: DataColumn[] = [
    {
      id: "seq",
      label: "Seq",
      kind: "numeric",
      align: "end",
      sortValue: (row) => evById.get(row.id)?.event_seq,
    },
    {
      id: "domain",
      label: "Domain",
      kind: "role",
      sortValue: (row) => evById.get(row.id)?.domain,
    },
    {
      id: "event",
      label: "Event Type",
      kind: "identity",
      sortValue: (row) => evById.get(row.id)?.event_type,
    },
    {
      id: "entity",
      label: "Entity",
      kind: "text",
      sortValue: (row) => {
        const ev = evById.get(row.id);
        return ev ? `${ev.entity_type}:${ev.entity_id}` : "";
      },
    },
    {
      id: "time",
      label: "Recorded",
      kind: "numeric",
      align: "end",
      sortValue: (row) => evById.get(row.id)?.recorded_at_ns,
    },
  ];
  const evRows: DataRow[] = filteredEvents.map((e) => ({
    id: String(e.event_seq),
    cells: {
      seq: <Text className="sd-tabular">#{e.event_seq}</Text>,
      domain: (
        <Badge tone="neutral" variant="outline" dot={false}>
          {e.domain}
        </Badge>
      ),
      event: e.event_type,
      entity: (
        <Text className="sd-mono">
          {e.entity_type}:{e.entity_id}
        </Text>
      ),
      time: <Text className="sd-tabular">{formatNsClock(normalizeToNs(e.recorded_at_ns))}</Text>,
    },
  }));

  const bindById = new Map(data.board_bindings.map((b) => [b.principal_id, b]));
  const bindCols: DataColumn[] = [
    {
      id: "principal",
      label: "Principal",
      kind: "identity",
      sortValue: (row) => bindById.get(row.id)?.principal_id,
    },
    {
      id: "role",
      label: "Role",
      kind: "role",
      sortValue: (row) => bindById.get(row.id)?.role_name,
    },
    {
      id: "scope",
      label: "Scope",
      kind: "text",
      sortValue: (row) => bindById.get(row.id)?.scope_ref,
    },
    {
      id: "rev",
      label: "Revision",
      kind: "numeric",
      align: "end",
      sortValue: (row) => bindById.get(row.id)?.binding_revision,
    },
    {
      id: "active",
      label: "Active",
      kind: "role",
      sortValue: (row) => (bindById.get(row.id)?.active ? 1 : 0),
    },
  ];
  const bindRows: DataRow[] = data.board_bindings.map((b) => ({
    id: b.principal_id,
    cells: {
      principal: <Text className="sd-mono">{b.principal_id}</Text>,
      role: (
        <Badge tone={b.role_name === "board_control" ? "brand" : "neutral"} dot={false}>
          {b.role_name}
        </Badge>
      ),
      scope: b.scope_ref,
      rev: <Text className="sd-tabular">{b.binding_revision}</Text>,
      active: (
        <Badge tone={b.active ? "success" : "neutral"} dot={false}>
          {b.active ? "Active" : "Revoked"}
        </Badge>
      ),
    },
  }));

  const condById = new Map(data.conditions.map((c) => [c.condition_id, c]));
  const condCols: DataColumn[] = [
    {
      id: "condition",
      label: "Condition",
      kind: "identity",
      sortValue: (row) => condById.get(row.id)?.condition_id,
    },
    {
      id: "owner",
      label: "Owner",
      kind: "text",
      sortValue: (row) => condById.get(row.id)?.owner_agent_id,
    },
    {
      id: "trigger",
      label: "Trigger",
      kind: "role",
      sortValue: (row) => condById.get(row.id)?.trigger_kind,
    },
    {
      id: "source",
      label: "Source Identity",
      kind: "text",
      sortValue: (row) => condById.get(row.id)?.source_identity ?? "",
    },
    {
      id: "due",
      label: "Due",
      kind: "numeric",
      align: "end",
      sortValue: (row) => condById.get(row.id)?.due_at_ns ?? null,
    },
    {
      id: "status",
      label: "Status",
      kind: "role",
      sortValue: (row) => condById.get(row.id)?.status,
    },
  ];
  const condRows: DataRow[] = data.conditions.map((c) => ({
    id: c.condition_id,
    cells: {
      condition: <Text className="sd-mono">{c.condition_id}</Text>,
      owner: <Text className="sd-mono">{c.owner_agent_id}</Text>,
      trigger: (
        <Badge tone="neutral" variant="outline" dot={false}>
          {c.trigger_kind}
        </Badge>
      ),
      source: <Text className="sd-mono">{c.source_identity ?? "None"}</Text>,
      due: (
        <Text className="sd-tabular">
          {c.due_at_ns === null ? "Not scheduled" : formatNsTimestamp(c.due_at_ns)}
        </Text>
      ),
      status: (
        <Badge tone={c.status === "armed" ? "brand" : "neutral"} dot={false}>
          {c.status}
        </Badge>
      ),
    },
  }));

  const runtimeCard = meta ? (
    <Card
      title="Control Plane Runtime Identity"
      variant="raised"
      headerAction={<CopyButton value={meta.company_id} presentation="quiet" />}
    >
      <div className="sd-facts-wrap">
        <RecordFacts
          source="GET /v1/meta"
          asOf={`seq #${data.event_seq}`}
          facts={[
            { id: "co", label: "Company ID", value: meta.company_id },
            { id: "ch", label: "Charter Revision", value: meta.charter_revision },
            { id: "pid", label: "Daemon PID", value: String(meta.daemon_pid) },
            { id: "inc", label: "Service Incarnation", value: meta.service_incarnation_id },
            { id: "key", label: "Store Key", value: meta.store_key },
            { id: "ver", label: "Control Plane Version", value: meta.control_plane_version },
            { id: "dom", label: "Installed Domains", value: meta.active_domains.join(", ") },
          ]}
        />
      </div>
    </Card>
  ) : null;

  const bindingsCard = (
    <Card
      title="Board Principal Bindings"
      variant="raised"
      className="sd-ledger-card"
    >
      {bindRows.length === 0 ? (
        <Text variant="help">No principals bound (company is bootstrap-locked).</Text>
      ) : (
        <DataTable caption="Board bindings" columns={bindCols} rows={bindRows} />
      )}
    </Card>
  );

  const fencesCard = (
    <Card title={`Active Work Leases & Fences (${data.active_fences.length})`} variant="raised">
      {data.active_fences.length === 0 ? (
        <Text variant="help">No running or waiting work runs holding active leases.</Text>
      ) : (
        <List label="Active work leases" divided>
          {data.active_fences.map((f) => (
            <li key={f.run_id} className="yg-list-item">
              <WorkEvent
                event={{
                  id: f.run_id,
                  kind: f.status === "done" || f.status === "completed" ? "task" : "routine",
                  title: `${f.duty_kind} · ${f.agent_id}`,
                  detail: `Fence #${f.fencing_token} · Lease: ${f.lease_owner ?? "Unassigned"} · Expires: ${f.lease_expires_at_ns === null ? "No expiry" : `${formatNsClock(normalizeToNs(f.lease_expires_at_ns))} (${formatDeadlineNs(normalizeToNs(f.lease_expires_at_ns))})`}`,
                  time: f.status,
                  state: f.status === "running" ? "running" : "waiting",
                }}
              />
            </li>
          ))}
        </List>
      )}
    </Card>
  );

  const conditionsCard = (
    <Card
      title={`Armed Work Conditions (${data.conditions.length})`}
      variant="raised"
      className="sd-ledger-card"
    >
      {condRows.length === 0 ? (
        <Text variant="help">No armed work conditions registered.</Text>
      ) : (
        <DataTable caption="Armed work conditions" columns={condCols} rows={condRows} />
      )}
    </Card>
  );

  return (
    <Stack gap={16}>
      {embedded ? (
        effectiveDaemon ? (
          <>
            <div className="sd-split-equal">
              {runtimeCard}
              {fencesCard}
            </div>
            {bindingsCard}
            {conditionsCard}
          </>
        ) : null
      ) : effectiveDaemon ? (
        <>
          <Navbar
            variant="back"
            label="Back to Audit"
            onBack={() => navigate("/audit", { focusReturnId: "system-daemon-entry" })}
          />
          <SpaBreadcrumb
            focusReturnId="system-daemon-entry"
            items={[
              { id: "audit", label: "Audit", href: "/audit" },
              { id: "daemon", label: "Daemon Diagnostics", href: "/audit?tab=daemon" },
            ]}
          />
          <PageHeader
            title="Daemon Diagnostics"
            detail="Single-writer UDS runtime identity, installed domains, Board authority bindings, and active fencing leases"
          />
          <div className="sd-split-equal">
            {runtimeCard}
            {fencesCard}
          </div>
          {bindingsCard}
          {conditionsCard}
        </>
      ) : (
        <>
          <PageHeader
            title="Audit"
            detail="Append-only redacted audit stream, armed work conditions, active fencing leases, and Board authority bindings"
            action={
              <Inline gap={8} align="center">
                <Button
                  emphasis="soft"
                  size="compact"
                  shape="pill"
                  data-focus-id="system-daemon-entry"
                  onClick={() =>
                    navigate("/audit?tab=daemon", { focusReturnId: "system-daemon-entry" })
                  }
                >
                  Daemon Diagnostics
                </Button>
              </Inline>
            }
          />
          <div className="sd-split-equal">
            {runtimeCard}
            {fencesCard}
          </div>
          {bindingsCard}
          {conditionsCard}
        </>
      )}

      {!embedded || !effectiveDaemon ? (
        <Card
          title="Append-Only Redacted Audit Events"
          variant="raised"
          className="sd-ledger-card"
        >
          <Stack gap={12}>
            <Inline gap={6} wrap>
              {visibleAuditDomains.map((domain) => (
                <Button
                  key={domain}
                  size="compact"
                  shape="pill"
                  emphasis={selectedDomain === domain ? "filled" : "soft"}
                  onClick={() => setSelectedDomain(domain)}
                >
                  {domain === "all" ? "All" : domain}
                </Button>
              ))}
            </Inline>
            <FilterBar
              label="Filter audit log"
              hiddenLabel
              placeholder="Filter by domain, event type, or entity..."
              size="compact"
              query={auditQuery}
              onQueryChange={setAuditQuery}
            />
            {data.events.length === 0 ? (
              <Text variant="help">No audit events recorded yet.</Text>
            ) : evRows.length === 0 ? (
              <EmptyResult
                query={auditQuery || selectedDomain}
                onClear={() => {
                  setAuditQuery("");
                  setSelectedDomain("all");
                }}
              />
            ) : (
              <DataTable caption="Audit events" columns={evCols} rows={evRows} />
            )}
          </Stack>
        </Card>
      ) : null}
    </Stack>
  );
}

export function AuditHubView({
  initialTab = "log",
}: {
  initialTab?: "log" | "evals" | "daemon";
}) {
  const [tab, setTab] = useState<"log" | "evals" | "daemon">(initialTab);

  useEffect(() => {
    setTab(initialTab);
  }, [initialTab]);

  return (
    <Stack gap={16}>
      <PageHeader
        title="Audit"
        detail="Append-only event log, offline tool evaluations, and single-writer daemon diagnostics"
      />
      <Tabs
        label="Audit sections"
        presentation="pill"
        value={tab}
        onValueChange={(next) => setTab(next as "log" | "evals" | "daemon")}
        items={[
          {
            id: "log",
            label: "Audit Log",
            panel: tab === "log" ? <SystemAuditView embedded mode="audit" /> : null,
          },
          {
            id: "evals",
            label: "Evaluations",
            panel: tab === "evals" ? <SystemEvalsView embedded /> : null,
          },
          {
            id: "daemon",
            label: "Daemon",
            panel: tab === "daemon" ? <SystemAuditView embedded mode="daemon" /> : null,
          },
        ]}
      />
    </Stack>
  );
}

export function BootstrapView() {
  const { meta, controlsLocked, outbox, dispatchCommand } = useApp();
  const [principalId, setPrincipalId] = useState("board-owner-1");
  const [proof, setProof] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const bootstrapFormRef = useRef<HTMLDivElement | null>(null);

  if (!meta) return <Skeleton label="Loading bootstrap status" lines={4} />;

  const challenge = meta.board_access.bootstrap_challenge ?? "";
  const trimmedPrincipal = principalId.trim();
  const trimmedProof = proof.trim();
  const isValidProofHex = /^[0-9a-f]{64}$/i.test(trimmedProof);
  const proofError =
    trimmedProof.length > 0 && !isValidProofHex
      ? "Invalid OwnerProof format: enter a valid 64-character hexadecimal SHA-256 digest."
      : undefined;
  const principalError =
    trimmedPrincipal.length > 128
      ? "Principal ID must be 128 characters or fewer."
      : undefined;
  const isPendingRegister =
    (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
    outbox.intent?.command_type === "register_owner";
  const canProceed = Boolean(
    trimmedPrincipal && trimmedPrincipal.length <= 128 && isValidProofHex
  );

  return (
    <Stack gap={16}>
      <PageHeader
        title="Register Initial Board Owner"
        detail="Bootstrap lock is active until an authenticated local Board owner binds authority"
      />

      <Notice title="Bootstrap Locked" tone="warning" urgent>
        No Board principal is bound to this company yet. Verify the server challenge below and
        compute SHA-256(&quot;&lt;challenge&gt;:&lt;company_id&gt;&quot;) to claim board_control.
      </Notice>

      <Card
        title="Owner Bootstrap Challenge"
        variant="raised"
        headerAction={
          challenge ? <CopyButton value={challenge} presentation="quiet" /> : undefined
        }
      >
        <div ref={bootstrapFormRef}>
        <Stack gap={12}>
          <div className="sd-facts-wrap">
            <RecordFacts
              source="GET /v1/meta (bootstrap_locked)"
              asOf="One-time setup"
              facts={[
                { id: "co", label: "Company ID", value: meta.company_id },
                { id: "ch", label: "OwnerChallenge", value: challenge || "Unavailable" },
              ]}
            />
          </div>

          <TextField
            label="Board Principal ID"
            value={principalId}
            onChange={(e) => setPrincipalId(e.target.value)}
            placeholder="board-owner-1"
            error={principalError}
          />

          <div data-agent-secret="">
            <TextField
              label="OwnerProof (hex SHA-256)"
              value={proof}
              onChange={(e) => setProof(e.target.value)}
              placeholder="Paste computed 64-char hex proof"
              error={proofError}
              help="64-character hex SHA-256 digest. Redacted from agent snapshots via data-agent-secret."
            />
          </div>

          <Inline justify="end">
            <Button
              emphasis="filled"
              tone="brand"
              shape="pill"
              disabled={!canProceed}
              onClick={() => setConfirmOpen(true)}
            >
              Review Owner Binding
            </Button>
          </Inline>

          <ControlledInlineConfirm
            open={confirmOpen}
            surface="inline"
            pending={isPendingRegister}
            onCancel={() => setConfirmOpen(false)}
            decision={{
              id: `register-owner-${meta.company_id}`,
              revision: `bootstrap:${challenge}`,
              title: `Bind ${trimmedPrincipal || "principal"} as Initial Board Owner`,
              consequence:
                "Unlocks Board governance and control actions for this principal and permanently closes one-time bootstrap registration.",
              confirmLabel: "Register Board Owner",
              destructive: false,
              available: (!controlsLocked || isPendingRegister) && canProceed && Boolean(challenge),
              whatChanges: [
                {
                  label: "Board role",
                  value: `${trimmedPrincipal || "principal"} bound as board_control`,
                },
                { label: "Bootstrap gate", value: "Permanently closed" },
              ],
              whatStaysUntouched: [
                { label: "Company charter", value: meta.charter_revision },
                { label: "Store key", value: meta.store_key },
              ],
              technicalDetails: [
                { label: "command_type", value: "register_owner" },
                { label: "contract_version", value: "rbac-command-v1" },
                { label: "company_id", value: meta.company_id },
              ],
            }}
            onConfirm={async () => {
              const lifecycle = await dispatchCommand({
                company_id: meta.company_id,
                command_id: newCommandId("owner"),
                command_type: "register_owner",
                contract_version: "rbac-command-v1",
                preconditions: { bootstrap_state: "locked" },
                payload: {
                  principal_id: trimmedPrincipal,
                  challenge,
                  proof: isValidProofHex ? trimmedProof.toLowerCase() : trimmedProof,
                },
                label: `Register Board owner (${trimmedPrincipal})`,
                sent_ns: Date.now() * 1_000_000,
              });
              if (
                lifecycle === "applied" ||
                lifecycle === "accepted" ||
                lifecycle === "unknown"
              ) {
                setConfirmOpen(false);
              } else if (lifecycle === "denied") {
                const focusForm = () => {
                  const active = document.activeElement;
                  if (active && active !== document.body && bootstrapFormRef.current?.contains(active)) {
                    return;
                  }
                  const target = bootstrapFormRef.current?.querySelector<HTMLElement>(
                    '[data-decision-confirm="open"] [data-agent-confirm]:not([disabled]), [data-decision-confirm="open"] button:not([disabled]), [data-agent-secret] input'
                  );
                  target?.focus();
                };
                focusForm();
                window.requestAnimationFrame(focusForm);
              }
            }}
          />

          <DisclosureRow label="Technical details" initiallyOpen={false}>
            <div className="sd-facts-wrap">
              <RecordFacts
                source="Store identity"
                asOf="Local daemon"
                facts={[
                  { id: "key", label: "Store key", value: meta.store_key },
                  { id: "ch_rev", label: "Charter revision", value: meta.charter_revision },
                ]}
              />
            </div>
          </DisclosureRow>
        </Stack>
        </div>
      </Card>
    </Stack>
  );
}
