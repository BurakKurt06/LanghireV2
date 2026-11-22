import { useEffect, useState } from "react";
import { Pause, Play, SkipForward, Square, CircleAlert, Hand } from "lucide-react";
import { pauseApplying, resumeApplying, stopApplying, skipJob, answerHelp, continueAfterLimit } from "../lib/api";
import { APPLY_MODE_LABELS, type ApplyMode } from "../lib/applyMode";
import { answeredOnly, minutesSeconds } from "../lib/questions";
import { refreshApplyStatus, useApplyStatus } from "../hooks/useApplyStatus";
import QuestionFields from "./QuestionFields";
import type { HelpRequest } from "../lib/types";

function elapsed(startedAt: string, now: number): string {
  return minutesSeconds((now - new Date(startedAt).getTime()) / 1000);
}

/** "3 h 12 min", "8 min 05 s": the time left until the ChatGPT limit resets. */
function untilReset(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d} d ${h} h`;           // the weekly limit
  if (h) return `${h} h ${m} min`;
  if (m >= 10) return `${m} min`;
  return `${m} min ${String(s % 60).padStart(2, "0")} s`;
}

/** The local clock time of a moment ("9:40 PM", or "Tue 9:40 PM" when it isn't today). */
function clockTime(ms: number): string {
  const at = new Date(ms);
  const time = at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return at.toDateString() === new Date().toDateString() ? time : `${at.toLocaleDateString([], { weekday: "short" })} ${time}`;
}

const HELP_TITLES: Record<NonNullable<HelpRequest["kind"]>, string> = {
  questions: "Your answer is needed",
  captcha: "Solve the CAPTCHA in the browser",
  verification: "Verification code needed",
  help: "Needs you",
};

/** What identifies one request: a new question (or a new wait) for the same job is a new card. */
function helpKey(help: HelpRequest): string {
  return [help.url, help.kind ?? "help", ...(help.questions ?? []).map((q) => q.question), help.problem].join("\u0000");
}

/**
 * One thing a running job is waiting for from the user. Its typing lives here, so text typed for
 * one job's question can never be sent with another job's (each request is its own card).
 */
function HelpCard({ help, jobTitle, now, onDone }: {
  help: HelpRequest;
  /** Shown when several jobs run at once, so the user knows which one asks. */
  jobTitle: string;
  now: number;
  onDone: () => Promise<void>;
}) {
  const [message, setMessage] = useState("");
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  // The backend says how long it waits; count down from that every second between polls.
  const [deadline, setDeadline] = useState<number | null>(() =>
    typeof help.seconds_left === "number" ? Date.now() + help.seconds_left * 1000 : null);
  useEffect(() => {
    setDeadline(typeof help.seconds_left === "number" ? Date.now() + help.seconds_left * 1000 : null);
  }, [help.seconds_left]);

  const kind = help.kind ?? "help";
  const questions = kind === "questions" ? help.questions ?? [] : [];
  const given = answeredOnly(answers);

  const reply = async (giveUp: boolean) => {
    setBusy(true);
    try {
      await answerHelp(help.url, giveUp ? "" : message.trim(), giveUp, giveUp ? {} : given);
      await onDone();
    } catch {
      /* the next poll shows the real state */
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-2 rounded-lg border border-amber-500/40 bg-amber-500/10 p-2">
      <p className="flex items-start gap-1 font-semibold text-amber-600">
        <CircleAlert className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" />
        <span>{HELP_TITLES[kind]}{jobTitle ? `: ${jobTitle}` : ""}</span>
      </p>
      {questions.length ? (
        <>
          <QuestionFields questions={questions} answers={answers} onChange={setAnswers} compact />
          {/* Also for what a field can't hold: "I ticked A and C myself, go on". */}
          <textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            rows={2}
            aria-label="Message to the agent"
            placeholder="Optional: tell the agent anything else, or that you filled it in yourself"
            className="input-base mt-2 !text-[12px]"
          />
        </>
      ) : (
        <>
          <p className="mt-1 whitespace-pre-wrap text-foreground">{help.problem}</p>
          {kind !== "captcha" && (
            <textarea
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              rows={2}
              aria-label={kind === "verification" ? "Verification code" : "Message to the agent"}
              placeholder={kind === "verification" ? "Type the code from the email" : "Optional: tell the agent what to do"}
              className="input-base mt-1.5 !text-[12px]"
            />
          )}
          {kind === "verification" && (
            <p className="mt-1 text-[11px] text-muted-foreground">
              No code to type? If you opened the email's link or typed the code in the browser yourself, just press Continue.
            </p>
          )}
        </>
      )}
      {deadline !== null && (
        <p className="mt-1.5 text-[11px] text-muted-foreground">
          The run moves on to the next job in {minutesSeconds(Math.min(help.seconds_left ?? Infinity, (deadline - now) / 1000))}. This job then stays in your list as
          "Needs you" and you can answer it there.
        </p>
      )}
      <div className="mt-1.5 flex gap-1.5">
        <button disabled={busy || (questions.length > 0 && Object.keys(given).length === 0 && !message.trim())}
          onClick={() => reply(false)}
          className="btn-primary flex-1 !px-2 !py-1 !text-[11px]">
          {questions.length ? "Answer and continue" : kind === "captcha" ? "I solved it" : "Continue"}
        </button>
        <button disabled={busy} onClick={() => reply(true)}
          title={'Leave this job for later (it stays in your list as "Needs you") and go on to the next one'}
          className="btn-secondary flex-1 !px-2 !py-1 !text-[11px]">Next job</button>
      </div>
    </div>
  );
}

/** Run controls in the sidebar (Discord-style), shown on every page while applications run. */
export default function RunPanel() {
  const status = useApplyStatus();
  const running = Boolean(status?.running);
  const [now, setNow] = useState(() => Date.now());
  const [busy, setBusy] = useState(false);

  // The elapsed-time and countdown clock, only while a run is shown.
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);

  // Only application runs show here; collection has its own progress and Stop on the Jobs page.
  // After a run that the AI plan's limit stopped, the reason stays here (with when the limit
  // resets) until the next run starts: the user who walked away sees why it ended.
  const notice = status?.notice?.text ? status.notice : null;
  if (!status) return null;
  if (!status.running) {
    if (notice?.kind !== "limit_stop") return null;
    return (
      <section aria-label="Why the last run stopped" className="mx-3 mb-3 rounded-xl border border-amber-500/40 bg-amber-500/10 p-3 text-[12px]">
        <p role="status" className="flex items-start gap-1.5 text-foreground">
          <CircleAlert className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-amber-600" />
          <span>{notice.text}. The jobs that weren't started are still ready.</span>
        </p>
      </section>
    );
  }

  const jobs = status.current ?? [];
  const titleOf = (url: string) => jobs.find((j) => j.url === url)?.title || "";
  const helpRequests = status.help ?? [];
  const paused = Boolean(status.paused);
  const userActive = jobs.some((j) => j.user_active);
  // The run waits for the ChatGPT limit (like a pause): it goes on by itself at the reset.
  const limitWait = notice?.kind === "limit_wait" ? notice : null;
  const resetsAt = limitWait?.resets_at ? limitWait.resets_at * 1000 : null;
  const mode = APPLY_MODE_LABELS[(status.run_mode || "auto") as ApplyMode] || APPLY_MODE_LABELS.auto;

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
  const refresh = async () => { await refreshApplyStatus(); };

  return (
    <section aria-label="Run controls" className="mx-3 mb-3 max-h-[70vh] overflow-y-auto rounded-xl border border-border bg-secondary/40 p-3 text-[12px]">
      <div className="flex items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1.5 font-semibold text-foreground">
          <span className={`h-2 w-2 rounded-full ${paused || userActive || limitWait ? "bg-amber-500" : "animate-pulse bg-success"}`} />
          {paused ? "Paused" : userActive ? "Waiting for you" : limitWait && !jobs.length ? "Waiting for the ChatGPT limit" : "Applying"}
        </span>
        <span className="rounded-full bg-background px-2 py-0.5 text-[10px] font-semibold text-muted-foreground">{mode}</span>
      </div>

      {limitWait ? (
        <div role="status" aria-label="Waiting for the ChatGPT limit"
          className="mt-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-foreground">
          <p className="flex items-start gap-1.5">
            <CircleAlert className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-amber-600" />
            <span>
              {limitWait.text}.{" "}
              {resetsAt !== null && resetsAt > now
                ? <>Goes on by itself in <strong>{untilReset(resetsAt - now)}</strong> (at {clockTime(resetsAt)}).</>
                : <>Goes on by itself as soon as it resets.</>}
              {jobs.length ? " Jobs already running finish first." : ""}
            </span>
          </p>
          <p className="mt-1 text-[11px] text-muted-foreground">
            If your limit was reset early (a free reset, a new plan), press Continue now. If it's still full, the run waits again.
          </p>
          <button disabled={busy} onClick={() => act(continueAfterLimit)}
            className="btn-primary mt-1.5 w-full !px-2 !py-1 !text-[11px]">
            <Play className="h-3.5 w-3.5" /> Continue now
          </button>
        </div>
      ) : notice && (
        <p role="status" className="mt-2 flex items-start gap-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-foreground">
          <CircleAlert className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-amber-600" />
          <span>{notice.text}{notice.kind === "limit_stop" ? ". Jobs already running finish; the rest stay ready." : ""}</span>
        </p>
      )}

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
                title="Skip this job now and go to the next one (it stays in your list)"
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
            {(job.user_active || job.note) && (
              <p role="status" className="mt-1.5 flex items-start gap-1 rounded-lg bg-background px-2 py-1.5 text-foreground">
                <Hand className="mt-0.5 h-3 w-3 flex-shrink-0 text-amber-600" />
                <span>{job.user_active ? "You stepped in, so the agent is waiting. It goes on when you've been idle for a moment, or press Resume." : job.note}</span>
              </p>
            )}
          </div>
        ))
      ) : (
        <p className="mt-2 text-muted-foreground">Starting…</p>
      )}

      {helpRequests.map((help) => (
        <HelpCard key={helpKey(help)} help={help} now={now} onDone={refresh}
          jobTitle={jobs.length > 1 ? titleOf(help.url) : ""} />
      ))}

      <div className="mt-2 flex justify-between gap-1">
        <button
          disabled={busy}
          onClick={() => act(paused || userActive ? resumeApplying : pauseApplying)}
          className="btn-secondary flex-1 !px-2 !py-1 !text-[11px]"
          title={paused || userActive ? "Resume: the agent re-reads the page and keeps your changes" : "Pause: the agent freezes where it is"}
        >
          {paused || userActive ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
          {paused || userActive ? "Resume" : "Pause"}
        </button>
        <button
          disabled={busy}
          onClick={() => act(stopApplying)}
          className="btn-secondary !px-2 !py-1 !text-[11px] hover:!text-destructive"
          title="Stop the whole run now"
          aria-label="Stop"
        >
          <Square className="h-3.5 w-3.5" />
        </button>
      </div>
    </section>
  );
}
