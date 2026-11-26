import { Fragment, useEffect, useState } from "react";
import {
  Badge,
  Button,
  Card,
  DataTable,
  Divider,
  Inline,
  List,
  Notice,
  PageHeader,
  RecordFacts,
  Result,
  Skeleton,
  Stack,
  Status,
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
  ExecutionTiersProjection,
  LiveCandidateBindingItem,
} from "../api/types";
import { ControlledInlineConfirm } from "../components/ControlledInlineConfirm";
import { useApp } from "../state/store";

function formatShortHash(digest: string | null | undefined): string {
  if (!digest) return "Unavailable";
  return digest.length > 16 ? `${digest.slice(0, 12)}…` : digest;
}

function formatTs(ns: number | null | undefined): string {
  if (!ns || !Number.isFinite(ns)) return "Unavailable";
  return `${formatNsClock(ns)} (${formatRelativeNs(ns)})`;
}

export function ExecutionTiersView() {
  const {
    navigate,
    refreshSeq,
    meta,
    canWrite,
    outbox,
    dispatchCommand,
    reportApiError,
    pushToast,
  } = useApp();

  const [data, setData] = useState<ExecutionTiersProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [promoteTargetId, setPromoteTargetId] = useState<string | null>(null);
  const [demoteAction, setDemoteAction] = useState<"demote_to_paper" | "suspend_live" | null>(
    null
  );

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const res = await fetchProjection<ExecutionTiersProjection>("execution-tiers", {
          signal: controller.signal,
        });
        if (!active || controller.signal.aborted) return;
        if (res.data) setData(res.data);
        setError(null);
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setError(err instanceof Error ? err.message : "Failed to load execution tiers");
      } finally {
        if (active && !controller.signal.aborted) setLoading(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [refreshSeq, retrySeq, reportApiError]);

  if (loading && !data) {
    return (
      <Stack gap={16}>
        <PageHeader
          title="Execution Tiers & Live Promotion"
          detail="Loading Tier 1 agent sandboxes, Tier 2 company paper, and Tier 3 live gate..."
        />
        <Skeleton label="Loading execution tiers" lines={5} />
      </Stack>
    );
  }

  if (!data) {
    return (
      <Result
        title="Execution tiers unavailable"
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
        {error ?? "Waiting for local control plane snapshot."}
      </Result>
    );
  }

  const canPromoteCommand =
    canWrite &&
    Boolean(
      meta?.supported_commands?.promote_execution_environment?.includes(
        "execution-tier-command-v1"
      )
    );
  const canDemoteCommand =
    canWrite &&
    Boolean(
      meta?.supported_commands?.demote_live_environment?.includes("execution-tier-command-v1")
    );
  const charterRevision = data.charter_revision || meta?.charter_revision || "";
  const totalSandboxFills = data.agent_sandboxes.reduce((acc, s) => acc + s.fill_count, 0);
  const liveActive = data.company_live !== null;

  const sandboxCols: DataColumn[] = [
    { id: "agent", label: "Agent", kind: "identity" },
    { id: "account", label: "Sandbox Account", kind: "text" },
    { id: "cash", label: "Virtual Cash", kind: "numeric", align: "end" },
    { id: "exposure", label: "Gross Exposure", kind: "numeric", align: "end" },
    { id: "pnl", label: "Realized P&L", kind: "numeric", align: "end" },
    { id: "terms", label: "Fee / Slippage", kind: "numeric", align: "end" },
    { id: "activity", label: "Fills / Open Pos", kind: "numeric", align: "end" },
    { id: "action", label: "Inspect", kind: "actions", align: "end" },
  ];

  const sandboxRows: DataRow[] = data.agent_sandboxes.map((sbx) => ({
    id: sbx.binding_id,
    cells: {
      agent: (
        <Stack gap={2}>
          <Text variant="body">{sbx.agent_display_name ?? sbx.agent_id}</Text>
          <Text variant="meta">{sbx.agent_role_label ?? sbx.agent_id}</Text>
        </Stack>
      ),
      account: (
        <Stack gap={2}>
          <Text variant="body" className="sd-mono">
            {sbx.account_id}
          </Text>
          <Text variant="meta">{sbx.quote_source}</Text>
        </Stack>
      ),
      cash: (
        <Text className="sd-tabular">
          {formatMinorCurrency(sbx.cash_minor_units, sbx.currency)}
        </Text>
      ),
      exposure: (
        <Text className="sd-tabular">
          {formatMinorCurrency(sbx.gross_exposure_minor_units, sbx.currency)}
        </Text>
      ),
      pnl: (
        <Text
          className={`sd-tabular ${
            sbx.realized_pnl_minor_units > 0
              ? "sd-tone-pos"
              : sbx.realized_pnl_minor_units < 0
                ? "sd-tone-neg"
                : ""
          }`}
        >
          {formatMinorCurrency(sbx.realized_pnl_minor_units, sbx.currency, { signed: true })}
        </Text>
      ),
      terms: (
        <Text variant="help" className="sd-tabular">
          {sbx.fee_bps} bps / {sbx.slippage_bps} bps
        </Text>
      ),
      activity: (
        <Text variant="help" className="sd-tabular">
          {sbx.fill_count} fills · {sbx.open_positions_count} open
        </Text>
      ),
      action: (
        <Button
          emphasis="quiet"
          size="compact"
          shape="pill"
          onClick={() => navigate(`/office/agents/${encodeURIComponent(sbx.agent_id)}`)}
        >
          Agent Desk
        </Button>
      ),
    },
  }));

  const qualCols: DataColumn[] = [
    { id: "receipt", label: "Receipt ID", kind: "identity" },
    { id: "adapter", label: "Adapter & Destination", kind: "text" },
    { id: "verdict", label: "Verdict", kind: "role" },
    { id: "cases", label: "Cases (Pass / Fail / Unrun)", kind: "numeric", align: "end" },
    { id: "core", label: "Core Digest Match", kind: "text" },
    { id: "recorded", label: "Recorded", kind: "numeric", align: "end" },
  ];

  const qualRows: DataRow[] = data.qualification_receipts.map((q) => {
    const coreMatches = q.core_digest === data.current_core_digest;
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
      data.company_paper_soak_ready && hasQual && !liveActive && canPromoteCommand;
    const isConfirmOpen = promoteTargetId === cand.binding_id;
    const isSubmitting =
      (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
      outbox.intent?.command_type === "promote_execution_environment" &&
      outbox.intent?.preconditions?.target_binding_id === cand.binding_id;

    const prereqFacts: Fact[] = [
      {
        id: "soak",
        label: "Tier 2 Company Paper Soak",
        value: (
          <Badge tone={data.company_paper_soak_ready ? "success" : "warning"} dot={false}>
            {data.company_paper_soak_ready ? "Verified" : "Missing paper fills/valuations"}
          </Badge>
        ),
      },
      {
        id: "qual",
        label: "Matching PASS Qualification",
        value: hasQual ? (
          <Badge tone="success" dot={false}>
            {cand.matching_qualification_receipt_id}
          </Badge>
        ) : (
          <Badge tone="warning" dot={false}>
            No matching PASS receipt for current core
          </Badge>
        ),
      },
      {
        id: "cred",
        label: "Host Credential Handle",
        value: (
          <Text className="sd-mono">{cand.credential_handle_ref ?? "Unbound"}</Text>
        ),
      },
      {
        id: "cap",
        label: "Exposure Cap",
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
                title: `Promote ${cand.account_id} to Tier 3 Company Live`,
                consequence:
                  "Activates real-capital external venue execution under this binding's exposure ceiling and host credential handle. Requires Board Control authority and verified qualification.",
                confirmLabel: "Confirm Promote to Live",
                destructive: false,
                available: allGatesGreen,
                unavailableNotice: !allGatesGreen ? (
                  <Notice title="Live promotion gate locked" tone="warning">
                    {liveActive
                      ? "Another Tier 3 live binding is already active. Demote the current live binding before promoting a replacement."
                      : !data.company_paper_soak_ready
                        ? "Tier 2 company_paper must record fills, valuations, or retained statements before promoting to live."
                        : !hasQual
                          ? "A passing venue_qualification_receipt matching this connector revision, instrument rules, and current trading core digest is required."
                          : "Board Control authority is required to activate Tier 3 company_live."}
                  </Notice>
                ) : undefined,
                requireReason: true,
                defaultReason: `Board approved Tier 3 live promotion for ${cand.account_id}`,
                whatChanges: [
                  {
                    label: "Active Tier 3 Live",
                    value: `${cand.account_id} (${cand.destination_id}) transitions to active`,
                  },
                  {
                    label: "Exposure Cap",
                    value: formatMinorCurrency(cand.exposure_limit_minor_units, cand.currency),
                  },
                ],
                whatStaysUntouched: [
                  {
                    label: "Tier 1 Agent Sandboxes",
                    value: "Personal per-Agent simulated accounts stay strictly isolated",
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
                  pushToast(`Promoted ${cand.account_id} to Tier 3 company_live.`);
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
      <PageHeader
        title="Execution Tiers & Live Promotion"
        detail={`Three-tier isolation · Charter ${charterRevision || "unknown"} · seq #${data.event_seq}`}
        action={
          <Inline gap={8} align="center" wrap>
            <Status tone={liveActive ? "brand" : "success"}>
              {liveActive ? "Tier 3 Live Active" : "Paper-Only Safe Mode"}
            </Status>
            <Button
              emphasis="quiet"
              size="compact"
              shape="pill"
              onClick={() => navigate("/")}
            >
              Live Trading
            </Button>
          </Inline>
        }
      />

      <div className="sd-tier-grid">
        <Card
          variant="raised"
          density="compact"
          title="Tier 1 · Agent Sandboxes"
          description="Isolated per-Agent virtual testing accounts"
          headerAction={
            <Badge tone="neutral" variant="soft" dot={false}>
              {data.agent_sandboxes.length} active
            </Badge>
          }
        >
          <Stack gap={8}>
            <Text className="sd-kpi-value sd-tabular">
              {totalSandboxFills} {totalSandboxFills === 1 ? "fill" : "fills"}
            </Text>
            <Text variant="help">
              Each Agent tests strategies privately with configurable fee &amp; slippage bps.
              Zero access to company paper or real funds.
            </Text>
          </Stack>
        </Card>

        <Card
          variant="raised"
          density="compact"
          title="Tier 2 · Company Paper"
          description="Shared company-wide simulated operations"
          headerAction={
            <Badge
              tone={data.company_paper ? (data.company_paper_soak_ready ? "success" : "warning") : "neutral"}
              variant="soft"
              dot={false}
            >
              {data.company_paper
                ? data.company_paper_soak_ready
                  ? "Soak Ready"
                  : "Soak Pending"
                : "Unbound"}
            </Badge>
          }
        >
          {data.company_paper ? (
            <Stack gap={6}>
              <Text className="sd-kpi-value sd-tabular">
                {formatMinorCurrency(
                  data.company_paper.starting_minor_units,
                  data.company_paper.currency
                )}
              </Text>
              <Text variant="help" className="sd-tabular">
                {data.company_paper.account_id} · Cap{" "}
                {formatMinorCurrency(
                  data.company_paper.exposure_limit_minor_units,
                  data.company_paper.currency
                )}{" "}
                · {data.company_paper.fee_bps} bps fee / {data.company_paper.slippage_bps} bps slip
              </Text>
            </Stack>
          ) : (
            <Text variant="help">
              No company paper binding registered yet. Bind Tier 2 company paper before live
              promotion.
            </Text>
          )}
        </Card>

        <Card
          variant="raised"
          density="compact"
          title="Tier 3 · Company Live"
          description="Board-gated external venue production capital"
          headerAction={
            <Badge tone={liveActive ? "brand" : "neutral"} variant="outline" dot={false}>
              {liveActive ? "Active" : "Gated"}
            </Badge>
          }
        >
          {data.company_live ? (
            <Stack gap={8}>
              <Text className="sd-kpi-value sd-tabular">
                {formatMinorCurrency(
                  data.company_live.exposure_limit_minor_units,
                  data.company_live.currency
                )}
              </Text>
              <Text variant="help">
                {data.company_live.account_id} · {data.company_live.destination_id}
              </Text>
              <Inline gap={8} wrap>
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
                    setDemoteAction((cur) =>
                      cur === "suspend_live" ? null : "suspend_live"
                    )
                  }
                >
                  Suspend Live
                </Button>
              </Inline>
            </Stack>
          ) : (
            <Stack gap={6}>
              <Text className="sd-kpi-value">Locked</Text>
              <Text variant="help">
                {data.live_candidates.length} candidate{" "}
                {data.live_candidates.length === 1 ? "binding" : "bindings"} registered. Explicit
                Board promotion required.
              </Text>
            </Stack>
          )}
        </Card>
      </div>

      {data.company_live && demoteAction && meta ? (
        <ControlledInlineConfirm
          open={Boolean(demoteAction)}
          pending={
            (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
            outbox.intent?.command_type === "demote_live_environment"
          }
          onCancel={() => setDemoteAction(null)}
          decision={{
            id: `demote-${data.company_live.binding_id}-${demoteAction}`,
            revision: `${data.company_live.binding_id}:${data.event_seq}`,
            title:
              demoteAction === "demote_to_paper"
                ? `Demote ${data.company_live.account_id} Back to Paper-Only`
                : `Suspend Tier 3 Live Binding ${data.company_live.account_id}`,
            consequence:
              "Immediately deactivates Tier 3 live order authority and returns the company to safe Tier 2 paper operations. Preserves full audit history and Tier 1 sandboxes.",
            confirmLabel:
              demoteAction === "demote_to_paper"
                ? "Confirm Demote to Paper"
                : "Confirm Suspend Live",
            destructive: true,
            available: canDemoteCommand,
            requireReason: true,
            defaultReason:
              demoteAction === "demote_to_paper"
                ? "Board demoted execution back to Tier 2 company_paper"
                : "Board suspended Tier 3 company_live execution",
            whatChanges: [
              {
                label: "Tier 3 Live Binding",
                value: `${data.company_live.binding_id} transitions to ${
                  demoteAction === "demote_to_paper" ? "demoted" : "suspended"
                }`,
              },
              {
                label: "Active Execution Tier",
                value: "Falls back cleanly to Tier 2 company_paper",
              },
            ],
            whatStaysUntouched: [
              {
                label: "Tier 1 Agent Sandboxes",
                value: "Continue operating privately without disruption",
              },
              {
                label: "Audit & Ledger History",
                value: "All promotion receipts and accounting journals retained",
              },
            ],
            technicalDetails: [
              { label: "command_type", value: "demote_live_environment" },
              { label: "target_binding_id", value: data.company_live.binding_id },
              { label: "action", value: demoteAction },
              { label: "charter_revision", value: charterRevision || "Unavailable" },
            ],
          }}
          onConfirm={async ({ reason }) => {
            if (!data.company_live) return;
            const lifecycle = await dispatchCommand({
              company_id: meta.company_id,
              command_id: newCommandId("demote-live"),
              command_type: "demote_live_environment",
              contract_version: "execution-tier-command-v1",
              preconditions: {
                charter_revision: charterRevision,
                target_binding_id: data.company_live.binding_id,
              },
              payload: { action: demoteAction, reason },
              label:
                demoteAction === "demote_to_paper"
                  ? `Demote ${data.company_live.account_id} to paper`
                  : `Suspend live ${data.company_live.account_id}`,
              sent_ns: Date.now() * 1_000_000,
            });
            if (lifecycle === "applied") {
              setDemoteAction(null);
              pushToast("Tier 3 company_live deactivated; returned to paper-only mode.");
            }
          }}
        />
      ) : null}

      <Card
        title="Tier 3 Live Candidates & Promotion Gate"
        description="Server-enforced checklist: paper soak + passing venue qualification + current core digest + Board Control signature"
        variant="raised"
        headerAction={
          <Text variant="meta" className="sd-mono">
            Core {formatShortHash(data.current_core_digest)}
          </Text>
        }
      >
        {data.live_candidates.length === 0 ? (
          <Text variant="help">
            {liveActive
              ? "No standby live candidates registered; current live binding is active above."
              : "No Tier 3 live candidate bindings registered yet. Company operates strictly in Tier 1/2 simulated mode."}
          </Text>
        ) : (
          <Stack gap={16}>
            {data.live_candidates.map((cand, idx) => renderCandidateRow(cand, idx))}
          </Stack>
        )}
      </Card>

      <Card
        title="Tier 1 · Per-Agent Paper Sandboxes"
        description="Each employee Agent maintains its own isolated simulated cash, fills, fees, and positions"
        variant="raised"
        className="sd-ledger-card"
      >
        {sandboxRows.length === 0 ? (
          <Text variant="help">No Agents have opened a personal Tier 1 paper sandbox yet.</Text>
        ) : (
          <DataTable
            caption="Per-Agent Tier 1 paper sandboxes"
            columns={sandboxCols}
            rows={sandboxRows}
          />
        )}
      </Card>

      <div className="sd-split-equal">
        <Card
          title="Venue Qualification Receipts"
          description="External testnet/wire qualification runs required before Tier 3 live promotion"
          variant="raised"
          className="sd-ledger-card"
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

        <Card
          title="Promotion & Demotion Ledger"
          description="Immutable Board governance record of every Tier 3 state transition"
          variant="raised"
        >
          {data.promotion_history.length === 0 ? (
            <Text variant="help">
              No live promotions or demotions recorded. Company remains paper-only.
            </Text>
          ) : (
            <List label="Execution tier promotion history" divided>
              {data.promotion_history.map((item) => (
                <li key={item.promotion_id} className="yg-list-item">
                  <WorkEvent
                    event={{
                      id: item.promotion_id,
                      kind: item.action === "promote_to_live" ? "review" : "task",
                      title: `${item.action} → ${item.to_binding_id}`,
                      detail: `${item.reason}${
                        item.qualification_receipt_id
                          ? ` · Qual ${item.qualification_receipt_id}`
                          : ""
                      } · Hash ${formatShortHash(item.payload_hash)}`,
                      time: formatTs(item.recorded_at_ns),
                      state: item.action === "promote_to_live" ? "ready" : "done",
                    }}
                  />
                </li>
              ))}
            </List>
          )}
        </Card>
      </div>
    </Stack>
  );
}
