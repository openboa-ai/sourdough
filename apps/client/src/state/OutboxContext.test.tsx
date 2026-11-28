import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LocalCommandIntent } from "../api/types";
import { clearProjectionCache } from "../api/useProjection";
import { ControlPlaneProvider, useControlPlane } from "./ControlPlaneContext";
import { OutboxProvider, useOutbox } from "./OutboxContext";
import { PreferencesProvider } from "./PreferencesContext";
import { RouterProvider } from "./RouterContext";

const MOCK_META = {
  company_id: "co-outbox-1",
  company_name: "Outbox Co",
  charter_revision: "charter-v1",
  store_key: "store-1",
  service_incarnation_id: "inc-1",
  event_seq: 1,
  active_domains: ["core"],
  board_access: {
    bootstrap_locked: false,
    principal_id: "owner",
    role: "board_control" as const,
    binding_revision: 1,
  },
};

const SAMPLE_INTENT: LocalCommandIntent = {
  company_id: "co-outbox-1",
  command_id: "cmd-outbox-1",
  command_type: "post_ceo_message",
  contract_version: "board-control-v1",
  preconditions: { charter_revision: "charter-v1" },
  payload: { body: "Check risk exposure" },
  label: "Send message to CEO",
  sent_ns: 1_700_000_000_000_000_000,
};

function wrapper({ children }: { children: ReactNode }) {
  return (
    <PreferencesProvider>
      <RouterProvider>
        <ControlPlaneProvider onClearCache={clearProjectionCache}>
          <OutboxProvider>{children}</OutboxProvider>
        </ControlPlaneProvider>
      </RouterProvider>
    </PreferencesProvider>
  );
}

describe("state/OutboxContext command state machine", () => {
  beforeEach(() => {
    clearProjectionCache();
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    clearProjectionCache();
  });

  it("writes local pending guard before submit, clears it on applied, and unlocks controls", async () => {
    const callOrder: string[] = [];

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.startsWith("/api/v1/meta")) {
        return new Response(JSON.stringify(MOCK_META), { status: 200 });
      }
      if (url.startsWith("/api/v1/events")) {
        return await new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError"))
          );
        });
      }
      if (url === "/api/local/pending" && method === "GET") {
        return new Response(JSON.stringify({ pending: null }), { status: 200 });
      }
      if (url === "/api/local/pending" && method === "PUT") {
        callOrder.push("PUT /api/local/pending");
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }
      if (url === "/api/v1/commands" && method === "POST") {
        callOrder.push("POST /api/v1/commands");
        return new Response(
          JSON.stringify({
            company_id: "co-outbox-1",
            command_id: "cmd-outbox-1",
            command_type: "post_ceo_message",
            state: "applied",
            disposition: "applied",
          }),
          { status: 200 }
        );
      }
      if (url === "/api/local/pending" && method === "DELETE") {
        callOrder.push("DELETE /api/local/pending");
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }
      return new Response("Not found", { status: 404 });
    });

    const { result } = renderHook(() => useOutbox(), { wrapper });

    await waitFor(() => {
      expect(result.current.canWrite).toBe(true);
    });

    let outcome = "";
    await act(async () => {
      outcome = await result.current.dispatchCommand(SAMPLE_INTENT);
    });

    expect(outcome).toBe("applied");
    expect(result.current.outbox.lifecycle).toBe("applied");
    expect(callOrder).toEqual([
      "PUT /api/local/pending",
      "POST /api/v1/commands",
      "DELETE /api/local/pending",
    ]);

    act(() => {
      result.current.dismissFinalOutbox();
    });
    expect(result.current.outbox.lifecycle).toBe("idle");
  });

  it("transitions to unknown on transport drop and reconciles to not_found_retry_choice on 404 lookup", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.startsWith("/api/v1/meta")) {
        return new Response(JSON.stringify(MOCK_META), { status: 200 });
      }
      if (url.startsWith("/api/v1/events")) {
        return await new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError"))
          );
        });
      }
      if (url === "/api/local/pending" && method === "GET") {
        return new Response(JSON.stringify({ pending: null }), { status: 200 });
      }
      if (url === "/api/local/pending" && method === "PUT") {
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }
      if (url === "/api/v1/commands" && method === "POST") {
        throw new TypeError("Failed to fetch");
      }
      if (url.startsWith("/api/v1/commands/cmd-outbox-1") && method === "GET") {
        return new Response(JSON.stringify({ status: "not_found" }), { status: 404 });
      }
      if (url === "/api/local/pending" && method === "DELETE") {
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }
      return new Response("Not found", { status: 404 });
    });

    const { result } = renderHook(() => useOutbox(), { wrapper });
    await waitFor(() => expect(result.current.canWrite).toBe(true));

    await act(async () => {
      const res = await result.current.dispatchCommand(SAMPLE_INTENT);
      expect(res).toBe("unknown");
    });
    expect(result.current.outbox.lifecycle).toBe("unknown");
    expect(result.current.controlsLocked).toBe(true);

    await act(async () => {
      await result.current.lookupUnknownCommand();
    });
    expect(result.current.outbox.lifecycle).toBe("not_found_retry_choice");

    await act(async () => {
      await result.current.discardPendingCommand();
    });
    expect(result.current.outbox.lifecycle).toBe("idle");
    expect(result.current.controlsLocked).toBe(false);
  });

  it("keeps pending recovery alive across same-session meta refreshes and locks controls until recovered", async () => {
    let resolvePending!: (res: Response) => void;
    const pendingPromise = new Promise<Response>((resolve) => {
      resolvePending = resolve;
    });
    let metaSeq = 1;

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.startsWith("/api/v1/meta")) {
        metaSeq += 1;
        return new Response(JSON.stringify({ ...MOCK_META, event_seq: metaSeq }), {
          status: 200,
        });
      }
      if (url.startsWith("/api/v1/events")) {
        return await new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError"))
          );
        });
      }
      if (url === "/api/local/pending" && method === "GET") {
        return await pendingPromise;
      }
      return new Response("Not found", { status: 404 });
    });

    const { result } = renderHook(
      () => ({ outbox: useOutbox(), cp: useControlPlane() }),
      { wrapper }
    );

    await waitFor(() => expect(result.current.cp.metaReady).toBe(true));
    // Controls must stay locked while durable outbox recovery is still in flight.
    expect(result.current.outbox.controlsLocked).toBe(true);
    expect(result.current.outbox.canWrite).toBe(false);

    // Trigger a same-session metadata refresh while /api/local/pending is still pending!
    await act(async () => {
      await result.current.cp.forceRefresh();
    });
    expect(result.current.cp.meta?.event_seq).toBeGreaterThan(2);

    // Now resolve the in-flight recovery with an unconfirmed pending command.
    await act(async () => {
      resolvePending(
        new Response(
          JSON.stringify({
            pending: SAMPLE_INTENT,
            receipt: null,
          }),
          { status: 200 }
        )
      );
    });

    await waitFor(() => {
      expect(result.current.outbox.outbox.lifecycle).toBe("unknown");
      expect(result.current.outbox.outbox.intent?.command_id).toBe(SAMPLE_INTENT.command_id);
    });
    expect(result.current.outbox.controlsLocked).toBe(true);
  });
});
