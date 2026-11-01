import { useId, useState } from "react";
import { useTranslation } from "react-i18next";
import TagInput from "./ui/TagInput";
import type { ExperienceLevel, JobSearchPrefs, JobType, PayPeriod, WorkMode } from "../lib/types";
import {
  EXPERIENCE_LABELS,
  JOB_TYPE_LABELS,
  PERIOD_LABELS,
  WORK_MODE_LABELS,
  noPayBar,
} from "../lib/jobSearch";

const POSTED = [0, 1, 3, 7, 14, 30] as const;
const COOLDOWNS = [0, 14, 30, 60, 90];

interface Props {
  value: JobSearchPrefs;
  onChange: (value: JobSearchPrefs) => void;
  /** The minimum pay's currency (Profile → Salary). */
  currency: string;
  /** Show the Quick / Detailed choice (the Collect page shows it on its own). */
  showMode?: boolean;
  disabled?: boolean;
}

/** Settings → Job search, and the same form for one search on the Collect page. */
export default function JobSearchForm({ value, onChange, currency, showMode = true, disabled = false }: Props) {
  const { t } = useTranslation("profile");
  const uid = useId();
  const set = <K extends keyof JobSearchPrefs>(key: K, v: JobSearchPrefs[K]) => onChange({ ...value, [key]: v });
  const toggle = <T extends string>(key: "job_types" | "work_modes" | "experience", item: T) => {
    const list = value[key] as string[];
    set(key, (list.includes(item) ? list.filter((x) => x !== item) : [...list, item]) as never);
  };
  const [drafts, setDrafts] = useState({ exclude_companies: "", exclude_title_words: "", dealbreakers: "" });
  type ListKey = keyof typeof drafts;
  const addTo = (key: ListKey) => {
    const text = drafts[key].trim();
    if (text && !value[key].some((x) => x.toLowerCase() === text.toLowerCase())) set(key, [...value[key], text]);
    setDrafts((d) => ({ ...d, [key]: "" }));
  };
  const bar = noPayBar(value);

  return (
    <fieldset disabled={disabled} className="flex flex-col gap-4">
      {showMode && <ModeChoice value={value.mode} onChange={(m) => set("mode", m)} />}

      <Chips label={t("jobSearch.jobTypes")} hint={t("jobSearch.anyHint")}
        options={JOB_TYPE_LABELS} selected={value.job_types} onToggle={(v) => toggle<JobType>("job_types", v as JobType)} />
      <Chips label={t("jobSearch.workModes")} hint={t("jobSearch.anyHint")}
        options={WORK_MODE_LABELS} selected={value.work_modes} onToggle={(v) => toggle<WorkMode>("work_modes", v as WorkMode)} />
      <Chips label={t("jobSearch.experience")} hint={t("jobSearch.anyHint")}
        options={EXPERIENCE_LABELS} selected={value.experience} onToggle={(v) => toggle<ExperienceLevel>("experience", v as ExperienceLevel)} />

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label htmlFor={`${uid}-posted`} className="block text-sm font-semibold text-foreground mb-1.5">{t("jobSearch.postedWithin")}</label>
          <select id={`${uid}-posted`} value={value.posted_within} className="input-base"
            onChange={(e) => set("posted_within", Number(e.target.value) as JobSearchPrefs["posted_within"])}>
            {POSTED.map((d) => <option key={d} value={d}>{t(`jobSearch.posted.${d}`)}</option>)}
          </select>
        </div>
        <div>
          <label htmlFor={`${uid}-cooldown`} className="block text-sm font-semibold text-foreground mb-1.5">{t("jobSearch.cooldown")}</label>
          <select id={`${uid}-cooldown`} value={value.company_cooldown_days} className="input-base"
            onChange={(e) => set("company_cooldown_days", Number(e.target.value))}>
            {COOLDOWNS.map((d) => (
              <option key={d} value={d}>{d ? t("jobSearch.cooldownDays", { days: d }) : t("jobSearch.cooldownOff")}</option>
            ))}
            {!COOLDOWNS.includes(value.company_cooldown_days) && (
              <option value={value.company_cooldown_days}>{t("jobSearch.cooldownDays", { days: value.company_cooldown_days })}</option>
            )}
          </select>
        </div>
      </div>

      <div>
        <label htmlFor={`${uid}-pay`} className="block text-sm font-semibold text-foreground mb-1.5">{t("jobSearch.minPay")}</label>
        <div className="flex gap-2 items-center">
          <span className="text-sm text-muted-foreground w-10">{value.min_pay.currency || currency}</span>
          <input id={`${uid}-pay`} type="number" min={0} step="any" value={value.min_pay.amount || ""} placeholder="0"
            onChange={(e) => set("min_pay", { ...value.min_pay, amount: Math.max(0, Number(e.target.value) || 0) })}
            className="input-base !w-36 !flex-initial" />
          <select aria-label={t("jobSearch.minPayPeriod")} value={value.min_pay.period} className="input-base !w-36 !flex-initial"
            onChange={(e) => set("min_pay", { ...value.min_pay, period: e.target.value as PayPeriod })}>
            {(Object.keys(PERIOD_LABELS) as PayPeriod[]).map((p) => <option key={p} value={p}>{PERIOD_LABELS[p]}</option>)}
          </select>
        </div>
        <p className="text-[12px] text-muted-foreground mt-1.5">{t("jobSearch.minPayHint")}</p>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label htmlFor={`${uid}-match`} className="block text-sm font-semibold text-foreground mb-1.5">{t("jobSearch.minMatch")}</label>
          <select id={`${uid}-match`} value={value.min_match} className="input-base"
            onChange={(e) => set("min_match", Number(e.target.value))}>
            <option value={0}>{t("jobSearch.matchOff")}</option>
            {Array.from({ length: 10 }, (_, i) => i + 1).map((n) => <option key={n} value={n}>{n} / 10</option>)}
            {value.min_match % 1 !== 0 && <option value={value.min_match}>{value.min_match} / 10</option>}
          </select>
          <p className="text-[12px] text-muted-foreground mt-1.5">
            {t("jobSearch.matchHint")}
            {bar !== value.min_match && <> {t("jobSearch.noPayBar", { bar })}</>}
          </p>
        </div>
        <div>
          <label htmlFor={`${uid}-hours`} className="block text-sm font-semibold text-foreground mb-1.5">{t("jobSearch.partTimeHours")}</label>
          <input id={`${uid}-hours`} type="number" min={4} max={39} value={value.part_time_hours} className="input-base"
            onChange={(e) => set("part_time_hours", Math.min(39, Math.max(4, Number(e.target.value) || 20)))} />
          <p className="text-[12px] text-muted-foreground mt-1.5">{t("jobSearch.partTimeHoursHint")}</p>
        </div>
      </div>

      <div>
        <label htmlFor={`${uid}-notes`} className="block text-sm font-semibold text-foreground mb-1.5">{t("jobSearch.notes")}</label>
        <p className="text-[12px] text-muted-foreground mb-1.5">{t("jobSearch.notesHint")}</p>
        <textarea id={`${uid}-notes`} value={value.notes} rows={3} maxLength={2000} className="input-base"
          placeholder={t("jobSearch.notesPlaceholder")} onChange={(e) => set("notes", e.target.value)} />
      </div>

      <div>
        <p className="text-sm font-semibold text-foreground mb-2">{t("jobSearch.leaveOut")}</p>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {(["exclude_companies", "exclude_title_words", "dealbreakers"] as ListKey[]).map((key) => (
            <div key={key}>
              <p className="text-xs font-medium text-muted-foreground mb-1">{t(`jobSearch.${key}`)}</p>
              <TagInput tags={value[key]} value={drafts[key]} variant="destructive"
                onChange={(text) => setDrafts((d) => ({ ...d, [key]: text }))}
                onAdd={() => addTo(key)} onRemove={(tag) => set(key, value[key].filter((x) => x !== tag))}
                placeholder={t(`jobSearch.${key}Placeholder`)} />
            </div>
          ))}
        </div>
        <label className="flex items-center gap-2.5 cursor-pointer mt-3">
          <input type="checkbox" checked={value.skip_staffing_agencies} className="w-4 h-4 rounded-md border-border accent-primary"
            onChange={(e) => set("skip_staffing_agencies", e.target.checked)} />
          <span className="text-sm text-foreground">{t("jobSearch.skipAgencies")}</span>
        </label>
      </div>
    </fieldset>
  );
}

export function ModeChoice({ value, onChange }: { value: JobSearchPrefs["mode"]; onChange: (m: JobSearchPrefs["mode"]) => void }) {
  const { t } = useTranslation("profile");
  return (
    <div role="radiogroup" aria-label={t("jobSearch.mode")}>
      <p className="text-sm font-semibold text-foreground mb-1.5">{t("jobSearch.mode")}</p>
      <div className="grid grid-cols-2 gap-2">
        {(["quick", "detailed"] as const).map((m) => (
          <button key={m} type="button" role="radio" aria-checked={value === m} onClick={() => onChange(m)}
            className={`text-left px-3 py-2.5 rounded-xl border transition-colors ${
              value === m ? "border-primary bg-primary/10" : "border-border bg-card hover:border-primary/50"}`}>
            <span className={`block text-sm font-semibold ${value === m ? "text-primary" : "text-foreground"}`}>
              {t(m === "quick" ? "jobSearch.modeQuick" : "jobSearch.modeDetailed")}
            </span>
            <span className="block text-[12px] text-muted-foreground mt-0.5">
              {t(m === "quick" ? "jobSearch.modeQuickHint" : "jobSearch.modeDetailedHint")}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

function Chips({ label, hint, options, selected, onToggle }: {
  label: string; hint: string; options: Record<string, string>; selected: string[]; onToggle: (value: string) => void;
}) {
  return (
    <div>
      <p className="text-sm font-semibold text-foreground mb-1.5">{label}</p>
      <div className="flex gap-2 flex-wrap" role="group" aria-label={label}>
        {Object.entries(options).map(([key, text]) => {
          const on = selected.includes(key);
          return (
            <button key={key} type="button" role="checkbox" aria-checked={on} onClick={() => onToggle(key)}
              className={`px-3 py-1.5 rounded-lg text-sm font-medium border transition-colors disabled:opacity-50 ${
                on ? "border-primary bg-primary/10 text-primary" : "border-border bg-card text-foreground hover:border-primary/50"}`}>
              {on && <span aria-hidden="true">✓ </span>}
              {text}
            </button>
          );
        })}
      </div>
      {selected.length === 0 && <p className="text-[12px] text-muted-foreground mt-1">{hint}</p>}
    </div>
  );
}
