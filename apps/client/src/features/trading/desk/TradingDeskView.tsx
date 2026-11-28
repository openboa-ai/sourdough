import { useEffect, useMemo, useState } from "react";
import { Button, PageHeader, Result, Skeleton, Stack } from "@openboa/yogurt";
import type { ApprovalQuestionItem, TradingDeskProjection } from "../../../api/types";
import { useProjection } from "../../../api/useProjection";
import {
  ContextualApprovalList,
  usePendingApprovals,
} from "../../../components/ContextualApprovalCard";
import { formatNsClock } from "../../../lib/format";
import { useControlPlane, useOutbox } from "../../../state/store";
import { BookOverviewStrip } from "./BookOverviewStrip";
import { ActiveLiveVenueCard, LiveControlsPanel } from "./LiveControlsPanel";
import { NewPaperBookCard } from "./NewPaperBookCard";
import { PositionsOrdersTabs } from "./PositionsOrdersTabs";

const isPromotionApproval = (q: ApprovalQuestionItem): boolean =>
  q.kind === "live_promotion" ||
  q.kind === "paper_to_live_migration" ||
  q.kind === "execution_promotion";

export function TradingDeskView({ mode }: { mode: "live" | "paper" }) {
  const { meta } = useControlPlane();
  const { canWrite } = useOutbox();

  const [bookSelection, setBookSelection] = useState<{
    mode: "live" | "paper";
    bookId: string | null;
  }>({ mode, bookId: null });
  const [newBookOpen, setNewBookOpen] = useState(false);

  // Reset book selection and open drawer cleanly when switching Live <-> Paper.
  useEffect(() => {
    setBookSelection({ mode, bookId: null });
    setNewBookOpen(false);
  }, [mode]);

  const selectedBookId = bookSelection.mode === mode ? bookSelection.bookId : null;

  const projectionPath = useMemo(() => {
    const qs = new URLSearchParams({ environment: mode });
    if (selectedBookId) {
      qs.set("book_id", selectedBookId);
    }
    return `trading-desk?${qs.toString()}`;
  }, [mode, selectedBookId]);

  const { data, loading, error, refetch } =
    useProjection<TradingDeskProjection>(projectionPath);
  const promotionApprovals = usePendingApprovals(isPromotionApproval);

  const activeData = data && data.environment === mode ? data : null;
  const isLive = mode === "live";

  if (loading && !activeData) {
    return (
      <Stack gap={16}>
        <PageHeader
          title={isLive ? "Live Trading" : "Paper Trading"}
          detail={
            isLive
              ? "Loading live capital KPIs, performance chart, positions, and venue gates..."
              : "Loading company paper books, simulated equity curves, positions, and execution tape..."
          }
        />
        <Skeleton label="Loading trading desk" lines={6} />
      </Stack>
    );
  }

  if (!activeData) {
    return (
      <Result
        title={isLive ? "Live Trading unavailable" : "Paper Trading unavailable"}
        action={
          <Button emphasis="soft" shape="pill" onClick={refetch}>
            Retry
          </Button>
        }
      >
        {error ?? "Waiting for local control plane snapshot."}
      </Result>
    );
  }

  if (!activeData.trading_enabled) {
    return (
      <Stack gap={16}>
        <PageHeader
          title={isLive ? "Live Trading" : "Paper Trading"}
          detail="Trading domain not installed"
        />
        <Result title="Trading module inactive">
          Activate the Trading domain module to access paper books, live venues, and execution controls.
        </Result>
      </Stack>
    );
  }

  const currency = activeData.kpis.portfolio_value.currency || "USD";
  const charterRevision = activeData.charter_revision || meta?.charter_revision || "";
  const asOfLabel = `As of ${formatNsClock(activeData.as_of_ns)} · seq #${activeData.event_seq}`;
  const selectedBook = activeData.selected_book;
  const supportsOpenPaperBook = Boolean(
    meta?.supported_commands?.open_company_paper_book?.includes("execution-tier-command-v1")
  );
  const canOpenPaperBook = canWrite && supportsOpenPaperBook && Boolean(charterRevision);

  return (
    <Stack gap={16}>
      <BookOverviewStrip
        mode={mode}
        data={activeData}
        currency={currency}
        charterRevision={charterRevision}
        asOfLabel={asOfLabel}
        selectedBook={selectedBook}
        onSelectBook={(bindingId) => setBookSelection({ mode, bookId: bindingId })}
        newBookOpen={newBookOpen}
        onToggleNewBook={() => setNewBookOpen((open) => !open)}
        approvalsSlot={<ContextualApprovalList questions={promotionApprovals} />}
        newBookSlot={
          !isLive && newBookOpen ? (
            <NewPaperBookCard
              currency={currency}
              charterRevision={charterRevision}
              canOpenPaperBook={canOpenPaperBook}
              onClose={() => setNewBookOpen(false)}
            />
          ) : null
        }
        activeLiveSlot={
          isLive && activeData.governance.company_live ? (
            <ActiveLiveVenueCard
              data={activeData}
              charterRevision={charterRevision}
            />
          ) : null
        }
      />

      <PositionsOrdersTabs
        mode={mode}
        data={activeData}
        currency={currency}
        asOfLabel={asOfLabel}
      />

      {isLive ? (
        <LiveControlsPanel data={activeData} charterRevision={charterRevision} />
      ) : null}
    </Stack>
  );
}
