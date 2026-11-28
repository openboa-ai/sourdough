import { useMemo } from "react";
import {
  Badge,
  Card,
  RecordFacts,
  RuntimePanel,
  Stack,
  Text,
  type Fact,
  type RuntimeState,
} from "@openboa/yogurt";
import type { AgentDetailProjection } from "../../../api/types";
import { formatMinorCurrency } from "../../../lib/format";

export interface AgentDeskAuthoritySectionProps {
  data: AgentDetailProjection;
  runtimeState: RuntimeState;
}

export function AgentDeskAuthoritySection({
  data,
  runtimeState,
}: AgentDeskAuthoritySectionProps) {
  const {
    profile,
    desk_workspace: deskWorkspace,
    authority_budget: authorityBudget,
    runtime_binding: topRuntimeBinding,
    responsibilities,
    active_snapshot_id: activeSnapshotId,
  } = data;
  const runtimeBinding = topRuntimeBinding ?? deskWorkspace.runtime_binding ?? null;

  const deskFacts = useMemo<Fact[]>(() => {
    const facts: Fact[] = [
      {
        id: "exec",
        label: "Execution Profile",
        value: deskWorkspace.execution_profile,
      },
      {
        id: "binding",
        label: "Runtime Binding",
        value: runtimeBinding
          ? `${runtimeBinding.provider} · ${runtimeBinding.model} (${runtimeBinding.sandbox_backend})`
          : "Profile bound (no runtime record)",
      },
      {
        id: "manifest",
        label: "Desk Manifest Revision",
        value: (
          <Text className="sd-mono">
            {deskWorkspace.desk_manifest_revision ?? "Unavailable"}
          </Text>
        ),
      },
      {
        id: "snapshot",
        label: "Active Method Snapshot",
        value: (
          <Text className="sd-mono">
            {activeSnapshotId ? activeSnapshotId.slice(0, 16) : "Base harness"}
          </Text>
        ),
      },
      {
        id: "drafts",
        label: "Private Drafts",
        value: (
          <Badge tone="neutral" variant="soft" dot={false}>
            {deskWorkspace.private_draft_count}{" "}
            {deskWorkspace.private_draft_count === 1 ? "private draft" : "private drafts"}
          </Badge>
        ),
      },
      {
        id: "pubs",
        label: "Published Reports",
        value: String(deskWorkspace.published_count),
      },
      {
        id: "runs",
        label: "Recorded Work Runs",
        value: String(deskWorkspace.work_run_count),
      },
      {
        id: "onboarding",
        label: "Onboarding",
        value: profile.onboarding_state
          ? `${profile.onboarding_state} (${profile.onboarding_attempts} ${profile.onboarding_attempts === 1 ? "attempt" : "attempts"})`
          : "Complete / Direct",
      },
    ];

    if (responsibilities && responsibilities.length > 0) {
      const activeResp = responsibilities.filter((r) => r.status === "active");
      const totalWakes = responsibilities.reduce((sum, r) => sum + r.window_wakes, 0);
      facts.push({
        id: "responsibilities",
        label: "Standing Responsibilities",
        value: `${activeResp.length} active (${totalWakes} window wakes)`,
      });
    }

    if (profile.onboarding_purpose) {
      facts.push({
        id: "onboarding-purpose",
        label: "Onboarding Purpose",
        value: profile.onboarding_purpose,
      });
    }

    if (profile.archived) {
      facts.push({
        id: "archive-reason",
        label: "Archive Reason",
        value: profile.archive_reason ?? "Archived (no reason recorded)",
      });
    }

    return facts;
  }, [activeSnapshotId, deskWorkspace, profile, responsibilities, runtimeBinding]);

  const authorityFacts = useMemo<Fact[]>(() => {
    const hiringValue = !authorityBudget.can_hire
      ? "Individual contributor (no hiring authority)"
      : authorityBudget.hiring_slots !== null &&
          authorityBudget.hiring_slots !== undefined &&
          authorityBudget.hiring_turns !== null &&
          authorityBudget.hiring_turns !== undefined
        ? `Can hire up to ${authorityBudget.hiring_slots} slots (${authorityBudget.hiring_turns} turns)`
        : `Can hire (${authorityBudget.hiring_slots ?? "Unavailable"} slots · ${authorityBudget.hiring_turns ?? "Unavailable"} turns)`;

    return [
      {
        id: "role",
        label: "Assigned Role",
        value: (
          <Badge tone={profile.is_ceo ? "brand" : "neutral"} dot={false}>
            {profile.role_label}
          </Badge>
        ),
      },
      {
        id: "scope",
        label: "Standing Grant Scope",
        value: authorityBudget.grant_id
          ? `${authorityBudget.grant_scope || "None"} (${authorityBudget.grant_id})`
          : authorityBudget.grant_scope || "None",
      },
      {
        id: "budget",
        label: "Monthly Operating Budget",
        value:
          authorityBudget.monthly_budget_minor > 0
            ? formatMinorCurrency(authorityBudget.monthly_budget_minor)
            : "Company mandate governed",
      },
      {
        id: "turns",
        label: "Allocated Turns",
        value:
          authorityBudget.allocated_turns === null
            ? profile.is_ceo
              ? "Founder / Unbounded"
              : "Unavailable"
            : `${authorityBudget.allocated_turns} turns`,
      },
      {
        id: "hire",
        label: "Staffing Authority",
        value: hiringValue,
      },
    ];
  }, [authorityBudget, profile.is_ceo, profile.role_label]);

  const deskFactsView = (
    <div className="sd-facts-wrap">
      <RecordFacts
        source="private_desk + shared_publication"
        asOf={`seq #${data.event_seq}`}
        facts={deskFacts}
      />
    </div>
  );

  return (
    <div className="sd-split-equal">
      {profile.archived ? (
        <Stack gap={12}>
          <RuntimePanel
            state="offline"
            title="Desk & Isolated Harness"
            detail={deskWorkspace.private_draft_notice}
          />
          <Card
            variant="raised"
            title="Archived Desk & Workspace Summary"
            description={profile.archive_reason ?? "Archived (no reason recorded)"}
          >
            {deskFactsView}
          </Card>
        </Stack>
      ) : (
        <RuntimePanel
          state={runtimeState}
          title="Desk & Isolated Harness"
          detail={deskWorkspace.private_draft_notice}
          preview={deskFactsView}
        />
      )}

      <Card
        title="Authority, Permissions & Budget"
        description="Scoped standing grants and hard operating ceilings"
        variant="raised"
      >
        <Stack gap={12}>
          <div className="sd-facts-wrap">
            <RecordFacts
              source="standing_grant + staffing_authority"
              asOf={`seq #${data.event_seq}`}
              facts={authorityFacts}
            />
          </div>
          <Stack gap={4}>
            <Text variant="meta">Hard Prohibitions</Text>
            {authorityBudget.prohibitions.length === 0 ? (
              <Text variant="help">No additional prohibitions recorded.</Text>
            ) : (
              authorityBudget.prohibitions.map((rule, idx) => (
                <Text key={idx} variant="help">
                  · {rule}
                </Text>
              ))
            )}
          </Stack>
        </Stack>
      </Card>
    </div>
  );
}
