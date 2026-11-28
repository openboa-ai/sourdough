import { useState } from "react";
import { CopyButton } from "@openboa/yogurt/motion-copy-button";
import { TaskList } from "@openboa/yogurt/motion-task-list";
import {
  Badge,
  Button,
  Card,
  DataTable,
  Inline,
  List,
  Notice,
  PageHeader,
  RecordFacts,
  Result,
  Skeleton,
  Stack,
  Surface,
  Text,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import type { SystemSafetyProjection } from "../../api/types";
import { useProjection } from "../../api/useProjection";
import { ControlledInlineConfirm } from "../../components/ControlledInlineConfirm";
import { formatNsWithClock, formatRelativeNs, newCommandId } from "../../lib/format";
import { useControlPlane, useOutbox, useRouter } from "../../state/store";

function formatWatermark(wm: Record<string, unknown> | null | undefined): string {
  if (!wm || Object.keys(wm).length === 0) return "None";
  return Object.entries(wm)
    .map(([k, v]) => `${k}: ${typeof v === "object" && v !== null ? JSON.stringify(v) : String(v)}`)
    .join(" · ");
}

export function SystemSafetyView() {
  const { navigate } = useRouter();
  const { meta } = useControlPlane();
  const { canWrite, outbox, dispatchCommand } = useOutbox();
  const { data, loading, error, refetch } =
    useProjection<SystemSafetyProjection>("system-safety");
  const [cutConfirmOpen, setCutConfirmOpen] = useState(false);

  if (loading && !data) return <Skeleton label="Loading safety and checkpoints" lines={5} />;
  if (!data) {
    return (
      <Result
        title="Safety & Restore unavailable"
        action={
          <Button emphasis="soft" shape="pill" onClick={refetch}>
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
      modified: <Text className="sd-tabular">{formatRelativeNs(cp.modified_ns)}</Text>,
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
      restored: <Text className="sd-tabular">{formatNsWithClock(r.restored_at_ns)}</Text>,
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
                    value: formatNsWithClock(data.store_retired.retired_at_ns),
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
                      Recorded {formatRelativeNs(c.recorded_at_ns)} · Rev{" "}
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
