import { useState } from "react";
import { Button, Card, Inline, Stack, TextField } from "@openboa/yogurt";
import { newCommandId } from "../../../lib/format";
import { useControlPlane, useOutbox, usePreferences } from "../../../state/store";

export interface NewPaperBookCardProps {
  currency: string;
  charterRevision: string;
  canOpenPaperBook: boolean;
  onClose: () => void;
}

export function NewPaperBookCard({
  currency,
  charterRevision,
  canOpenPaperBook,
  onClose,
}: NewPaperBookCardProps) {
  const { meta } = useControlPlane();
  const { outbox, dispatchCommand } = useOutbox();
  const { pushToast } = usePreferences();

  const [newBookName, setNewBookName] = useState("");
  const [newAccountId, setNewAccountId] = useState("");
  const [newAccountTouched, setNewAccountTouched] = useState(false);
  const [newStartingUsd, setNewStartingUsd] = useState("100000");
  const [newExposureUsd, setNewExposureUsd] = useState("50000");
  const [newStrategyRelease, setNewStrategyRelease] = useState("");
  const [newFeeBps, setNewFeeBps] = useState("4");
  const [newSlippageBps, setNewSlippageBps] = useState("2");

  const isOpeningBook =
    (outbox.lifecycle === "submitting" || outbox.lifecycle === "accepted") &&
    outbox.intent?.command_type === "open_company_paper_book";

  const trimmedBookName = newBookName.trim();
  const trimmedAccountId = newAccountId.trim();
  const parsedStartingUsd = Number(newStartingUsd);
  const parsedExposureUsd = Number(newExposureUsd);
  const parsedFeeBps = Number.parseInt(newFeeBps, 10);
  const parsedSlippageBps = Number.parseInt(newSlippageBps, 10);

  const accountIdError =
    trimmedAccountId && !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(trimmedAccountId)
      ? "Use letters, digits, '.', '_', ':', or '-'."
      : undefined;
  const startingError =
    newStartingUsd && (!Number.isFinite(parsedStartingUsd) || parsedStartingUsd <= 0)
      ? "Enter a positive dollar amount."
      : undefined;
  const exposureError =
    newExposureUsd &&
    (!Number.isFinite(parsedExposureUsd) ||
      parsedExposureUsd <= 0 ||
      (Number.isFinite(parsedStartingUsd) && parsedExposureUsd > parsedStartingUsd))
      ? "Exposure ceiling must be positive and cannot exceed starting capital."
      : undefined;
  const feeError =
    newFeeBps && (!Number.isInteger(parsedFeeBps) || parsedFeeBps < 0 || parsedFeeBps > 1000)
      ? "0 to 1000 bps."
      : undefined;
  const slippageError =
    newSlippageBps &&
    (!Number.isInteger(parsedSlippageBps) || parsedSlippageBps < 0 || parsedSlippageBps > 500)
      ? "0 to 500 bps."
      : undefined;

  const canSubmitNewBook =
    canOpenPaperBook &&
    !isOpeningBook &&
    trimmedBookName.length > 0 &&
    trimmedAccountId.length > 0 &&
    !accountIdError &&
    !startingError &&
    !exposureError &&
    !feeError &&
    !slippageError &&
    Number.isFinite(parsedStartingUsd) &&
    parsedStartingUsd > 0 &&
    Number.isFinite(parsedExposureUsd) &&
    parsedExposureUsd > 0 &&
    parsedExposureUsd <= parsedStartingUsd;

  const handleBookNameChange = (value: string) => {
    setNewBookName(value);
    if (!newAccountTouched) {
      const slug = value
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "")
        .slice(0, 40);
      setNewAccountId(slug ? `paper-${slug}` : "");
    }
  };

  const handleCreatePaperBook = async () => {
    if (!meta || !canSubmitNewBook) return;
    const startingMinor = Math.round(parsedStartingUsd * 100);
    const exposureMinor = Math.round(parsedExposureUsd * 100);
    const lifecycle = await dispatchCommand({
      company_id: meta.company_id,
      command_id: newCommandId("open-paper-book"),
      command_type: "open_company_paper_book",
      contract_version: "execution-tier-command-v1",
      preconditions: { charter_revision: charterRevision },
      payload: {
        account_id: trimmedAccountId,
        book_name: trimmedBookName,
        starting_minor_units: startingMinor,
        exposure_limit_minor_units: exposureMinor,
        currency,
        strategy_release_id: newStrategyRelease.trim() || null,
        fee_bps: Number.isInteger(parsedFeeBps) ? parsedFeeBps : 4,
        slippage_bps: Number.isInteger(parsedSlippageBps) ? parsedSlippageBps : 2,
        reason: `Board opened paper book '${trimmedBookName}'`,
      },
      label: `Open paper book ${trimmedBookName}`,
      sent_ns: Date.now() * 1_000_000,
    });
    if (lifecycle === "applied") {
      pushToast(`Opened paper book "${trimmedBookName}".`);
      setNewBookName("");
      setNewAccountId("");
      setNewAccountTouched(false);
      setNewStrategyRelease("");
      onClose();
    }
  };

  return (
    <Card
      title="Open New Company Paper Book"
      description="Allocate an isolated simulated capital pool with independent positions, exposure ceiling, and equity curve"
      variant="raised"
    >
      <Stack gap={12}>
        <div className="sd-split-equal">
          <TextField
            label="Book Name"
            value={newBookName}
            onChange={(e) => handleBookNameChange(e.target.value)}
            placeholder="ETH Mean Reversion"
          />
          <TextField
            label="Account ID"
            value={newAccountId}
            onChange={(e) => {
              setNewAccountTouched(true);
              setNewAccountId(e.target.value);
            }}
            placeholder="paper-eth-mean-reversion"
            error={accountIdError}
          />
        </div>
        <div className="sd-split-equal">
          <TextField
            label={`Starting Capital (${currency})`}
            value={newStartingUsd}
            onChange={(e) => setNewStartingUsd(e.target.value)}
            placeholder="100000"
            error={startingError}
          />
          <TextField
            label={`Exposure Ceiling (${currency})`}
            value={newExposureUsd}
            onChange={(e) => setNewExposureUsd(e.target.value)}
            placeholder="50000"
            error={exposureError}
          />
        </div>
        <div className="sd-split-equal">
          <TextField
            label="Strategy Release ID (optional)"
            value={newStrategyRelease}
            onChange={(e) => setNewStrategyRelease(e.target.value)}
            placeholder="rel-eth-mr-v1"
          />
          <div className="sd-split-equal">
            <TextField
              label="Fee (bps)"
              value={newFeeBps}
              onChange={(e) => setNewFeeBps(e.target.value)}
              placeholder="4"
              error={feeError}
            />
            <TextField
              label="Slippage (bps)"
              value={newSlippageBps}
              onChange={(e) => setNewSlippageBps(e.target.value)}
              placeholder="2"
              error={slippageError}
            />
          </div>
        </div>
        <Inline justify="end" gap={8}>
          <Button
            emphasis="quiet"
            size="compact"
            shape="pill"
            onClick={onClose}
          >
            Cancel
          </Button>
          <Button
            emphasis="filled"
            tone="brand"
            size="compact"
            shape="pill"
            disabled={!canSubmitNewBook}
            onClick={() => void handleCreatePaperBook()}
          >
            {isOpeningBook ? "Creating…" : "Create Paper Book"}
          </Button>
        </Inline>
      </Stack>
    </Card>
  );
}
