import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ApiError, fetchEventsNdjson, fetchMeta } from "../api/client";
import type { ControlPlaneMeta } from "../api/types";
import { resolveActiveDomainModules, resolveHomeMode } from "../domains/registry";
import { isMessagesPathname, splitRoute } from "../lib/routes";
import { useRouter } from "./RouterContext";

export type LinkState = "connecting" | "live" | "stale" | "unauthorized" | "error";

export interface ControlPlaneContextValue {
  meta: ControlPlaneMeta | null;
  metaReady: boolean;
  linkState: LinkState;
  lastError: string | null;
  refreshSeq: number;
  lastEventSeq: number;
  recentEventTypes: string[];
  unreadCeoReply: boolean;
  getLastSeenSeq: (companyId: string, principalId?: string | null) => number;
  markSeenSeq: (
    companyId: string,
    principalId: string | null | undefined,
    seq: number
  ) => void;
  markMessagesVisited: (seq: number) => void;
  reportApiError: (err: unknown) => void;
  forceRefresh: () => Promise<boolean>;
  loadMeta: (
    signal?: AbortSignal,
    opts?: { retryTransientNetwork?: boolean }
  ) => Promise<ControlPlaneMeta | null>;
  bumpRefresh: () => void;
  clearData: (reason: string, state?: LinkState) => void;
  subscribeSessionClear: (listener: () => void) => () => void;
}

const ControlPlaneContext = createContext<ControlPlaneContextValue | null>(null);

function lastSeenStorageKey(companyId: string, principalId?: string | null): string {
  return `sd.lastSeenSeq.${companyId}.${principalId || "unbound"}`;
}

function lastMessagesStorageKey(companyId: string, principalId?: string | null): string {
  return `sd.lastMessagesSeq.${companyId}.${principalId || "unbound"}`;
}

function readStoredSeq(key: string): number {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return 0;
    const parsed = Number.parseInt(raw, 10);
    return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : 0;
  } catch {
    return 0;
  }
}

function writeStoredSeq(key: string, seq: number): void {
  if (!Number.isFinite(seq) || seq < 0) return;
  try {
    window.localStorage.setItem(key, String(Math.floor(seq)));
  } catch {
    // Ignore storage failures in restricted contexts.
  }
}

export const IDLE_META_REFRESH_MS = 6000;

export function ControlPlaneProvider({
  children,
  onClearCache,
  idleMetaRefreshMs = IDLE_META_REFRESH_MS,
}: {
  children: ReactNode;
  onClearCache?: () => void;
  idleMetaRefreshMs?: number;
}) {
  const {
    routePathname,
    routeSearchParams,
    routeRef,
    navigate,
    clearPendingAskContextDirect,
  } = useRouter();

  const [meta, setMeta] = useState<ControlPlaneMeta | null>(null);
  const [metaReady, setMetaReady] = useState(false);
  const [linkState, setLinkState] = useState<LinkState>("connecting");
  const [lastError, setLastError] = useState<string | null>(null);
  const [refreshSeq, setRefreshSeq] = useState(0);
  const [lastEventSeq, setLastEventSeq] = useState(0);
  const [recentEventTypes, setRecentEventTypes] = useState<string[]>([]);
  const [unreadCeoReply, setUnreadCeoReply] = useState(false);

  const incarnationRef = useRef<string | null>(null);
  const companyIdRef = useRef<string | null>(null);
  const storeKeyRef = useRef<string | null>(null);
  const principalIdRef = useRef<string | null | undefined>(undefined);
  const lastSeqRef = useRef<number>(0);
  const linkStateRef = useRef<LinkState>(linkState);
  linkStateRef.current = linkState;
  const metaReadyRef = useRef(metaReady);
  metaReadyRef.current = metaReady;
  const startupMsgCheckedRef = useRef<string | null>(null);
  const prevBootstrapLockedRef = useRef<boolean | undefined>(undefined);
  const clearListenersRef = useRef<Set<() => void>>(new Set());
  const lastMetaLoadedAtRef = useRef<number>(0);
  const metaInFlightCountRef = useRef<number>(0);

  const subscribeSessionClear = useCallback((listener: () => void) => {
    clearListenersRef.current.add(listener);
    return () => {
      clearListenersRef.current.delete(listener);
    };
  }, []);

  const bumpRefresh = useCallback(() => {
    setRefreshSeq((n) => n + 1);
  }, []);

  const getLastSeenSeq = useCallback(
    (companyId: string, principalId?: string | null): number => {
      if (!companyId) return 0;
      return readStoredSeq(lastSeenStorageKey(companyId, principalId));
    },
    []
  );

  const markSeenSeq = useCallback(
    (companyId: string, principalId: string | null | undefined, seq: number): void => {
      if (!companyId) return;
      writeStoredSeq(lastSeenStorageKey(companyId, principalId), seq);
    },
    []
  );

  const markMessagesVisited = useCallback((seq: number): void => {
    const companyId = companyIdRef.current;
    if (companyId && Number.isFinite(seq) && seq >= 0) {
      writeStoredSeq(lastMessagesStorageKey(companyId, principalIdRef.current), seq);
    }
    setUnreadCeoReply(false);
  }, []);

  useEffect(() => {
    if (!isMessagesPathname(routePathname)) return;
    const channel = routeSearchParams.get("channel");
    if (channel && channel !== "ceo-direct") return;
    markMessagesVisited(lastSeqRef.current || meta?.event_seq || 0);
  }, [
    routePathname,
    routeSearchParams,
    unreadCeoReply,
    meta?.event_seq,
    markMessagesVisited,
  ]);

  const clearData = useCallback(
    (reason: string, state: LinkState = "unauthorized") => {
      setMeta(null);
      setMetaReady(false);
      setLinkState(state);
      setLastError(reason);
      setRecentEventTypes([]);
      setUnreadCeoReply(false);
      startupMsgCheckedRef.current = null;
      lastMetaLoadedAtRef.current = 0;
      clearPendingAskContextDirect();
      onClearCache?.();
      for (const listener of clearListenersRef.current) {
        listener();
      }
    },
    [clearPendingAskContextDirect, onClearCache]
  );

  const reportApiError = useCallback(
    (err: unknown) => {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        clearData(err.message, "unauthorized");
      }
    },
    [clearData]
  );

  const loadMeta = useCallback(
    async (signal?: AbortSignal, opts?: { retryTransientNetwork?: boolean }) => {
      metaInFlightCountRef.current += 1;
      try {
        const nextMeta = await fetchMeta(signal, opts);
        if (signal?.aborted) return null;
        const nextPrincipal = nextMeta.board_access.principal_id;
        if (
          (companyIdRef.current !== null &&
            companyIdRef.current !== nextMeta.company_id) ||
          (storeKeyRef.current !== null &&
            storeKeyRef.current !== nextMeta.store_key) ||
          (principalIdRef.current !== undefined &&
            principalIdRef.current !== null &&
            principalIdRef.current !== nextPrincipal)
        ) {
          clearData(
            "Company, store, or Board principal scope changed; cleared session data.",
            "unauthorized"
          );
          return null;
        }
        companyIdRef.current = nextMeta.company_id;
        storeKeyRef.current = nextMeta.store_key;
        principalIdRef.current = nextPrincipal;
        incarnationRef.current = nextMeta.service_incarnation_id;
        lastSeqRef.current = Math.max(lastSeqRef.current, nextMeta.event_seq);
        lastMetaLoadedAtRef.current = Date.now();
        setLastEventSeq(lastSeqRef.current);
        setMeta(nextMeta);
        setLinkState("live");
        setLastError(null);
        setMetaReady(true);

        const scopeKey = `${nextMeta.company_id}:${nextPrincipal ?? "unbound"}`;
        if (startupMsgCheckedRef.current !== scopeKey) {
          startupMsgCheckedRef.current = scopeKey;
          const savedMsgSeq = readStoredSeq(
            lastMessagesStorageKey(nextMeta.company_id, nextPrincipal)
          );
          if (savedMsgSeq > 0 && savedMsgSeq < nextMeta.event_seq) {
            void fetchEventsNdjson(savedMsgSeq, 0, signal)
              .then((events) => {
                if (signal?.aborted) return;
                if (events.some((e) => e.event_type === "ceo.message_replied")) {
                  const sp = splitRoute(routeRef.current);
                  const ch = new URLSearchParams(
                    sp.search.startsWith("?") ? sp.search.slice(1) : sp.search
                  ).get("channel");
                  if (isMessagesPathname(sp.pathname) && (!ch || ch === "ceo-direct")) {
                    markMessagesVisited(nextMeta.event_seq);
                  } else {
                    setUnreadCeoReply(true);
                  }
                }
              })
              .catch(() => undefined);
          }
        }

        return nextMeta;
      } catch (err) {
        if (signal?.aborted) return null;
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          clearData(err.message, "unauthorized");
          return null;
        }
        setLinkState((prev) => (prev === "unauthorized" ? "unauthorized" : "stale"));
        setLastError(err instanceof Error ? err.message : "Control plane unreachable");
        return null;
      } finally {
        metaInFlightCountRef.current = Math.max(0, metaInFlightCountRef.current - 1);
      }
    },
    [clearData, markMessagesVisited, routeRef]
  );

  // Redirect away from /bootstrap once bootstrap_locked clears.
  useEffect(() => {
    const prevLocked = prevBootstrapLockedRef.current;
    const isLocked = meta?.board_access.bootstrap_locked;
    prevBootstrapLockedRef.current = isLocked;
    if (
      isLocked === false &&
      (prevLocked === true || prevLocked === undefined) &&
      splitRoute(routeRef.current).pathname === "/bootstrap"
    ) {
      const homeMode = resolveHomeMode(resolveActiveDomainModules(meta?.active_domains));
      navigate(homeMode === "core_only" ? "/agents" : "/", { replace: true });
    }
  }, [meta?.board_access.bootstrap_locked, meta?.active_domains, navigate, routeRef]);

  // Initial metadata bootstrap with transient retry backoff.
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const retryTimers: number[] = [];

    const waitRetry = (ms: number) =>
      new Promise<void>((resolve) => {
        const id = window.setTimeout(resolve, ms);
        retryTimers.push(id);
      });

    const shouldStopRetry = () =>
      !active ||
      controller.signal.aborted ||
      linkStateRef.current === "unauthorized" ||
      metaReadyRef.current;

    void (async () => {
      let loaded = await loadMeta(controller.signal, { retryTransientNetwork: true });
      const retryDelays = [300, 800, 1600];
      for (let i = 0; !loaded && i < retryDelays.length; i += 1) {
        if (shouldStopRetry()) break;
        await waitRetry(retryDelays[i]);
        if (shouldStopRetry()) break;
        loaded = await loadMeta(controller.signal, { retryTransientNetwork: true });
      }
    })();

    return () => {
      active = false;
      controller.abort();
      for (const id of retryTimers) {
        window.clearTimeout(id);
      }
    };
  }, [loadMeta]);

  // Coalesced NDJSON /api/v1/events long-poll stream (waits for initial meta load).
  useEffect(() => {
    if (!metaReady) return;
    let active = true;
    const controller = new AbortController();
    let coalesceTimer: number | null = null;
    let settleTimer: number | null = null;
    let pendingWaitResolve: (() => void) | null = null;

    const waitMsDelay = (ms: number) =>
      new Promise<void>((resolve) => {
        if (!active) {
          resolve();
          return;
        }
        pendingWaitResolve = resolve;
        settleTimer = window.setTimeout(() => {
          settleTimer = null;
          pendingWaitResolve = null;
          resolve();
        }, ms);
      });

    const cancelPendingWait = () => {
      if (settleTimer !== null) {
        window.clearTimeout(settleTimer);
        settleTimer = null;
      }
      if (pendingWaitResolve) {
        const resolve = pendingWaitResolve;
        pendingWaitResolve = null;
        resolve();
      }
    };

    const shutdown = () => {
      active = false;
      controller.abort();
      cancelPendingWait();
      if (coalesceTimer !== null) {
        window.clearTimeout(coalesceTimer);
        coalesceTimer = null;
      }
    };

    const onVisibilityChange = () => {
      if (
        !active ||
        controller.signal.aborted ||
        document.visibilityState !== "visible" ||
        metaInFlightCountRef.current > 0
      ) {
        return;
      }
      if (Date.now() - lastMetaLoadedAtRef.current >= idleMetaRefreshMs) {
        void loadMeta(controller.signal);
      }
    };

    window.addEventListener("pagehide", shutdown);
    document.addEventListener("visibilitychange", onVisibilityChange);

    async function loop() {
      let waitMs = 0;
      while (active) {
        try {
          const events = await fetchEventsNdjson(
            lastSeqRef.current,
            waitMs,
            controller.signal
          );
          if (!active) break;
          if (events.length > 0) {
            const maxSeq = events[events.length - 1].event_seq;
            lastSeqRef.current = Math.max(lastSeqRef.current, maxSeq);
            setLastEventSeq(lastSeqRef.current);
            setRecentEventTypes(events.map((e) => e.event_type));
            const ceoReplies = events.filter(
              (e) => e.event_type === "ceo.message_replied"
            );
            if (ceoReplies.length > 0) {
              const sp = splitRoute(routeRef.current);
              const ch = new URLSearchParams(
                sp.search.startsWith("?") ? sp.search.slice(1) : sp.search
              ).get("channel");
              if (isMessagesPathname(sp.pathname) && (!ch || ch === "ceo-direct")) {
                markMessagesVisited(ceoReplies[ceoReplies.length - 1].event_seq);
              } else {
                setUnreadCeoReply(true);
              }
            }
            if (coalesceTimer !== null) {
              window.clearTimeout(coalesceTimer);
            }
            coalesceTimer = window.setTimeout(() => {
              if (!active) return;
              void loadMeta(controller.signal);
              setRefreshSeq((n) => n + 1);
            }, 150);
          } else if (linkStateRef.current === "stale") {
            await loadMeta(controller.signal);
          } else if (
            (typeof document === "undefined" ||
              document.visibilityState !== "hidden") &&
            Date.now() - lastMetaLoadedAtRef.current >= idleMetaRefreshMs
          ) {
            await loadMeta(controller.signal);
          }
          if (waitMs === 0) {
            waitMs = 3000;
            await waitMsDelay(400);
            if (!active) break;
          }
        } catch (err) {
          if (!active || controller.signal.aborted) break;
          if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
            clearData(err.message, "unauthorized");
            break;
          }
          setLinkState("stale");
          setLastError(err instanceof Error ? err.message : "Live event stream disconnected");
          await waitMsDelay(1500);
          if (active) {
            await loadMeta(controller.signal);
          }
        }
      }
    }

    void loop();
    return () => {
      window.removeEventListener("pagehide", shutdown);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      shutdown();
    };
  }, [clearData, idleMetaRefreshMs, loadMeta, markMessagesVisited, metaReady, routeRef]);

  const forceRefresh = useCallback(async (): Promise<boolean> => {
    const loaded = await loadMeta();
    setRefreshSeq((n) => n + 1);
    return loaded !== null;
  }, [loadMeta]);

  const value = useMemo<ControlPlaneContextValue>(
    () => ({
      meta,
      metaReady,
      linkState,
      lastError,
      refreshSeq,
      lastEventSeq,
      recentEventTypes,
      unreadCeoReply,
      getLastSeenSeq,
      markSeenSeq,
      markMessagesVisited,
      reportApiError,
      forceRefresh,
      loadMeta,
      bumpRefresh,
      clearData,
      subscribeSessionClear,
    }),
    [
      meta,
      metaReady,
      linkState,
      lastError,
      refreshSeq,
      lastEventSeq,
      recentEventTypes,
      unreadCeoReply,
      getLastSeenSeq,
      markSeenSeq,
      markMessagesVisited,
      reportApiError,
      forceRefresh,
      loadMeta,
      bumpRefresh,
      clearData,
      subscribeSessionClear,
    ]
  );

  return (
    <ControlPlaneContext.Provider value={value}>{children}</ControlPlaneContext.Provider>
  );
}

export function useControlPlane(): ControlPlaneContextValue {
  const ctx = useContext(ControlPlaneContext);
  if (!ctx) {
    throw new Error("useControlPlane must be used inside ControlPlaneProvider");
  }
  return ctx;
}
