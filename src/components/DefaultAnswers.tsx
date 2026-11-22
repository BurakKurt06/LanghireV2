import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, RotateCcw } from "lucide-react";
import { getDefaultAnswers, setDefaultAnswer, apiErrorMessage, type DefaultAnswer } from "../lib/api";

/**
 * Default answers: what the agent answers when your profile, notes and saved answers say nothing
 * special (Q&A page). Edit one to change it for every later application; clear it, or press
 * Reset, to put the built-in answer back. Your own saved answers on this page always win.
 */
export default function DefaultAnswers() {
  const [rows, setRows] = useState<DefaultAnswer[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getDefaultAnswers()
      .then((res) => setRows(res.answers || []))
      .catch(() => setFailed(true));
  }, []);

  const save = async (id: string, answer: string) => {
    setSaving(id);
    setError(null);
    try {
      const res = await setDefaultAnswer(id, answer);
      setRows(res.answers);
      setDrafts((d) => {
        const next = { ...d };
        delete next[id];
        return next;
      });
    } catch (e) {
      setError(apiErrorMessage(e, "Couldn't save that default answer. Try again."));
    } finally {
      setSaving(null);
    }
  };

  if (failed) {
    return <p role="alert" className="error-banner mt-4">Couldn't load the default answers.</p>;
  }
  if (!rows) return null;
  const changed = rows.filter((r) => r.changed).length;

  return (
    <section aria-label="Default answers" className="card mt-5">
      <button onClick={() => setOpen(!open)} aria-expanded={open}
        className="w-full flex items-center justify-between text-left">
        <span>
          <span className="section-title inline-flex items-center gap-1.5">
            {open ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
            Default answers
          </span>
          <span className="block text-[12px] text-muted-foreground mt-0.5">
            What the agent answers when your profile and notes say nothing special, without asking you
            ({rows.length} answers{changed ? `, ${changed} changed by you` : ""}).
          </span>
        </span>
      </button>
      {open && (
        <div className="mt-4 space-y-3">
          <p className="text-[12px] text-muted-foreground">
            Your profile, your notes and your own answers below always come first. Change a default and it is used for
            every later application; clear it to put the built-in answer back.
          </p>
          {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
          {rows.map((row) => {
            const draft = drafts[row.id] ?? row.answer;
            const dirty = draft !== row.answer;
            const inputId = `default-answer-${row.id}`;
            return (
              <div key={row.id} className="rounded-xl border border-border p-3">
                <label htmlFor={inputId} className="block text-[13px] font-semibold text-foreground">{row.question}</label>
                <textarea id={inputId} rows={2} value={draft}
                  onChange={(e) => setDrafts((d) => ({ ...d, [row.id]: e.target.value }))}
                  className="input-base mt-1.5 text-sm" />
                <div className="mt-1.5 flex items-center gap-2 text-[11px] text-muted-foreground">
                  <span>{row.changed ? "Changed by you" : "Built-in answer"}</span>
                  {dirty && (
                    <button onClick={() => save(row.id, draft)} disabled={saving === row.id}
                      className="btn-primary !px-2.5 !py-1 !text-[11px]">
                      {saving === row.id ? "Saving…" : "Save"}
                    </button>
                  )}
                  {row.changed && !dirty && (
                    <button onClick={() => save(row.id, "")} disabled={saving === row.id}
                      className="btn-secondary !px-2.5 !py-1 !text-[11px]"
                      aria-label={`Reset "${row.question}" to the built-in answer`}>
                      <RotateCcw className="w-3 h-3" /> Reset
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
