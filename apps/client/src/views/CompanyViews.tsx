import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { CopyButton } from "@openboa/yogurt/motion-copy-button";
import {
  AgentRow,
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
  Notice,
  PageHeader,
  RecordFacts,
  Result,
  Select,
  Skeleton,
  Stack,
  Surface,
  Switch,
  Table,
  Tabs,
  Text,
  WorkEvent,
  type DataColumn,
  type DataRow,
  type Fact,
} from "@openboa/yogurt";
import {
  fetchProjection,
  formatMinorCurrency,
  formatRelativeNs,
  newCommandId,
} from "../api/client";
import type {
  AgentsGraphProjection,
  FinanceProjection,
  OrgProjection,
  RestrictionItem,
  RestrictionsProjection,
} from "../api/types";
import {
  ContextualApprovalList,
  usePendingApprovals,
} from "../components/ContextualApprovalCard";
import { ControlledInlineConfirm } from "../components/ControlledInlineConfirm";
import { useApp } from "../state/store";
import {
  formatNsWithClock,
  normalizeToNs,
  renderSortableTableHead,
  sortItems,
  type SortState,
  type SortableColumn,
} from "./tableUtils";

function formatIssueLine(issue: Record<string, unknown>, idx: number): string {
  const code = typeof issue.code === "string" ? issue.code : null;
  const reference = typeof issue.reference === "string" ? issue.reference : null;
  const owner = typeof issue.owner === "string" ? issue.owner : null;
  const detail =
    typeof issue.reason === "string"
      ? issue.reason
      : typeof issue.detail === "string"
        ? issue.detail
        : null;
  const parts = [code, reference, owner, detail].filter(Boolean);
  return parts.length > 0 ? parts.join(" · ") : `Issue #${idx + 1}: ${JSON.stringify(issue)}`;
}

function formatStatementPayloadFacts(
  requestId: string,
  payload: Record<string, unknown>
): Fact[] {
  const entries = Object.entries(payload);
  if (entries.length === 0) {
    return [{ id: `${requestId}-empty`, label: "Payload", value: "No statement metrics" }];
  }
  const currency = typeof payload.currency === "string" ? payload.currency : "USD";
  return entries.map(([key, val]) => {
    const label = key.replace(/_/g, " ");
    let formatted: string;
    if (val === null || val === undefined) {
      formatted = "Unavailable";
    } else if (typeof val === "number" && /minor/i.test(key)) {
      formatted = formatMinorCurrency(val, currency);
    } else if (typeof val === "number" && /_ns$/i.test(key)) {
      formatted = formatNsWithClock(val);
    } else if (typeof val === "object") {
      formatted = JSON.stringify(val);
    } else {
      formatted = String(val);
    }
    return { id: `${requestId}-${key}`, label, value: formatted };
  });
}

type CloseItem = FinanceProjection["accounting_closes"][number];
type JournalItem = FinanceProjection["journals"][number];

const CLOSE_COLUMNS: SortableColumn<CloseItem>[] = [
  { id: "req", label: "Close Request", kind: "identity", sortValue: (c) => c.request_id },
  { id: "period", label: "Period", kind: "text", sortValue: (c) => c.period ?? "" },
  { id: "scope", label: "Scope", kind: "text", sortValue: (c) => c.scope },
  { id: "status", label: "Status", kind: "role", sortValue: (c) => c.status },
  {
    id: "supersedes",
    label: "Supersedes",
    kind: "text",
    sortValue: (c) => c.supersedes ?? "",
  },
  {
    id: "assessed",
    label: "Assessed",
    kind: "numeric",
    align: "end",
    sortValue: (c) => c.assessed_ns ?? null,
  },
  {
    id: "issues",
    label: "Open Issues",
    kind: "numeric",
    align: "end",
    sortValue: (c) => c.issues.length,
  },
  { id: "details", label: "Details", kind: "actions", align: "end" },
];

const JOURNAL_COLUMNS: SortableColumn<JournalItem>[] = [
  { id: "entry", label: "Entry ID", kind: "identity", sortValue: (j) => j.entry_id },
  {
    id: "source",
    label: "Source / Event",
    kind: "text",
    sortValue: (j) => `${j.source}:${j.source_event}`,
  },
  {
    id: "date",
    label: "Effective Date",
    kind: "numeric",
    align: "end",
    sortValue: (j) => j.effective_date,
  },
  { id: "currency", label: "Currency", kind: "role", sortValue: (j) => j.currency },
  { id: "lines", label: "Debit / Credit Lines", kind: "text" },
  { id: "details", label: "Details", kind: "actions", align: "end" },
];

export function FinanceView() {
  const { refreshSeq, reportApiError } = useApp();
  const [data, setData] = useState<FinanceProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [closeQuery, setCloseQuery] = useState("");
  const [closeSort, setCloseSort] = useState<SortState>(null);
  const [expandedCloses, setExpandedCloses] = useState<Set<string>>(() => new Set());
  const [journalQuery, setJournalQuery] = useState("");
  const [journalSort, setJournalSort] = useState<SortState>({ id: "date", descending: true });
  const [expandedJournals, setExpandedJournals] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const res = await fetchProjection<FinanceProjection>("finance", {
          signal: controller.signal,
        });
        if (!active || controller.signal.aborted) return;
        if (res.data) setData(res.data);
        setError(null);
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setError(err instanceof Error ? err.message : "Could not load finance data.");
      } finally {
        if (active && !controller.signal.aborted) setLoading(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [refreshSeq, retrySeq, reportApiError]);

  const filteredCloses = useMemo(() => {
    const q = closeQuery.trim().toLowerCase();
    const list = (data?.accounting_closes ?? []).filter((c) => {
      if (!q) return true;
      return `${c.request_id} ${c.period ?? ""} ${c.scope} ${c.status} ${c.supersedes ?? ""} ${JSON.stringify(c.issues)}`
        .toLowerCase()
        .includes(q);
    });
    return sortItems(list, closeSort, CLOSE_COLUMNS);
  }, [data?.accounting_closes, closeQuery, closeSort]);

  const filteredJournals = useMemo(() => {
    const q = journalQuery.trim().toLowerCase();
    const list = (data?.journals ?? []).filter((j) => {
      if (!q) return true;
      const accounts = j.lines.map((l) => l.account).join(" ");
      return `${j.entry_id} ${j.source} ${j.source_event} ${j.effective_date} ${j.currency} ${accounts}`
        .toLowerCase()
        .includes(q);
    });
    return sortItems(list, journalSort, JOURNAL_COLUMNS);
  }, [data?.journals, journalQuery, journalSort]);

  if (loading && !data) return <Skeleton label="Loading finance ledger" lines={5} />;
  if (!data) {
    return (
      <Result
        title="Finance & Costs unavailable"
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
        {error ?? "Could not load finance data."}
      </Result>
    );
  }

  const balById = new Map(data.account_balances.map((b) => [b.account, b]));
  const balCols: DataColumn[] = [
    {
      id: "acct",
      label: "GL Account",
      kind: "identity",
      sortValue: (row) => balById.get(row.id)?.account,
    },
    {
      id: "bal",
      label: "Balance",
      kind: "numeric",
      align: "end",
      sortValue: (row) => balById.get(row.id)?.balance_minor,
    },
  ];
  const balRows: DataRow[] = data.account_balances.map((b) => ({
    id: b.account,
    cells: {
      acct: b.account,
      bal: <Text className="sd-tabular">{formatMinorCurrency(b.balance_minor)}</Text>,
    },
  }));

  const toggleClose = (requestId: string) => {
    setExpandedCloses((prev) => {
      const next = new Set(prev);
      if (next.has(requestId)) next.delete(requestId);
      else next.add(requestId);
      return next;
    });
  };

  const toggleJournal = (entryId: string) => {
    setExpandedJournals((prev) => {
      const next = new Set(prev);
      if (next.has(entryId)) next.delete(entryId);
      else next.add(entryId);
      return next;
    });
  };

  return (
    <Stack gap={16}>
      <PageHeader
        title="Finance"
        detail="Double-entry general ledger, period accounting closes, retained statements, and funding settlements"
      />

      {data.open_close_issues.length > 0 ? (
        <Notice title="Accounting close contains open issues" tone="warning" urgent>
          <Stack gap={6}>
            <Text variant="body">
              {data.open_close_issues.length} unresolved cost or valuation item(s). Company Net
              P&amp;L stays explicitly Unavailable until closed cleanly.
            </Text>
            <List label="Open close issues" divided>
              {data.open_close_issues.map((issue, idx) => (
                <li key={idx} className="yg-list-item sd-list-row-padded">
                  <Text variant="meta" className="sd-mono">
                    {formatIssueLine(issue, idx)}
                  </Text>
                </li>
              ))}
            </List>
          </Stack>
        </Notice>
      ) : null}

      <div className="sd-split-equal">
        <Card
          title="General Ledger Account Balances"
          variant="raised"
          className="sd-ledger-card"
        >
          {balRows.length === 0 ? (
            <Text variant="help">No journal entries posted yet.</Text>
          ) : (
            <DataTable caption="GL account balances" columns={balCols} rows={balRows} />
          )}
        </Card>

        <Card
          title={`Retained Window Statements (${data.retained_statements.length})`}
          description="Sealed window statement snapshots retained for reconciliation and strategy evaluation"
          variant="raised"
        >
          {data.retained_statements.length === 0 ? (
            <Text variant="help">
              {!data.trading_enabled
                ? "Trading domain not enabled — no retained window statements."
                : "No retained window statements sealed yet."}
            </Text>
          ) : (
            <Stack gap={12}>
              {data.retained_statements.map((stmt) => (
                <Surface key={stmt.request_id} variant="surface" density="compact">
                  <Stack gap={8}>
                    <Inline justify="between" align="center" gap={8} wrap>
                      <Inline gap={8} align="center">
                        <Heading level={4} className="sd-mono">
                          {stmt.request_id}
                        </Heading>
                        <Badge tone="neutral" variant="outline" dot={false}>
                          {stmt.window_id}
                        </Badge>
                      </Inline>
                      <Inline gap={6} align="center">
                        <Text variant="meta" className="sd-mono">
                          Rev: {stmt.revision}
                        </Text>
                        <CopyButton value={stmt.revision} presentation="quiet" />
                      </Inline>
                    </Inline>
                    <div className="sd-facts-wrap">
                      <RecordFacts
                        source={`statement:${stmt.window_id}`}
                        asOf={stmt.revision}
                        facts={formatStatementPayloadFacts(stmt.request_id, stmt.payload)}
                      />
                    </div>
                  </Stack>
                </Surface>
              ))}
            </Stack>
          )}
        </Card>
      </div>

      <Card title="Period Accounting Closes" variant="raised" className="sd-ledger-card">
          <Stack gap={12}>
            <FilterBar
              label="Filter accounting closes"
              hiddenLabel
              placeholder="Filter by request ID, period, scope, or issue..."
              size="compact"
              query={closeQuery}
              onQueryChange={setCloseQuery}
            />
            {data.accounting_closes.length === 0 ? (
              <Text variant="help">No accounting closes recorded yet.</Text>
            ) : filteredCloses.length === 0 ? (
              <EmptyResult query={closeQuery} onClear={() => setCloseQuery("")} />
            ) : (
              <Table caption="Accounting closes">
                {renderSortableTableHead(CLOSE_COLUMNS, closeSort, setCloseSort)}
                <tbody>
                  {filteredCloses.map((c) => {
                    const isExpanded = expandedCloses.has(c.request_id);
                    return (
                      <Fragment key={c.request_id}>
                        <tr>
                          <td data-kind="identity" data-align="start">
                            <Text className="sd-mono">{c.request_id}</Text>
                          </td>
                          <td data-kind="text" data-align="start">
                            <Text className="sd-tabular">{c.period ?? "Unspecified"}</Text>
                          </td>
                          <td data-kind="text" data-align="start">
                            {c.scope}
                          </td>
                          <td data-kind="role" data-align="start">
                            <Badge tone={c.issues.length > 0 ? "warning" : "success"} dot={false}>
                              {c.status}
                            </Badge>
                          </td>
                          <td data-kind="text" data-align="start">
                            <Text className="sd-mono">{c.supersedes ?? "—"}</Text>
                          </td>
                          <td data-kind="numeric" data-align="end" data-numeric="true">
                            <Text className="sd-tabular">
                              {c.assessed_ns
                                ? formatRelativeNs(normalizeToNs(c.assessed_ns))
                                : "Not assessed"}
                            </Text>
                          </td>
                          <td data-kind="numeric" data-align="end" data-numeric="true">
                            <Text className="sd-tabular">{c.issues.length}</Text>
                          </td>
                          <td data-kind="actions" data-align="end">
                            <Button
                              emphasis="quiet"
                              size="compact"
                              shape="pill"
                              aria-expanded={isExpanded}
                              onClick={() => toggleClose(c.request_id)}
                            >
                              {isExpanded ? "Hide" : "Details"}
                            </Button>
                          </td>
                        </tr>
                        {isExpanded ? (
                          <tr className="sd-tape-detail-row">
                            <td colSpan={CLOSE_COLUMNS.length}>
                              <div className="sd-ledger-detail-sticky">
                                <Stack gap={8}>
                                  <Text variant="meta">
                                    Assessed:{" "}
                                    {c.assessed_ns
                                      ? formatNsWithClock(c.assessed_ns)
                                      : "Not assessed"}
                                    {c.supersedes ? ` · Supersedes: ${c.supersedes}` : ""}
                                  </Text>
                                  {c.issues.length === 0 ? (
                                    <Text variant="help">
                                      No open close issues on this request.
                                    </Text>
                                  ) : (
                                    <List label={`Issues for ${c.request_id}`} divided>
                                      {c.issues.map((issue, idx) => (
                                        <li key={idx} className="yg-list-item sd-list-row-padded">
                                          <Text variant="meta" className="sd-mono">
                                            {formatIssueLine(issue, idx)}
                                          </Text>
                                        </li>
                                      ))}
                                    </List>
                                  )}
                                </Stack>
                              </div>
                            </td>
                          </tr>
                        ) : null}
                      </Fragment>
                    );
                  })}
                </tbody>
              </Table>
            )}
          </Stack>
        </Card>

      <Card
        title={`General Ledger Journal Entries (${data.journals.length})`}
        description="Double-entry postings with ordered debit and credit lines"
        variant="raised"
        className="sd-ledger-card"
      >
        <Stack gap={12}>
          <FilterBar
            label="Filter journal entries"
            hiddenLabel
            placeholder="Filter by entry ID, source, event, date, or account..."
            size="compact"
            query={journalQuery}
            onQueryChange={setJournalQuery}
          />
          {data.journals.length === 0 ? (
            <Text variant="help">No general ledger journal entries recorded yet.</Text>
          ) : filteredJournals.length === 0 ? (
            <EmptyResult query={journalQuery} onClear={() => setJournalQuery("")} />
          ) : (
            <Table caption="General ledger journal entries">
              {renderSortableTableHead(JOURNAL_COLUMNS, journalSort, setJournalSort)}
              <tbody>
                {filteredJournals.map((j) => {
                  const isExpanded = expandedJournals.has(j.entry_id);
                  const orderedLines = [...j.lines].sort((a, b) => a.ordinal - b.ordinal);
                  const lineSummary = orderedLines
                    .map(
                      (l) =>
                        `${l.amount_minor >= 0 ? "DR" : "CR"} ${l.account} ${formatMinorCurrency(Math.abs(l.amount_minor), j.currency)}`
                    )
                    .join(" · ");

                  return (
                    <Fragment key={j.entry_id}>
                      <tr>
                        <td data-kind="identity" data-align="start">
                          <Inline gap={6} align="center">
                            <Text className="sd-mono">{j.entry_id}</Text>
                            <CopyButton value={j.entry_id} presentation="quiet" />
                          </Inline>
                        </td>
                        <td data-kind="text" data-align="start">
                          <Stack gap={2}>
                            <Text variant="body">{j.source}</Text>
                            <Text variant="meta" className="sd-mono">
                              {j.source_event}
                            </Text>
                          </Stack>
                        </td>
                        <td data-kind="numeric" data-align="end" data-numeric="true">
                          <Text className="sd-tabular">{j.effective_date}</Text>
                        </td>
                        <td data-kind="role" data-align="start">
                          <Badge tone="neutral" variant="outline" dot={false}>
                            {j.currency}
                          </Badge>
                        </td>
                        <td data-kind="text" data-align="start">
                          <Text variant="meta" className="sd-tabular">
                            {lineSummary || "No lines"}
                          </Text>
                        </td>
                        <td data-kind="actions" data-align="end">
                          <Button
                            emphasis="quiet"
                            size="compact"
                            shape="pill"
                            aria-expanded={isExpanded}
                            onClick={() => toggleJournal(j.entry_id)}
                          >
                            {isExpanded ? "Hide" : "Lines"}
                          </Button>
                        </td>
                      </tr>
                      {isExpanded ? (
                        <tr className="sd-tape-detail-row">
                          <td colSpan={JOURNAL_COLUMNS.length}>
                            <div className="sd-ledger-detail-sticky">
                              <List label={`Journal lines for ${j.entry_id}`} divided>
                                {orderedLines.map((line) => (
                                  <li key={line.ordinal} className="yg-list-item sd-list-row-padded">
                                    <Inline
                                      justify="between"
                                      align="center"
                                      gap={8}
                                      wrap
                                    >
                                      <Inline gap={8} align="center">
                                        <Text variant="meta" className="sd-tabular">
                                          #{line.ordinal}
                                        </Text>
                                        <Badge
                                          tone={line.amount_minor >= 0 ? "success" : "neutral"}
                                          dot={false}
                                        >
                                          {line.amount_minor >= 0 ? "Debit" : "Credit"}
                                        </Badge>
                                        <Text className="sd-mono">{line.account}</Text>
                                      </Inline>
                                      <Text className="sd-tabular">
                                        {formatMinorCurrency(Math.abs(line.amount_minor), j.currency)}
                                      </Text>
                                    </Inline>
                                  </li>
                                ))}
                              </List>
                            </div>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </Table>
          )}
        </Stack>
      </Card>

      {data.trading_enabled || data.funding_settlements.length > 0 ? (
        <DisclosureRow
          label={`8h Funding Settlements (${data.funding_settlements.length})`}
          initiallyOpen={false}
        >
          {data.funding_settlements.length === 0 ? (
            <Text variant="help">No funding intervals recorded.</Text>
          ) : (
            <List label="8h funding settlements" divided>
              {data.funding_settlements.map((f) => (
                <li key={f.settlement_id} className="yg-list-item">
                  <WorkEvent
                    event={{
                      id: f.settlement_id,
                      kind: "routine",
                      title: `${f.instrument_id} · pos ${f.signed_position_lots}`,
                      detail:
                        f.status === "unavailable"
                          ? `Unavailable (${f.unavailable_reason ?? "missing"})`
                          : formatMinorCurrency(f.funding_cashflow_atoms),
                      time: formatNsWithClock(f.interval_end_ns),
                      state: f.status === "unavailable" ? "waiting" : "done",
                    }}
                  />
                </li>
              ))}
            </List>
          )}
        </DisclosureRow>
      ) : null}
    </Stack>
  );
}

export function OrgReportsView() {
  const { navigate, askCeo, refreshSeq, reportApiError, meta, routeSearchParams } = useApp();
  const [data, setData] = useState<OrgProjection | null>(null);
  const [graph, setGraph] = useState<AgentsGraphProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [tab, setTab] = useState(() =>
    routeSearchParams.get("tab") === "roster" ? "roster" : "reports"
  );
  const [rosterQuery, setRosterQuery] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [reportsQuery, setReportsQuery] = useState("");
  const [expandedPubs, setExpandedPubs] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const [orgRes, graphRes] = await Promise.all([
          fetchProjection<OrgProjection>("org", { signal: controller.signal }),
          fetchProjection<AgentsGraphProjection>("agents-graph", {
            signal: controller.signal,
          }).catch(() => null),
        ]);
        if (!active || controller.signal.aborted) return;
        if (orgRes.data) setData(orgRes.data);
        if (graphRes?.data) setGraph(graphRes.data);
        setError(null);
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setError(err instanceof Error ? err.message : "Could not load organization data.");
      } finally {
        if (active && !controller.signal.aborted) setLoading(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [refreshSeq, retrySeq, reportApiError]);

  const graphById = useMemo(
    () => new Map((graph?.nodes ?? []).map((n) => [n.agent_id, n])),
    [graph?.nodes]
  );

  const agentNameById = useMemo(() => {
    const map = new Map<string, string>();
    for (const a of data?.roster ?? []) {
      if (a.agent_id && a.display_name) map.set(a.agent_id, a.display_name);
    }
    for (const n of graph?.nodes ?? []) {
      if (n.agent_id && n.display_name) map.set(n.agent_id, n.display_name);
    }
    for (const p of data?.publications ?? []) {
      if (p.author_id && p.author_name && !map.has(p.author_id)) {
        map.set(p.author_id, p.author_name);
      }
    }
    if (meta?.founder_id) {
      map.set(meta.founder_id, "Founder · CEO");
    }
    return map;
  }, [data?.roster, data?.publications, graph?.nodes, meta?.founder_id]);

  const displayNameFor = useCallback(
    (id: string): string => agentNameById.get(id) ?? id,
    [agentNameById]
  );

  const filteredRoster = useMemo(() => {
    const q = rosterQuery.trim().toLowerCase();
    return (data?.roster ?? []).filter((agent) => {
      if (!showArchived && agent.archived) return false;
      if (!q) return true;
      const g = graphById.get(agent.agent_id);
      const liveStatus = g?.live_status ?? agent.live_status ?? agent.onboarding_state ?? "active";
      return `${agent.display_name} ${agent.agent_id} ${agent.role_label} ${agent.grant_scope} ${agent.onboarding_state ?? ""} ${liveStatus}`
        .toLowerCase()
        .includes(q);
    });
  }, [data?.roster, showArchived, rosterQuery, graphById]);

  const filteredPublications = useMemo(() => {
    const q = reportsQuery.trim().toLowerCase();
    return (data?.publications ?? []).filter((p) => {
      if (!q) return true;
      const audienceNames = p.audience.map(displayNameFor).join(" ");
      return `${p.asset_id} ${p.author_name} ${p.author_role} ${p.project_id} ${p.work_item_id} ${p.audience.join(" ")} ${audienceNames} ${p.content}`
        .toLowerCase()
        .includes(q);
    });
  }, [data?.publications, reportsQuery, displayNameFor]);

  if (loading && !data) return <Skeleton label="Loading organization and reports" lines={5} />;
  if (!data) {
    return (
      <Result
        title="Org & Reports unavailable"
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
        {error ?? "Could not load organization data."}
      </Result>
    );
  }

  const custodyById = new Map(
    data.delegated_hr_custody.archived_employees.map((a) => [`${a.cutoff}-${a.agent_id}`, a])
  );
  const custodyCols: DataColumn[] = [
    {
      id: "cutoff",
      label: "Cutoff",
      kind: "numeric",
      align: "end",
      sortValue: (row) => custodyById.get(row.id)?.cutoff,
    },
    {
      id: "agent",
      label: "Archived Agent",
      kind: "identity",
      sortValue: (row) => custodyById.get(row.id)?.agent_name,
    },
    {
      id: "manager",
      label: "Custody Manager",
      kind: "text",
      sortValue: (row) => custodyById.get(row.id)?.manager_name,
    },
    {
      id: "employment_revision",
      label: "Employment Rev",
      kind: "text",
      sortValue: (row) => custodyById.get(row.id)?.employment_revision,
    },
    {
      id: "scope",
      label: "Prior Scope",
      kind: "role",
      sortValue: (row) => custodyById.get(row.id)?.previous_scope,
    },
    {
      id: "reason",
      label: "Reason",
      kind: "text",
      sortValue: (row) => custodyById.get(row.id)?.reason,
    },
  ];
  const custodyRows: DataRow[] = data.delegated_hr_custody.archived_employees.map((a) => ({
    id: `${a.cutoff}-${a.agent_id}`,
    cells: {
      cutoff: <Text className="sd-tabular">#{a.cutoff}</Text>,
      agent: (
        <Stack gap={2}>
          <Text variant="body">{a.agent_name}</Text>
          <Text variant="meta" className="sd-mono">
            {a.agent_id}
          </Text>
        </Stack>
      ),
      manager: `${a.manager_name} (${a.manager_id})`,
      employment_revision: (
        <Inline gap={6} align="center">
          <Text className="sd-mono">{a.employment_revision}</Text>
          <CopyButton value={a.employment_revision} presentation="quiet" />
        </Inline>
      ),
      scope: a.previous_scope,
      reason: a.reason,
    },
  }));

  const togglePub = (assetId: string) => {
    setExpandedPubs((prev) => {
      const next = new Set(prev);
      if (next.has(assetId)) next.delete(assetId);
      else next.add(assetId);
      return next;
    });
  };

  return (
    <Stack gap={16}>
      <PageHeader
        title="Reports"
        detail="Published company assets, employee roster, onboarding status, and delegated HR custody audit"
      />

      <Tabs
        label="Organization sections"
        presentation="pill"
        value={tab}
        onValueChange={setTab}
        items={[
          {
            id: "roster",
            label: `Roster (${filteredRoster.length})`,
            panel: tab === "roster" ? (
              <Card title="Company Roster & Onboarding" variant="raised">
                <Stack gap={12}>
                  <Stack gap={8}>
                    <FilterBar
                      label="Filter roster"
                      hiddenLabel
                      placeholder="Filter by agent, role, grant scope, or onboarding state..."
                      size="compact"
                      query={rosterQuery}
                      onQueryChange={setRosterQuery}
                    />
                    <Inline justify="end">
                      <Switch
                        label="Show archived"
                        checked={showArchived}
                        onCheckedChange={setShowArchived}
                      />
                    </Inline>
                  </Stack>
                  {filteredRoster.length === 0 ? (
                    <EmptyResult
                      query={rosterQuery}
                      onClear={() => setRosterQuery("")}
                    />
                  ) : (
                    <List label="Company agents" divided>
                      {filteredRoster.map((agent) => {
                        const graphNode = graphById.get(agent.agent_id);
                        const derivedAvatarState =
                          graphNode?.avatar_state ??
                          agent.avatar_state ??
                          (agent.archived
                            ? "done"
                            : agent.onboarding_state === "blocked"
                              ? "blocked"
                              : "ready");
                        const derivedLiveStatus =
                          graphNode?.live_status ??
                          agent.live_status ??
                          (agent.archived
                            ? "archived"
                            : agent.onboarding_state === "blocked"
                              ? "blocked"
                              : agent.onboarding_state ?? "active");
                        const onboardingLabel = agent.onboarding_state ?? "none";

                        return (
                          <li key={agent.agent_id} className="yg-list-item sd-list-row-padded">
                            <div data-focus-id={`org-agent-${agent.agent_id}`}>
                              <Stack gap={6}>
                                <AgentRow
                                  agent={{
                                    id: agent.agent_id,
                                    name: agent.display_name,
                                    role: `${agent.role_label} · ${derivedLiveStatus}`,
                                    state: derivedAvatarState,
                                    art: agent.avatar_variant,
                                    preview: `${agent.grant_scope || "No grant"} · ${agent.private_draft_count} private drafts`,
                                  }}
                                  onSelect={() =>
                                    navigate(`/agents/${encodeURIComponent(agent.agent_id)}`, {
                                      focusReturnId: `org-agent-${agent.agent_id}`,
                                    })
                                  }
                                />
                                <Inline gap={8} align="center" wrap>
                                  <Badge
                                    tone={
                                      agent.archived
                                        ? "neutral"
                                        : derivedLiveStatus === "blocked"
                                          ? "danger"
                                          : "brand"
                                    }
                                    dot={false}
                                  >
                                    {agent.archived ? "Archived" : derivedLiveStatus}
                                  </Badge>
                                  <Text variant="meta">
                                    Onboarding: {onboardingLabel} ({agent.onboarding_attempts}/3) · Budget:{" "}
                                    {formatMinorCurrency(agent.monthly_budget_minor)}
                                  </Text>
                                </Inline>
                              </Stack>
                            </div>
                          </li>
                        );
                      })}
                    </List>
                  )}
                </Stack>
              </Card>
            ) : null,
          },
          {
            id: "reports",
            label: `Published Reports (${data.publications.length})`,
            panel: tab === "reports" ? (
              <Card title="Selective Company Publications" variant="raised">
                <Stack gap={12}>
                  <FilterBar
                    label="Filter published reports"
                    hiddenLabel
                    placeholder="Filter by asset ID, author, project, work item, or content..."
                    size="compact"
                    query={reportsQuery}
                    onQueryChange={setReportsQuery}
                  />
                  {data.publications.length === 0 ? (
                    <Text variant="help">No published company reports yet.</Text>
                  ) : filteredPublications.length === 0 ? (
                    <EmptyResult query={reportsQuery} onClear={() => setReportsQuery("")} />
                  ) : (
                    <List label="Published company reports" divided>
                      {filteredPublications.map((p) => {
                        const isLong = p.content.length > 220;
                        const isExpanded = expandedPubs.has(p.asset_id);
                        const bodyText =
                          isLong && !isExpanded ? `${p.content.slice(0, 220)}…` : p.content;
                        const audienceList =
                          p.audience.length > 0 ? p.audience : ["internal"];

                        return (
                          <li key={p.asset_id} className="yg-list-item sd-list-row-padded">
                            <Surface variant="surface" density="compact">
                              <Stack gap={8}>
                                <Inline justify="between" align="center" gap={8} wrap>
                                  <Inline gap={6} align="center" wrap>
                                    <Heading level={4}>Report {p.asset_id}</Heading>
                                    <CopyButton value={p.asset_id} presentation="quiet" />
                                    <Text variant="meta">
                                      {p.author_name} ({p.author_role}) · #{p.project_id} · Work item:{" "}
                                      {p.work_item_id}
                                    </Text>
                                  </Inline>
                                  <Inline gap={6} align="center" wrap>
                                    {audienceList.map((aud) => (
                                      <Badge
                                        key={aud}
                                        tone="neutral"
                                        variant="outline"
                                        dot={false}
                                      >
                                        {displayNameFor(aud)}
                                      </Badge>
                                    ))}
                                  </Inline>
                                </Inline>
                                <Text variant="body">{bodyText}</Text>
                                <Inline gap={8} align="center" wrap>
                                  <Button
                                    emphasis="soft"
                                    size="compact"
                                    shape="pill"
                                    onClick={() =>
                                      askCeo({
                                        kind: "shared_publication",
                                        id: p.asset_id,
                                        label: `Report ${p.asset_id} by ${p.author_name}`,
                                      })
                                    }
                                  >
                                    Ask CEO about this report
                                  </Button>
                                  <Button
                                    emphasis="quiet"
                                    size="compact"
                                    shape="pill"
                                    onClick={() =>
                                      navigate(`/agents/${encodeURIComponent(p.author_id)}`)
                                    }
                                  >
                                    View author
                                  </Button>
                                  {isLong ? (
                                    <Button
                                      emphasis="quiet"
                                      size="compact"
                                      shape="pill"
                                      aria-expanded={isExpanded}
                                      onClick={() => togglePub(p.asset_id)}
                                    >
                                      {isExpanded ? "Collapse" : "Show full report"}
                                    </Button>
                                  ) : null}
                                </Inline>
                              </Stack>
                            </Surface>
                          </li>
                        );
                      })}
                    </List>
                  )}
                </Stack>
              </Card>
            ) : null,
          },
          {
            id: "custody",
            label: `Delegated HR Custody (${custodyRows.length})`,
            panel: tab === "custody" ? (
              <Stack gap={12}>
                <Notice title={data.delegated_hr_custody.policy_notice} tone="neutral">
                  Routine onboarding and offboarding custody transfers are delegated to Founder ·
                  CEO under standing authority. Board members inspect offboarding custody transfers
                  here read-only.
                </Notice>
                <Card
                  title="Archived Employees & Custody Ledger"
                  variant="raised"
                  className="sd-ledger-card"
                >
                  {custodyRows.length === 0 ? (
                    <Text variant="help">No archived employees recorded.</Text>
                  ) : (
                    <DataTable
                      caption="Delegated HR custody ledger"
                      columns={custodyCols}
                      rows={custodyRows}
                    />
                  )}
                </Card>
              </Stack>
            ) : null,
          },
        ]}
      />
    </Stack>
  );
}

export function RestrictionsView() {
  const { refreshSeq, meta, canWrite, outbox, dispatchCommand, reportApiError } = useApp();
  const [data, setData] = useState<RestrictionsProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [tab, setTab] = useState("active");
  const [historyQuery, setHistoryQuery] = useState("");
  const [clearingCause, setClearingCause] = useState<RestrictionItem | null>(null);
  const [addPauseOpen, setAddPauseOpen] = useState(false);
  const [tradingRiskConfirmOpen, setTradingRiskConfirmOpen] = useState(false);
  const [tradingRiskScope, setTradingRiskScope] = useState<
    "execution:all" | "execution:paper" | "execution:live"
  >("execution:all");

  const controlApprovals = usePendingApprovals(
    (q) =>
      q.kind === "risk_release" ||
      q.kind === "restriction_approval" ||
      q.kind === "restriction_release" ||
      q.kind === "pause_company"
  );

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const res = await fetchProjection<RestrictionsProjection>("restrictions", {
          signal: controller.signal,
        });
        if (!active || controller.signal.aborted) return;
        if (res.data) setData(res.data);
        setError(null);
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setError(err instanceof Error ? err.message : "Could not load restrictions.");
      } finally {
        if (active && !controller.signal.aborted) setLoading(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [refreshSeq, retrySeq, reportApiError]);

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
      opened: <Text className="sd-tabular">{formatRelativeNs(normalizeToNs(h.recorded_at_ns))}</Text>,
      cleared: (
        <Text className="sd-tabular">
          {h.cleared_at_ns ? formatRelativeNs(normalizeToNs(h.cleared_at_ns)) : "Not cleared"}
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
            consequence:
              "Adds a distinct operational restriction cause without disturbing existing stops or open positions.",
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
            {!releasableTradingCause && (data.trading_risk.paper_suspended || data.trading_risk.live_suspended) ? (
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
                            description={`Scope: ${cause.scope_ref} · Rev ${cause.restriction_revision} · ${formatRelativeNs(normalizeToNs(cause.recorded_at_ns))}`}
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
