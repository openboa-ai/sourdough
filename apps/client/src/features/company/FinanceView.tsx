import { Fragment, useMemo, useState } from "react";
import { CopyButton } from "@openboa/yogurt/motion-copy-button";
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
  Notice,
  PageHeader,
  RecordFacts,
  Result,
  Skeleton,
  Stack,
  Surface,
  Table,
  Text,
  WorkEvent,
  type DataColumn,
  type DataRow,
  type Fact,
} from "@openboa/yogurt";
import type { FinanceProjection } from "../../api/types";
import { useProjection } from "../../api/useProjection";
import {
  formatMinorCurrency,
  formatNsWithClock,
  formatRelativeNs,
} from "../../lib/format";
import {
  renderSortableTableHead,
  sortItems,
  type SortState,
  type SortableColumn,
} from "../../views/tableUtils";

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
  const { data, loading, error, refetch } = useProjection<FinanceProjection>("finance");
  const [closeQuery, setCloseQuery] = useState("");
  const [closeSort, setCloseSort] = useState<SortState>(null);
  const [expandedCloses, setExpandedCloses] = useState<Set<string>>(() => new Set());
  const [journalQuery, setJournalQuery] = useState("");
  const [journalSort, setJournalSort] = useState<SortState>({ id: "date", descending: true });
  const [expandedJournals, setExpandedJournals] = useState<Set<string>>(() => new Set());

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
          <Button emphasis="soft" shape="pill" onClick={refetch}>
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

  const showRetainedStatements =
    data.trading_enabled || data.retained_statements.length > 0;
  const showFundingSettlements =
    data.trading_enabled || data.funding_settlements.length > 0;
  const financeDetail =
    showRetainedStatements && showFundingSettlements
      ? "Double-entry general ledger, period accounting closes, retained statements, and funding settlements"
      : showRetainedStatements
        ? "Double-entry general ledger, period accounting closes, and retained statements"
        : showFundingSettlements
          ? "Double-entry general ledger, period accounting closes, and funding settlements"
          : "Double-entry general ledger and period accounting closes";

  return (
    <Stack gap={16}>
      <PageHeader title="Finance" detail={financeDetail} />

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

      <div className={showRetainedStatements ? "sd-split-equal" : undefined}>
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

        {showRetainedStatements ? (
          <Card
            title={`Retained Window Statements (${data.retained_statements.length})`}
            description="Sealed window statement snapshots retained for reconciliation and strategy evaluation"
            variant="raised"
          >
            {data.retained_statements.length === 0 ? (
              <Text variant="help">No retained window statements sealed yet.</Text>
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
        ) : null}
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
                            {c.assessed_ns ? formatRelativeNs(c.assessed_ns) : "Not assessed"}
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
