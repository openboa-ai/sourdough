import { useEffect, useRef, useState, type ReactNode } from "react";
import { Button as SDKButton } from "@ui-sdk/react/button";
import {
  Button,
  ButtonGroup,
  Card,
  DisclosureRow,
  Heading,
  Inline,
  Notice,
  RecordFacts,
  Stack,
  Surface,
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
  unavailableNotice?: ReactNode;
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
  surface?: "card" | "inline";
  extraPreview?: ReactNode;
  onCancel: () => void;
  onConfirm: (latched: {
    id: string;
    revision: string;
    reason: string;
  }) => Promise<void> | void;
}

export function ControlledInlineConfirm({
  open,
  decision,
  pending = false,
  surface = "card",
  extraPreview,
  onCancel,
  onConfirm,
}: ControlledInlineConfirmProps) {
  const openedRef = useRef<{ id: string; revision: string } | null>(null);
  const sentRef = useRef(false);
  const prevPendingRef = useRef(pending);
  const prevLocalPendingRef = useRef(false);
  const userEditedReasonRef = useRef(false);
  const confirmWrapRef = useRef<HTMLDivElement | null>(null);
  const [localPending, setLocalPending] = useState(false);
  const [reason, setReason] = useState(decision.defaultReason ?? "");
  const [agentBlockedMsg, setAgentBlockedMsg] = useState<string | null>(null);

  useEffect(() => {
    if (prevPendingRef.current && !pending) {
      sentRef.current = false;
    }
    prevPendingRef.current = pending;
  }, [pending]);

  useEffect(() => {
    const isNowPending = pending || localPending;
    const wasPending = prevLocalPendingRef.current;
    prevLocalPendingRef.current = isNowPending;
    const active = document.activeElement;
    if (
      wasPending &&
      !isNowPending &&
      open &&
      (!active || active === document.body || active === document.documentElement)
    ) {
      const container =
        confirmWrapRef.current ??
        document.querySelector<HTMLElement>('[data-decision-confirm="open"]');
      const target = container?.querySelector<HTMLElement>(
        "[data-agent-confirm]:not([disabled]), button:not([disabled]), textarea:not([disabled])"
      );
      target?.focus();
    }
  }, [pending, localPending, open]);

  useEffect(() => {
    if (open) {
      if (openedRef.current === null) {
        openedRef.current = { id: decision.id, revision: decision.revision };
        sentRef.current = false;
        setLocalPending(false);
        userEditedReasonRef.current = false;
        setReason(decision.defaultReason ?? "");
        setAgentBlockedMsg(null);
      } else if (!userEditedReasonRef.current) {
        setReason(decision.defaultReason ?? "");
      }
    } else {
      openedRef.current = null;
      sentRef.current = false;
      setLocalPending(false);
      userEditedReasonRef.current = false;
      setAgentBlockedMsg(null);
    }
  }, [open, decision.id, decision.revision, decision.defaultReason]);

  if (!open) return null;

  const isPending = pending || localPending;
  const changed =
    openedRef.current !== null &&
    (openedRef.current.id !== decision.id ||
      openedRef.current.revision !== decision.revision);

  const trimmedReason = reason.trim();
  const reasonMissing = Boolean(decision.requireReason && trimmedReason.length === 0);
  const confirmDisabled =
    !decision.available || changed || isPending || reasonMissing;

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

  const actionControls = (
    <ButtonGroup label="Decision" justify="end">
      <Button emphasis="quiet" shape="pill" onClick={onCancel} disabled={isPending}>
        Cancel
      </Button>
      <SDKButton
        className="yg-button"
        data-emphasis="filled"
        data-tone={decision.destructive ? "danger" : "brand"}
        data-control-size="regular"
        data-shape="pill"
        data-agent-confirm={decision.consequence}
        data-agent-hidden={!decision.available ? "" : undefined}
        aria-busy={isPending || undefined}
        data-pending={isPending || undefined}
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
            isPending ||
            !decision.available ||
            !openedRef.current ||
            changed ||
            reasonMissing
          ) {
            return;
          }
          sentRef.current = true;
          setLocalPending(true);
          const latched = { ...openedRef.current };
          void Promise.resolve(
            onConfirm({
              id: latched.id,
              revision: latched.revision,
              reason: trimmedReason || decision.defaultReason || "Board confirmed",
            })
          ).finally(() => {
            sentRef.current = false;
            setLocalPending(false);
          });
        }}
      >
        {isPending ? "Submitting..." : decision.confirmLabel}
      </SDKButton>
    </ButtonGroup>
  );

  const previewNode = extraPreview ?? decision.extraPreview;

  const bodyContent = (
    <Stack gap={12}>
      {changed ? (
        <Notice title="Decision changed during review" tone="warning" urgent>
          This decision changed while open. Collapse and review it again before confirming.
        </Notice>
      ) : null}

      {!decision.available ? (
        decision.unavailableNotice !== undefined ? (
          decision.unavailableNotice
        ) : (
          <Notice title="Read-only or locked" tone="neutral">
            Confirmation is unavailable until controls are unlocked with Board Control authority.
          </Notice>
        )
      ) : null}

      {agentBlockedMsg ? (
        <Notice title="Human confirmation required" tone="warning" urgent>
          {agentBlockedMsg}
        </Notice>
      ) : null}

      <div className="sd-split-equal">
        <Surface variant="surface" density="compact">
          <RecordFacts
            title="What changes"
            facts={changesFacts}
            source="Board review"
            asOf="On confirmation"
          />
        </Surface>
        <Surface variant="surface" density="compact">
          <RecordFacts
            title="What stays untouched"
            facts={untouchedFacts}
            source="Preserved scope"
            asOf="Unchanged"
          />
        </Surface>
      </div>

      {previewNode}

      {decision.requireReason ? (
        <Textarea
          label="Decision reason"
          value={reason}
          disabled={isPending}
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
    </Stack>
  );

  if (surface === "inline") {
    return (
      <div
        ref={confirmWrapRef}
        className="sd-decision-inline sd-live-enter"
        data-state={decision.destructive ? "destructive" : "pending"}
        data-decision-confirm="open"
      >
        <div className="sd-decision-inline-header">
          <div className="sd-decision-inline-copy">
            <Heading level={4}>{decision.title}</Heading>
            <Text variant="help">{decision.consequence}</Text>
          </div>
          {decision.secondaryAction}
        </div>
        {bodyContent}
        <Inline justify="end">{actionControls}</Inline>
      </div>
    );
  }

  return (
    <div ref={confirmWrapRef} data-decision-confirm="open">
      <Card
        className="yg-decision-card sd-live-enter"
        data-state={decision.destructive ? "destructive" : "pending"}
        variant="raised"
        title={decision.title}
        description={decision.consequence}
        headerAction={decision.secondaryAction}
        action={actionControls}
      >
        {bodyContent}
      </Card>
    </div>
  );
}
