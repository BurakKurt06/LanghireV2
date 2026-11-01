import type { InboxCategory } from "./types";

/** The kinds of job email the Inbox sorts into, in display order. */
export const INBOX_CATEGORIES: InboxCategory[] = ["applied", "assessment", "interview", "offer", "rejected"];

export const INBOX_STYLES: Record<InboxCategory, string> = {
  applied: "bg-secondary text-foreground",
  assessment: "bg-blue-500/10 text-blue-600",
  interview: "bg-amber-500/10 text-amber-600",
  offer: "bg-green-500/10 text-green-600",
  rejected: "bg-destructive/10 text-destructive",
};

/** Automatic Gmail checks (Settings → Accounts & email; the Inbox shows the current one). */
export const INBOX_SCHEDULE_LABELS: Record<"off" | "daily" | "weekly", string> = {
  off: "Off",
  daily: "Daily",
  weekly: "Weekly",
};
