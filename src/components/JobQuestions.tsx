import { useState } from "react";
import { Hand } from "lucide-react";
import { answerJobQuestions } from "../lib/api";
import type { Job } from "../lib/types";
import QuestionFields from "./QuestionFields";
import { answeredOnly } from "../lib/questions";

/**
 * The questions a job is waiting on, answerable right on its card.
 *
 * The answers are saved as the user's own (Q&A), so every later application uses them too.
 * When the run is still waiting on this job it continues at once; otherwise the job goes back to
 * "Ready to apply" and the next run tries it again. Nothing starts by itself.
 */
export default function JobQuestions({ job, onAnswered }: {
  job: Job;
  /** Called with what happened, once the answers are saved (the card may then leave "Needs you"). */
  onAnswered?: (message: string) => void;
}) {
  const questions = job.pending_questions ?? [];
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (!questions.length) return null;
  const given = answeredOnly(answers);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const res = await answerJobQuestions(job.url, given);
      const left = res.remaining ?? 0;
      const message = res.continued
        ? "Thanks, the run is continuing with your answer."
        : res.ready
        ? "Saved. The job is ready to apply again; start a run when you like."
        : res.may_have_been_sent
        ? "Saved. This application may already have been sent, so it isn't tried again by itself: check the site, then use Retry if it wasn't."
        : left > 0
        ? `Saved. ${left === 1 ? "One more question" : `${left} more questions`} to answer before it can be tried again.`
        : "Saved for this and later applications.";
      setResult(message);
      setAnswers({});
      onAnswered?.(message);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't save the answer. Try again.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section aria-label={`Questions for ${job.title || "this job"}`}
      className="mt-2 rounded-xl border border-amber-500/40 bg-amber-500/10 p-3">
      <p className="flex items-center gap-1.5 text-[13px] font-semibold text-amber-700 dark:text-amber-300">
        <Hand className="h-3.5 w-3.5" />
        {questions.length === 1 ? "This application needs one answer from you" : `This application needs ${questions.length} answers from you`}
      </p>
      <QuestionFields questions={questions} answers={answers} onChange={setAnswers} />
      <p className="mt-2 text-[11px] text-muted-foreground">
        Your answers are saved on the Q&amp;A page and used for later applications too.
      </p>
      {error && <p role="alert" className="mt-1 text-sm text-destructive">{error}</p>}
      {result && <p role="status" className="mt-1 text-sm text-foreground">{result}</p>}
      <button onClick={save} disabled={saving || Object.keys(given).length === 0} className="btn-primary mt-2 text-sm">
        {saving ? "Saving…" : "Save answers"}
      </button>
    </section>
  );
}
