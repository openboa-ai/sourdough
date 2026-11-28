import { Fragment, useState } from "react";
import {
  Badge,
  Button,
  Card,
  DataTable,
  Divider,
  Inline,
  List,
  Notice,
  RecordFacts,
  Stack,
  Text,
  WorkEvent,
  type DataColumn,
  type DataRow,
  type Fact,
} from "@openboa/yogurt";
import type { LiveCandidateBindingItem, TradingDeskProjection } from "../../../api/types";
import { ControlledInlineConfirm } from "../../../components/ControlledInlineConfirm";
import {
  formatMinorCurrency,
  formatNsClock,
  formatRelativeNs,
  newCommandId,
  normalizeToNs,
} from "../../../lib/format";
import { useControlPlane, useOutbox, usePreferences } from "../../../state/store";

function formatShortHash(digest: string | null | undefined): string {
  if (!digest) return "Unavailable";
  return digest.length > 16 ? `${digest.slice(0, 12)}…` : digest;
}

function formatTs(ns: number | null | undefined): string {
  const normalized = normalizeToNs(ns);
  if (!normalized) return "Unavailable";
  return `${formatNsClock(normalized)} (${formatRelativeNs(normalized)})`;
}

export interface LiveControlsPanelProps {
  data: TradingDeskProjection;
  charterRevision: string;
}

export function ActiveLiveVenueCard({ data, charterRevision }: LiveControlsPanelProps) {
  const { meta } = useControlPlane();
  const { canWrite, outbox, dispatchCommand } = useOutbox();
  const { pushToast } = usePreferences();
  const [demoteAction, setDemoteAction] = useState<"demote_to_paper" | "suspend_live" | null>(
    null
  );

  const companyLive = data.governance.company_live;
  if (!companyLive) return null;

  const shadow = data.shadow_benchmark;
  const canDemoteCommand =
    canWrite &&
    Boolean(
      meta?.supported_commands?.demote_live_environment?.includes("execution-tier-command-v1")
    );
  const activeLiveMigration =
    data.governance.migration_history.find(
      (m) => m.target_live_binding_id === companyLive.binding_id
    ) ?? null;

  return (
    <Card
      title={`Active Live Venue · ${companyLive.account_id}`}
      description={`${companyLive.destination_id} · ${companyLive.connector_revision} · Credential ${companyLive.credential_handle_ref ?? "host-managed"}`}
      variant="raised"
      density="compact"
      headerAction={
        <Inline gap={8} align="center" wrap>
          {shadow ? (
            <Badge tone="success" variant="soft" dot={false}>
              Shadow Benchmark: {shadow.book_name}
            </Badge>
          ) : null}
          <Button
            emphasis="soft"
            tone="danger"
            size="compact"
            shape="pill"
            disabled={!canDemoteCommand}
            onClick={() =>
              setDemoteAction((cur) =>
                cur === "demote_to_paper" ? null : "demote_to_paper"
              )
            }
          >
            Demote to Paper
          </Button>
          <Button
            emphasis="quiet"
            tone="danger"
            size="compact"
            shape="pill"
            disabled={!canDemoteCommand}
            onClick={() =>
              setDemoteAction((cur) => (cur === "suspend_live" ? null : "suspend_live"))
            }
          >
            Suspend Live
          </Button>
        </Inline>
      }
    >
      <Stack gap={8}>
        {activeLiveMigration ? (
          <Inline gap={8} align="center" wrap>
            <Text variant="help">
              Migrated from {activeLiveMigration.source_book_name} (
              {activeLiveMigration.migration_mode} · 0 virtual positions copied)
            </Text>
            {shadow ? (
              <Badge
                tone={Math.abs(shadow.execution_drift_pp) <= 0.25 ? "success" : "warning"}
                variant="outline"
                dot={false}
              >
                Live vs Shadow Drift {shadow.execution_drift_pp > 0 ? "+" : ""}
                {shadow.execution_drift_pp.toFixed(2)} pp
              </Badge>
            ) : null}
          </Inline>
        ) : null}

        {meta && demoteAction !== null ? (
          <ControlledInlineConfirm
            open
            surface="inline"
            pending={
              (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
              outbox.intent?.command_type === "demote_live_environment"
            }
            onCancel={() => setDemoteAction(null)}
            decision={{
              id: `demote-${companyLive.binding_id}-${demoteAction}`,
              revision: `${companyLive.binding_id}:${demoteAction}`,
              title:
                demoteAction === "demote_to_paper"
                  ? `Demote ${companyLive.account_id} back to Paper Mode`
                  : `Emergency Suspend ${companyLive.account_id}`,
              consequence:
                demoteAction === "demote_to_paper"
                  ? "Returns the live venue binding to registered standby and halts external real-capital execution while keeping Paper Trading running."
                  : "Immediately suspends live venue execution and records an operator safety hold.",
              confirmLabel:
                demoteAction === "demote_to_paper"
                  ? "Confirm Demote to Paper"
                  : "Confirm Emergency Suspend",
              destructive: true,
              available: canDemoteCommand,
              requireReason: true,
              defaultReason:
                demoteAction === "demote_to_paper"
                  ? "Board returning execution to paper standby mode"
                  : "Emergency Board suspension of live venue execution",
              whatChanges: [
                {
                  label: "Live Venue Status",
                  value:
                    demoteAction === "demote_to_paper"
                      ? "active → registered (paper standby)"
                      : "active → suspended (safety hold)",
                },
              ],
              whatStaysUntouched: [
                {
                  label: "Paper Books & Audit History",
                  value: "Company paper books and historical live fills remain intact",
                },
              ],
              technicalDetails: [
                { label: "command_type", value: "demote_live_environment" },
                { label: "target_binding_id", value: companyLive.binding_id },
                { label: "action", value: demoteAction },
                { label: "charter_revision", value: charterRevision || "Unavailable" },
              ],
            }}
            onConfirm={async ({ reason }) => {
              const lifecycle = await dispatchCommand({
                company_id: meta.company_id,
                command_id: newCommandId("demote-live"),
                command_type: "demote_live_environment",
                contract_version: "execution-tier-command-v1",
                preconditions: {
                  charter_revision: charterRevision,
                  target_binding_id: companyLive.binding_id,
                },
                payload: {
                  action: demoteAction,
                  reason,
                },
                label:
                  demoteAction === "demote_to_paper"
                    ? `Demote ${companyLive.account_id} to paper`
                    : `Suspend ${companyLive.account_id}`,
                sent_ns: Date.now() * 1_000_000,
              });
              if (lifecycle === "applied") {
                setDemoteAction(null);
                pushToast(
                  demoteAction === "demote_to_paper"
                    ? "Returned execution to Paper Standby Mode."
                    : "Suspended live venue execution."
                );
              }
            }}
          />
        ) : null}
      </Stack>
    </Card>
  );
}

export function LiveControlsPanel({ data, charterRevision }: LiveControlsPanelProps) {
  const { meta } = useControlPlane();
  const { canWrite, outbox, dispatchCommand } = useOutbox();
  const { pushToast } = usePreferences();
  const [promoteTargetId, setPromoteTargetId] = useState<string | null>(null);

  const liveActive = data.governance.company_live !== null;
  const canPromoteCommand =
    canWrite &&
    Boolean(
      meta?.supported_commands?.promote_execution_environment?.includes(
        "execution-tier-command-v1"
      )
    );

  // Live-only Qualification Receipts table
  const qualCols: DataColumn[] = [
    { id: "receipt", label: "Receipt ID", kind: "identity" },
    { id: "adapter", label: "Adapter & Destination", kind: "text" },
    { id: "verdict", label: "Verdict", kind: "role" },
    { id: "cases", label: "Cases (Pass / Fail / Unrun)", kind: "numeric", align: "end" },
    { id: "core", label: "Core Digest", kind: "text" },
    { id: "recorded", label: "Recorded", kind: "numeric", align: "end" },
  ];

  const qualRows: DataRow[] = data.governance.qualification_receipts.map((q) => {
    const coreMatches = q.core_digest === data.governance.current_core_digest;
    const isPass = q.verdict === "PASS" && q.cases_failed === 0 && q.cases_unrun === 0;
    return {
      id: q.receipt_id,
      cells: {
        receipt: <Text className="sd-mono">{q.receipt_id}</Text>,
        adapter: (
          <Stack gap={2}>
            <Text variant="body">{q.adapter_revision}</Text>
            <Text variant="meta">
              {q.environment} · {q.destination_id}
            </Text>
          </Stack>
        ),
        verdict: (
          <Badge tone={isPass ? "success" : "danger"} dot={false}>
            {q.verdict}
          </Badge>
        ),
        cases: (
          <Text className="sd-tabular">
            {q.cases_passed} / {q.cases_failed} / {q.cases_unrun}
          </Text>
        ),
        core: (
          <Inline gap={6} align="center">
            <Badge tone={coreMatches ? "success" : "warning"} variant="soft" dot={false}>
              {coreMatches ? "Current Core" : "Stale Core"}
            </Badge>
            <Text variant="meta" className="sd-mono">
              {formatShortHash(q.core_digest)}
            </Text>
          </Inline>
        ),
        recorded: (
          <Text variant="meta" className="sd-tabular">
            {formatTs(q.recorded_at_ns)}
          </Text>
        ),
      },
    };
  });

  const renderCandidateRow = (cand: LiveCandidateBindingItem, idx: number) => {
    const hasQual = Boolean(cand.matching_qualification_receipt_id && cand.promotion_payload_hash);
    const allGatesGreen =
      data.governance.paper_soak_ready &&
      hasQual &&
      !liveActive &&
      canPromoteCommand;
    const isConfirmOpen = promoteTargetId === cand.binding_id;
    const isSubmitting =
      (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
      outbox.intent?.command_type === "promote_execution_environment" &&
      outbox.intent?.preconditions?.target_binding_id === cand.binding_id;

    const prereqFacts: Fact[] = [
      {
        id: "soak",
        label: "Paper Soak Readiness",
        value: (
          <Badge
            tone={data.governance.paper_soak_ready ? "success" : "warning"}
            dot={false}
          >
            {data.governance.paper_soak_ready ? "Verified" : "Pending paper fills"}
          </Badge>
        ),
      },
      {
        id: "qual",
        label: "Venue Qualification",
        value: hasQual ? (
          <Badge tone="success" dot={false}>
            {cand.matching_qualification_receipt_id}
          </Badge>
        ) : (
          <Badge tone="warning" dot={false}>
            No PASS receipt for current core
          </Badge>
        ),
      },
      {
        id: "cred",
        label: "Host Credential Handle",
        value: <Text className="sd-mono">{cand.credential_handle_ref ?? "Unbound"}</Text>,
      },
      {
        id: "cap",
        label: "Exposure Ceiling",
        value: (
          <Text className="sd-tabular">
            {formatMinorCurrency(cand.exposure_limit_minor_units, cand.currency)}
          </Text>
        ),
      },
    ];

    return (
      <Fragment key={cand.binding_id}>
        {idx > 0 ? <Divider /> : null}
        <Stack gap={12}>
          <Inline justify="between" align="center" gap={8} wrap>
            <Stack gap={2}>
              <Inline gap={8} align="center" wrap>
                <Text variant="body">{cand.account_id}</Text>
                <Badge tone="neutral" variant="outline" dot={false}>
                  {cand.status}
                </Badge>
                <Text variant="meta" className="sd-mono">
                  {cand.binding_id}
                </Text>
              </Inline>
              <Text variant="help">
                {cand.connector_revision} · {cand.destination_id} · Rules{" "}
                {cand.instrument_rules_digest}
              </Text>
            </Stack>
            <Button
              emphasis={allGatesGreen ? "filled" : "soft"}
              tone={allGatesGreen ? "brand" : "neutral"}
              size="compact"
              shape="pill"
              onClick={() =>
                setPromoteTargetId((cur) => (cur === cand.binding_id ? null : cand.binding_id))
              }
            >
              {isConfirmOpen ? "Close Promotion Gate" : "Review Live Promotion"}
            </Button>
          </Inline>

          <div className="sd-facts-wrap">
            <RecordFacts
              source="execution_environment_binding"
              asOf={`seq #${data.event_seq}`}
              facts={prereqFacts}
            />
          </div>

          {meta ? (
            <ControlledInlineConfirm
              open={isConfirmOpen}
              surface="inline"
              pending={isSubmitting}
              onCancel={() => setPromoteTargetId(null)}
              decision={{
                id: `promote-${cand.binding_id}`,
                revision: `${cand.binding_id}:${cand.matching_qualification_receipt_id ?? "none"}:${cand.promotion_payload_hash ?? "none"}`,
                title: `Promote ${cand.account_id} to Live Trading`,
                consequence:
                  "Activates real-capital external venue execution under this binding's exposure ceiling and host credential handle. Starts from a clean 0% live baseline with 0 virtual positions copied.",
                confirmLabel: "Confirm Promote to Live",
                destructive: false,
                available: allGatesGreen,
                unavailableNotice: !allGatesGreen ? (
                  <Notice title="Live promotion gate locked" tone="warning">
                    {liveActive
                      ? "Another live venue binding is already active. Demote the current live binding before promoting a replacement."
                      : !data.governance.paper_soak_ready
                        ? "Company paper trading must record fills or valuations before promoting to live."
                        : !hasQual
                          ? "A passing venue qualification receipt matching this connector revision, instrument rules, and current trading core digest is required."
                          : "Board Control authority is required to activate live trading."}
                  </Notice>
                ) : undefined,
                requireReason: true,
                defaultReason: `Board approved live promotion for ${cand.account_id}`,
                whatChanges: [
                  {
                    label: "Active Live Venue",
                    value: `${cand.account_id} (${cand.destination_id}) transitions to active`,
                  },
                  {
                    label: "Exposure Cap",
                    value: formatMinorCurrency(cand.exposure_limit_minor_units, cand.currency),
                  },
                ],
                whatStaysUntouched: [
                  {
                    label: "Paper Books & Agent Sandboxes",
                    value: "Simulated accounts remain strictly isolated from real funds",
                  },
                  {
                    label: "Host API Secrets",
                    value: `Resolved only in memory via ${cand.credential_handle_ref ?? "host handle"}`,
                  },
                ],
                technicalDetails: [
                  { label: "command_type", value: "promote_execution_environment" },
                  { label: "target_binding_id", value: cand.binding_id },
                  {
                    label: "qualification_receipt_id",
                    value: cand.matching_qualification_receipt_id ?? "Unavailable",
                  },
                  {
                    label: "expected_payload_hash",
                    value: cand.promotion_payload_hash ?? "Unavailable",
                  },
                  { label: "charter_revision", value: charterRevision || "Unavailable" },
                ],
              }}
              onConfirm={async ({ reason }) => {
                if (!cand.matching_qualification_receipt_id || !cand.promotion_payload_hash) {
                  return;
                }
                const lifecycle = await dispatchCommand({
                  company_id: meta.company_id,
                  command_id: newCommandId("promote-live"),
                  command_type: "promote_execution_environment",
                  contract_version: "execution-tier-command-v1",
                  preconditions: {
                    charter_revision: charterRevision,
                    target_binding_id: cand.binding_id,
                    qualification_receipt_id: cand.matching_qualification_receipt_id,
                    expected_payload_hash: cand.promotion_payload_hash,
                  },
                  payload: { reason },
                  label: `Promote ${cand.account_id} to live`,
                  sent_ns: Date.now() * 1_000_000,
                });
                if (lifecycle === "applied") {
                  setPromoteTargetId(null);
                  pushToast(`Promoted ${cand.account_id} to Live Trading.`);
                }
              }}
            />
          ) : null}
        </Stack>
      </Fragment>
    );
  };

  return (
    <Stack gap={16}>
      <Card
        title="Live Venue Candidates & Promotion Gate"
        description="Fail-closed prerequisites checked server-side before activating real-capital external execution"
        variant="raised"
      >
        {data.governance.live_candidates.length === 0 ? (
          <Text variant="help">
            No candidate live venue bindings registered yet. Register a live candidate with a
            host credential handle and run venue qualification first.
          </Text>
        ) : (
          <Stack gap={12}>
            {data.governance.live_candidates.map((cand, idx) =>
              renderCandidateRow(cand, idx)
            )}
          </Stack>
        )}
      </Card>

      <Card
        title="Venue Qualification Receipts"
        description="Signed wire-harness verification receipts tied to connector revision, instrument rules, and trading core digest"
        variant="raised"
        className="sd-ledger-card"
        headerAction={
          <Badge tone="neutral" variant="soft" dot={false}>
            {qualRows.length} {qualRows.length === 1 ? "receipt" : "receipts"}
          </Badge>
        }
      >
        {qualRows.length === 0 ? (
          <Text variant="help">No venue qualification receipts recorded yet.</Text>
        ) : (
          <DataTable
            caption="Venue qualification receipts"
            columns={qualCols}
            rows={qualRows}
          />
        )}
      </Card>

      {data.governance.migration_history.length > 0 ? (
        <Card
          title="Paper-to-Live Migration Ledger"
          description="Immutable audit trail of Board-approved migrations from Company Paper Books to Live venues (always 0 virtual positions copied)"
          variant="raised"
        >
          <List label="Paper-to-Live migrations" divided>
            {data.governance.migration_history.map((mig) => (
              <li key={mig.migration_id} className="yg-list-item">
                <WorkEvent
                  event={{
                    id: mig.migration_id,
                    kind: "review",
                    title: `${mig.source_book_name} → ${mig.target_account_id} (${mig.migration_mode})`,
                    detail: `${mig.reason} · source ${mig.source_paper_disposition} · ${mig.positions_copied} virtual positions copied · receipt ${mig.qualification_receipt_id}`,
                    time: formatTs(mig.recorded_at_ns),
                    state: "done",
                  }}
                />
              </li>
            ))}
          </List>
        </Card>
      ) : null}

      <Card
        title="Promotion & Demotion Ledger"
        description="Immutable Board decisions for live environment activation, demotion, and emergency suspension"
        variant="raised"
      >
        {data.governance.promotion_history.length === 0 ? (
          <Text variant="help">
            No environment promotions or demotions recorded yet. The company remains in
            paper standby mode.
          </Text>
        ) : (
          <List label="Environment promotion ledger" divided>
            {data.governance.promotion_history.map((rec) => (
              <li key={rec.promotion_id} className="yg-list-item">
                <WorkEvent
                  event={{
                    id: rec.promotion_id,
                    kind: "review",
                    title: `${rec.action} · ${rec.to_binding_id}`,
                    detail: `${rec.reason}${rec.qualification_receipt_id ? ` · receipt ${rec.qualification_receipt_id}` : ""} · hash ${formatShortHash(rec.payload_hash)}`,
                    time: formatTs(rec.recorded_at_ns),
                    state: rec.action === "promote_live" ? "done" : "failed",
                  }}
                />
              </li>
            ))}
          </List>
        )}
      </Card>
    </Stack>
  );
}
