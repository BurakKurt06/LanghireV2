import { useCallback, useEffect, useState } from "react";
import { Mail, RefreshCw, ExternalLink, FileText, Loader2, ChevronDown, ChevronUp } from "lucide-react";
import { getInbox, checkInbox, makePrepSheet } from "../lib/api";
import { PageHeader, Section } from "../components/ui";
import type { InboxCategory, InboxEmail, InboxStatus } from "../lib/types";
import { INBOX_CATEGORIES, INBOX_SCHEDULE_LABELS, INBOX_STYLES } from "../lib/inbox";
import { settingsHref } from "../lib/settingsSections";

// Quick while a check runs; slow otherwise (a scheduled check can start any time).
const POLL_MS = 3000;
const IDLE_POLL_MS = 30000;

const CATEGORY_LABELS: Record<InboxCategory, string> = {
  applied: "Applied",
  assessment: "Assessment",
  interview: "Interview",
  offer: "Offer",
  rejected: "Rejected",
};
const FILTERS: Array<InboxCategory | ""> = ["", ...INBOX_CATEGORIES];

function shortDate(value: string): string {
  const d = new Date(value);
  return isNaN(d.getTime()) ? value : d.toLocaleDateString();
}

export default function Inbox() {
  const [inbox, setInbox] = useState<InboxStatus | null>(null);
  const [filter, setFilter] = useState<InboxCategory | "">("");
  const [error, setError] = useState<string | null>(null);
  const [preparing, setPreparing] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const refresh = useCallback(() => {
    getInbox()
      .then((data) => setInbox(data))
      .catch(() => {});
  }, []);

  const busy = Boolean(inbox?.checking || inbox?.requested);
  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, busy ? POLL_MS : IDLE_POLL_MS);
    return () => clearInterval(timer);
  }, [refresh, busy]);

  const onCheck = async () => {
    setError(null);
    try {
      await checkInbox();
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const onPrep = async (email: InboxEmail) => {
    setError(null);
    setPreparing(email.id);
    try {
      await makePrepSheet(email.id);
      setOpen(email.id);
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPreparing(null);
    }
  };

  if (!inbox) return null;

  const emails = inbox.emails.filter((e) => !filter || e.category === filter);
  const count = (c: InboxCategory | "") => inbox.emails.filter((e) => !c || e.category === c).length;
  const working = inbox.checking || inbox.requested;

  return (
    <div className="max-w-5xl">
      <PageHeader title="Inbox" subtitle="Replies to your applications, read from Gmail" />

      <Section title="Check Gmail">
        <p className="text-[13px] text-muted-foreground mb-3">
          Reads job emails from Gmail through the sign-in in Settings → Accounts &amp; email (a browser window opens briefly).
          Nothing is sent from your Gmail, deleted or marked as read. The emails' text goes to your AI model so it can
          sort them; it never runs during an application run. After a year, only an email's sender, subject and
          category stay here (the email itself stays in Gmail).
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <button onClick={onCheck} disabled={working} className="btn-primary">
            {working ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            {working ? "Checking…" : "Check now"}
          </button>
          <span className="text-sm">
            <span className="text-muted-foreground">Check automatically: </span>
            {INBOX_SCHEDULE_LABELS[inbox.schedule] ?? inbox.schedule}{" "}
            <a href={settingsHref("accounts")} className="text-[13px] text-primary font-medium hover:underline">
              Change in Settings
            </a>
          </span>
          <span className="text-[13px] text-muted-foreground">
            {inbox.last_checked ? `Last checked ${new Date(inbox.last_checked).toLocaleString()}` : "Not checked yet"}
          </span>
        </div>
        {(inbox.status || inbox.error || error) && (
          <p role="status" className={`mt-3 text-[13px] ${inbox.error || error ? "text-destructive" : "text-muted-foreground"}`}>
            {error || inbox.error || inbox.status}
          </p>
        )}
      </Section>

      <div className="flex flex-wrap gap-2 mb-4">
        {FILTERS.map((c) => (
          <button
            key={c || "all"}
            onClick={() => setFilter(c)}
            className={`filter-tab ${filter === c ? "filter-tab-active" : "filter-tab-inactive"}`}
          >
            {c ? CATEGORY_LABELS[c] : "All"} ({count(c)})
          </button>
        ))}
      </div>

      {emails.length === 0 ? (
        <div className="card text-center py-10 text-muted-foreground">
          <Mail className="w-6 h-6 mx-auto mb-2" />
          {inbox.emails.length === 0 ? "No job emails yet. Press Check now." : "Nothing in this list."}
        </div>
      ) : (
        <div className="divide-y divide-border rounded-xl border border-border overflow-hidden">
          {emails.map((email) => {
            const canPrep = (email.category === "interview" || email.category === "offer") && !inbox.performance_mode;
            return (
              <div key={email.id} className="p-4 bg-card">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className={`rounded-full px-2.5 py-0.5 text-[11px] font-semibold ${INBOX_STYLES[email.category]}`}>
                        {CATEGORY_LABELS[email.category]}
                      </span>
                      <span className="font-semibold text-foreground">{email.company || email.sender || "Unknown"}</span>
                      {email.role && <span className="text-muted-foreground">· {email.role}</span>}
                    </div>
                    <p className="mt-1 text-[13px] text-foreground truncate" title={email.subject}>{email.subject}</p>
                    <p className="text-[12px] text-muted-foreground">
                      {shortDate(email.date)}
                      {email.job_url ? " · matched to a job in your list" : ""}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {canPrep && !email.prep && (
                      <button onClick={() => void onPrep(email)} disabled={preparing !== null} className="btn-secondary text-xs">
                        {preparing === email.id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <FileText className="w-3.5 h-3.5" />}
                        {preparing === email.id ? "Writing…" : "Make prep sheet"}
                      </button>
                    )}
                    {email.prep && (
                      <button onClick={() => setOpen(open === email.id ? null : email.id)} className="btn-secondary text-xs">
                        <FileText className="w-3.5 h-3.5" /> Prep sheet
                        {open === email.id ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
                      </button>
                    )}
                    <a href={email.gmail_url} target="_blank" rel="noopener noreferrer"
                      className="p-1.5 text-muted-foreground hover:text-primary" title="Open in Gmail" aria-label="Open in Gmail">
                      <ExternalLink className="w-4 h-4" />
                    </a>
                  </div>
                </div>
                {email.prep && open === email.id && (
                  <pre className="mt-3 whitespace-pre-wrap rounded-lg bg-secondary/50 p-3 text-[13px] font-sans text-foreground select-text">
                    {email.prep}
                  </pre>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
