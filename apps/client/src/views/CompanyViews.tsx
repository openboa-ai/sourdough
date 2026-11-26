import { useEffect, useState } from "react";
import {
  AgentRow,
  Badge,
  Button,
  Card,
  DataTable,
  DisclosureRow,
  List,
  Notice,
  PageHeader,
  RecordFacts,
  ResourceRow,
  Result,
  Skeleton,
  Stack,
  Tabs,
  Text,
  WorkEvent,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import {
  fetchProjection,
  formatMinorCurrency,
  formatRelativeNs,
  newCommandId,
} from "../api/client";
import type {
  FinanceProjection,
  OrgProjection,
  RestrictionItem,
  RestrictionsProjection,
  StrategiesProjection,
} from "../api/types";
import { ControlledInlineConfirm } from "../components/ControlledInlineConfirm";
import { useApp } from "../state/store";

export function StrategiesView() {
  const { refreshSeq, meta, canWrite, dispatchCommand, reportApiError } = useApp();
  const [data, setData] = useState<StrategiesProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState("active");
  const [confirmTarget, setConfirmTarget] = useState<{
    kind: "activate" | "rollback";
    releaseId: string;
    comparisonId?: string | null;
  } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const res = await fetchProjection<StrategiesProjection>("strategies", {
          signal: controller.signal,
        });
        if (res.data) setData(res.data);
      } catch (err) {
        if (!controller.signal.aborted) reportApiError(err);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [refreshSeq, reportApiError]);

  if (loading && !data) return <Skeleton label="Loading strategy trials" lines={5} />;
  if (!data) return <Result title="Strategies unavailable">Could not load strategies.</Result>;

  if (!data.paper_enabled) {
    return (
      <Stack gap={16}>
        <PageHeader title="Strategies & Trials" detail="Paper domain not installed" />
        <Result title="Paper trading strategies inactive">
          Activate the opt-in paper domain to register releases and run paired challenger trials.
        </Result>
      </Stack>
    );
  }

  const trialCols: DataColumn[] = [
    { id: "id", label: "Comparison", kind: "identity" },
    { id: "pair", label: "Incumbent vs Challenger", kind: "text" },
    { id: "verdict", label: "Verdict", kind: "role" },
    { id: "action", label: "Action", kind: "actions", align: "end" },
  ];

  const trialRows: DataRow[] = data.trials.map((t) => ({
    id: t.comparison_id,
    cells: {
      id: <Text className="sd-mono">{t.comparison_id}</Text>,
      pair: `${t.incumbent_release_id} → ${t.challenger_release_id}`,
      verdict: (
        <Badge
          tone={
            t.verdict === "PROMOTE"
              ? "success"
              : t.verdict === "REJECT"
                ? "danger"
                : "neutral"
          }
          dot={false}
        >
          {t.verdict ?? t.status}
        </Badge>
      ),
      action:
        t.verdict === "PROMOTE" && data.adapter ? (
          <Button
            emphasis="soft"
            tone="brand"
            size="compact"
            shape="pill"
            disabled={!canWrite}
            onClick={() =>
              setConfirmTarget({
                kind: "activate",
                releaseId: t.challenger_release_id,
                comparisonId: t.comparison_id,
              })
            }
          >
            Promote
          </Button>
        ) : (
          <Text variant="meta">—</Text>
        ),
    },
  }));

  return (
    <Stack gap={16}>
      <PageHeader
        title="Strategies & Challenger Trials"
        detail="Paired incumbent-vs-challenger evaluation and swappable PaperExecution adapter"
      />

      {confirmTarget && meta && data.adapter ? (
        <ControlledInlineConfirm
          open
          onCancel={() => setConfirmTarget(null)}
          decision={{
            id: `${confirmTarget.kind}-${confirmTarget.releaseId}`,
            revision: `${data.active_deployment?.deployment_id ?? "none"}:${data.adapter.binding_revision}`,
            title:
              confirmTarget.kind === "activate"
                ? `Promote Strategy Release ${confirmTarget.releaseId}`
                : `Roll Back to Strategy Release ${confirmTarget.releaseId}`,
            consequence:
              confirmTarget.kind === "activate"
                ? "Activates the promoted challenger strategy as the sole active paper deployment."
                : "Creates a forward rollback deployment returning to the selected prior release.",
            confirmLabel:
              confirmTarget.kind === "activate"
                ? `Promote ${confirmTarget.releaseId}`
                : `Roll Back to ${confirmTarget.releaseId}`,
            destructive: confirmTarget.kind === "rollback",
            available: canWrite,
            whatChanges: [
              { label: "Active strategy", value: confirmTarget.releaseId },
              {
                label: "Previous deployment",
                value: data.active_deployment?.deployment_id ?? "None",
              },
            ],
            whatStaysUntouched: [
              { label: "Environment", value: "Strictly Paper (no live funds)" },
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
              { label: "binding_revision", value: data.adapter.binding_revision },
              {
                label: "comparison_id",
                value: confirmTarget.comparisonId ?? "none",
              },
            ],
          }}
          onConfirm={() => {
            if (confirmTarget.kind === "activate") {
              void dispatchCommand({
                company_id: meta.company_id,
                command_id: newCommandId("strat-act"),
                command_type: "activate_strategy_release",
                contract_version: "strategy-command-v1",
                preconditions: {
                  binding_revision: data.adapter!.binding_revision,
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
            } else if (data.active_deployment) {
              void dispatchCommand({
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
            }
          }}
        />
      ) : null}

      <Tabs
        label="Strategy sections"
        presentation="pill"
        value={activeTab}
        onValueChange={setActiveTab}
        items={[
          {
            id: "active",
            label: "Active Deployment",
            panel: (
              <Card variant="raised" title="Active Paper Strategy Deployment">
                {data.active_deployment ? (
                  <RecordFacts
                    source="paper_strategy_deployment"
                    asOf={`seq #${data.event_seq}`}
                    facts={[
                      {
                        id: "dep",
                        label: "Deployment ID",
                        value: data.active_deployment.deployment_id,
                      },
                      {
                        id: "rel",
                        label: "Active Release",
                        value: data.active_deployment.release_id,
                      },
                      {
                        id: "prog",
                        label: "Program Identity",
                        value: data.active_deployment.program_identity,
                      },
                      {
                        id: "cmp",
                        label: "Qualifying Trial",
                        value: data.active_deployment.comparison_id ?? "Initial baseline",
                      },
                    ]}
                  />
                ) : (
                  <Text variant="help">No active strategy deployment recorded yet.</Text>
                )}
              </Card>
            ),
          },
          {
            id: "trials",
            label: `Challenger Trials (${data.trials.length})`,
            panel: (
              <Card variant="raised" title="Paired Incumbent vs Challenger Trials">
                {trialRows.length === 0 ? (
                  <Text variant="help">No challenger trials registered yet.</Text>
                ) : (
                  <DataTable caption="Challenger trials" columns={trialCols} rows={trialRows} />
                )}
              </Card>
            ),
          },
          {
            id: "adapter",
            label: "PaperExecution Adapter",
            panel: (
              <Card variant="raised" title="Swappable PaperExecution Connector">
                {data.adapter ? (
                  <RecordFacts
                    source="paper_binding"
                    asOf={`seq #${data.event_seq}`}
                    facts={[
                      { id: "env", label: "Environment", value: data.adapter.environment },
                      { id: "conn", label: "Connector", value: data.adapter.connector_revision },
                      { id: "acct", label: "Paper Account", value: data.adapter.account_id },
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
                ) : (
                  <Text variant="help">No paper adapter bound.</Text>
                )}
              </Card>
            ),
          },
        ]}
      />
    </Stack>
  );
}

export function FinanceView() {
  const { refreshSeq, reportApiError } = useApp();
  const [data, setData] = useState<FinanceProjection | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const res = await fetchProjection<FinanceProjection>("finance", {
          signal: controller.signal,
        });
        if (res.data) setData(res.data);
      } catch (err) {
        if (!controller.signal.aborted) reportApiError(err);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [refreshSeq, reportApiError]);

  if (loading && !data) return <Skeleton label="Loading finance ledger" lines={5} />;
  if (!data) return <Result title="Finance & Costs unavailable">Could not load finance data.</Result>;

  const balCols: DataColumn[] = [
    { id: "acct", label: "GL Account", kind: "identity" },
    { id: "bal", label: "Balance", kind: "numeric", align: "end" },
  ];
  const balRows: DataRow[] = data.account_balances.map((b) => ({
    id: b.account,
    cells: {
      acct: b.account,
      bal: <Text className="sd-tabular">{formatMinorCurrency(b.balance_minor)}</Text>,
    },
  }));

  const closeCols: DataColumn[] = [
    { id: "req", label: "Close Request", kind: "identity" },
    { id: "scope", label: "Scope", kind: "text" },
    { id: "status", label: "Status", kind: "role" },
    { id: "issues", label: "Open Issues", kind: "numeric", align: "end" },
  ];
  const closeRows: DataRow[] = data.accounting_closes.map((c) => ({
    id: c.request_id,
    cells: {
      req: <Text className="sd-mono">{c.request_id}</Text>,
      scope: c.scope,
      status: (
        <Badge tone={c.issues.length > 0 ? "warning" : "success"} dot={false}>
          {c.status}
        </Badge>
      ),
      issues: <Text className="sd-tabular">{c.issues.length}</Text>,
    },
  }));

  return (
    <Stack gap={16}>
      <PageHeader
        title="Finance & Operating Costs"
        detail="Double-entry general ledger, period accounting closes, and 8h funding settlements"
      />

      {data.open_close_issues.length > 0 ? (
        <Notice title="Accounting close contains open issues" tone="warning" urgent>
          {data.open_close_issues.length} unresolved cost or valuation item(s). Company Net P&amp;L
          stays explicitly Unavailable until closed cleanly.
        </Notice>
      ) : null}

      <div className="sd-split-equal">
        <Card title="General Ledger Account Balances" variant="raised">
          {balRows.length === 0 ? (
            <Text variant="help">No journal entries posted yet.</Text>
          ) : (
            <DataTable caption="GL account balances" columns={balCols} rows={balRows} />
          )}
        </Card>

        <Card title="Period Accounting Closes" variant="raised">
          {closeRows.length === 0 ? (
            <Text variant="help">No accounting closes recorded yet.</Text>
          ) : (
            <DataTable caption="Accounting closes" columns={closeCols} rows={closeRows} />
          )}
        </Card>
      </div>

      <DisclosureRow
        label={`8h Funding Settlements (${data.funding_settlements.length})`}
        initiallyOpen={false}
      >
        {data.funding_settlements.length === 0 ? (
          <Text variant="help">No funding intervals recorded.</Text>
        ) : (
          <List label="8h funding settlements" divided>
            {data.funding_settlements.map((f) => (
              <WorkEvent
                key={f.settlement_id}
                event={{
                  id: f.settlement_id,
                  kind: "routine",
                  title: `${f.instrument_id} · pos ${f.signed_position_lots}`,
                  detail:
                    f.status === "unavailable"
                      ? `Unavailable (${f.unavailable_reason ?? "missing"})`
                      : formatMinorCurrency(f.funding_cashflow_atoms),
                  state: f.status === "unavailable" ? "waiting" : "done",
                }}
              />
            ))}
          </List>
        )}
      </DisclosureRow>
    </Stack>
  );
}

export function OrgReportsView() {
  const { navigate, refreshSeq, reportApiError } = useApp();
  const [data, setData] = useState<OrgProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState("roster");

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const res = await fetchProjection<OrgProjection>("org", {
          signal: controller.signal,
        });
        if (res.data) setData(res.data);
      } catch (err) {
        if (!controller.signal.aborted) reportApiError(err);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [refreshSeq, reportApiError]);

  if (loading && !data) return <Skeleton label="Loading organization and reports" lines={5} />;
  if (!data) return <Result title="Org & Reports unavailable">Could not load organization data.</Result>;

  const custodyCols: DataColumn[] = [
    { id: "agent", label: "Archived Agent", kind: "identity" },
    { id: "manager", label: "Retired By (CEO/Manager)", kind: "text" },
    { id: "reason", label: "Reason", kind: "text" },
    { id: "scope", label: "Prior Scope", kind: "role" },
  ];
  const custodyRows: DataRow[] = data.delegated_hr_custody.archived_employees.map((a) => ({
    id: `${a.cutoff}-${a.agent_id}`,
    cells: {
      agent: a.agent_name,
      manager: a.manager_name,
      reason: a.reason,
      scope: a.previous_scope,
    },
  }));

  return (
    <Stack gap={16}>
      <PageHeader
        title="Organization & Published Reports"
        detail="Company roster, selective publications, and CEO-delegated HR custody audit"
      />

      <Tabs
        label="Organization sections"
        presentation="pill"
        value={tab}
        onValueChange={setTab}
        items={[
          {
            id: "roster",
            label: `Roster (${data.roster.length})`,
            panel: (
              <Card title="Active Company Roster" variant="raised">
                <List label="Company agents" divided>
                  {data.roster.map((agent) => (
                    <AgentRow
                      key={agent.agent_id}
                      agent={{
                        id: agent.agent_id,
                        name: agent.display_name,
                        role: agent.role_label,
                        state: agent.avatar_state,
                        art: agent.avatar_variant,
                        preview: `${agent.grant_scope} · ${agent.private_draft_count} private drafts`,
                      }}
                      onSelect={() =>
                        navigate(`/office/agents/${encodeURIComponent(agent.agent_id)}`)
                      }
                    />
                  ))}
                </List>
              </Card>
            ),
          },
          {
            id: "reports",
            label: `Published Reports (${data.publications.length})`,
            panel: (
              <Card title="Selective Company Publications" variant="raised">
                {data.publications.length === 0 ? (
                  <Text variant="help">No published company reports yet.</Text>
                ) : (
                  <List label="Published company reports" divided>
                    {data.publications.map((p) => (
                      <ResourceRow
                        key={p.asset_id}
                        resource={{
                          id: p.asset_id,
                          kind: "output",
                          title: `Report ${p.asset_id} · ${p.author_name} (${p.author_role})`,
                          detail: `#${p.project_id} · ${p.content}`,
                        }}
                        onOpen={() =>
                          navigate("/messages", {
                            contextRef: {
                              kind: "shared_publication",
                              id: p.asset_id,
                              label: `Report ${p.asset_id} by ${p.author_name}`,
                            },
                          })
                        }
                      />
                    ))}
                  </List>
                )}
              </Card>
            ),
          },
          {
            id: "custody",
            label: `Delegated HR Custody (${custodyRows.length})`,
            panel: (
              <Stack gap={12}>
                <Notice title={data.delegated_hr_custody.policy_notice} tone="neutral">
                  Routine employee hiring and retirement are delegated to Founder · CEO under
                  standing authority. Board members inspect offboarding custody transfers here
                  read-only.
                </Notice>
                <Card title="Archived Employees & Custody Ledger" variant="raised">
                  {custodyRows.length === 0 ? (
                    <Text variant="help">No retired employees recorded.</Text>
                  ) : (
                    <DataTable
                      caption="Delegated HR custody ledger"
                      columns={custodyCols}
                      rows={custodyRows}
                    />
                  )}
                </Card>
              </Stack>
            ),
          },
        ]}
      />
    </Stack>
  );
}

export function RestrictionsView() {
  const { refreshSeq, meta, canWrite, dispatchCommand, reportApiError } = useApp();
  const [data, setData] = useState<RestrictionsProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [clearingCause, setClearingCause] = useState<RestrictionItem | null>(null);
  const [addPauseOpen, setAddPauseOpen] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const res = await fetchProjection<RestrictionsProjection>("restrictions", {
          signal: controller.signal,
        });
        if (res.data) setData(res.data);
      } catch (err) {
        if (!controller.signal.aborted) reportApiError(err);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [refreshSeq, reportApiError]);

  if (loading && !data) return <Skeleton label="Loading active restrictions" lines={5} />;
  if (!data) return <Result title="Restrictions unavailable">Could not load restrictions.</Result>;

  return (
    <Stack gap={16}>
      <PageHeader
        title="Active Restrictions & Risk Gates"
        detail="Each restriction cause is tracked independently; clearing one never clears another active stop"
        action={
          <Button
            emphasis="soft"
            tone="danger"
            size="compact"
            shape="pill"
            disabled={!canWrite}
            onClick={() => setAddPauseOpen((v) => !v)}
          >
            Add Operational Stop
          </Button>
        }
      />

      {meta ? (
        <ControlledInlineConfirm
          open={addPauseOpen}
          onCancel={() => setAddPauseOpen(false)}
          decision={{
            id: `pause-company-${data.charter_revision}`,
            revision: `${data.charter_revision}:${data.active_causes.length}`,
            title: "Apply Cause-Scoped Company Pause",
            consequence:
              "Adds a distinct operational restriction cause without disturbing existing stops or open positions.",
            confirmLabel: "Apply Operational Stop",
            destructive: true,
            available: canWrite,
            requireReason: true,
            defaultReason: "Board operational hold pending review",
            whatChanges: [
              { label: "New restriction cause", value: "Added as active board_pause" },
              { label: "Existing stops", value: "Tracked independently" },
            ],
            whatStaysUntouched: [
              { label: "Open paper positions", value: "Never auto-liquidated" },
              { label: "Historical records", value: "Append-only audit preserved" },
            ],
            technicalDetails: [
              { label: "command_type", value: "pause_company" },
              { label: "contract_version", value: "restriction-command-v1" },
              { label: "charter_revision", value: data.charter_revision },
            ],
          }}
          onConfirm={({ reason }) => {
            const rid = `rest-board-${Date.now()}`;
            void dispatchCommand({
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
          }}
        />
      ) : null}

      {data.paper_risk ? (
        <Card title="Paper Risk Gate Readiness (Non-Binding Telemetry)" variant="raised">
          <Stack gap={8}>
            <RecordFacts
              source="paper_risk_state + paper_order"
              asOf={`seq #${data.event_seq}`}
              facts={[
                {
                  id: "state",
                  label: "Paper Risk Gate",
                  value: data.paper_risk.suspended ? "Suspended" : "Open",
                },
                {
                  id: "rev",
                  label: "Effective Revision",
                  value: String(data.paper_risk.effective_revision),
                },
                {
                  id: "unres",
                  label: "Unresolved Paper Orders",
                  value: String(data.paper_risk.unresolved_orders_count),
                },
                {
                  id: "settled",
                  label: "Settled Company Check",
                  value: data.paper_risk.settled_company_ok ? "Ready" : "Pending settlement",
                },
              ]}
            />
            <Text variant="meta">{data.paper_risk.server_freshness_note}</Text>
          </Stack>
        </Card>
      ) : null}

      <Card title={`Active Restriction Causes (${data.active_causes.length})`} variant="raised">
        {data.active_causes.length === 0 ? (
          <Text variant="help">No active operational restriction causes.</Text>
        ) : (
          <Stack gap={12}>
            {data.active_causes.map((cause) => (
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
                      disabled={!canWrite}
                      onClick={() =>
                        setClearingCause(
                          clearingCause?.restriction_id === cause.restriction_id ? null : cause
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
                      onCancel={() => setClearingCause(null)}
                      decision={{
                        id: cause.restriction_id,
                        revision: `${cause.restriction_revision}:${data.charter_revision}`,
                        title: `Clear Restriction ${cause.restriction_id}`,
                        consequence:
                          "Clears strictly this restriction cause. Any other active competing stops remain in effect.",
                        confirmLabel: `Clear ${cause.restriction_id}`,
                        destructive: false,
                        available: canWrite && cause.operator_releasable,
                        requireReason: true,
                        defaultReason: `Board cleared ${cause.restriction_id} after verification`,
                        whatChanges: [
                          { label: "Target cause", value: `${cause.restriction_id} marked cleared` },
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
                      onConfirm={({ reason }) => {
                        void dispatchCommand({
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
                      }}
                    />
                  ) : null}
                </Stack>
              </Card>
            ))}
          </Stack>
        )}
      </Card>
    </Stack>
  );
}
