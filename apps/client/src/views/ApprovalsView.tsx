import { useEffect, useState } from "react";
import {
  Badge,
  Button,
  ButtonGroup,
  Card,
  DataTable,
  Inline,
  PageHeader,
  Result,
  Skeleton,
  Stack,
  Text,
  type DataColumn,
  type DataRow,
} from "@openboa/yogurt";
import {
  fetchProjection,
  formatRelativeNs,
  newCommandId,
} from "../api/client";
import type { ApprovalQuestionItem, ApprovalsProjection } from "../api/types";
import { ControlledInlineConfirm } from "../components/ControlledInlineConfirm";
import { useApp } from "../state/store";

/**
 * Board Approvals (/company/approvals):
 * ZERO dimmed popup modals and ZERO right slide-over drawers.
 * Clicking any pending item expands an inline confirmation panel right inside the card.
 */
export function ApprovalsView() {
  const { navigate, refreshSeq, meta, canWrite, dispatchCommand, reportApiError } = useApp();
  const [data, setData] = useState<ApprovalsProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [selectedChoice, setSelectedChoice] = useState<Record<string, string>>({});

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const res = await fetchProjection<ApprovalsProjection>("approvals", {
          signal: controller.signal,
        });
        if (res.data) {
          setData(res.data);
          // Auto-expand first pending approval if none selected and user has pending work.
          if (res.data.pending.length === 1 && expandedId === null) {
            setExpandedId(res.data.pending[0].question_id);
          }
        }
      } catch (err) {
        if (!controller.signal.aborted) reportApiError(err);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [refreshSeq, reportApiError]);

  if (loading && !data) {
    return <Skeleton label="Loading pending board approvals" lines={5} />;
  }

  if (!data) {
    return <Result title="Board Approvals unavailable">Could not load approvals queue.</Result>;
  }

  const submitAnswer = (item: ApprovalQuestionItem, choice: string, reason: string) => {
    if (!meta) return;
    void dispatchCommand({
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
      label: `${choice === "decline" ? "Decline" : "Approve"}: ${item.title}`,
      sent_ns: Date.now() * 1_000_000,
    });
  };

  const historyCols: DataColumn[] = [
    { id: "title", label: "Decision", kind: "identity" },
    { id: "choice", label: "Accepted Choice", kind: "role" },
    { id: "app", label: "Application Status", kind: "role" },
    { id: "deliv", label: "Delivery Status", kind: "role" },
    { id: "when", label: "Resolved", kind: "numeric", align: "end" },
  ];

  const historyRows: DataRow[] = data.history.map((item) => ({
    id: item.question_id,
    cells: {
      title: (
        <Stack gap={2}>
          <Text variant="body">{item.title}</Text>
          <Text variant="meta">{item.resolution_reason ?? item.summary}</Text>
        </Stack>
      ),
      choice: (
        <Badge tone={item.state === "applied" ? "success" : "neutral"} dot={false}>
          {item.resolution_choice ?? item.state}
        </Badge>
      ),
      app: (
        <Badge tone={item.application_status === "applied" ? "success" : "neutral"} dot={false}>
          {item.application_status}
        </Badge>
      ),
      deliv: (
        <Badge tone={item.delivery_status === "delivered" ? "brand" : "neutral"} dot={false}>
          {item.delivery_status}
        </Badge>
      ),
      when: <Text className="sd-tabular">{formatRelativeNs(item.resolved_at_ns)}</Text>,
    },
  }));

  return (
    <Stack gap={16}>
      <PageHeader
        title="Board Approvals"
        detail={`${data.pending_count} pending decisions · Click any card to review and confirm inline (no popups)`}
      />

      {data.pending.length === 0 ? (
        <Card variant="outlined">
          <Result title="No pending Board approvals">
            All governance requests have been resolved. New proposals from Founder · CEO will appear
            here and increment the sidebar badge automatically.
          </Result>
        </Card>
      ) : (
        <Stack gap={12}>
          {data.pending.map((item) => {
            const isOpen = expandedId === item.question_id;
            const choice = selectedChoice[item.question_id] ?? "approve";
            const isDecline = choice === "decline";
            return (
              <Card
                key={item.question_id}
                variant="outlined"
                title={item.title}
                description={`Requested by ${item.created_by_name} (${item.created_by_role}) · ${formatRelativeNs(item.created_at_ns)}`}
                headerAction={
                  <Inline gap={8} align="center">
                    <Badge tone="brand" variant="soft" dot={false}>
                      {item.kind.replace("_", " ")}
                    </Badge>
                    <Button
                      emphasis={isOpen ? "quiet" : "soft"}
                      tone={isOpen ? "neutral" : "brand"}
                      size="compact"
                      shape="pill"
                      onClick={() =>
                        setExpandedId(isOpen ? null : item.question_id)
                      }
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
                        <ButtonGroup label="Select decision stance">
                          <Button
                            emphasis={!isDecline ? "filled" : "soft"}
                            tone={!isDecline ? "brand" : "neutral"}
                            size="compact"
                            shape="pill"
                            onClick={() =>
                              setSelectedChoice((prev) => ({
                                ...prev,
                                [item.question_id]: "approve",
                              }))
                            }
                          >
                            Approve Path
                          </Button>
                          <Button
                            emphasis={isDecline ? "filled" : "soft"}
                            tone={isDecline ? "danger" : "neutral"}
                            size="compact"
                            shape="pill"
                            onClick={() =>
                              setSelectedChoice((prev) => ({
                                ...prev,
                                [item.question_id]: "decline",
                              }))
                            }
                          >
                            Decline Path
                          </Button>
                        </ButtonGroup>

                        <Button
                          emphasis="quiet"
                          size="compact"
                          shape="pill"
                          onClick={() =>
                            navigate("/messages", {
                              contextRef: {
                                kind: "decision_question",
                                id: item.question_id,
                                label: item.title,
                              },
                            })
                          }
                        >
                          Ask CEO in Messages →
                        </Button>
                      </Inline>

                      <ControlledInlineConfirm
                        open={isOpen}
                        onCancel={() => setExpandedId(null)}
                        decision={{
                          id: item.question_id,
                          revision: `${item.revision}:${item.payload_hash}`,
                          title: isDecline
                            ? `Decline: ${item.title}`
                            : item.impact.confirm_verb ?? `Approve: ${item.title}`,
                          consequence: isDecline
                            ? "Records a formal Board decline and notifies Founder · CEO without applying the proposed change."
                            : item.impact.consequence_note ??
                              "Records formal Board authorization and delivers the signed decision to the company runtime.",
                          confirmLabel: isDecline
                            ? "Decline Proposal"
                            : item.impact.confirm_verb ?? "Approve Proposal",
                          destructive: isDecline || Boolean(item.impact.destructive),
                          available: canWrite,
                          requireReason: true,
                          defaultReason: isDecline
                            ? "Board declined proposal pending further evidence"
                            : "Board approved after reviewing impact and untouched scope",
                          whatChanges: isDecline
                            ? [
                                { label: "Proposal status", value: "Marked declined" },
                                { label: "CEO notification", value: "Delivered with Board rationale" },
                              ]
                            : item.impact.what_changes ?? [
                                { label: "Decision status", value: "Approved and applied" },
                              ],
                          whatStaysUntouched: item.impact.what_stays_untouched ?? [
                            { label: "Paper-only boundary", value: "Live trading remains disabled" },
                            { label: "Existing positions", value: "Open exposure untouched" },
                          ],
                          technicalDetails: [
                            { label: "question_id", value: item.question_id },
                            { label: "expected_revision", value: String(item.revision) },
                            { label: "expected_payload_hash", value: item.payload_hash },
                            { label: "contract_version", value: "decision-command-v1" },
                          ],
                        }}
                        onConfirm={({ reason }) => {
                          submitAnswer(item, choice, reason);
                        }}
                      />
                    </Stack>
                  ) : null}
                </Stack>
              </Card>
            );
          })}
        </Stack>
      )}

      {/* Resolution & Delivery Ledger */}
      <Card
        title="Decision Resolution & Delivery Ledger"
        description="Tracks accepted Board choice, application status, and delivery status separately"
        variant="outlined"
      >
        {historyRows.length === 0 ? (
          <Text variant="help">No resolved Board decisions recorded yet.</Text>
        ) : (
          <DataTable
            caption="Board decision history"
            columns={historyCols}
            rows={historyRows}
          />
        )}
      </Card>
    </Stack>
  );
}
