import { useMemo } from "react";
import {
  Badge,
  Card,
  EmptyResult,
  Heading,
  Inline,
  RecordFacts,
  Stack,
  Surface,
  Text,
  type Fact,
} from "@openboa/yogurt";
import type { RsiTransferSummaryItem } from "../../api/types";

export interface RsiTransferPanelProps {
  summaries: RsiTransferSummaryItem[];
  query: string;
  onClearQuery: () => void;
}

function rsiVerdictTone(
  verdict: string
): "success" | "danger" | "warning" | "neutral" {
  switch (verdict.toUpperCase()) {
    case "SUPPORTED":
      return "success";
    case "UNSUPPORTED":
      return "danger";
    case "INCONCLUSIVE":
      return "warning";
    default:
      return "neutral";
  }
}

function fmtFloat(val: number | undefined, digits = 4): string {
  if (val === undefined || val === null || !Number.isFinite(val)) return "Unavailable";
  return val.toFixed(digits);
}

export function RsiTransferPanel({
  summaries,
  query,
  onClearQuery,
}: RsiTransferPanelProps) {
  const q = query.trim().toLowerCase();
  const filtered = useMemo(() => {
    if (!q) return summaries;
    return summaries.filter((s) =>
      `${s.suite_id} ${s.revision} ${s.verdict} ${(s.reasons ?? []).join(" ")}`
        .toLowerCase()
        .includes(q)
    );
  }, [summaries, q]);

  return (
    <Card
      title={`RSI Transfer Verdicts (${filtered.length})`}
      description="Paired blind cross-family transfer evaluation (cluster bootstrap LCB/UCB & permutation p-value)"
      variant="raised"
    >
      {summaries.length === 0 ? (
        <Text variant="help">
          No recursive transfer summaries available. Attach an offline recursive evaluator store
          via --evaluator-store to inspect paired transfer verdicts.
        </Text>
      ) : filtered.length === 0 ? (
        <EmptyResult query={query} onClear={onClearQuery} />
      ) : (
        <Stack gap={12}>
          {filtered.map((s) => {
            const facts: Fact[] = [
              {
                id: "pairs",
                label: "Pairs (Complete / Planned)",
                value: `${s.complete_pairs} / ${s.planned_pairs}`,
              },
              {
                id: "families",
                label: "Distinct Transfer Families",
                value:
                  s.distinct_transfer_families !== undefined
                    ? String(s.distinct_transfer_families)
                    : "Unavailable",
              },
              {
                id: "wlt",
                label: "Candidate Wins / Losses / Ties",
                value:
                  s.candidate_pairwise_wins !== undefined &&
                  s.incumbent_pairwise_wins !== undefined &&
                  s.pairwise_ties !== undefined
                    ? `${s.candidate_pairwise_wins}W / ${s.incumbent_pairwise_wins}L / ${s.pairwise_ties}T`
                    : "Unavailable",
              },
              {
                id: "bounds",
                label: "LCB / UCB (alpha)",
                value:
                  s.lcb_alpha !== undefined && s.ucb_alpha !== undefined
                    ? `[${fmtFloat(s.lcb_alpha, 3)}, ${fmtFloat(s.ucb_alpha, 3)}]`
                    : "Unavailable",
              },
              {
                id: "delta",
                label: "Mean Family Delta",
                value: fmtFloat(s.mean_family_delta, 4),
              },
              {
                id: "perm-p",
                label: "Permutation p-value",
                value: fmtFloat(s.perm_p, 4),
              },
              {
                id: "violations",
                label: "Candidate Authority Violations",
                value:
                  s.candidate_authority_violations !== undefined
                    ? String(s.candidate_authority_violations)
                    : "Unavailable",
              },
            ];

            return (
              <Surface
                key={`${s.suite_id}:${s.revision}`}
                variant="surface"
                density="compact"
              >
                <Stack gap={12}>
                  <Inline justify="between" align="center" gap={8} wrap>
                    <Heading level={4} className="sd-mono">
                      {s.suite_id} ({s.revision})
                    </Heading>
                    <Badge tone={rsiVerdictTone(s.verdict)} dot={false}>
                      {s.verdict}
                    </Badge>
                  </Inline>
                  <div className="sd-facts-wrap">
                    <RecordFacts
                      source="offline-trial-registry-v1"
                      asOf={s.revision}
                      facts={facts}
                    />
                  </div>
                  {s.reasons && s.reasons.length > 0 ? (
                    <Text variant="meta">Reasons: {s.reasons.join(" · ")}</Text>
                  ) : null}
                </Stack>
              </Surface>
            );
          })}
        </Stack>
      )}
    </Card>
  );
}
