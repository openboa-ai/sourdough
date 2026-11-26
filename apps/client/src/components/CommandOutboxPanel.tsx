import { useEffect, useState } from "react";
import {
  Button,
  ButtonGroup,
  Card,
  Inline,
  Notice,
  Receipt,
  Stack,
  Text,
  type ReceiptRecord,
} from "@openboa/yogurt";
import { useApp } from "../state/store";

/**
 * Persistent inline CommandOutboxPanel mounted strictly below the 48px Top Bar.
 * Implements the exact 8-variant CommandLifecycle state machine:
 *   - submitting / accepted: Receipt state="pending" + pending_age (NO onLookup)
 *   - unknown: Receipt state="unknown" + onLookup ("Check status") ONLY here
 *   - applied: Receipt state="confirmed"
 *   - denied: Receipt state="failed" + denial_code
 *   - not_found_retry_choice: Retry same request vs Discard draft (NO onLookup)
 */
export function CommandOutboxPanel() {
  const {
    outbox,
    lookupUnknownCommand,
    retryNotFoundCommand,
    discardPendingCommand,
    dismissFinalOutbox,
  } = useApp();

  const [nowMs, setNowMs] = useState(() => Date.now());
  const isPending =
    outbox.lifecycle === "submitting" ||
    outbox.lifecycle === "accepted" ||
    outbox.lifecycle === "unknown";

  useEffect(() => {
    if (!isPending) return;
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [isPending]);

  if (outbox.lifecycle === "idle" && !outbox.quarantineNotice) {
    return null;
  }

  if (outbox.lifecycle === "idle" && outbox.quarantineNotice) {
    return (
      <Notice
        title="Pending command storage cleaned"
        tone="warning"
        action={
          <Button emphasis="quiet" size="compact" shape="pill" onClick={dismissFinalOutbox}>
            Dismiss
          </Button>
        }
      >
        {outbox.quarantineNotice}
      </Notice>
    );
  }

  const intent = outbox.intent;
  const elapsedSec = intent
    ? Math.max(0, Math.floor((nowMs - intent.sent_ns / 1_000_000) / 1000))
    : 0;

  let receiptState: ReceiptRecord["state"] = "pending";
  if (outbox.lifecycle === "unknown" || outbox.lifecycle === "not_found_retry_choice") {
    receiptState = "unknown";
  } else if (outbox.lifecycle === "applied") {
    receiptState = "confirmed";
  } else if (outbox.lifecycle === "denied") {
    receiptState = "failed";
  }

  const record: ReceiptRecord = {
    id: intent?.command_id ?? "command",
    state: receiptState,
    description: (
      <Stack gap={4}>
        <Text variant="body">
          {intent?.label ?? intent?.command_type ?? "Board command"}
        </Text>
        {outbox.errorMessage ? (
          <Text variant="help">{outbox.errorMessage}</Text>
        ) : outbox.receipt?.effective_revision !== undefined ? (
          <Text variant="meta" className="sd-tabular">
            Effective revision: {outbox.receipt.effective_revision} · PID {outbox.receipt.peer_pid ?? "—"}
          </Text>
        ) : null}
        {isPending ? (
          <Text variant="meta" className="sd-tabular">
            Elapsed: {elapsedSec}s
          </Text>
        ) : null}
      </Stack>
    ),
  };

  return (
    <Card variant="outlined" density="compact">
      <Stack gap={8}>
        <Receipt
          record={record}
          onLookup={
            outbox.lifecycle === "unknown"
              ? () => {
                  void lookupUnknownCommand();
                }
              : undefined
          }
          pending={outbox.lifecycle === "submitting"}
        />

        {outbox.lifecycle === "not_found_retry_choice" ? (
          <Inline justify="between" align="center" wrap>
            <Text variant="help">
              No daemon receipt was recorded. Controls remain locked until you retry the exact original request or discard it.
            </Text>
            <ButtonGroup label="Unrecorded command choice" justify="end">
              <Button
                emphasis="quiet"
                size="compact"
                shape="pill"
                onClick={() => {
                  void discardPendingCommand();
                }}
              >
                Discard draft
              </Button>
              <Button
                emphasis="filled"
                tone="brand"
                size="compact"
                shape="pill"
                onClick={() => {
                  void retryNotFoundCommand();
                }}
              >
                Retry same request
              </Button>
            </ButtonGroup>
          </Inline>
        ) : null}

        {outbox.lifecycle === "applied" || outbox.lifecycle === "denied" ? (
          <Inline justify="end">
            <Button
              emphasis="quiet"
              size="compact"
              shape="pill"
              onClick={dismissFinalOutbox}
            >
              Dismiss receipt
            </Button>
          </Inline>
        ) : null}
      </Stack>
    </Card>
  );
}
