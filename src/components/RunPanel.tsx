import { useEffect, useState } from "react";
import { Pause, Play, SkipForward, Square, CircleAlert, Check, X } from "lucide-react";
import {
  pauseApplying,
  resumeApplying,
  stopApplying,
  skipJob,
  answerHelp,
  reviewApplication,
  decideBrowserRequests,
  stopJobCollection,
} from "../lib/api";
import { APPLY_MODE_LABELS, type ApplyMode } from "../lib/applyMode";
import { refreshApplyStatus, useApplyStatus } from "../hooks/useApplyStatus";

function elapsed(startedAt: string, now: number): string {
  const secs = Math.max(0, Math.floor((now - new Date(startedAt).getTime()) / 1000));
  const m = Math.floor(secs / 60);
  return `${m}:${String(secs % 60).padStart(2, "0")}`;
}

/** Run controls in the sidebar (Discord-style), shown on every page while applications run. */
export default function RunPanel() {
  const status = useApplyStatus();
  const running = Boolean(status?.running);
  const [now, setNow] = useState(() => Date.now());
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  // The elapsed-time clock, only while a run is shown.
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);

  if (!status || (!status.running && !status.requests?.length)) return null;

  const jobs = status.current ?? [];
  const titleOf = (url: string) => jobs.find((j) => j.url === url)?.title || "";
  const help = status.help?.[0];
  const review = status.reviews?.[0];
  const requests = status.requests ?? [];
  const paused = Boolean(status.paused);
  const mode = APPLY_MODE_LABELS[(status.run_mode || requests[0]?.mode || "trial") as ApplyMode] || APPLY_MODE_LABELS.trial;

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      await refreshApplyStatus();
    } catch {
      /* the next poll shows the real state */
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-label="Run controls" className="mx-3 mb-3 max-h-[70vh] overflow-y-auto rounded-xl border border-border bg-secondary/40 p-3 text-[12px]">
      <div className="flex items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1.5 font-semibold text-foreground">
          <span className={`h-2 w-2 rounded-full ${paused ? "bg-amber-500" : "animate-pulse bg-success"}`} />
          {status.running ? (paused ? "Paused" : "Applying") : "Collecting"}
        </span>
        <span className="rounded-full bg-background px-2 py-0.5 text-[10px] font-semibold text-muted-foreground">{mode}</span>
      </div>

      {jobs.length ? (
        jobs.map((job) => (
          <div key={job.worker} className="mt-2">
            <div className="flex items-start gap-1">
              <div className="min-w-0 flex-1">
                <p className="truncate font-semibold text-foreground" title={job.title}>{job.title || "Untitled"}</p>
                <p className="truncate text-muted-foreground" title={job.company}>{job.company || "Unknown company"}</p>
              </div>
              <button
                disabled={busy}
                onClick={() => act(() => skipJob(job.url))}
                className="rounded p-1 text-muted-foreground hover:bg-background hover:text-foreground"
                title="Skip this job and go to the next one (it stays in your list)"
                aria-label={jobs.length > 1 ? `Skip ${job.title || "this job"}` : "Skip this job"}
              >
                <SkipForward className="h-3.5 w-3.5" />
              </button>
            </div>
            <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-background">
              <div
                className="h-full rounded-full bg-primary transition-all"
                style={{ width: `${Math.min(100, Math.round((job.step / Math.max(1, job.max_steps)) * 100))}%` }}
              />
            </div>
            <div className="mt-1 flex justify-between text-[11px] text-muted-foreground">
              <span>Step {job.step}</span>
              <span>{elapsed(job.started_at, now)}</span>
            </div>
          </div>
        ))
      ) : (
        <p className="mt-2 text-muted-foreground">Starting…</p>
      )}

      {help && (
        <div className="mt-2 rounded-lg border border-amber-500/40 bg-amber-500/10 p-2">
          <p className="flex items-start gap-1 font-semibold text-amber-600">
            <CircleAlert className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" /> Needs you{jobs.length > 1 && titleOf(help.url) ? `: ${titleOf(help.url)}` : ""}
          </p>
          <p className="mt-1 line-clamp-3 text-foreground" title={help.problem}>{help.problem}</p>
          <textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            rows={2}
            aria-label="Message to the agent"
            placeholder="Optional: tell the agent what to do"
            className="input-base mt-1.5 !text-[12px]"
          />
          <div className="mt-1.5 flex gap-1.5">
            <button disabled={busy} onClick={() => act(async () => { await answerHelp(help.url, message, false); setMessage(""); })}
              className="btn-primary flex-1 !px-2 !py-1 !text-[11px]">Continue</button>
            <button disabled={busy} onClick={() => act(() => answerHelp(help.url, "", true))}
              className="btn-secondary flex-1 !px-2 !py-1 !text-[11px]">Give up</button>
          </div>
        </div>
      )}

      {requests.length > 0 && (
        <div className="mt-2 rounded-lg border border-amber-500/40 bg-amber-500/10 p-2" role="region" aria-label="Account details approval">
          <p className="font-semibold">Account details need your approval</p>
          <p className="mt-1">The app didn't recognise this site as the employer's own application site. Allow your account details only if it is.</p>
          <div className="mt-2 max-h-72 space-y-3 overflow-y-auto">
            {requests.map((r) => (
              <div key={r.id} role="group" aria-label={`Review ${r.origin}`} className="rounded border border-border bg-background p-2">
                <p className="font-semibold break-all"><bdi>{r.company || r.title}</bdi></p>
                <p className="break-all"><code dir="ltr" style={{ unicodeBidi: "isolate" }}>{r.origin}</code></p>
                {r.tenant && <p>Tenant: {r.tenant}</p>}
                <p className="mt-1">{r.description}</p>
                <p className="mt-1 break-all text-[10px]"><code dir="ltr" style={{ unicodeBidi: "isolate" }}>{r.url}</code></p>
                <div className="mt-1.5 flex gap-1.5">
                  <button disabled={busy} onClick={() => act(() => decideBrowserRequests([r.id], true))}
                    className="btn-primary flex-1 !px-2 !py-1 !text-[11px]">Allow account details</button>
                  <button disabled={busy} onClick={() => act(() => decideBrowserRequests([r.id], false))}
                    className="btn-secondary !px-2 !py-1 !text-[11px]">Block</button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {!help && review && (
        <div className="mt-2 rounded-lg border border-primary/40 bg-primary/10 p-2">
          <p className="font-semibold text-foreground">Ready to submit{jobs.length > 1 && titleOf(review.url) ? `: ${titleOf(review.url)}` : ""}</p>
          <p className="mt-1">Check the form in the browser and change anything you like, then submit it or skip. Skip sends nothing.</p>
          <div className="mt-1.5 flex gap-1.5">
            <button disabled={busy} onClick={() => act(() => reviewApplication(review.url, true))}
              className="btn-primary flex-1 !px-2 !py-1 !text-[11px]"><Check className="h-3 w-3" /> Submit</button>
            <button disabled={busy} onClick={() => act(() => reviewApplication(review.url, false))}
              className="btn-secondary flex-1 !px-2 !py-1 !text-[11px]"><X className="h-3 w-3" /> Skip</button>
          </div>
        </div>
      )}

      <div className="mt-2 flex justify-between gap-1">
        {status.running && <button
          disabled={busy}
          onClick={() => act(paused ? resumeApplying : pauseApplying)}
          className="btn-secondary flex-1 !px-2 !py-1 !text-[11px]"
          title={paused ? "Resume: the agent re-reads the page and keeps your changes" : "Pause after the current action, so you can fix something"}
        >
          {paused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
          {paused ? "Resume" : "Pause"}
        </button>}
        <button
          disabled={busy}
          onClick={() => act(status.running ? stopApplying : stopJobCollection)}
          className="btn-secondary !px-2 !py-1 !text-[11px] hover:!text-destructive"
          title="Stop the whole run"
          aria-label="Stop"
        >
          <Square className="h-3.5 w-3.5" />
        </button>
      </div>
    </section>
  );
}
