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
import { createYogurtAgent } from "@openboa/yogurt/agent";
import type { Appearance, Material } from "@openboa/yogurt";
import {
  ApiError,
  deletePendingOutboxApi,
  fetchEventsNdjson,
  fetchMeta,
  lookupCommandReceiptApi,
  readPendingOutboxApi,
  submitCommandApi,
  writePendingOutboxApi,
} from "../api/client";
import type {
  CommandLifecycle,
  CommandReceiptResponse,
  ContextRef,
  ControlPlaneMeta,
  LocalCommandIntent,
} from "../api/types";

export type LinkState = "connecting" | "live" | "stale" | "unauthorized" | "error";

export interface CommandOutboxState {
  lifecycle: CommandLifecycle;
  intent: LocalCommandIntent | null;
  receipt: CommandReceiptResponse | null;
  errorMessage: string | null;
  quarantineNotice: string | null;
}

export interface ToastItem {
  id: string;
  message: string;
}

interface AppContextValue {
  route: string;
  navigate: (path: string, opts?: { contextRef?: ContextRef | null; focusReturnId?: string }) => void;
  focusReturnId: string | null;
  clearFocusReturnId: () => void;
  pendingAskContext: ContextRef | null;
  setPendingAskContext: (ctx: ContextRef | null) => void;
  appearance: Appearance;
  setAppearance: (next: Appearance) => void;
  material: Material;
  setMaterial: (next: Material) => void;
  toasts: ToastItem[];
  pushToast: (message: string) => void;
  dismissToast: (id: string) => void;
  meta: ControlPlaneMeta | null;
  linkState: LinkState;
  lastError: string | null;
  refreshSeq: number;
  lastEventSeq: number;
  recentEventTypes: string[];
  hostLocked: boolean;
  setHostLocked: (locked: boolean) => void;
  controlsLocked: boolean;
  canWrite: boolean;
  outbox: CommandOutboxState;
  dispatchCommand: (intent: LocalCommandIntent) => Promise<void>;
  lookupUnknownCommand: () => Promise<void>;
  retryNotFoundCommand: () => Promise<void>;
  discardPendingCommand: () => Promise<void>;
  dismissFinalOutbox: () => void;
  reportApiError: (err: unknown) => void;
  forceRefresh: () => void;
}

const AppContext = createContext<AppContextValue | null>(null);

function normalizeRoute(pathname: string): string {
  if (!pathname || pathname === "/") return "/";
  return pathname.endsWith("/") ? pathname.slice(0, -1) : pathname;
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [route, setRoute] = useState<string>(() =>
    normalizeRoute(window.location.pathname + window.location.search)
  );
  const [focusReturnId, setFocusReturnId] = useState<string | null>(null);
  const [pendingAskContext, setPendingAskContext] = useState<ContextRef | null>(null);
  const [appearance, setAppearanceState] = useState<Appearance>(() => {
    try {
      const saved = window.localStorage.getItem("sd-yogurt-appearance");
      if (saved === "light" || saved === "dark" || saved === "auto") return saved;
    } catch {
      // Ignore storage failures in restricted contexts.
    }
    return "light";
  });
  const [material, setMaterialState] = useState<Material>(() => {
    try {
      const saved = window.localStorage.getItem("sd-yogurt-material");
      if (saved === "neutral" || saved === "paper" || saved === "mist") return saved;
    } catch {
      // Ignore storage failures in restricted contexts.
    }
    return "neutral";
  });
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  const setAppearance = useCallback((next: Appearance) => {
    setAppearanceState(next);
    try {
      window.localStorage.setItem("sd-yogurt-appearance", next);
    } catch {
      // Ignore storage failures.
    }
  }, []);

  const setMaterial = useCallback((next: Material) => {
    setMaterialState(next);
    try {
      window.localStorage.setItem("sd-yogurt-material", next);
    } catch {
      // Ignore storage failures.
    }
  }, []);

  const dismissToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((item) => item.id !== id));
  }, []);

  const pushToast = useCallback((message: string) => {
    const id = `toast-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
    setToasts((prev) => [...prev.slice(-3), { id, message }]);
    window.setTimeout(() => {
      setToasts((prev) => prev.filter((item) => item.id !== id));
    }, 4500);
  }, []);

  const [meta, setMeta] = useState<ControlPlaneMeta | null>(null);
  const [linkState, setLinkState] = useState<LinkState>("connecting");
  const [lastError, setLastError] = useState<string | null>(null);
  const [refreshSeq, setRefreshSeq] = useState(0);
  const [lastEventSeq, setLastEventSeq] = useState(0);
  const [recentEventTypes, setRecentEventTypes] = useState<string[]>([]);
  const [hostLocked, setHostLocked] = useState(false);

  const [outbox, setOutbox] = useState<CommandOutboxState>({
    lifecycle: "idle",
    intent: null,
    receipt: null,
    errorMessage: null,
    quarantineNotice: null,
  });

  const incarnationRef = useRef<string | null>(null);
  const companyIdRef = useRef<string | null>(null);
  const storeKeyRef = useRef<string | null>(null);
  const principalIdRef = useRef<string | null | undefined>(undefined);
  const lastSeqRef = useRef<number>(0);
  const submittingRef = useRef<boolean>(false);
  const [metaReady, setMetaReady] = useState(false);

  // Mount Yogurt agent bridge disabled by default (allow: () => false).
  useEffect(() => {
    try {
      createYogurtAgent({ allow: () => false });
    } catch {
      // Safe no-op in non-browser test hosts.
    }
  }, []);

  // Browser history synchronization.
  useEffect(() => {
    const onPop = () => {
      setRoute(normalizeRoute(window.location.pathname + window.location.search));
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = useCallback(
    (
      path: string,
      opts?: { contextRef?: ContextRef | null; focusReturnId?: string }
    ) => {
      const normalized = normalizeRoute(path);
      if (opts && "contextRef" in opts) {
        setPendingAskContext(opts.contextRef ?? null);
      }
      if (opts?.focusReturnId) {
        setFocusReturnId(opts.focusReturnId);
      }
      if (window.location.pathname + window.location.search !== normalized) {
        window.history.pushState({}, "", normalized);
      }
      setRoute(normalized);
      // Clear already-confirmed routine CEO chat receipts on navigation so
      // non-blocking chat confirmations do not linger across unrelated screens.
      setOutbox((prev) =>
        prev.lifecycle === "applied" && prev.intent?.command_type === "send_ceo_message"
          ? {
              lifecycle: "idle",
              intent: null,
              receipt: null,
              errorMessage: null,
              quarantineNotice: null,
            }
          : prev
      );
      window.scrollTo({ top: 0, behavior: "instant" as ScrollBehavior });
    },
    []
  );

  const clearFocusReturnId = useCallback(() => setFocusReturnId(null), []);

  const clearData = useCallback((reason: string, state: LinkState = "unauthorized") => {
    setMeta(null);
    setLinkState(state);
    setLastError(reason);
    setRecentEventTypes([]);
    setPendingAskContext(null);
    setOutbox({
      lifecycle: "idle",
      intent: null,
      receipt: null,
      errorMessage: null,
      quarantineNotice: null,
    });
  }, []);

  const reportApiError = useCallback(
    (err: unknown) => {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        clearData(err.message, "unauthorized");
      }
    },
    [clearData]
  );

  const loadMeta = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const nextMeta = await fetchMeta(signal);
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
        setLastEventSeq(lastSeqRef.current);
        setMeta(nextMeta);
        setLinkState("live");
        setLastError(null);
        setMetaReady(true);
        return nextMeta;
      } catch (err) {
        if (signal?.aborted) return null;
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          clearData(err.message, "unauthorized");
          return null;
        }
        // Stale disconnect preserves last known read data while locking mutations.
        setLinkState((prev) => (prev === "unauthorized" ? "unauthorized" : "stale"));
        setLastError(err instanceof Error ? err.message : "Control plane unreachable");
        return null;
      }
    },
    [clearData]
  );

  // Startup bootstrap + pending outbox recovery.
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      const loaded = await loadMeta(controller.signal);
      if (!loaded || controller.signal.aborted) return;
      try {
        const pendingInfo = await readPendingOutboxApi();
        if (pendingInfo.quarantined_stale) {
          setOutbox({
            lifecycle: "idle",
            intent: null,
            receipt: null,
            errorMessage: null,
            quarantineNotice:
              pendingInfo.notice ??
              "A stale pending command file from another session was quarantined.",
          });
          return;
        }
        if (pendingInfo.pending) {
          const rec = pendingInfo.receipt;
          if (!rec) {
            setOutbox({
              lifecycle: "unknown",
              intent: pendingInfo.pending,
              receipt: null,
              errorMessage: "Reconnected with an unconfirmed request. Check its status before retrying.",
              quarantineNotice: null,
            });
          } else if (rec.status === "not_found") {
            setOutbox({
              lifecycle: "not_found_retry_choice",
              intent: pendingInfo.pending,
              receipt: rec,
              errorMessage: "No matching command receipt was found on the daemon.",
              quarantineNotice: null,
            });
          } else if (rec.state === "applied") {
            try {
              await deletePendingOutboxApi();
              setOutbox({
                lifecycle: "applied",
                intent: pendingInfo.pending,
                receipt: rec,
                errorMessage: null,
                quarantineNotice: null,
              });
            } catch {
              setOutbox({
                lifecycle: "unknown",
                intent: pendingInfo.pending,
                receipt: rec,
                errorMessage: "Applied on daemon, but failed to clear local pending file.",
                quarantineNotice: null,
              });
            }
          } else if (rec.state === "denied") {
            await deletePendingOutboxApi().catch(() => undefined);
            setOutbox({
              lifecycle: "denied",
              intent: pendingInfo.pending,
              receipt: rec,
              errorMessage: rec.denial_code ?? "Denied",
              quarantineNotice: null,
            });
          } else {
            setOutbox({
              lifecycle: "accepted",
              intent: pendingInfo.pending,
              receipt: rec,
              errorMessage: null,
              quarantineNotice: null,
            });
          }
        }
      } catch {
        // Ignore outbox recovery network hiccup; meta poll will retry.
      }
    })();
    return () => controller.abort();
  }, [loadMeta]);

  // Coalesced NDJSON /api/v1/events long-poll stream (waits for initial meta load).
  useEffect(() => {
    if (!metaReady) return;
    let active = true;
    const controller = new AbortController();
    let coalesceTimer: number | null = null;

    async function loop() {
      while (active) {
        try {
          const events = await fetchEventsNdjson(
            lastSeqRef.current,
            3000,
            controller.signal
          );
          if (!active) break;
          if (events.length > 0) {
            const maxSeq = events[events.length - 1].event_seq;
            lastSeqRef.current = Math.max(lastSeqRef.current, maxSeq);
            setLastEventSeq(lastSeqRef.current);
            setRecentEventTypes(events.map((e) => e.event_type));
            if (coalesceTimer !== null) {
              window.clearTimeout(coalesceTimer);
            }
            coalesceTimer = window.setTimeout(() => {
              if (!active) return;
              void loadMeta(controller.signal);
              setRefreshSeq((n) => n + 1);
            }, 150);
          }
        } catch (err) {
          if (!active || controller.signal.aborted) break;
          if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
            clearData(err.message, "unauthorized");
            break;
          }
          setLinkState("stale");
          setLastError(err instanceof Error ? err.message : "Live event stream disconnected");
          await new Promise((r) => window.setTimeout(r, 1500));
          if (active) {
            await loadMeta(controller.signal);
          }
        }
      }
    }

    void loop();
    return () => {
      active = false;
      controller.abort();
      if (coalesceTimer !== null) window.clearTimeout(coalesceTimer);
    };
  }, [clearData, loadMeta, metaReady]);

  const dispatchCommand = useCallback(
    async (intent: LocalCommandIntent) => {
      if (submittingRef.current) return;
      submittingRef.current = true;
      try {
        setOutbox({
          lifecycle: "submitting",
          intent,
          receipt: null,
          errorMessage: null,
          quarantineNotice: null,
        });

        // Persist pending intent BEFORE wire transmission so mid-flight drops recover cleanly.
        try {
          await writePendingOutboxApi(intent);
        } catch (err) {
          setOutbox({
            lifecycle: "denied",
            intent,
            receipt: null,
            errorMessage:
              err instanceof Error
                ? `Failed to record local pending guard: ${err.message}`
                : "Failed to record local pending guard",
            quarantineNotice: null,
          });
          return;
        }

        try {
          const receipt = await submitCommandApi(intent);
          if (receipt.state === "applied") {
            try {
              await deletePendingOutboxApi();
            } catch {
              setOutbox({
                lifecycle: "unknown",
                intent,
                receipt,
                errorMessage: "Command applied, but clearing local pending file failed.",
                quarantineNotice: null,
              });
              return;
            }
            setOutbox({
              lifecycle: "applied",
              intent,
              receipt,
              errorMessage: null,
              quarantineNotice: null,
            });
            await loadMeta();
            setRefreshSeq((n) => n + 1);
          } else if (receipt.state === "denied") {
            await deletePendingOutboxApi().catch(() => undefined);
            setOutbox({
              lifecycle: "denied",
              intent,
              receipt,
              errorMessage:
                receipt.denial_code === "conflicting_revision"
                  ? "State changed before your action was applied (conflicting_revision). Review the updated values and decide again."
                  : `Denied: ${receipt.denial_code ?? receipt.disposition ?? "rejected"}`,
              quarantineNotice: null,
            });
            await loadMeta();
            setRefreshSeq((n) => n + 1);
          } else {
            setOutbox({
              lifecycle: "accepted",
              intent,
              receipt,
              errorMessage: null,
              quarantineNotice: null,
            });
          }
        } catch (err) {
          if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
            await deletePendingOutboxApi().catch(() => undefined);
            if (err.status === 401) {
              clearData(err.message, "unauthorized");
              return;
            }
            setOutbox({
              lifecycle: "denied",
              intent,
              receipt: null,
              errorMessage: err.message,
              quarantineNotice: null,
            });
            return;
          }
          // Transport timeout / 5xx -> request outcome is unknown; retain board-pending file!
          setOutbox({
            lifecycle: "unknown",
            intent,
            receipt: null,
            errorMessage:
              err instanceof Error
                ? `Connection interrupted after send (${err.message}). Check original request before retrying.`
                : "Outcome unknown. Check original request before retrying.",
            quarantineNotice: null,
          });
        }
      } finally {
        submittingRef.current = false;
      }
    },
    [clearData, loadMeta]
  );

  const lookupUnknownCommand = useCallback(async () => {
    const intent = outbox.intent;
    if (!intent) return;
    try {
      const receipt = await lookupCommandReceiptApi(
        intent.command_id,
        intent.command_type
      );
      if (receipt.status === "not_found") {
        setOutbox({
          lifecycle: "not_found_retry_choice",
          intent,
          receipt,
          errorMessage:
            "Original request was not recorded on the daemon. Choose whether to retry the exact original request or discard it.",
          quarantineNotice: null,
        });
        return;
      }
      if (receipt.state === "applied") {
        try {
          await deletePendingOutboxApi();
        } catch {
          setOutbox({
            lifecycle: "unknown",
            intent,
            receipt,
            errorMessage: "Applied on daemon, but failed to remove pending file.",
            quarantineNotice: null,
          });
          return;
        }
        setOutbox({
          lifecycle: "applied",
          intent,
          receipt,
          errorMessage: null,
          quarantineNotice: null,
        });
        await loadMeta();
        setRefreshSeq((n) => n + 1);
      } else if (receipt.state === "denied") {
        await deletePendingOutboxApi().catch(() => undefined);
        setOutbox({
          lifecycle: "denied",
          intent,
          receipt,
          errorMessage: receipt.denial_code ?? "Denied",
          quarantineNotice: null,
        });
        await loadMeta();
        setRefreshSeq((n) => n + 1);
      } else {
        setOutbox({
          lifecycle: "accepted",
          intent,
          receipt,
          errorMessage: null,
          quarantineNotice: null,
        });
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        await deletePendingOutboxApi().catch(() => undefined);
        setOutbox({
          lifecycle: "idle",
          intent: null,
          receipt: null,
          errorMessage: null,
          quarantineNotice: "Stale unowned pending receipt returned 403 and was cleared.",
        });
        return;
      }
      setOutbox((prev) => ({
        ...prev,
        lifecycle: "unknown",
        errorMessage:
          err instanceof Error ? err.message : "Could not reach daemon to verify receipt.",
      }));
    }
  }, [loadMeta, outbox.intent]);

  const retryNotFoundCommand = useCallback(async () => {
    if (!outbox.intent) return;
    await dispatchCommand(outbox.intent);
  }, [dispatchCommand, outbox.intent]);

  const discardPendingCommand = useCallback(async () => {
    await deletePendingOutboxApi().catch(() => undefined);
    setOutbox({
      lifecycle: "idle",
      intent: null,
      receipt: null,
      errorMessage: null,
      quarantineNotice: null,
    });
  }, []);

  const dismissFinalOutbox = useCallback(() => {
    setOutbox((prev) =>
      prev.lifecycle === "applied" || prev.lifecycle === "denied" || prev.lifecycle === "idle"
        ? {
            lifecycle: "idle",
            intent: null,
            receipt: null,
            errorMessage: null,
            quarantineNotice: null,
          }
        : prev
    );
  }, []);

  const forceRefresh = useCallback(() => {
    void loadMeta();
    setRefreshSeq((n) => n + 1);
  }, [loadMeta]);

  const controlsLocked =
    linkState !== "live" ||
    hostLocked ||
    outbox.lifecycle === "submitting" ||
    outbox.lifecycle === "unknown" ||
    outbox.lifecycle === "accepted" ||
    outbox.lifecycle === "not_found_retry_choice";

  const canWrite =
    !controlsLocked &&
    meta !== null &&
    !meta.board_access.bootstrap_locked &&
    meta.board_access.role === "board_control";

  const value = useMemo<AppContextValue>(
    () => ({
      route,
      navigate,
      focusReturnId,
      clearFocusReturnId,
      pendingAskContext,
      setPendingAskContext,
      appearance,
      setAppearance,
      material,
      setMaterial,
      toasts,
      pushToast,
      dismissToast,
      meta,
      linkState,
      lastError,
      refreshSeq,
      lastEventSeq,
      recentEventTypes,
      hostLocked,
      setHostLocked,
      controlsLocked,
      canWrite,
      outbox,
      dispatchCommand,
      lookupUnknownCommand,
      retryNotFoundCommand,
      discardPendingCommand,
      dismissFinalOutbox,
      reportApiError,
      forceRefresh,
    }),
    [
      route,
      navigate,
      focusReturnId,
      clearFocusReturnId,
      pendingAskContext,
      appearance,
      setAppearance,
      material,
      setMaterial,
      toasts,
      pushToast,
      dismissToast,
      meta,
      linkState,
      lastError,
      refreshSeq,
      lastEventSeq,
      recentEventTypes,
      hostLocked,
      controlsLocked,
      canWrite,
      outbox,
      dispatchCommand,
      lookupUnknownCommand,
      retryNotFoundCommand,
      discardPendingCommand,
      dismissFinalOutbox,
      reportApiError,
      forceRefresh,
    ]
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useApp(): AppContextValue {
  const ctx = useContext(AppContext);
  if (!ctx) {
    throw new Error("useApp must be used inside AppProvider");
  }
  return ctx;
}
