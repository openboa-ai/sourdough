import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PreferencesProvider } from "../state/PreferencesContext";
import { RouterProvider } from "../state/RouterContext";
import { ControlPlaneProvider } from "../state/ControlPlaneContext";
import { clearProjectionCache, useProjection } from "./useProjection";

const MOCK_META = {
  company_id: "co-test-1",
  company_name: "Test Co",
  charter_revision: "charter-v1",
  store_key: "store-1",
  service_incarnation_id: "inc-1",
  event_seq: 10,
  active_domains: ["core"],
  board_access: {
    bootstrap_locked: false,
    principal_id: "owner",
    role: "board_control" as const,
    binding_revision: 1,
  },
};

function wrapper({ children }: { children: ReactNode }) {
  return (
    <PreferencesProvider>
      <RouterProvider>
        <ControlPlaneProvider onClearCache={clearProjectionCache}>
          {children}
        </ControlPlaneProvider>
      </RouterProvider>
    </PreferencesProvider>
  );
}

describe("api/useProjection (ETag / 304 caching & abort safety)", () => {
  beforeEach(() => {
    clearProjectionCache();
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    clearProjectionCache();
  });

  it("caches initial 200 payload + ETag, sends If-None-Match on refetch, and preserves data on 304", async () => {
    const seenHeaders: Array<string | null> = [];
    let projectionCalls = 0;

    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.startsWith("/api/v1/meta")) {
        return new Response(JSON.stringify(MOCK_META), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.startsWith("/api/v1/events")) {
        // Keep events stream quiet until aborted.
        return await new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError"))
          );
        });
      }
      if (url === "/api/v1/projections/finance") {
        projectionCalls += 1;
        const h = new Headers(init?.headers);
        seenHeaders.push(h.get("If-None-Match"));
        if (projectionCalls === 1) {
          return new Response(JSON.stringify({ cash: 1000, revision: 1 }), {
            status: 200,
            headers: {
              "Content-Type": "application/json",
              ETag: '"fin-etag-1"',
            },
          });
        }
        return new Response(null, {
          status: 304,
          headers: { ETag: '"fin-etag-1"' },
        });
      }
      return new Response("Not found", { status: 404 });
    });

    const { result } = renderHook(
      () => useProjection<{ cash: number; revision: number }>("finance"),
      { wrapper }
    );

    await waitFor(() => {
      expect(result.current.data).toEqual({ cash: 1000, revision: 1 });
      expect(result.current.loading).toBe(false);
    });
    expect(result.current.etag).toBe('"fin-etag-1"');
    expect(seenHeaders[0]).toBeNull();

    const firstRef = result.current.data;

    act(() => {
      result.current.refetch();
    });

    await waitFor(() => {
      expect(result.current.notModifiedCount).toBe(1);
    });
    expect(seenHeaders[1]).toBe('"fin-etag-1"');
    // Referential equality preserved on 304 Not Modified.
    expect(result.current.data).toBe(firstRef);
    expect(result.current.error).toBeNull();
    expect(fetchMock).toHaveBeenCalled();
  });
});
