import { useState } from "react";
import { CircleAlert, CircleHelp } from "lucide-react";
import { answerQuestionEverywhere } from "../lib/api";
import { answeredOnly } from "../lib/questions";
import { useDialog } from "../hooks/useDialog";
import type { StartCheck } from "../lib/types";
import QuestionFields from "./QuestionFields";

/**
 * What Start says before the first job. The user presses Start and walks away, so whatever would
 * stop the run is said then, once: what is missing (the AI sign-in or its limit, Gmail, the
 * CAPTCHA solver's balance, the resume, the job-site password), and the questions these jobs'
 * sites are known to ask that nothing answers yet. An answer given here is saved as the user's
 * own and is never asked again.
 */
export default function StartCheckDialog({ check, onDecide }: { check: StartCheck; onDecide: (start: boolean) => void }) {
  const panel = useDialog<HTMLDivElement>(true, () => onDecide(false));
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const problems = check.problems ?? [];
  const questions = check.questions ?? [];
  const blocked = problems.some((p) => p.required);
  const given = answeredOnly(answers);

  const start = async (withAnswers: boolean) => {
    if (withAnswers) {
      setSaving(true);
      setError(null);
      try {
        for (const [question, answer] of Object.entries(given)) {
          await answerQuestionEverywhere(question, answer);
        }
      } catch {
        setError("Couldn't save your answers. Try again, or start without answering.");
        setSaving(false);
        return;
      }
    }
    onDecide(true);
  };

  const title = blocked ? "The run can't start yet" : problems.length ? "Before the run starts: something is missing" : "Before you walk away";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6" onClick={() => onDecide(false)}>
      <div ref={panel} tabIndex={-1} role="dialog" aria-modal="true" aria-label={title}
        className="bg-card rounded-2xl shadow-xl w-full max-w-2xl max-h-[85vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="px-5 py-4 border-b border-border">
          <h3 className="text-sm font-bold text-foreground">{title}</h3>
        </div>
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {problems.length > 0 && (
            <section aria-label="What is missing" className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-3">
              <p className="flex items-center gap-1.5 text-[13px] font-semibold text-amber-700 dark:text-amber-300">
                <CircleAlert className="h-3.5 w-3.5" />
                {blocked ? "Nothing was started. Fix this first:" : "Nothing was started. These jobs would end up waiting for you:"}
              </p>
              <ul className="mt-1.5 list-disc space-y-1 pl-5 text-sm text-foreground">
                {problems.map((p) => <li key={p.id}>{p.message}</li>)}
              </ul>
            </section>
          )}
          {!blocked && questions.length > 0 && (
            <section aria-label="Questions these jobs will ask">
              <p className="flex items-center gap-1.5 text-[13px] font-semibold text-foreground">
                <CircleHelp className="h-3.5 w-3.5" />
                {questions.length === 1
                  ? "One question these jobs' sites will ask that nothing answers yet"
                  : `${questions.length} questions these jobs' sites will ask that nothing answers yet`}
              </p>
              <p className="mt-0.5 text-[12px] text-muted-foreground">
                Answer now and the run won't stop for them. Each answer is saved on the Q&amp;A page and is never asked again.
              </p>
              {questions.map((q) => (
                <div key={q.question} className="mt-3 rounded-xl border border-border p-3">
                  <QuestionFields questions={[q]} answers={answers} onChange={setAnswers} />
                  <p className="mt-1 text-[11px] text-muted-foreground">
                    {q.jobs === 1 ? "1 of these jobs" : `${q.jobs} of these jobs`}: {q.why}.
                  </p>
                </div>
              ))}
            </section>
          )}
          {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        </div>
        <div className="flex flex-wrap justify-end gap-2 border-t border-border px-5 py-3">
          {blocked ? (
            <button onClick={() => onDecide(false)} className="btn-primary text-sm">Close</button>
          ) : (
            <>
              <button onClick={() => onDecide(false)} disabled={saving} className="btn-secondary text-sm">Cancel</button>
              {questions.length > 0 ? (
                <>
                  <button onClick={() => start(false)} disabled={saving} className="btn-secondary text-sm">Start without answering</button>
                  <button onClick={() => start(true)} disabled={saving || Object.keys(given).length === 0} className="btn-primary text-sm">
                    {saving ? "Saving…" : "Save answers and start"}
                  </button>
                </>
              ) : (
                <button onClick={() => start(false)} className="btn-primary text-sm">Start anyway</button>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
