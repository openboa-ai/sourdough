import { useEffect, useRef, useState, type ReactNode } from "react";
import { Button as SDKButton } from "@ui-sdk/react/button";
import {
  Button,
  ButtonGroup,
  DisclosureRow,
  Heading,
  Inline,
  Notice,
  RecordFacts,
  Stack,
  Text,
  Textarea,
  type Fact,
} from "@openboa/yogurt";

export interface InlineConfirmDecision {
  id: string;
  revision: string;
  title: string;
  consequence: string;
  confirmLabel: string;
  destructive?: boolean;
  available: boolean;
  whatChanges: Array<{ label: string; value: string }>;
  whatStaysUntouched: Array<{ label: string; value: string }>;
  technicalDetails: Array<{ label: string; value: string }>;
  requireReason?: boolean;
  defaultReason?: string;
  reasonPlaceholder?: string;
  extraPreview?: ReactNode;
  secondaryAction?: ReactNode;
}

interface ControlledInlineConfirmProps {
  open: boolean;
  decision: InlineConfirmDecision;
  pending?: boolean;
  onCancel: () => void;
  onConfirm: (latched: { id: string; revision: string; reason: string }) => void;
}

/**
 * Universal Inline Human-Language Confirmation surface (zero popup modals, zero right drawers).
 * Enforces all five safety invariants:
 *   1. Revision latch on expand (openedRef)
 *   2. Mid-review change detection + disable + inline warning
 *   3. One-shot submit guard (sentRef) + SDKButton details.source !== "agent" check
 *   4. Specific consequence-verb CTA (#175174 brand or danger tone)
 *   5. data-agent-confirm strictly on the confirm SDKButton
 */
export function ControlledInlineConfirm({
  open,
  decision,
  pending = false,
  onCancel,
  onConfirm,
}: ControlledInlineConfirmProps) {
  const openedRef = useRef<{ id: string; revision: string } | null>(null);
  const sentRef = useRef(false);
  const userEditedReasonRef = useRef(false);
  const [reason, setReason] = useState(decision.defaultReason ?? "");
  const [agentBlockedMsg, setAgentBlockedMsg] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      if (openedRef.current === null) {
        openedRef.current = { id: decision.id, revision: decision.revision };
        sentRef.current = false;
        userEditedReasonRef.current = false;
        setReason(decision.defaultReason ?? "");
        setAgentBlockedMsg(null);
      } else if (!userEditedReasonRef.current) {
        setReason(decision.defaultReason ?? "");
      }
    } else {
      openedRef.current = null;
      sentRef.current = false;
      userEditedReasonRef.current = false;
      setAgentBlockedMsg(null);
    }
  }, [open, decision.id, decision.revision, decision.defaultReason]);

  if (!open) return null;

  const changed =
    openedRef.current !== null &&
    (openedRef.current.id !== decision.id ||
      openedRef.current.revision !== decision.revision);

  const trimmedReason = reason.trim();
  const reasonMissing = Boolean(decision.requireReason && trimmedReason.length === 0);
  const confirmDisabled =
    !decision.available || changed || pending || reasonMissing;

  const changesFacts: Fact[] = decision.whatChanges.map((item, idx) => ({
    id: `chg-${idx}`,
    label: item.label,
    value: item.value,
  }));
  const untouchedFacts: Fact[] = decision.whatStaysUntouched.map((item, idx) => ({
    id: `stay-${idx}`,
    label: item.label,
    value: item.value,
  }));
  const techFacts: Fact[] = decision.technicalDetails.map((item, idx) => ({
    id: `tech-${idx}`,
    label: item.label,
    value: <Text className="sd-mono">{item.value}</Text>,
  }));

  return (
    <div
      className="sd-inline-confirm"
      data-destructive={decision.destructive ? "true" : "false"}
      role="region"
      aria-label={`Confirm ${decision.title}`}
    >
      <Stack gap={12}>
        <Inline justify="between" align="center" wrap>
          <Heading level={3}>{decision.title}</Heading>
          {decision.secondaryAction}
        </Inline>

        <Text variant="body">{decision.consequence}</Text>

        {changed ? (
          <Text className="yg-error" role="status">
            This decision changed while open. Collapse and review it again before confirming.
          </Text>
        ) : null}

        {agentBlockedMsg ? (
          <Notice title="Human confirmation required" tone="warning" urgent>
            {agentBlockedMsg}
          </Notice>
        ) : null}

        <div className="sd-impact-grid">
          <div className="sd-impact-well">
            <RecordFacts
              title="What changes"
              facts={changesFacts}
              source="Board review"
              asOf="On confirmation"
            />
          </div>
          <div className="sd-impact-well">
            <RecordFacts
              title="What stays untouched"
              facts={untouchedFacts}
              source="Preserved scope"
              asOf="Unchanged"
            />
          </div>
        </div>

        {decision.extraPreview}

        {decision.requireReason ? (
          <Textarea
            label="Decision reason"
            value={reason}
            onChange={(event) => {
              userEditedReasonRef.current = true;
              setReason(event.target.value);
            }}
            placeholder={
              decision.reasonPlaceholder ??
              "Explain why you are taking this action (recorded in audit trail)..."
            }
            rows={2}
            help="Required (1 to 2,000 characters). Included in the command digest."
          />
        ) : null}

        <DisclosureRow label="Technical details" initiallyOpen={false}>
          <RecordFacts
            facts={techFacts}
            source="Optimistic preconditions"
            asOf="Latched on expand"
          />
        </DisclosureRow>

        <ButtonGroup label="Decision" justify="end">
          <Button emphasis="quiet" shape="pill" onClick={onCancel} disabled={pending}>
            Cancel
          </Button>
          <SDKButton
            className="yg-button"
            data-emphasis="filled"
            data-tone={decision.destructive ? "danger" : "brand"}
            data-control-size="regular"
            data-shape="pill"
            data-agent-confirm={decision.consequence}
            disabled={confirmDisabled}
            onClick={(event, details) => {
              if (details?.source === "agent") {
                event.preventDefault();
                setAgentBlockedMsg(
                  "Automated invocation was refused. Click directly as a human Board member."
                );
                return;
              }
              if (
                sentRef.current ||
                !decision.available ||
                !openedRef.current ||
                changed ||
                reasonMissing
              ) {
                return;
              }
              sentRef.current = true;
              const latched = { ...openedRef.current };
              onCancel();
              onConfirm({
                id: latched.id,
                revision: latched.revision,
                reason: trimmedReason || decision.defaultReason || "Board confirmed",
              });
            }}
          >
            {decision.confirmLabel}
          </SDKButton>
        </ButtonGroup>
      </Stack>
    </div>
  );
}
