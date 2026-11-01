import { Mail } from "lucide-react";
import type { InboxCategory } from "../lib/types";
import { INBOX_STYLES } from "../lib/inbox";

const LABELS: Record<InboxCategory, string> = {
  applied: "Email suggests application received",
  assessment: "Assessment",
  interview: "Interview",
  offer: "Offer",
  rejected: "Rejected",
};

/** What the latest email from the employer said (from the Inbox page's Gmail check). */
export default function InboxBadge({ status, subject }: { status?: string; subject?: string }) {
  if (!status || !(status in LABELS)) return null;
  const category = status as InboxCategory;
  return (
    <span
      title={subject ? `Email: ${subject}` : undefined}
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold ${INBOX_STYLES[category]}`}
    >
      <Mail className="h-3 w-3" /> {LABELS[category]}
    </span>
  );
}
