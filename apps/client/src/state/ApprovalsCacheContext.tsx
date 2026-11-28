import { createContext, useContext, useMemo, type ReactNode } from "react";
import type { ApprovalQuestionItem, ApprovalsProjection } from "../api/types";
import { useProjection } from "../api/useProjection";
import { useControlPlane } from "./ControlPlaneContext";

export interface ApprovalsCacheContextValue {
  projection: ApprovalsProjection | null;
  pendingApprovals: ApprovalQuestionItem[];
  pendingCount: number;
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

const ApprovalsCacheContext = createContext<ApprovalsCacheContextValue | null>(null);

export function ApprovalsCacheProvider({ children }: { children: ReactNode }) {
  const { meta, linkState } = useControlPlane();
  const enabled = Boolean(meta) && !meta?.board_access.bootstrap_locked && linkState !== "unauthorized";
  const { data, loading, error, refetch } = useProjection<ApprovalsProjection>(
    enabled ? "approvals" : null,
    { enabled }
  );

  const pendingApprovals = useMemo(
    () => (data?.pending ?? []).filter((q) => q.state === "open"),
    [data]
  );

  const value = useMemo<ApprovalsCacheContextValue>(
    () => ({
      projection: data,
      pendingApprovals,
      pendingCount: pendingApprovals.length,
      loading,
      error,
      refetch,
    }),
    [data, pendingApprovals, loading, error, refetch]
  );

  return (
    <ApprovalsCacheContext.Provider value={value}>
      {children}
    </ApprovalsCacheContext.Provider>
  );
}

export function useApprovalsCache(): ApprovalsCacheContextValue {
  const ctx = useContext(ApprovalsCacheContext);
  if (!ctx) {
    throw new Error("useApprovalsCache must be used inside ApprovalsCacheProvider");
  }
  return ctx;
}
