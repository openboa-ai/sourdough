import { useEffect, useState } from "react";
import { Badge, Button, Card, Inline, Stack, Text } from "@openboa/yogurt";
import { fetchProjection, formatDeadlineNs, newCommandId } from "../api/client";
import type { ApprovalQuestionItem, ApprovalsProjection } from "../api/types";
import { ControlledInlineConfirm } from "./ControlledInlineConfirm";
import { useApp } from "../state/store";

export function usePendingApprovals(
  predicate: (item: ApprovalQuestionItem) => boolean
): ApprovalQuestionItem[] {
  const { refreshSeq, linkState, reportApiError } = useApp();
  const [items, setItems] = useState<ApprovalQuestionItem[]>([]);

  useEffect(() => {
    if (linkState === "unauthorized") return;
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const res = await fetchProjection<ApprovalsProjection>("approvals", {
          signal: controller.signal,
        });
        if (!active || controller.signal.aborted) return;
        if (res.data) {
          setItems(res.data.pending.filter((q) => q.state === "open"));
        }
      } catch (err) {
        if (!active || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        reportApiError(err);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [refreshSeq, linkState, reportApiError]);

  return items.filter(predicate);
}

export function ContextualApprovalCard({
  question,
}: {
  question: ApprovalQuestionItem;
}) {
  const { meta, canWrite, outbox, dispatchCommand, navigate, pushToast } = useApp();
  const [expandedChoice, setExpandedChoice] = useState<string | null>(null);

  if (!meta) return null;

  const isSubmitting =
    (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
    outbox.intent?.command_type === "submit_decision_answer" &&
    outbox.intent?.preconditions?.question_id === question.question_id;

  const approveChoice = question.options.includes("approve")
    ? "approve"
    : question.options[0] ?? "approve";
  const declineChoice = question.options.includes("decline") ? "decline" : null;
  const confirmVerb = question.impact.confirm_verb ?? "Confirm Approval";

  const whatChanges = question.impact.what_changes ?? [
    { label: "Decision", value: question.title },
    { label: "Kind", value: question.kind },
  ];
  const whatStaysUntouched = question.impact.what_stays_untouched ?? [
    { label: "Audit Trail", value: "Decision receipt sealed in command_receipt" },
    { label: "Other Systems", value: "Unrelated domains remain unaffected" },
  ];

  return (
    <Card
      variant="raised"
      density="compact"
      title={question.title}
      description={`${question.summary} · Requested by ${question.created_by_name}`}
      headerAction={
        <Inline gap={6} align="center" wrap>
          <Badge tone="brand" dot={false}>
            Approval Needed
          </Badge>
          {question.respond_by_ns ? (
            <Text variant="meta" className="sd-tabular">
              Due {formatDeadlineNs(question.respond_by_ns)}
            </Text>
          ) : null}
        </Inline>
      }
    >
      <Stack gap={12}>
        <Inline justify="between" align="center" gap={8} wrap>
          <Inline gap={8} align="center" wrap>
            <Button
              emphasis="filled"
              tone="brand"
              size="compact"
              shape="pill"
              disabled={!canWrite}
              onClick={() =>
                setExpandedChoice((cur) => (cur === approveChoice ? null : approveChoice))
              }
            >
              {expandedChoice === approveChoice ? "Close" : confirmVerb}
            </Button>
            {declineChoice ? (
              <Button
                emphasis="soft"
                tone="danger"
                size="compact"
                shape="pill"
                disabled={!canWrite}
                onClick={() =>
                  setExpandedChoice((cur) => (cur === declineChoice ? null : declineChoice))
                }
              >
                {expandedChoice === declineChoice ? "Close" : "Decline"}
              </Button>
            ) : null}
          </Inline>
          <Button
            emphasis="quiet"
            size="compact"
            shape="pill"
            onClick={() =>
              navigate(`/approvals/${encodeURIComponent(question.question_id)}`)
            }
          >
            Open in Approvals
          </Button>
        </Inline>

        {expandedChoice ? (
          <ControlledInlineConfirm
            open
            surface="inline"
            pending={isSubmitting}
            onCancel={() => setExpandedChoice(null)}
            decision={{
              id: `ctx-approval-${question.question_id}-${expandedChoice}`,
              revision: `${question.question_id}:${question.revision}:${question.payload_hash}`,
              title:
                expandedChoice === "decline"
                  ? `Decline: ${question.title}`
                  : `${confirmVerb}: ${question.title}`,
              consequence:
                question.impact.consequence_note ??
                "Applies this Board decision identically to approving in the unified Approvals queue.",
              confirmLabel:
                expandedChoice === "decline" ? "Confirm Decline" : confirmVerb,
              destructive: expandedChoice === "decline" || Boolean(question.impact.destructive),
              available: canWrite,
              requireReason: true,
              defaultReason:
                expandedChoice === "decline"
                  ? `Board declined: ${question.title}`
                  : `Board approved: ${question.title}`,
              whatChanges,
              whatStaysUntouched,
              technicalDetails: [
                { label: "command_type", value: "submit_decision_answer" },
                { label: "question_id", value: question.question_id },
                { label: "choice", value: expandedChoice },
                { label: "expected_revision", value: String(question.revision) },
                { label: "expected_payload_hash", value: question.payload_hash },
              ],
            }}
            onConfirm={async ({ reason }) => {
              const lifecycle = await dispatchCommand({
                company_id: meta.company_id,
                command_id: newCommandId("decision"),
                command_type: "submit_decision_answer",
                contract_version: "decision-command-v1",
                preconditions: {
                  question_id: question.question_id,
                  expected_revision: question.revision,
                  expected_payload_hash: question.payload_hash,
                },
                payload: {
                  choice: expandedChoice,
                  reason,
                },
                label: `${expandedChoice === "decline" ? "Decline" : "Approve"}: ${question.title}`,
                sent_ns: Date.now() * 1_000_000,
              });
              if (lifecycle === "applied") {
                setExpandedChoice(null);
                pushToast(
                  expandedChoice === "decline"
                    ? `Declined "${question.title}".`
                    : `Approved "${question.title}".`
                );
              }
            }}
          />
        ) : null}
      </Stack>
    </Card>
  );
}

export function ContextualApprovalList({
  questions,
}: {
  questions: ApprovalQuestionItem[];
}) {
  if (questions.length === 0) return null;
  return (
    <Stack gap={12}>
      {questions.map((q) => (
        <ContextualApprovalCard key={q.question_id} question={q} />
      ))}
    </Stack>
  );
}
