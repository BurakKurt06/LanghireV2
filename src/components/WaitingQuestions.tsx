import { useEffect, useState } from "react";
import { Hand } from "lucide-react";
import { answerQuestionEverywhere, getWaitingQuestions } from "../lib/api";
import { answeredOnly } from "../lib/questions";
import type { WaitingQuestion } from "../lib/types";
import QuestionFields from "./QuestionFields";

/**
 * The questions jobs are waiting on, each shown once with how many jobs wait on it. One answer
 * goes to all of them: it is saved as the user's own (never asked again, on any site), and every
 * job that waited only on it is ready to apply again.
 */
export default function WaitingQuestions({ refreshKey, onAnswered }: {
  /** Changes when the job list changed: the questions are read again. */
  refreshKey?: unknown;
  onAnswered?: (message: string) => void;
}) {
  const [questions, setQuestions] = useState<WaitingQuestion[]>([]);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(0);

  useEffect(() => {
    let active = true;
    getWaitingQuestions()
      .then((data) => { if (active) setQuestions(data?.questions ?? []); })
      .catch(() => { /* the job cards still show each job's own questions */ });
    return () => { active = false; };
  }, [refreshKey, loaded]);

  // One question on one job is answered on that job's card; this is for what several jobs share.
  const shared = questions.filter((q) => q.jobs.length > 1);
  if (shared.length === 0) return null;

  const answer = async (q: WaitingQuestion) => {
    const given = answeredOnly({ [q.question]: answers[q.question] ?? "" })[q.question];
    if (!given) return;
    setSaving(q.question);
    setError(null);
    try {
      const res = await answerQuestionEverywhere(q.question, given);
      const ready = res.ready ?? 0;
      const message = res.continued
        ? "Thanks, the run is continuing with your answer."
        : ready > 0
        ? `Saved. ${ready === 1 ? "1 job is" : `${ready} jobs are`} ready to apply again${res.still_waiting ? `; ${res.still_waiting} still wait on another question` : ""}.`
        : "Saved for this and later applications.";
      setAnswers((previous) => {
        const next = { ...previous };
        delete next[q.question];
        return next;
      });
      setLoaded((n) => n + 1);
      onAnswered?.(message);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't save the answer. Try again.");
    } finally {
      setSaving(null);
    }
  };

  return (
    <section aria-label="Questions your jobs are waiting on"
      className="mb-3 rounded-xl border border-amber-500/40 bg-amber-500/10 p-3">
      <p className="flex items-center gap-1.5 text-[13px] font-semibold text-amber-700 dark:text-amber-300">
        <Hand className="h-3.5 w-3.5" />
        {shared.length === 1 ? "One question several jobs are waiting on" : `${shared.length} questions several jobs are waiting on`}
      </p>
      <p className="mt-0.5 text-[11px] text-muted-foreground">
        Answer once: it goes to every job that asks it, is saved on the Q&amp;A page and is never asked again.
      </p>
      {shared.map((q) => (
        <div key={q.question} className="mt-2.5 rounded-lg bg-background/60 p-2.5">
          <QuestionFields questions={[q]} answers={answers} onChange={setAnswers} />
          <div className="mt-1.5 flex items-center justify-between gap-3">
            <span className="text-[11px] text-muted-foreground" title={q.jobs.map((j) => `${j.title} at ${j.company}`).join("\n")}>
              {q.jobs.length} jobs wait on this
            </span>
            <button onClick={() => answer(q)} disabled={saving !== null || !(answers[q.question] ?? "").trim()}
              className="btn-primary !px-3 !py-1 text-[12px]">
              {saving === q.question ? "Saving…" : "Answer for all"}
            </button>
          </div>
        </div>
      ))}
      {error && <p role="alert" className="mt-1.5 text-sm text-destructive">{error}</p>}
    </section>
  );
}
