import { useId, useState } from "react";
import type { PendingQuestion } from "../lib/types";

interface QuestionFieldsProps {
  questions: PendingQuestion[];
  answers: Record<string, string>;
  onChange: (answers: Record<string, string>) => void;
  /** Smaller text, for the sidebar run panel. */
  compact?: boolean;
}

/** The choice that lets the user type an answer the list doesn't have (or that is past its end). */
const OTHER = "\u0000other";

/**
 * Questions the app has to ask, each shown whole: the form section it is in, its full text,
 * and its choices when it is a dropdown, radio or checkbox question (never a bare field label).
 * A "tick all that apply" question gets tick boxes (the answer is the ticked choices, in order);
 * any list of choices also takes a typed answer, for a choice the list leaves out.
 */
export default function QuestionFields({ questions, answers, onChange, compact }: QuestionFieldsProps) {
  // Ids unique to this list: the run panel and several job cards can show questions at once.
  const uid = useId();
  const [ticked, setTicked] = useState<Record<string, string[]>>({});
  const [typing, setTyping] = useState<Record<string, boolean>>({});
  const set = (question: string, value: string) => onChange({ ...answers, [question]: value });
  const text = compact ? "text-[12px]" : "text-sm";
  const field = `input-base mt-1 ${compact ? "!py-1.5 !text-[12px]" : ""}`;

  return (
    <div className={`mt-1.5 space-y-2.5 ${text}`}>
      {questions.map((q, index) => {
        const id = `${uid}-question-${index}`;
        const options = (q.options ?? []).filter((o) => o.trim() !== "");
        const section = q.section ? (
          <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{q.section}</p>
        ) : null;

        if (options.length > 0 && q.field_type === "checkbox") {
          const chosen = ticked[q.question] ?? [];
          const toggle = (option: string) => {
            const next = chosen.includes(option) ? chosen.filter((o) => o !== option) : [...chosen, option];
            const ordered = options.filter((o) => next.includes(o));
            setTicked({ ...ticked, [q.question]: ordered });
            set(q.question, ordered.join(", "));
          };
          return (
            <fieldset key={q.question}>
              {section}
              <legend className="block font-semibold text-foreground whitespace-pre-wrap">{q.question}</legend>
              <div className="mt-1 space-y-1">
                {options.map((option, i) => (
                  <label key={option} htmlFor={`${id}-${i}`} className="flex items-start gap-2">
                    <input id={`${id}-${i}`} type="checkbox" checked={chosen.includes(option)} onChange={() => toggle(option)}
                      className="mt-0.5 h-3.5 w-3.5 rounded border-border" />
                    <span>{option}</span>
                  </label>
                ))}
              </div>
            </fieldset>
          );
        }

        if (options.length > 0) {
          const own = typing[q.question] ?? false;
          return (
            <div key={q.question}>
              {section}
              <label htmlFor={id} className="block font-semibold text-foreground whitespace-pre-wrap">{q.question}</label>
              <select id={id} value={own ? OTHER : answers[q.question] ?? ""}
                onChange={(e) => {
                  const other = e.target.value === OTHER;
                  setTyping({ ...typing, [q.question]: other });
                  set(q.question, other ? "" : e.target.value);
                }}
                className={field}>
                <option value="">Choose…</option>
                {options.map((o) => <option key={o} value={o}>{o}</option>)}
                <option value={OTHER}>Something else (type it)</option>
              </select>
              {own && (
                <input aria-label={`Your answer: ${q.question}`} value={answers[q.question] ?? ""}
                  onChange={(e) => set(q.question, e.target.value)} placeholder="Type the answer as the form should get it"
                  className={field} />
              )}
            </div>
          );
        }

        return (
          <div key={q.question}>
            {section}
            <label htmlFor={id} className="block font-semibold text-foreground whitespace-pre-wrap">{q.question}</label>
            {q.field_type === "date" ? (
              <input id={id} value={answers[q.question] ?? ""} onChange={(e) => set(q.question, e.target.value)}
                placeholder="e.g. June 2027" className={field} />
            ) : (
              <textarea id={id} rows={compact ? 2 : 3} value={answers[q.question] ?? ""}
                onChange={(e) => set(q.question, e.target.value)} placeholder="Your answer" className={field} />
            )}
          </div>
        );
      })}
    </div>
  );
}
