import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { CopyButton } from "@openboa/yogurt/motion-copy-button";
import {
  Badge,
  Button,
  Card,
  EmptyResult,
  FilterBar,
  Inline,
  List,
  Navbar,
  Notice,
  PageHeader,
  RecordFacts,
  ResourceRow,
  Result,
  SegmentedControl,
  Select,
  Skeleton,
  Stack,
  Surface,
  Table,
  Text,
  type Fact,
} from "@openboa/yogurt";
import {
  fetchProjection,
  formatDeadlineNs,
  formatRelativeNs,
  newCommandId,
} from "../api/client";
import type { ApprovalQuestionItem, ApprovalsProjection } from "../api/types";
import { ControlledInlineConfirm } from "../components/ControlledInlineConfirm";
import { SpaBreadcrumb } from "../components/SpaBreadcrumb";
import { useApp } from "../state/store";

function normalizeToNs(epoch: number | null | undefined): number | null {
  if (epoch === null || epoch === undefined || !Number.isFinite(epoch) || epoch <= 0) {
    return null;
  }
  if (epoch < 1e12) return Math.round(epoch * 1_000_000_000);
  if (epoch < 1e15) return Math.round(epoch * 1_000_000);
  return epoch;
}

function isQuestionExpired(item: ApprovalQuestionItem, nowMs: number): boolean {
  if (item.state === "expired") return true;
  const deadlineNs = normalizeToNs(item.respond_by_ns);
  return deadlineNs !== null && deadlineNs <= nowMs * 1_000_000;
}

function formatKindLabel(kind: string): string {
  return kind.replace(/_/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase());
}

function formatChoiceLabel(choice: string): string {
  if (choice === "approve") return "Approve";
  if (choice === "defer") return "Defer";
  if (choice === "decline") return "Decline";
  return choice.replace(/_/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase());
}

function buildDecisionMeta(item: ApprovalQuestionItem, choice: string) {
  if (choice === "decline") {
    return {
      title: "Decline Proposal",
      consequence:
        "Records a formal Board decline and notifies Founder · CEO without applying the proposed change.",
      confirmLabel: "Decline Proposal",
      destructive: true,
      defaultReason: "Board declined proposal pending further evidence",
      whatChanges: [
        { label: "Proposal status", value: "Marked declined" },
        { label: "CEO notification", value: "Delivered with Board rationale" },
      ],
    };
  }
  if (choice === "approve") {
    return {
      title: item.impact.confirm_verb ?? "Confirm Approval",
      consequence:
        item.impact.consequence_note ??
        "Records formal Board authorization and delivers the signed decision to the company runtime.",
      confirmLabel: item.impact.confirm_verb ?? "Approve Proposal",
      destructive: Boolean(item.impact.destructive),
      defaultReason: "Board approved after reviewing impact and untouched scope",
      whatChanges: item.impact.what_changes ?? [
        { label: "Decision status", value: "Approved and applied" },
      ],
    };
  }
  const choiceLabel = formatChoiceLabel(choice);
  return {
    title: `Confirm "${choiceLabel}"`,
    consequence: `Records formal Board "${choiceLabel}" response and delivers it to Founder · CEO.`,
    confirmLabel: `Submit "${choiceLabel}"`,
    destructive: false,
    defaultReason: `Board selected "${choiceLabel}" pending follow-up`,
    whatChanges: [
      { label: "Recorded choice", value: choiceLabel },
      { label: "CEO notification", value: "Delivered with Board rationale" },
    ],
  };
}

function formatVerdictDetail(detail: Record<string, unknown>): string {
  const parts: string[] = [];
  if (typeof detail.mean_diff_bps === "number") {
    parts.push(`Mean diff ${detail.mean_diff_bps.toFixed(1)} bps`);
  }
  if (typeof detail.lower_bound_bps === "number") {
    parts.push(`Lower bound ${detail.lower_bound_bps.toFixed(1)} bps`);
  }
  if (typeof detail.alpha_effective === "number") {
    parts.push(`Alpha ${detail.alpha_effective.toFixed(3)}`);
  }
  return parts.join(" · ");
}

interface LedgerColumnDef {
  id: string;
  label: string;
  kind: "identity" | "text" | "numeric" | "role" | "actions";
  align: "start" | "end";
  sortValue?: (item: ApprovalQuestionItem) => string | number | null;
}

const LEDGER_COLUMNS: LedgerColumnDef[] = [
  {
    id: "question",
    label: "Question",
    kind: "identity",
    align: "start",
    sortValue: (item) => item.title,
  },
  {
    id: "kind",
    label: "Kind",
    kind: "text",
    align: "start",
    sortValue: (item) => item.kind,
  },
  {
    id: "choice",
    label: "Choice",
    kind: "role",
    align: "start",
    sortValue: (item) => item.resolution_choice ?? item.state,
  },
  {
    id: "created_by",
    label: "Created By",
    kind: "text",
    align: "start",
    sortValue: (item) => item.created_by_name,
  },
  {
    id: "resolved_at",
    label: "Resolved At",
    kind: "numeric",
    align: "end",
    sortValue: (item) => normalizeToNs(item.resolved_at_ns),
  },
  {
    id: "command_id",
    label: "Command ID",
    kind: "text",
    align: "start",
    sortValue: (item) => item.resolved_command_id ?? "",
  },
  {
    id: "delivery",
    label: "Delivery",
    kind: "role",
    align: "start",
    sortValue: (item) => `${item.application_status}:${item.delivery_status}`,
  },
  {
    id: "details",
    label: "Details",
    kind: "actions",
    align: "end",
  },
];

export function ApprovalsView() {
  const {
    navigate,
    askCeo,
    activeApprovalQuestionId,
    registerEscapeBack,
    refreshSeq,
    meta,
    canWrite,
    outbox,
    dispatchCommand,
    reportApiError,
  } = useApp();

  const [data, setData] = useState<ApprovalsProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [selectedChoice, setSelectedChoice] = useState<Record<string, string>>({});
  const [query, setQuery] = useState("");
  const [kindFilter, setKindFilter] = useState("all");
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [ledgerSort, setLedgerSort] = useState<{ id: string; descending: boolean } | null>(
    null
  );
  const [expandedHistoryIds, setExpandedHistoryIds] = useState<Set<string>>(() => new Set());

  const autoExpandedOnceRef = useRef(false);
  const handledDeepLinkRef = useRef<string | null>(null);
  const cardRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const historyRowRefs = useRef<Record<string, HTMLTableRowElement | null>>({});

  useEffect(() => {
    if (!activeApprovalQuestionId) return;
    return registerEscapeBack(
      "/approvals",
      `approval-${activeApprovalQuestionId}`
    );
  }, [activeApprovalQuestionId, registerEscapeBack]);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const res = await fetchProjection<ApprovalsProjection>("approvals", {
          signal: controller.signal,
        });
        if (!active || controller.signal.aborted) return;
        if (res.data) {
          setData(res.data);
        }
        setError(null);
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
        setError(err instanceof Error ? err.message : "Could not load approvals queue.");
      } finally {
        if (active && !controller.signal.aborted) setLoading(false);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [refreshSeq, retrySeq, reportApiError]);

  const hasDeadlines = Boolean(data?.pending.some((p) => p.respond_by_ns !== null));

  useEffect(() => {
    if (!hasDeadlines) return;
    setNowMs(Date.now());
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [hasDeadlines]);

  useEffect(() => {
    if (!data) return;
    const reducedMotion =
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const behavior: ScrollBehavior = reducedMotion ? "auto" : "smooth";

    if (activeApprovalQuestionId) {
      if (handledDeepLinkRef.current === activeApprovalQuestionId) return;
      handledDeepLinkRef.current = activeApprovalQuestionId;
      autoExpandedOnceRef.current = true;

      const pendingMatch = data.pending.find(
        (p) => p.question_id === activeApprovalQuestionId
      );
      if (pendingMatch) {
        setQuery("");
        setKindFilter("all");
        setExpandedId(pendingMatch.question_id);
        window.requestAnimationFrame(() => {
          cardRefs.current[pendingMatch.question_id]?.scrollIntoView({
            block: "nearest",
            behavior,
          });
        });
        return;
      }

      const historyMatch = data.history.find(
        (h) => h.question_id === activeApprovalQuestionId
      );
      if (historyMatch) {
        setExpandedHistoryIds((prev) => new Set(prev).add(historyMatch.question_id));
        window.requestAnimationFrame(() => {
          historyRowRefs.current[historyMatch.question_id]?.scrollIntoView({
            block: "nearest",
            behavior,
          });
        });
      }
      return;
    }

    handledDeepLinkRef.current = null;
    if (!autoExpandedOnceRef.current) {
      autoExpandedOnceRef.current = true;
      if (data.pending.length === 1) {
        setExpandedId(data.pending[0].question_id);
      }
    }
  }, [activeApprovalQuestionId, data]);

  const uniqueKinds = useMemo(() => {
    if (!data) return [];
    return Array.from(new Set(data.pending.map((p) => p.kind)));
  }, [data]);

  const filteredPending = useMemo(() => {
    if (!data) return [];
    const q = query.trim().toLowerCase();
    return data.pending.filter((item) => {
      if (kindFilter !== "all" && item.kind !== kindFilter) return false;
      if (!q) return true;
      return (
        item.title.toLowerCase().includes(q) ||
        item.summary.toLowerCase().includes(q) ||
        item.question_id.toLowerCase().includes(q) ||
        item.kind.toLowerCase().includes(q) ||
        item.created_by_name.toLowerCase().includes(q)
      );
    });
  }, [data, kindFilter, query]);

  const sortedHistory = useMemo(() => {
    if (!data) return [];
    if (!ledgerSort) return data.history;
    const col = LEDGER_COLUMNS.find((c) => c.id === ledgerSort.id);
    if (!col?.sortValue) return data.history;
    const extract = col.sortValue;
    return [...data.history].sort((a, b) => {
      const av = extract(a);
      const bv = extract(b);
      if (av === null && bv === null) return 0;
      if (av === null) return 1;
      if (bv === null) return -1;
      let cmp = 0;
      if (typeof av === "number" && typeof bv === "number") {
        cmp = av - bv;
      } else {
        cmp = String(av).localeCompare(String(bv), undefined, {
          numeric: true,
          sensitivity: "base",
        });
      }
      return ledgerSort.descending ? -cmp : cmp;
    });
  }, [data, ledgerSort]);

  if (loading && !data) {
    return <Skeleton label="Loading pending board approvals" lines={5} />;
  }

  if (!data) {
    return (
      <Result
        title="Board Approvals unavailable"
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
        {error ?? "Could not load approvals queue."}
      </Result>
    );
  }

  const matchedPending = activeApprovalQuestionId
    ? data.pending.find((p) => p.question_id === activeApprovalQuestionId) ?? null
    : null;
  const matchedHistory = activeApprovalQuestionId
    ? data.history.find((h) => h.question_id === activeApprovalQuestionId) ?? null
    : null;
  const matchedItem = matchedPending ?? matchedHistory;

  const toggleHistoryRow = (questionId: string) => {
    setExpandedHistoryIds((prev) => {
      const next = new Set(prev);
      if (next.has(questionId)) {
        next.delete(questionId);
      } else {
        next.add(questionId);
      }
      return next;
    });
  };

  const submitAnswer = async (
    item: ApprovalQuestionItem,
    choice: string,
    reason: string
  ) => {
    if (!meta) return;
    const lifecycle = await dispatchCommand({
      company_id: meta.company_id,
      command_id: newCommandId("approve"),
      command_type: "submit_decision_answer",
      contract_version: "decision-command-v1",
      preconditions: {
        question_id: item.question_id,
        expected_revision: item.revision,
        expected_payload_hash: item.payload_hash,
      },
      payload: { choice, reason },
      label: `${formatChoiceLabel(choice)}: ${item.title}`,
      sent_ns: Date.now() * 1_000_000,
    });
    if (
      lifecycle === "applied" ||
      lifecycle === "accepted" ||
      lifecycle === "unknown"
    ) {
      setExpandedId(null);
    }
  };

  return (
    <Stack gap={16}>
      {activeApprovalQuestionId ? (
        <Fragment>
          <Navbar
            variant="back"
            label="Back to Approvals"
            onBack={() =>
              navigate("/approvals", {
                focusReturnId: `approval-${activeApprovalQuestionId}`,
              })
            }
          />
          <SpaBreadcrumb
            focusReturnId={`approval-${activeApprovalQuestionId}`}
            items={[
              { id: "approvals", label: "Approvals", href: "/approvals" },
              {
                id: activeApprovalQuestionId,
                label: matchedItem?.title ?? activeApprovalQuestionId,
                href: `/approvals/${encodeURIComponent(activeApprovalQuestionId)}`,
              },
            ]}
          />
        </Fragment>
      ) : null}

      <PageHeader
        title="Approvals"
        detail={`${data.pending_count} pending decisions · Unified action queue synced live with every screen`}
      />

      {activeApprovalQuestionId && !matchedItem ? (
        <Notice
          title="Decision not found"
          tone="warning"
          action={
            <Button
              emphasis="soft"
              size="compact"
              shape="pill"
              onClick={() => navigate("/approvals")}
            >
              Show all approvals
            </Button>
          }
        >
          No pending or resolved Board decision matches "{activeApprovalQuestionId}".
        </Notice>
      ) : null}

      {activeApprovalQuestionId && matchedHistory ? (
        <Notice
          title="Decision already resolved"
          tone="neutral"
          action={
            <Button
              emphasis="soft"
              size="compact"
              shape="pill"
              onClick={() => navigate("/company/approvals")}
            >
              Show all approvals
            </Button>
          }
        >
          Decision "{matchedHistory.title}" was already resolved (
          {matchedHistory.resolution_choice ?? matchedHistory.state}). Its ledger record is
          expanded below.
        </Notice>
      ) : null}

      {data.pending.length === 0 ? (
        <Card variant="raised">
          <Result title="No pending Board approvals">
            All governance requests have been resolved. New proposals from Founder · CEO will appear
            here and increment the sidebar badge automatically.
          </Result>
        </Card>
      ) : (
        <Stack gap={12}>
          <FilterBar
            label="Filter approvals"
            hiddenLabel
            placeholder="Search pending approvals by title, summary, or ID..."
            size="compact"
            query={query}
            onQueryChange={setQuery}
          >
            <Select
              label="Category"
              hiddenLabel
              size="compact"
              value={kindFilter}
              onValueChange={setKindFilter}
              options={[
                { id: "all", label: "All categories" },
                ...uniqueKinds.map((k) => ({ id: k, label: formatKindLabel(k) })),
              ]}
            />
          </FilterBar>

          {filteredPending.length === 0 ? (
            <EmptyResult
              query={query || formatKindLabel(kindFilter)}
              onClear={() => {
                setQuery("");
                setKindFilter("all");
              }}
            />
          ) : (
            filteredPending.map((item) => {
              const isOpen = expandedId === item.question_id;
              const availableOptions =
                item.options.length > 0 ? item.options : ["approve", "decline"];
              const storedChoice = selectedChoice[item.question_id];
              const choice = availableOptions.includes(storedChoice)
                ? storedChoice
                : availableOptions[0];
              const deadlineNs = normalizeToNs(item.respond_by_ns);
              const isExpired = isQuestionExpired(item, nowMs);
              const isPendingThis =
                outbox.intent?.command_type === "submit_decision_answer" &&
                outbox.intent?.preconditions?.question_id === item.question_id &&
                (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted");
              const deadlineLabel =
                deadlineNs !== null
                  ? isExpired
                    ? `Deadline: ${formatDeadlineNs(deadlineNs)}`
                    : `Respond by: ${formatDeadlineNs(deadlineNs)}`
                  : "No deadline";
              const decisionMeta = buildDecisionMeta(item, choice);

              return (
                <div
                  key={item.question_id}
                  ref={(el) => {
                    cardRefs.current[item.question_id] = el;
                  }}
                  data-approval-id={item.question_id}
                >
                  <Card
                    variant="raised"
                    title={item.title}
                    description={`${formatKindLabel(item.kind)} · Requested by ${item.created_by_name} (${item.created_by_role}) · ${formatRelativeNs(item.created_at_ns)} · ${deadlineLabel}`}
                    headerAction={
                      <Inline gap={8} align="center">
                        {isExpired ? (
                          <Badge tone="danger" variant="soft" dot={false}>
                            Expired
                          </Badge>
                        ) : (
                          <Badge tone="brand" variant="soft" dot={false}>
                            {formatKindLabel(item.kind)}
                          </Badge>
                        )}
                        <Button
                          emphasis={isOpen ? "quiet" : "soft"}
                          size="compact"
                          shape="pill"
                          data-focus-id={`approval-${item.question_id}`}
                          onClick={() => setExpandedId(isOpen ? null : item.question_id)}
                        >
                          {isOpen ? "Collapse" : "Review & Decide"}
                        </Button>
                      </Inline>
                    }
                  >
                    <Stack gap={12}>
                      <Text variant="body">{item.summary}</Text>

                      {isOpen ? (
                        <Stack gap={12}>
                          <Inline justify="between" align="center" wrap>
                            <div className="sd-intrinsic">
                              <SegmentedControl
                                label="Select decision stance"
                                value={choice}
                                onValueChange={(next) =>
                                  setSelectedChoice((prev) => ({
                                    ...prev,
                                    [item.question_id]: next,
                                  }))
                                }
                                options={availableOptions.map((opt) => ({
                                  id: opt,
                                  label: formatChoiceLabel(opt),
                                }))}
                              />
                            </div>

                            <Button
                              emphasis="quiet"
                              size="compact"
                              shape="pill"
                              onClick={() =>
                                askCeo({
                                  kind: "decision_question",
                                  id: item.question_id,
                                  label: item.title,
                                })
                              }
                            >
                              Ask CEO in Messages →
                            </Button>
                          </Inline>

                          {isExpired ? (
                            <Notice title="Decision deadline expired" tone="warning">
                              The response deadline (
                              {deadlineNs ? formatDeadlineNs(deadlineNs) : "expired"}) has elapsed;
                              confirmation is disabled. Ask Founder · CEO in Messages if a new
                              proposal is needed.
                            </Notice>
                          ) : null}

                          <ControlledInlineConfirm
                            open={isOpen}
                            surface="inline"
                            pending={isPendingThis}
                            onCancel={() => setExpandedId(null)}
                            decision={{
                              id: item.question_id,
                              revision: `${item.revision}:${item.payload_hash}`,
                              title: decisionMeta.title,
                              consequence: decisionMeta.consequence,
                              confirmLabel: decisionMeta.confirmLabel,
                              destructive: decisionMeta.destructive,
                              available: (canWrite || isPendingThis) && !isExpired,
                              unavailableNotice: isExpired ? null : undefined,
                              requireReason: true,
                              defaultReason: decisionMeta.defaultReason,
                              whatChanges: decisionMeta.whatChanges,
                              whatStaysUntouched: item.impact.what_stays_untouched ?? [
                                {
                                  label: "Paper-only boundary",
                                  value: "Live trading remains disabled",
                                },
                                {
                                  label: "Existing positions",
                                  value: "Open exposure untouched",
                                },
                              ],
                              technicalDetails: [
                                { label: "question_id", value: item.question_id },
                                { label: "expected_revision", value: String(item.revision) },
                                {
                                  label: "expected_payload_hash",
                                  value: item.payload_hash,
                                },
                                {
                                  label: "contract_version",
                                  value: "decision-command-v1",
                                },
                              ],
                            }}
                            onConfirm={({ reason }) => submitAnswer(item, choice, reason)}
                          />
                        </Stack>
                      ) : null}
                    </Stack>
                  </Card>
                </div>
              );
            })
          )}
        </Stack>
      )}

      {data.promote_candidates.length > 0 ? (
        <Card
          variant="raised"
          title="Strategy Promotion Candidates"
          description="Challenger trials with PROMOTE verdict linked to Strategy Trials"
          headerAction={
            <Button
              emphasis="soft"
              size="compact"
              shape="pill"
              onClick={() => navigate("/company/strategy-trials")}
            >
              Strategy Trials →
            </Button>
          }
        >
          <List label="Strategy promotion candidates" divided>
            {data.promote_candidates.map((c) => {
              const detailMetrics = formatVerdictDetail(c.verdict_detail);
              const evaluatedLabel = c.evaluated_at_ns
                ? `Evaluated ${formatRelativeNs(normalizeToNs(c.evaluated_at_ns))}`
                : "Not evaluated";
              return (
                <li key={c.comparison_id} className="yg-list-item">
                  <ResourceRow
                    resource={{
                      id: c.comparison_id,
                      kind: "output",
                      title: `${c.comparison_id} · ${c.challenger_release_id} vs ${c.incumbent_release_id}`,
                      detail: [c.verdict, c.status, detailMetrics, evaluatedLabel]
                        .filter(Boolean)
                        .join(" · "),
                    }}
                    onOpen={() => navigate("/company/strategy-trials")}
                  />
                </li>
              );
            })}
          </List>
        </Card>
      ) : null}

      <Card
        title="Decision Resolution & Delivery Ledger"
        description="Tracks accepted Board choice, application status, and delivery status separately"
        variant="raised"
        className="sd-ledger-card"
      >
        {sortedHistory.length === 0 ? (
          <Text variant="help">No resolved Board decisions recorded yet.</Text>
        ) : (
          <Table caption="Board decision resolution and delivery ledger">
            <thead>
              <tr>
                {LEDGER_COLUMNS.map((col) => {
                  const isNumeric = col.kind === "numeric";
                  const isSorted = ledgerSort?.id === col.id && Boolean(col.sortValue);
                  return (
                    <th
                      key={col.id}
                      scope="col"
                      data-kind={col.kind}
                      data-align={col.align}
                      data-numeric={isNumeric || undefined}
                      aria-sort={
                        isSorted
                          ? ledgerSort!.descending
                            ? "descending"
                            : "ascending"
                          : undefined
                      }
                    >
                      {col.sortValue ? (
                        <button
                          type="button"
                          className="yg-table-sort"
                          onClick={() =>
                            setLedgerSort((prev) => ({
                              id: col.id,
                              descending: prev?.id === col.id && !prev.descending,
                            }))
                          }
                        >
                          {col.label}
                          <span aria-hidden="true">
                            {isSorted ? (ledgerSort!.descending ? "↓" : "↑") : "↕"}
                          </span>
                        </button>
                      ) : (
                        col.label
                      )}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {sortedHistory.map((item) => {
                const isExpanded = expandedHistoryIds.has(item.question_id);
                const resolvedNs = normalizeToNs(item.resolved_at_ns);
                const changeFacts: Fact[] = (
                  item.impact.what_changes ?? [
                    {
                      label: "Recorded choice",
                      value: item.resolution_choice ?? item.state,
                    },
                  ]
                ).map((entry, idx) => ({
                  id: `chg-${item.question_id}-${idx}`,
                  label: entry.label,
                  value: entry.value,
                }));
                const untouchedFacts: Fact[] = (
                  item.impact.what_stays_untouched ?? [
                    { label: "Paper-only boundary", value: "Live trading remains disabled" },
                  ]
                ).map((entry, idx) => ({
                  id: `stay-${item.question_id}-${idx}`,
                  label: entry.label,
                  value: entry.value,
                }));

                return (
                  <Fragment key={item.question_id}>
                    <tr
                      ref={(el) => {
                        historyRowRefs.current[item.question_id] = el;
                      }}
                      onClick={(event) => {
                        const target = event.target;
                        if (
                          target instanceof HTMLElement &&
                          target.closest("button, a, input, summary, [role='button']")
                        ) {
                          return;
                        }
                        toggleHistoryRow(item.question_id);
                      }}
                    >
                      <td data-kind="identity" data-align="start">
                        <Stack gap={2}>
                          <Text variant="body">{item.title}</Text>
                          <Text variant="meta" className="sd-mono">
                            {item.question_id}
                          </Text>
                        </Stack>
                      </td>
                      <td data-kind="text" data-align="start" data-nowrap="true">
                        <Text variant="meta">{formatKindLabel(item.kind)}</Text>
                      </td>
                      <td data-kind="role" data-align="start">
                        <Badge
                          tone={
                            item.state === "applied"
                              ? "success"
                              : item.state === "expired"
                                ? "danger"
                                : "neutral"
                          }
                          dot={false}
                        >
                          {item.resolution_choice ?? item.state}
                        </Badge>
                      </td>
                      <td data-kind="text" data-align="start">
                        <Stack gap={2}>
                          <Text variant="body">{item.created_by_name}</Text>
                          <Text variant="meta">{item.created_by_role}</Text>
                        </Stack>
                      </td>
                      <td data-kind="numeric" data-align="end" data-numeric="true">
                        <Text className="sd-tabular">
                          {resolvedNs ? formatRelativeNs(resolvedNs) : "Not resolved"}
                        </Text>
                      </td>
                      <td data-kind="text" data-align="start">
                        {item.resolved_command_id ? (
                          <Inline gap={6} align="center">
                            <Text className="sd-mono">{item.resolved_command_id}</Text>
                            <CopyButton
                              value={item.resolved_command_id}
                              presentation="quiet"
                            />
                          </Inline>
                        ) : (
                          <Text variant="meta">No command</Text>
                        )}
                      </td>
                      <td data-kind="role" data-align="start">
                        <Stack gap={2}>
                          <Badge
                            tone={item.application_status === "applied" ? "success" : "neutral"}
                            dot={false}
                          >
                            App: {item.application_status}
                          </Badge>
                          <Text variant="meta">Deliv: {item.delivery_status}</Text>
                        </Stack>
                      </td>
                      <td data-kind="actions" data-align="end">
                        <Button
                          emphasis="quiet"
                          size="compact"
                          shape="pill"
                          data-focus-id={`approval-${item.question_id}`}
                          aria-expanded={isExpanded}
                          onClick={() => toggleHistoryRow(item.question_id)}
                        >
                          {isExpanded ? "Hide" : "Details"}
                        </Button>
                      </td>
                    </tr>
                    {isExpanded ? (
                      <tr className="sd-tape-detail-row">
                        <td colSpan={LEDGER_COLUMNS.length}>
                          <div className="sd-ledger-detail-sticky">
                            <Stack gap={12}>
                              <Stack gap={4}>
                                <Text variant="cap">Resolution reason</Text>
                                <Text variant="body">
                                  {item.resolution_reason ?? "No reason recorded"}
                                </Text>
                              </Stack>
                              <Stack gap={4}>
                                <Text variant="cap">Decision summary</Text>
                                <Text variant="help">{item.summary}</Text>
                              </Stack>
                              <div className="sd-split-equal">
                                <Surface variant="raised" density="compact">
                                  <RecordFacts
                                    title="What changes"
                                    facts={changeFacts}
                                    source="Decision payload"
                                    asOf={`Revision ${item.revision}`}
                                  />
                                </Surface>
                                <Surface variant="raised" density="compact">
                                  <RecordFacts
                                    title="What stays untouched"
                                    facts={untouchedFacts}
                                    source="Preserved scope"
                                    asOf={`State: ${item.state}`}
                                  />
                                </Surface>
                              </div>
                              <Inline justify="between" align="center" gap={8} wrap>
                                <Text variant="meta" className="sd-mono">
                                  {item.question_id} · rev {item.revision} · {item.payload_hash}
                                </Text>
                                <Button
                                  emphasis="quiet"
                                  size="compact"
                                  shape="pill"
                                  onClick={() =>
                                    askCeo({
                                      kind: "decision_question",
                                      id: item.question_id,
                                      label: item.title,
                                    })
                                  }
                                >
                                  Ask CEO in Messages →
                                </Button>
                              </Inline>
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
      </Card>
    </Stack>
  );
}
