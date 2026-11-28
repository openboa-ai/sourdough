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
  PageHeader,
  RecordFacts,
  Result,
  Select,
  Skeleton,
  Stack,
  Surface,
  Tabs,
  Text,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import type {
  ApprovalQuestionItem,
  RestrictionItem,
  RestrictionsProjection,
} from "../../api/types";
import { useProjection } from "../../api/useProjection";
import {
  ContextualApprovalList,
  usePendingApprovals,
} from "../../components/ContextualApprovalCard";
import { ControlledInlineConfirm } from "../../components/ControlledInlineConfirm";
import { formatRelativeNs, newCommandId } from "../../lib/format";
import { useControlPlane, useOutbox } from "../../state/store";

function isControlApproval(q: ApprovalQuestionItem): boolean {
  return (
    q.kind === "risk_release" ||
    q.kind === "restriction_approval" ||
    q.kind === "restriction_release" ||
    q.kind === "pause_company"
  );
}

export function RestrictionsView() {
  const { meta } = useControlPlane();
  const { canWrite, outbox, dispatchCommand } = useOutbox();
  const { data, loading, error, refetch } =
    useProjection<RestrictionsProjection>("restrictions");

  const [tab, setTab] = useState("active");
  const [historyQuery, setHistoryQuery] = useState("");
  const [clearingCause, setClearingCause] = useState<RestrictionItem | null>(null);
  const [addPauseOpen, setAddPauseOpen] = useState(false);
  const [tradingRiskConfirmOpen, setTradingRiskConfirmOpen] = useState(false);
  const [tradingRiskScope, setTradingRiskScope] = useState<
    "execution:all" | "execution:paper" | "execution:live"
  >("execution:all");

  const controlApprovals = usePendingApprovals(isControlApproval);

  const filteredHistory = useMemo(() => {
    const q = historyQuery.trim().toLowerCase();
    return (data?.history ?? []).filter((h) => {
      if (!q) return true;
      return `${h.restriction_id} ${h.cause_category} ${h.scope_ref} ${h.reason} ${h.last_command_id ?? ""}`
        .toLowerCase()
        .includes(q);
    });
  }, [data?.history, historyQuery]);

  if (loading && !data) return <Skeleton label="Loading active restrictions" lines={5} />;
  if (!data) {
    return (
      <Result
        title="Restrictions unavailable"
        action={
          <Button emphasis="soft" shape="pill" onClick={refetch}>
            Retry
          </Button>
        }
      >
        {error ?? "Could not load restrictions."}
      </Result>
    );
  }

  const canPauseCompany = Boolean(
    meta?.supported_commands?.pause_company?.includes("restriction-command-v1")
  );
  const canResumeCompany = Boolean(
    meta?.supported_commands?.resume_company?.includes("restriction-command-v1")
  );
  const isPausePending =
    (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
    outbox.intent?.command_type === "pause_company";
  const isTradingRiskPending =
    (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
    (outbox.intent?.command_type === "pause_company" ||
      outbox.intent?.command_type === "resume_company");

  const tradingActive = Boolean(meta?.active_domains.includes("trading"));
  const releasableTradingCause =
    data.active_causes.find(
      (c) =>
        c.active &&
        c.operator_releasable &&
        (c.scope_ref.startsWith("execution:") || c.cause_category === "trading_risk_pause")
    ) ?? null;

  const historyById = new Map(filteredHistory.map((h) => [h.restriction_id, h]));
  const historyCols: DataColumn[] = [
    {
      id: "id",
      label: "Restriction ID",
      kind: "identity",
      sortValue: (row) => historyById.get(row.id)?.restriction_id,
    },
    {
      id: "category",
      label: "Category",
      kind: "role",
      sortValue: (row) => historyById.get(row.id)?.cause_category,
    },
    {
      id: "scope",
      label: "Scope",
      kind: "text",
      sortValue: (row) => historyById.get(row.id)?.scope_ref,
    },
    {
      id: "reason",
      label: "Reason",
      kind: "text",
      sortValue: (row) => historyById.get(row.id)?.reason,
    },
    {
      id: "opened",
      label: "Opened",
      kind: "numeric",
      align: "end",
      sortValue: (row) => historyById.get(row.id)?.recorded_at_ns,
    },
    {
      id: "cleared",
      label: "Cleared",
      kind: "numeric",
      align: "end",
      sortValue: (row) => historyById.get(row.id)?.cleared_at_ns ?? null,
    },
    {
      id: "command",
      label: "Command ID",
      kind: "text",
      sortValue: (row) => historyById.get(row.id)?.last_command_id ?? "",
    },
  ];

  const historyRows: DataRow[] = filteredHistory.map((h) => ({
    id: h.restriction_id,
    cells: {
      id: <Text className="sd-mono">{h.restriction_id}</Text>,
      category: (
        <Badge tone="neutral" variant="outline" dot={false}>
          {h.cause_category}
        </Badge>
      ),
      scope: h.scope_ref,
      reason: h.reason,
      opened: <Text className="sd-tabular">{formatRelativeNs(h.recorded_at_ns)}</Text>,
      cleared: (
        <Text className="sd-tabular">
          {h.cleared_at_ns ? formatRelativeNs(h.cleared_at_ns) : "Not cleared"}
        </Text>
      ),
      command: h.last_command_id ? (
        <Inline gap={6} align="center">
          <Text className="sd-mono">{h.last_command_id}</Text>
          <CopyButton value={h.last_command_id} presentation="quiet" />
        </Inline>
      ) : (
        <Text variant="meta">None</Text>
      ),
    },
  }));

  return (
    <Stack gap={16}>
      <PageHeader
        title="Controls"
        detail="Operational stops, risk gates, and cause-scoped restrictions tracked independently"
        action={
          <Inline gap={8} align="center">
            <Button
              emphasis="soft"
              tone="danger"
              size="compact"
              shape="pill"
              disabled={!canWrite && !isPausePending}
              onClick={() => setAddPauseOpen((v) => !v)}
            >
              Add Operational Stop
            </Button>
          </Inline>
        }
      />

      <ContextualApprovalList questions={controlApprovals} />

      {meta ? (
        <ControlledInlineConfirm
          open={addPauseOpen}
          pending={isPausePending}
          onCancel={() => setAddPauseOpen(false)}
          decision={{
            id: `pause-company-${data.charter_revision}`,
            revision: `${data.charter_revision}:${data.active_causes.length}`,
            title: "Apply Cause-Scoped Company Pause",
            consequence: meta.active_domains.includes("trading")
              ? "Adds a distinct operational restriction cause without disturbing existing stops or open positions."
              : "Adds a distinct operational restriction cause without disturbing existing stops.",
            confirmLabel: "Apply Operational Stop",
            destructive: true,
            available: canWrite || isPausePending,
            requireReason: true,
            defaultReason: "Board operational hold pending review",
            whatChanges: [
              { label: "New restriction cause", value: "Added as active board_pause" },
              { label: "Existing stops", value: "Tracked independently" },
            ],
            whatStaysUntouched: [
              { label: "Active work & assets", value: "Never auto-liquidated" },
              { label: "Historical records", value: "Append-only audit preserved" },
            ],
            technicalDetails: [
              { label: "command_type", value: "pause_company" },
              { label: "contract_version", value: "restriction-command-v1" },
              { label: "charter_revision", value: data.charter_revision },
            ],
          }}
          onConfirm={async ({ reason }) => {
            const rid = `rest-board-${Date.now()}-${Math.random().toString(36).slice(2, 5)}`;
            const lifecycle = await dispatchCommand({
              company_id: meta.company_id,
              command_id: newCommandId("rest-add"),
              command_type: "pause_company",
              contract_version: "restriction-command-v1",
              preconditions: {
                charter_revision: data.charter_revision,
                restriction_id: rid,
                expected_restriction_revision: 0,
              },
              payload: {
                cause_category: "board_pause",
                scope_ref: "company:all",
                reason,
              },
              label: `Apply operational stop (${rid})`,
              sent_ns: Date.now() * 1_000_000,
            });
            if (
              lifecycle === "applied" ||
              lifecycle === "accepted" ||
              lifecycle === "unknown"
            ) {
              setAddPauseOpen(false);
            }
          }}
        />
      ) : null}

      {tradingActive && data.trading_risk ? (
        <Card
          title="Trading Execution Risk Gate"
          variant="raised"
          headerAction={
            releasableTradingCause ? (
              <Button
                emphasis="filled"
                tone="brand"
                size="compact"
                shape="pill"
                disabled={(!canWrite || !canResumeCompany) && !isTradingRiskPending}
                onClick={() => setTradingRiskConfirmOpen((v) => !v)}
              >
                Resume Trading Gate
              </Button>
            ) : (
              <Button
                emphasis="soft"
                tone="danger"
                size="compact"
                shape="pill"
                disabled={(!canWrite || !canPauseCompany) && !isTradingRiskPending}
                onClick={() => setTradingRiskConfirmOpen((v) => !v)}
              >
                Pause Trading Execution
              </Button>
            )
          }
        >
          <Stack gap={12}>
            <div className="sd-facts-wrap">
              <RecordFacts
                source="restriction_cause + trading_book_order"
                asOf={`seq #${data.event_seq}`}
                facts={[
                  {
                    id: "paper_state",
                    label: "Paper Execution",
                    value: data.trading_risk.paper_suspended ? "Suspended" : "Open",
                  },
                  {
                    id: "live_state",
                    label: "Live Execution",
                    value: data.trading_risk.live_suspended ? "Suspended" : "Open",
                  },
                  {
                    id: "rev",
                    label: "Effective Revision",
                    value: String(data.trading_risk.effective_revision),
                  },
                  {
                    id: "unres",
                    label: "Unresolved Orders",
                    value: String(data.trading_risk.unresolved_orders_count),
                  },
                  {
                    id: "ext",
                    label: "Pending Venue Actions",
                    value: String(data.trading_risk.unresolved_external_actions_count),
                  },
                  {
                    id: "settled",
                    label: "Settled Company Check",
                    value: data.trading_risk.settled_company_ok ? "Ready" : "Pending settlement",
                  },
                ]}
              />
            </div>
            <Text variant="meta">{data.trading_risk.server_freshness_note}</Text>
            {!releasableTradingCause &&
            (data.trading_risk.paper_suspended || data.trading_risk.live_suspended) ? (
              <Text variant="help">
                Execution is currently gated by an active company-wide or system-held restriction
                below. Clear the active cause in Active Restriction Causes to resume.
              </Text>
            ) : null}
            {!releasableTradingCause && tradingRiskConfirmOpen ? (
              <Inline gap={8} align="center">
                <Select
                  label="Execution pause scope"
                  size="compact"
                  value={tradingRiskScope}
                  onValueChange={(next) =>
                    setTradingRiskScope(
                      next as "execution:all" | "execution:paper" | "execution:live"
                    )
                  }
                  options={[
                    { id: "execution:all", label: "All Execution (execution:all)" },
                    { id: "execution:paper", label: "Paper Only (execution:paper)" },
                    { id: "execution:live", label: "Live Only (execution:live)" },
                  ]}
                />
              </Inline>
            ) : null}
            {meta ? (
              <ControlledInlineConfirm
                open={tradingRiskConfirmOpen}
                surface="inline"
                pending={isTradingRiskPending}
                onCancel={() => setTradingRiskConfirmOpen(false)}
                decision={{
                  id: releasableTradingCause
                    ? `trading-risk-resume-${releasableTradingCause.restriction_id}`
                    : `trading-risk-pause-${data.trading_risk.effective_revision}`,
                  revision: `${data.charter_revision}:${data.trading_risk.effective_revision}`,
                  title: releasableTradingCause
                    ? `Resume ${releasableTradingCause.scope_ref}`
                    : `Pause ${tradingRiskScope}`,
                  consequence: releasableTradingCause
                    ? "Clears the scoped trading risk restriction after server-side quote freshness and settlement checks."
                    : "Suspends new risk-increasing orders within the selected execution scope without auto-liquidating open positions.",
                  confirmLabel: releasableTradingCause
                    ? "Confirm Resume Trading"
                    : "Confirm Pause Trading",
                  destructive: !releasableTradingCause,
                  available:
                    (canWrite || isTradingRiskPending) &&
                    (releasableTradingCause ? canResumeCompany : canPauseCompany),
                  requireReason: true,
                  defaultReason: releasableTradingCause
                    ? "Board verified settled balances and resumed trading"
                    : `Board paused ${tradingRiskScope} pending risk review`,
                  whatChanges: [
                    {
                      label: "Target scope",
                      value: releasableTradingCause
                        ? `${releasableTradingCause.scope_ref} → Open`
                        : `${tradingRiskScope} → Suspended`,
                    },
                    {
                      label: "Cause category",
                      value: releasableTradingCause
                        ? releasableTradingCause.cause_category
                        : "trading_risk_pause",
                    },
                  ],
                  whatStaysUntouched: [
                    {
                      label: "Open positions",
                      value: "Preserved unchanged (no forced liquidation)",
                    },
                    {
                      label: "Cause-scoped stops",
                      value: "Independent active restrictions remain unchanged",
                    },
                  ],
                  technicalDetails: [
                    {
                      label: "command_type",
                      value: releasableTradingCause ? "resume_company" : "pause_company",
                    },
                    { label: "contract_version", value: "restriction-command-v1" },
                    { label: "charter_revision", value: data.charter_revision },
                    ...(releasableTradingCause
                      ? [
                          {
                            label: "restriction_id",
                            value: releasableTradingCause.restriction_id,
                          },
                          {
                            label: "expected_restriction_revision",
                            value: String(releasableTradingCause.restriction_revision),
                          },
                        ]
                      : [{ label: "scope_ref", value: tradingRiskScope }]),
                  ],
                }}
                onConfirm={async ({ reason }) => {
                  const lifecycle = releasableTradingCause
                    ? await dispatchCommand({
                        company_id: meta.company_id,
                        command_id: newCommandId("trisk-clr"),
                        command_type: "resume_company",
                        contract_version: "restriction-command-v1",
                        preconditions: {
                          charter_revision: data.charter_revision,
                          restriction_id: releasableTradingCause.restriction_id,
                          expected_restriction_revision:
                            releasableTradingCause.restriction_revision,
                        },
                        payload: { reason },
                        label: `Resume ${releasableTradingCause.scope_ref}`,
                        sent_ns: Date.now() * 1_000_000,
                      })
                    : await dispatchCommand({
                        company_id: meta.company_id,
                        command_id: newCommandId("trisk-pause"),
                        command_type: "pause_company",
                        contract_version: "restriction-command-v1",
                        preconditions: {
                          charter_revision: data.charter_revision,
                          restriction_id: `rest-trisk-${Date.now()}-${Math.random().toString(36).slice(2, 5)}`,
                          expected_restriction_revision: 0,
                        },
                        payload: {
                          cause_category: "trading_risk_pause",
                          scope_ref: tradingRiskScope,
                          reason,
                        },
                        label: `Pause ${tradingRiskScope}`,
                        sent_ns: Date.now() * 1_000_000,
                      });
                  if (
                    lifecycle === "applied" ||
                    lifecycle === "accepted" ||
                    lifecycle === "unknown"
                  ) {
                    setTradingRiskConfirmOpen(false);
                  }
                }}
              />
            ) : null}
          </Stack>
        </Card>
      ) : null}

      <Tabs
        label="Restriction views"
        presentation="pill"
        value={tab}
        onValueChange={setTab}
        items={[
          {
            id: "active",
            label: `Active (${data.active_causes.length})`,
            panel: tab === "active" ? (
              <Surface variant="raised">
                <Stack gap={12}>
                  <Stack gap={4}>
                    <Heading level={3}>
                      Active Restriction Causes ({data.active_causes.length})
                    </Heading>
                    <Text variant="help">
                      Each active cause is tracked and cleared independently without releasing competing stops.
                    </Text>
                  </Stack>
                  {data.active_causes.length === 0 ? (
                    <Text variant="help">No active operational restriction causes.</Text>
                  ) : (
                    <Stack gap={12}>
                      {data.active_causes.map((cause) => {
                        const isThisClearPending =
                          (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
                          outbox.intent?.command_type === "resume_company" &&
                          outbox.intent?.preconditions?.restriction_id === cause.restriction_id;

                        return (
                          <Card
                            key={cause.restriction_id}
                            variant="outlined"
                            density="compact"
                            title={`${cause.cause_category} · ${cause.restriction_id}`}
                            description={`Scope: ${cause.scope_ref} · Rev ${cause.restriction_revision} · ${formatRelativeNs(cause.recorded_at_ns)}`}
                            headerAction={
                              cause.operator_releasable ? (
                                <Button
                                  emphasis="soft"
                                  tone="brand"
                                  size="compact"
                                  shape="pill"
                                  disabled={!canWrite && !isThisClearPending}
                                  onClick={() =>
                                    setClearingCause(
                                      clearingCause?.restriction_id === cause.restriction_id
                                        ? null
                                        : cause
                                    )
                                  }
                                >
                                  Clear Cause
                                </Button>
                              ) : (
                                <Badge tone="warning" dot={false}>
                                  System-Held
                                </Badge>
                              )
                            }
                          >
                            <Stack gap={12}>
                              <Text variant="body">{cause.reason}</Text>
                              {clearingCause?.restriction_id === cause.restriction_id && meta ? (
                                <ControlledInlineConfirm
                                  open
                                  surface="inline"
                                  pending={isThisClearPending}
                                  onCancel={() => setClearingCause(null)}
                                  decision={{
                                    id: cause.restriction_id,
                                    revision: `${cause.restriction_revision}:${data.charter_revision}`,
                                    title: `Clear Restriction ${cause.restriction_id}`,
                                    consequence:
                                      "Clears strictly this restriction cause. Any other active competing stops remain in effect.",
                                    confirmLabel: `Clear ${cause.restriction_id}`,
                                    destructive: false,
                                    available:
                                      (canWrite || isThisClearPending) && cause.operator_releasable,
                                    requireReason: true,
                                    defaultReason: `Board cleared ${cause.restriction_id} after verification`,
                                    whatChanges: [
                                      {
                                        label: "Target cause",
                                        value: `${cause.restriction_id} marked cleared`,
                                      },
                                    ],
                                    whatStaysUntouched: [
                                      {
                                        label: "Other active stops",
                                        value: `${Math.max(data.active_causes.length - 1, 0)} competing cause(s) untouched`,
                                      },
                                    ],
                                    technicalDetails: [
                                      { label: "command_type", value: "resume_company" },
                                      { label: "restriction_id", value: cause.restriction_id },
                                      {
                                        label: "expected_restriction_revision",
                                        value: String(cause.restriction_revision),
                                      },
                                    ],
                                  }}
                                  onConfirm={async ({ reason }) => {
                                    const lifecycle = await dispatchCommand({
                                      company_id: meta.company_id,
                                      command_id: newCommandId("rest-clr"),
                                      command_type: "resume_company",
                                      contract_version: "restriction-command-v1",
                                      preconditions: {
                                        charter_revision: data.charter_revision,
                                        restriction_id: cause.restriction_id,
                                        expected_restriction_revision: cause.restriction_revision,
                                      },
                                      payload: { reason },
                                      label: `Clear restriction ${cause.restriction_id}`,
                                      sent_ns: Date.now() * 1_000_000,
                                    });
                                    if (
                                      lifecycle === "applied" ||
                                      lifecycle === "accepted" ||
                                      lifecycle === "unknown"
                                    ) {
                                      setClearingCause(null);
                                    }
                                  }}
                                />
                              ) : null}
                            </Stack>
                          </Card>
                        );
                      })}
                    </Stack>
                  )}
                </Stack>
              </Surface>
            ) : null,
          },
          {
            id: "history",
            label: `History (${data.history.length})`,
            panel: tab === "history" ? (
              <Card
                title="Cleared Restriction History"
                variant="raised"
                className="sd-ledger-card"
              >
                <Stack gap={12}>
                  <FilterBar
                    label="Filter restriction history"
                    hiddenLabel
                    placeholder="Filter by restriction ID, category, scope, reason, or command..."
                    size="compact"
                    query={historyQuery}
                    onQueryChange={setHistoryQuery}
                  />
                  {data.history.length === 0 ? (
                    <Text variant="help">No cleared restriction history recorded yet.</Text>
                  ) : historyRows.length === 0 ? (
                    <EmptyResult query={historyQuery} onClear={() => setHistoryQuery("")} />
                  ) : (
                    <DataTable
                      caption="Cleared restriction history"
                      columns={historyCols}
                      rows={historyRows}
                    />
                  )}
                </Stack>
              </Card>
            ) : null,
          },
        ]}
      />
    </Stack>
  );
}
