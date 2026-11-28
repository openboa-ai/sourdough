import { useMemo } from "react";
import { CopyButton } from "@openboa/yogurt/motion-copy-button";
import {
  Badge,
  Card,
  DataTable,
  DisclosureRow,
  Inline,
  Notice,
  Stack,
  Surface,
  Text,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import type { AgentActiveMethodItem } from "../../../api/types";

export interface AgentMethodsTabProps {
  activeMethods: AgentActiveMethodItem[];
  activeSnapshotId: string | null;
}

export function AgentMethodsTab({
  activeMethods,
  activeSnapshotId,
}: AgentMethodsTabProps) {
  const methodById = useMemo(
    () =>
      new Map(
        activeMethods.map((m) => [
          m.delta_id ?? `${m.method_id}:${m.revision_sha256}`,
          m,
        ])
      ),
    [activeMethods]
  );

  const cols: DataColumn[] = useMemo(
    () => [
      {
        id: "method",
        label: "Method / Skill",
        kind: "identity",
        sortValue: (row) => methodById.get(row.id)?.method_id,
      },
      {
        id: "scope",
        label: "Audience Scope",
        kind: "role",
        sortValue: (row) => methodById.get(row.id)?.audience_scope,
      },
      {
        id: "kind",
        label: "Kind",
        kind: "role",
        sortValue: (row) =>
          methodById.get(row.id)?.item_kind ?? methodById.get(row.id)?.target_kind ?? "",
      },
      {
        id: "content",
        label: "Disclosure",
        kind: "text",
      },
      {
        id: "seq",
        label: "Activation",
        kind: "numeric",
        align: "end",
        sortValue: (row) => methodById.get(row.id)?.activation_seq ?? 0,
      },
    ],
    [methodById]
  );

  const rows: DataRow[] = useMemo(
    () =>
      activeMethods.map((m) => {
        const key = m.delta_id ?? `${m.method_id}:${m.revision_sha256}`;
        const isPrivate =
          m.audience_scope === "one_agent" ||
          Boolean(m.body_redacted) ||
          Boolean(m.method_id_redacted);
        return {
          id: key,
          cells: {
            method: (
              <Stack gap={2}>
                <Inline gap={6} align="center">
                  <Text className="sd-mono">{m.method_id}</Text>
                  <CopyButton value={key} presentation="quiet" />
                </Inline>
                <Text variant="help" className="sd-mono">
                  rev {m.revision_sha256.slice(0, 12)}
                </Text>
              </Stack>
            ),
            scope: (
              <Badge
                tone={isPrivate ? "neutral" : "brand"}
                variant={isPrivate ? "soft" : "outline"}
                dot={false}
              >
                {isPrivate ? "one_agent · redacted" : m.audience_scope}
              </Badge>
            ),
            kind: (
              <Text variant="body">{m.item_kind ?? m.target_kind ?? "work_method"}</Text>
            ),
            content: isPrivate ? (
              <Stack gap={2}>
                <Text variant="help">Private agent-only method body redacted.</Text>
                {m.body_digest ? (
                  <Text variant="help" className="sd-mono">
                    digest {m.body_digest.slice(0, 16)}
                  </Text>
                ) : null}
              </Stack>
            ) : m.body_text ? (
              <DisclosureRow label="Shared method body" initiallyOpen={false}>
                <Surface variant="surface" density="compact">
                  <Text as="p" className="sd-mono sd-agent-pre">
                    {m.body_text}
                  </Text>
                </Surface>
              </DisclosureRow>
            ) : (
              <Text variant="help">Metadata only</Text>
            ),
            seq: (
              <Text className="sd-tabular">
                {m.activation_seq !== undefined ? `#${m.activation_seq}` : "—"}
              </Text>
            ),
          },
        };
      }),
    [activeMethods]
  );

  return (
    <Stack gap={12}>
      <Notice title="Active Skill & Method Snapshot" tone="neutral">
        <Inline gap={8} align="center" wrap>
          <Text variant="meta">Snapshot ID:</Text>
          <Text className="sd-mono">{activeSnapshotId ?? "Base harness (no active methods)"}</Text>
          {activeSnapshotId ? (
            <CopyButton value={activeSnapshotId} presentation="quiet" />
          ) : null}
        </Inline>
      </Notice>

      <Card
        title={`Active Methods (${activeMethods.length})`}
        description="Qualified methods currently projected into this Agent's harness (one_agent bodies and raw identifiers remain redacted)"
        variant="raised"
        className="sd-ledger-card"
      >
        {rows.length === 0 ? (
          <Text variant="help">No qualified methods are currently active for this agent.</Text>
        ) : (
          <DataTable caption="Active agent methods" columns={cols} rows={rows} />
        )}
      </Card>
    </Stack>
  );
}
