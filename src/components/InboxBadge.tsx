import { Mail } from "lucide-react";
import type { InboxCategory } from "../lib/types";

const LABELS: Record<InboxCategory, string> = {
  applied: "Application received",
  assessment: "Assessment",
  interview: "Interview",
  offer: "Offer",
  rejected: "Rejected",
};
const STYLES: Record<InboxCategory, string> = {
  applied: "bg-secondary text-foreground",
  assessment: "bg-blue-500/10 text-blue-600",
  interview: "bg-amber-500/10 text-amber-600",
  offer: "bg-green-500/10 text-green-600",
  rejected: "bg-destructive/10 text-destructive",
};

/** What the latest email from the employer said (from the Inbox page's Gmail check). */
export default function InboxBadge({ status, subject }: { status?: string; subject?: string }) {
  if (!status || !(status in LABELS)) return null;
  const category = status as InboxCategory;
  return (
    <span
      title={subject ? `Email: ${subject}` : undefined}
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold ${STYLES[category]}`}
    >
      <Mail className="h-3 w-3" /> {LABELS[category]}
    </span>
  );
}
