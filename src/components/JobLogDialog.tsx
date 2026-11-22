import { useEffect, useState } from "react";
import { Loader2, ScrollText, X } from "lucide-react";
import { getJobLogs } from "../lib/api";
import { useDialog } from "../hooks/useDialog";
import type { RunLog } from "../lib/types";
import LogLine from "./LogLine";

/** One job's own log: what the app did on it, attempt after attempt (the job card's "View log"). */
export default function JobLogDialog({ url, title, onClose }: { url: string; title: string; onClose: () => void }) {
  const [lines, setLines] = useState<RunLog[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    getJobLogs(url)
      .then((data) => { if (active) setLines(data || []); })
      .catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [url]);

  // Focus moves into the dialog, Tab stays in it, Esc closes it, and focus goes back afterwards.
  const panel = useDialog<HTMLDivElement>(true, onClose);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6" onClick={onClose}>
      <div ref={panel} tabIndex={-1} role="dialog" aria-modal="true" aria-label={`Log for ${title}`}
        className="bg-card rounded-2xl shadow-xl w-full max-w-4xl max-h-[85vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between gap-3 px-5 py-4 border-b border-border">
          <h3 className="text-sm font-bold text-foreground flex items-center gap-2 min-w-0">
            <ScrollText className="w-4 h-4 flex-shrink-0" />
            <span className="truncate">Log: {title}</span>
          </h3>
          <button onClick={onClose} aria-label="Close" className="text-muted-foreground hover:text-foreground">
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="log-viewer !max-h-none flex-1 overflow-y-auto">
          {failed ? (
            <p role="alert" className="text-destructive text-center mt-10">Couldn't load this job's log. Try again in a moment.</p>
          ) : lines === null ? (
            <div className="flex items-center justify-center h-32"><Loader2 className="w-6 h-6 animate-spin text-muted-foreground" /></div>
          ) : lines.length === 0 ? (
            <p className="text-muted-foreground text-center mt-10">
              No log for this job yet. A log appears here once the app has worked on it (logs are kept for 30 days).
            </p>
          ) : (
            lines.map((log) => <LogLine key={log.id} line={log.message} />)
          )}
        </div>
      </div>
    </div>
  );
}
