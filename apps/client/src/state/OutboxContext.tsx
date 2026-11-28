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
import {
  ApiError,
  deletePendingOutboxApi,
  lookupCommandReceiptApi,
  readPendingOutboxApi,
  submitCommandApi,
  writePendingOutboxApi,
} from "../api/client";
import type {
  CommandLifecycle,
  CommandReceiptResponse,
  LocalCommandIntent,
} from "../api/types";
import { useControlPlane } from "./ControlPlaneContext";
import { usePreferences } from "./PreferencesContext";
import { useRouter } from "./RouterContext";

export interface CommandOutboxState {
  lifecycle: CommandLifecycle;
  intent: LocalCommandIntent | null;
  receipt: CommandReceiptResponse | null;
  errorMessage: string | null;
  quarantineNotice: string | null;
}

export const IDLE_OUTBOX_STATE: CommandOutboxState = {
  lifecycle: "idle",
  intent: null,
  receipt: null,
  errorMessage: null,
  quarantineNotice: null,
};

export interface OutboxContextValue {
  outbox: CommandOutboxState;
  controlsLocked: boolean;
  canWrite: boolean;
  dispatchCommand: (intent: LocalCommandIntent) => Promise<CommandLifecycle>;
  lookupUnknownCommand: () => Promise<void>;
  retryNotFoundCommand: () => Promise<void>;
  discardPendingCommand: () => Promise<void>;
  dismissFinalOutbox: () => void;
}

const OutboxContext = createContext<OutboxContextValue | null>(null);

export function OutboxProvider({ children }: { children: ReactNode }) {
  const { hostLocked } = usePreferences();
  const { subscribePathChange } = useRouter();
  const {
    meta,
    metaReady,
    linkState,
    loadMeta,
    bumpRefresh,
    clearData,
    subscribeSessionClear,
  } = useControlPlane();

  const [outbox, setOutbox] = useState<CommandOutboxState>(IDLE_OUTBOX_STATE);
  const submittingRef = useRef<boolean>(false);
  const outboxRef = useRef<CommandOutboxState>(outbox);
  outboxRef.current = outbox;
  const recoveredForSessionRef = useRef<string | null>(null);

  // Clear "applied" confirmation banner on pathname changes.
  useEffect(() => {
    return subscribePathChange(() => {
      setOutbox((prev) => (prev.lifecycle === "applied" ? IDLE_OUTBOX_STATE : prev));
    });
  }, [subscribePathChange]);

  // Reset outbox when control plane session clears.
  useEffect(() => {
    return subscribeSessionClear(() => {
      recoveredForSessionRef.current = null;
      setOutbox(IDLE_OUTBOX_STATE);
    });
  }, [subscribeSessionClear]);

  // Pending outbox recovery once initial metadata is loaded for the active session.
  useEffect(() => {
    if (!metaReady || !meta) return;
    const sessionKey = `${meta.company_id}:${meta.store_key}:${meta.service_incarnation_id}`;
    if (recoveredForSessionRef.current === sessionKey) return;
    recoveredForSessionRef.current = sessionKey;

    let active = true;
    void (async () => {
      try {
        const pendingInfo = await readPendingOutboxApi();
        if (!active) return;
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
              errorMessage:
                "Reconnected with an unconfirmed request. Check its status before retrying.",
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
              if (!active) return;
              setOutbox({
                lifecycle: "applied",
                intent: pendingInfo.pending,
                receipt: rec,
                errorMessage: null,
                quarantineNotice: null,
              });
            } catch {
              if (!active) return;
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
            if (!active) return;
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

    return () => {
      active = false;
    };
  }, [meta, metaReady]);

  const dispatchCommand = useCallback(
    async (intent: LocalCommandIntent): Promise<CommandLifecycle> => {
      if (submittingRef.current) return outboxRef.current.lifecycle;
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
          return "denied";
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
              return "unknown";
            }
            setOutbox({
              lifecycle: "applied",
              intent,
              receipt,
              errorMessage: null,
              quarantineNotice: null,
            });
            await loadMeta();
            bumpRefresh();
            return "applied";
          }
          if (receipt.state === "denied") {
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
            bumpRefresh();
            return "denied";
          }
          setOutbox({
            lifecycle: "accepted",
            intent,
            receipt,
            errorMessage: null,
            quarantineNotice: null,
          });
          return "accepted";
        } catch (err) {
          if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
            await deletePendingOutboxApi().catch(() => undefined);
            if (err.status === 401) {
              clearData(err.message, "unauthorized");
              return "denied";
            }
            setOutbox({
              lifecycle: "denied",
              intent,
              receipt: null,
              errorMessage: err.message,
              quarantineNotice: null,
            });
            return "denied";
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
          return "unknown";
        }
      } finally {
        submittingRef.current = false;
      }
    },
    [bumpRefresh, clearData, loadMeta]
  );

  const lookupUnknownCommand = useCallback(async () => {
    const intent = outboxRef.current.intent;
    if (!intent) return;
    try {
      const receipt = await lookupCommandReceiptApi(
        intent.command_id,
        intent.command_type
      );
      if (receipt.status === "not_found" || (!receipt.state && receipt.status !== "found")) {
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
        bumpRefresh();
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
        bumpRefresh();
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
  }, [bumpRefresh, loadMeta]);

  const retryNotFoundCommand = useCallback(async () => {
    const intent = outboxRef.current.intent;
    if (!intent) return;
    await dispatchCommand(intent);
  }, [dispatchCommand]);

  const discardPendingCommand = useCallback(async () => {
    await deletePendingOutboxApi().catch(() => undefined);
    setOutbox(IDLE_OUTBOX_STATE);
  }, []);

  const dismissFinalOutbox = useCallback(() => {
    setOutbox((prev) =>
      prev.lifecycle === "applied" || prev.lifecycle === "denied" || prev.lifecycle === "idle"
        ? IDLE_OUTBOX_STATE
        : prev
    );
  }, []);

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

  const value = useMemo<OutboxContextValue>(
    () => ({
      outbox,
      controlsLocked,
      canWrite,
      dispatchCommand,
      lookupUnknownCommand,
      retryNotFoundCommand,
      discardPendingCommand,
      dismissFinalOutbox,
    }),
    [
      outbox,
      controlsLocked,
      canWrite,
      dispatchCommand,
      lookupUnknownCommand,
      retryNotFoundCommand,
      discardPendingCommand,
      dismissFinalOutbox,
    ]
  );

  return <OutboxContext.Provider value={value}>{children}</OutboxContext.Provider>;
}

export function useOutbox(): OutboxContextValue {
  const ctx = useContext(OutboxContext);
  if (!ctx) {
    throw new Error("useOutbox must be used inside OutboxProvider");
  }
  return ctx;
}
