import type { ReactNode } from "react";
import { Button, Result, Skeleton } from "@openboa/yogurt";

export interface ProjectionBoundaryProps<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  loadingLabel?: string;
  skeletonLines?: number;
  errorTitle?: string;
  onRetry?: () => void;
  children: (data: T) => ReactNode;
}

export function ProjectionBoundary<T>({
  data,
  loading,
  error,
  loadingLabel = "Loading projection",
  skeletonLines = 5,
  errorTitle = "Unable to load view",
  onRetry,
  children,
}: ProjectionBoundaryProps<T>) {
  if (loading && !data) {
    return <Skeleton label={loadingLabel} lines={skeletonLines} />;
  }
  if (error && !data) {
    return (
      <Result
        title={errorTitle}
        action={
          onRetry ? (
            <Button emphasis="soft" shape="pill" onClick={onRetry}>
              Retry
            </Button>
          ) : undefined
        }
      >
        {error}
      </Result>
    );
  }
  if (!data) {
    return <Skeleton label={loadingLabel} lines={skeletonLines} />;
  }
  return <>{children(data)}</>;
}
