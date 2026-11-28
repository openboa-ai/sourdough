import { useEffect, useState } from "react";
import { PageHeader, Stack, Tabs } from "@openboa/yogurt";
import { SystemAuditView } from "./SystemAuditView";
import { SystemEvalsView } from "./SystemEvalsView";

export function AuditHubView({
  initialTab = "log",
}: {
  initialTab?: "log" | "evals" | "daemon";
}) {
  const [tab, setTab] = useState<"log" | "evals" | "daemon">(initialTab);

  useEffect(() => {
    setTab(initialTab);
  }, [initialTab]);

  return (
    <Stack gap={16}>
      <PageHeader
        title="Audit"
        detail="Append-only event log, offline tool evaluations, and single-writer daemon diagnostics"
      />
      <Tabs
        label="Audit sections"
        presentation="pill"
        value={tab}
        onValueChange={(next) => setTab(next as "log" | "evals" | "daemon")}
        items={[
          {
            id: "log",
            label: "Audit Log",
            panel: tab === "log" ? <SystemAuditView embedded mode="audit" /> : null,
          },
          {
            id: "evals",
            label: "Evaluations",
            panel: tab === "evals" ? <SystemEvalsView embedded /> : null,
          },
          {
            id: "daemon",
            label: "Daemon",
            panel: tab === "daemon" ? <SystemAuditView embedded mode="daemon" /> : null,
          },
        ]}
      />
    </Stack>
  );
}
