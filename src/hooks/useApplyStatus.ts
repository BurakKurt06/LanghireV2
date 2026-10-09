import { useEffect, useState } from "react";
import { getApplyStatus } from "../lib/api";
import type { ApplyStatus } from "../lib/types";

const POLL_MS = 2000;

// One poll of /apply/status shared by every component on screen that shows it
// (the sidebar run panel, the "needs you" pill, the Jobs counts).
const listeners = new Set<(status: ApplyStatus) => void>();
let timer: ReturnType<typeof setInterval> | null = null;
let last: ApplyStatus | null = null;

/** Fetch the status now and hand it to every component using useApplyStatus. */
export async function refreshApplyStatus(): Promise<void> {
  try {
    const status = await getApplyStatus();
    if (listeners.size === 0) return; // everyone left while it was in flight: keep no stale copy
    last = status;
    listeners.forEach((listener) => listener(status));
  } catch {
    /* the next poll tries again */
  }
}

/** The apply status (null until the first answer), polled while any component uses it. */
export function useApplyStatus(): ApplyStatus | null {
  const [status, setStatus] = useState<ApplyStatus | null>(last);
  useEffect(() => {
    listeners.add(setStatus);
    if (listeners.size === 1) {
      void refreshApplyStatus();
      timer = setInterval(() => void refreshApplyStatus(), POLL_MS);
    }
    return () => {
      listeners.delete(setStatus);
      if (listeners.size === 0) {
        if (timer) clearInterval(timer);
        timer = null;
        last = null;
      }
    };
  }, []);
  return status;
}
