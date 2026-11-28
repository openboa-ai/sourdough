import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ControlPlaneMeta } from "../api/types";
import { DaemonDiagnosticsPanel } from "../features/system/DaemonDiagnosticsPanel";
import { ControlPlaneProvider, useControlPlane } from "./ControlPlaneContext";
import { PreferencesProvider } from "./PreferencesContext";
import { RouterProvider } from "./RouterContext";

function makeMeta(overrides?: Partial<ControlPlaneMeta["resident_health"]>): ControlPlaneMeta {
  return {
    control_plane_version: "1",
    company_id: "co-p1",
    charter_revision: "charter-v1",
    founder_id: "agent-founder",
    store_key: "store-p1",
    service_incarnation_id: "inc-p1",
    schema_version: 1,
    active_domains: ["core"],
    daemon_pid: 1234,
    event_seq: 12,
    board_access: {
      bootstrap_locked: false,
      role: "board_control",
      principal_id: "board-owner",
      binding_revision: 1,
    },
    pending_approvals_count: 0,
    active_restrictions_count: 0,
    supported_projections: [],
    supported_commands: {},
    resident_health: {
      running: true,
      sleep_state: "awake",
      ticks_completed: 5,
      runs_executed: 1,
      triggers_enqueued: 0,
      wake_count: 2,
      last_wake_reason: "startup",
      recovered_runs_count: 0,
      last_tick_at_ns: 1_700_000_000_000_000_000,
      last_good_tick_at_ns: 1_700_000_000_000_000_000,
      last_error: null,
      ...overrides,
    },
  };
}

function HealthProbe() {
  const { meta } = useControlPlane();
  return <DaemonDiagnosticsPanel health={meta?.resident_health} eventSeq={meta?.event_seq ?? 0} />;
}

describe("ControlPlaneContext resident_health idle refresh", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("refreshes /api/v1/meta independently of audit events so sleep transitions and scheduler errors surface while live", async () => {
    let metaCalls = 0;
    let eventCalls = 0;
    const snapshots: ControlPlaneMeta[] = [
      makeMeta({ sleep_state: "awake", ticks_completed: 5, last_error: null }),
      makeMeta({
        sleep_state: "sleeping",
        ticks_completed: 9,
        last_error: "scheduler tick lease timeout",
      }),
    ];

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/v1/meta")) {
        const idx = Math.min(metaCalls, snapshots.length - 1);
        metaCalls += 1;
        return new Response(JSON.stringify(snapshots[idx]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/api/v1/events")) {
        eventCalls += 1;
        if (eventCalls > 2) {
          return await new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () =>
              reject(new DOMException("Aborted", "AbortError"))
            );
          });
        }
        return new Response("", {
          status: 200,
          headers: { "Content-Type": "application/x-ndjson" },
        });
      }
      return new Response("{}", { status: 200 });
    });

    render(
      <PreferencesProvider>
        <RouterProvider>
          <ControlPlaneProvider idleMetaRefreshMs={25}>
            <HealthProbe />
          </ControlPlaneProvider>
        </RouterProvider>
      </PreferencesProvider>
    );

    await waitFor(() => {
      expect(screen.getAllByText("Awake").length).toBeGreaterThan(0);
    });

    await waitFor(
      () => {
        expect(metaCalls).toBeGreaterThanOrEqual(2);
        expect(screen.getAllByText("Sleeping").length).toBeGreaterThan(0);
        expect(screen.getByText("scheduler tick lease timeout")).toBeTruthy();
      },
      { timeout: 3000 }
    );
  });
});
