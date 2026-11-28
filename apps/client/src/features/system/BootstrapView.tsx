import { useRef, useState } from "react";
import { CopyButton } from "@openboa/yogurt/motion-copy-button";
import {
  Button,
  Card,
  DisclosureRow,
  Inline,
  Notice,
  PageHeader,
  RecordFacts,
  Skeleton,
  Stack,
  TextField,
} from "@openboa/yogurt";
import { ControlledInlineConfirm } from "../../components/ControlledInlineConfirm";
import { newCommandId } from "../../lib/format";
import { useControlPlane, useOutbox } from "../../state/store";

export function BootstrapView() {
  const { meta } = useControlPlane();
  const { controlsLocked, outbox, dispatchCommand } = useOutbox();
  const [principalId, setPrincipalId] = useState("board-owner-1");
  const [proof, setProof] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const bootstrapFormRef = useRef<HTMLDivElement | null>(null);

  if (!meta) return <Skeleton label="Loading bootstrap status" lines={4} />;

  const challenge = meta.board_access.bootstrap_challenge ?? "";
  const trimmedPrincipal = principalId.trim();
  const trimmedProof = proof.trim();
  const isValidProofHex = /^[0-9a-f]{64}$/i.test(trimmedProof);
  const proofError =
    trimmedProof.length > 0 && !isValidProofHex
      ? "Invalid OwnerProof format: enter a valid 64-character hexadecimal SHA-256 digest."
      : undefined;
  const principalError =
    trimmedPrincipal.length > 128
      ? "Principal ID must be 128 characters or fewer."
      : undefined;
  const isPendingRegister =
    (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
    outbox.intent?.command_type === "register_owner";
  const canProceed = Boolean(
    trimmedPrincipal && trimmedPrincipal.length <= 128 && isValidProofHex
  );

  return (
    <Stack gap={16}>
      <PageHeader
        title="Register Initial Board Owner"
        detail="Bootstrap lock is active until an authenticated local Board owner binds authority"
      />

      <Notice title="Bootstrap Locked" tone="warning" urgent>
        No Board principal is bound to this company yet. Verify the server challenge below and
        compute SHA-256(&quot;&lt;challenge&gt;:&lt;company_id&gt;&quot;) to claim board_control.
      </Notice>

      <Card
        title="Owner Bootstrap Challenge"
        variant="raised"
        headerAction={
          challenge ? <CopyButton value={challenge} presentation="quiet" /> : undefined
        }
      >
        <div ref={bootstrapFormRef}>
          <Stack gap={12}>
            <div className="sd-facts-wrap">
              <RecordFacts
                source="GET /v1/meta (bootstrap_locked)"
                asOf="One-time setup"
                facts={[
                  { id: "co", label: "Company ID", value: meta.company_id },
                  { id: "ch", label: "OwnerChallenge", value: challenge || "Unavailable" },
                ]}
              />
            </div>

            <TextField
              label="Board Principal ID"
              value={principalId}
              onChange={(e) => setPrincipalId(e.target.value)}
              placeholder="board-owner-1"
              error={principalError}
            />

            <div data-agent-secret="">
              <TextField
                label="OwnerProof (hex SHA-256)"
                value={proof}
                onChange={(e) => setProof(e.target.value)}
                placeholder="Paste computed 64-char hex proof"
                error={proofError}
                help="64-character hex SHA-256 digest. Redacted from agent snapshots via data-agent-secret."
              />
            </div>

            <Inline justify="end">
              <Button
                emphasis="filled"
                tone="brand"
                shape="pill"
                disabled={!canProceed}
                onClick={() => setConfirmOpen(true)}
              >
                Review Owner Binding
              </Button>
            </Inline>

            <ControlledInlineConfirm
              open={confirmOpen}
              surface="inline"
              pending={isPendingRegister}
              onCancel={() => setConfirmOpen(false)}
              decision={{
                id: `register-owner-${meta.company_id}`,
                revision: `bootstrap:${challenge}`,
                title: `Bind ${trimmedPrincipal || "principal"} as Initial Board Owner`,
                consequence:
                  "Unlocks Board governance and control actions for this principal and permanently closes one-time bootstrap registration.",
                confirmLabel: "Register Board Owner",
                destructive: false,
                available: (!controlsLocked || isPendingRegister) && canProceed && Boolean(challenge),
                whatChanges: [
                  {
                    label: "Board role",
                    value: `${trimmedPrincipal || "principal"} bound as board_control`,
                  },
                  { label: "Bootstrap gate", value: "Permanently closed" },
                ],
                whatStaysUntouched: [
                  { label: "Company charter", value: meta.charter_revision },
                  { label: "Store key", value: meta.store_key },
                ],
                technicalDetails: [
                  { label: "command_type", value: "register_owner" },
                  { label: "contract_version", value: "rbac-command-v1" },
                  { label: "company_id", value: meta.company_id },
                ],
              }}
              onConfirm={async () => {
                const lifecycle = await dispatchCommand({
                  company_id: meta.company_id,
                  command_id: newCommandId("owner"),
                  command_type: "register_owner",
                  contract_version: "rbac-command-v1",
                  preconditions: { bootstrap_state: "locked" },
                  payload: {
                    principal_id: trimmedPrincipal,
                    challenge,
                    proof: isValidProofHex ? trimmedProof.toLowerCase() : trimmedProof,
                  },
                  label: `Register Board owner (${trimmedPrincipal})`,
                  sent_ns: Date.now() * 1_000_000,
                });
                if (
                  lifecycle === "applied" ||
                  lifecycle === "accepted" ||
                  lifecycle === "unknown"
                ) {
                  setConfirmOpen(false);
                } else if (lifecycle === "denied") {
                  const focusForm = () => {
                    const active = document.activeElement;
                    if (
                      active &&
                      active !== document.body &&
                      bootstrapFormRef.current?.contains(active)
                    ) {
                      return;
                    }
                    const target = bootstrapFormRef.current?.querySelector<HTMLElement>(
                      '[data-decision-confirm="open"] [data-agent-confirm]:not([disabled]), [data-decision-confirm="open"] button:not([disabled]), [data-agent-secret] input'
                    );
                    target?.focus();
                  };
                  focusForm();
                  window.requestAnimationFrame(focusForm);
                }
              }}
            />

            <DisclosureRow label="Technical details" initiallyOpen={false}>
              <div className="sd-facts-wrap">
                <RecordFacts
                  source="Store identity"
                  asOf="Local daemon"
                  facts={[
                    { id: "key", label: "Store key", value: meta.store_key },
                    { id: "ch_rev", label: "Charter revision", value: meta.charter_revision },
                  ]}
                />
              </div>
            </DisclosureRow>
          </Stack>
        </div>
      </Card>
    </Stack>
  );
}
