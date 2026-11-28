import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DaemonDiagnosticsPanel } from "./DaemonDiagnosticsPanel";
import { MethodLearningPanel } from "./MethodLearningPanel";
import { RsiTransferPanel } from "./RsiTransferPanel";

const CANARY_PRIVATE_QUARANTINE_REASON = "CANARY_SECRET_PRIVATE_QUARANTINE_REASON_8841";

describe("System Always-On Diagnostics, Method Learning, and RSI Transfer panels", () => {
  it("renders ResidentHealth Always-On metrics accurately", () => {
    const { container } = render(
      <DaemonDiagnosticsPanel
        eventSeq={99}
        health={{
          running: true,
          ticks_completed: 14,
          runs_executed: 6,
          triggers_enqueued: 9,
          last_tick_at_ns: 1_700_000_100_000_000_000,
          last_error: null,
          sleep_state: "awake",
          wake_count: 5,
          last_wake_reason: "ipc_commit",
          last_good_tick_at_ns: 1_700_000_100_000_000_000,
          recovered_runs_count: 1,
        }}
      />
    );
    const text = container.textContent ?? "";
    expect(text).toContain("Awake");
    expect(text).toContain("14");
    expect(text).toContain("ipc_commit");
    expect(text).toContain("Recovered Runs");
  });

  it("renders Method Learning 3-stage qualifications, blast radius, and redacts private one_agent quarantine reasons", () => {
    const { container } = render(
      <MethodLearningPanel
        query=""
        onClearQuery={() => undefined}
        deltas={[
          {
            delta_id: "private_method:abc123:rev001",
            method_id: "private_method:abc123",
            method_id_redacted: true,
            revision_sha256: "rev001122334455",
            delta_kind: "ADD",
            item_kind: "work_method",
            audience_scope: "one_agent",
            audience_target_id: "agent-1",
            author_agent_id: "agent-1",
            parent_method_ids: [],
            active_snapshot_id: null,
            superseded_revision: null,
            redaction_passed: true,
            status: "quarantined",
            created_at_ns: 1_700_000_010_000_000_000,
          },
        ]}
        qualifications={[
          {
            qualification_id: "mqual-1",
            method_id: "private_method:abc123",
            method_id_redacted: true,
            revision_sha256: "rev001122334455",
            delta_id: "private_method:abc123:rev001",
            baseline_revision_sha256: null,
            stage1_passed: true,
            stage2_passed: true,
            stage3_passed: true,
            trial_count: 6,
            metrics: { score: 0.92 },
            evaluator_revision: "method-evaluator-v1",
            passed: true,
            recorded_at_ns: 1_700_000_020_000_000_000,
          },
        ]}
        activations={[
          {
            request_id: "mact-1",
            operation: "activate",
            method_id: "private_method:abc123",
            method_id_redacted: true,
            revision_sha256: "rev001122334455",
            delta_id: "private_method:abc123:rev001",
            qualification_id: "mqual-1",
            audience_scope: "one_agent",
            audience_target_id: "agent-1",
            activation_seq: 3,
            principal_id: "agent-1",
            grant_id: "grant-1",
            quarantined: true,
            quarantine_reason: CANARY_PRIVATE_QUARANTINE_REASON,
            recorded_at_ns: 1_700_000_030_000_000_000,
            blast_radius_descendants: ["private_method:def456:rev002"],
            fallback_revision: "rev000000000000",
          },
        ]}
      />
    );
    const text = container.textContent ?? "";
    expect(text).toContain("S1:PASS · S2:PASS · S3:PASS");
    expect(text).toContain("private_method:def456:rev002");
    expect(text).toContain("rev000000000");
    expect(text).toContain("private one_agent reason redacted");
    expect(text).not.toContain(CANARY_PRIVATE_QUARANTINE_REASON);
  });

  it("renders RSI Transfer statistical verdict, win/loss/tie counts, LCB/UCB, and perm p-value", () => {
    const { container } = render(
      <RsiTransferPanel
        query=""
        onClearQuery={() => undefined}
        summaries={[
          {
            suite_id: "rsi-suite-gen2",
            revision: "rev-gen2-1",
            verdict: "SUPPORTED",
            reasons: ["lcb-above-margin", "perm-p-significant"],
            planned_pairs: 6,
            complete_pairs: 6,
            distinct_transfer_families: 3,
            candidate_pairwise_wins: 5,
            incumbent_pairwise_wins: 1,
            pairwise_ties: 0,
            lcb_alpha: 0.125,
            ucb_alpha: 0.48,
            mean_family_delta: 0.3025,
            perm_p: 0.008,
            candidate_authority_violations: 0,
          },
        ]}
      />
    );
    const text = container.textContent ?? "";
    expect(text).toContain("SUPPORTED");
    expect(text).toContain("5W / 1L / 0T");
    expect(text).toContain("[0.125, 0.480]");
    expect(text).toContain("0.0080");
    expect(text).toContain("lcb-above-margin");
  });
});
