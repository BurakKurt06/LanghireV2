/** Profile → Job search: defaults, labels and pay display (backend: core/job_search.py, core/pay.py). */
import type { ExperienceLevel, Job, JobPay, JobSearchPrefs, JobType, PayPeriod, WorkMode } from "./types";

export const DEFAULT_JOB_SEARCH: JobSearchPrefs = {
  mode: "quick",
  job_types: [],
  work_modes: [],
  experience: [],
  posted_within: 7,
  min_pay: { amount: 0, period: "year" },
  part_time_hours: 20,
  min_match: 5,
  notes: "",
  exclude_companies: [],
  exclude_title_words: [],
  dealbreakers: [],
  skip_staffing_agencies: false,
  company_cooldown_days: 0,
};

export const JOB_TYPE_LABELS: Record<JobType, string> = {
  full_time: "Full-time",
  part_time: "Part-time",
  contract: "Contract",
  temporary: "Temporary",
  internship: "Internship",
};

export const WORK_MODE_LABELS: Record<WorkMode, string> = {
  remote: "Remote",
  hybrid: "Hybrid",
  onsite: "On-site",
};

export const EXPERIENCE_LABELS: Record<ExperienceLevel, string> = {
  internship: "Internship",
  entry: "Entry level",
  associate: "Associate",
  mid: "Mid-senior",
  senior: "Senior",
  director: "Director",
};

export const PERIOD_LABELS: Record<PayPeriod, string> = {
  hour: "an hour",
  day: "a day",
  week: "a week",
  month: "a month",
  year: "a year",
};

const PERIOD_SHORT: Record<PayPeriod, string> = { hour: "/hr", day: "/day", week: "/wk", month: "/mo", year: "/yr" };

/** Saved preferences (possibly from an older version) with every field filled in. */
export function withDefaults(prefs?: Partial<JobSearchPrefs> | null): JobSearchPrefs {
  const p = prefs ?? {};
  return {
    ...DEFAULT_JOB_SEARCH,
    ...p,
    min_pay: { ...DEFAULT_JOB_SEARCH.min_pay, ...(p.min_pay ?? {}) },
    job_types: [...(p.job_types ?? [])],
    work_modes: [...(p.work_modes ?? [])],
    experience: [...(p.experience ?? [])],
    exclude_companies: [...(p.exclude_companies ?? [])],
    exclude_title_words: [...(p.exclude_title_words ?? [])],
    dealbreakers: [...(p.dealbreakers ?? [])],
  };
}

/** The match a job without pay needs when a minimum pay is set: halfway from the bar to 10. */
export function noPayBar(prefs: JobSearchPrefs): number {
  if (!prefs.min_match || !prefs.min_pay.amount) return prefs.min_match;
  return prefs.min_match + (10 - prefs.min_match) / 2;
}

const SYMBOLS: Record<string, string> = { USD: "$", CAD: "CA$", AUD: "A$", NZD: "NZ$", GBP: "£", EUR: "€", INR: "₹" };

export function money(amount: number, currency: string): string {
  const symbol = SYMBOLS[currency] ?? (currency ? `${currency} ` : "");
  const value = amount >= 1000 ? Math.round(amount) : Math.round(amount * 100) / 100;
  return symbol + value.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

function range(low: number, high: number, currency: string): string {
  return low === high ? money(high, currency) : `${money(low, currency)}–${money(high, currency)}`;
}

export type SortKey = "newest" | "match" | "pay" | "company";
export const SORT_KEYS: SortKey[] = ["newest", "match", "pay", "company"];

/** Jobs in the chosen order; ones without a score or pay go last. "newest" keeps the list's own order. */
export function sortJobs(jobs: Job[], key: SortKey): Job[] {
  if (key === "newest") return jobs;
  if (key === "company") {
    return [...jobs].sort((a, b) => (a.company || "").localeCompare(b.company || "", undefined, { sensitivity: "base" }));
  }
  const value = (job: Job): number | undefined => (key === "match" ? job.match : job.pay?.yearly_high);
  return [...jobs].sort((a, b) => {
    const va = value(a), vb = value(b);
    if (va === undefined && vb === undefined) return 0;
    if (va === undefined) return 1;
    if (vb === undefined) return -1;
    return vb - va;
  });
}

/** "$22–$26/hr" and "≈ $45,760–$54,080/yr at 40 h/wk" for a stored pay. */
export function formatPay(pay?: JobPay | null): { main: string; yearly?: string } | null {
  if (!pay) return null;
  const main = (pay.period_inferred || pay.source_estimate ? "≈ " : "") + range(pay.low, pay.high, pay.currency) + PERIOD_SHORT[pay.period];
  if (pay.period === "year") return { main };
  const at = pay.period === "hour" ? ` at ${pay.hours} h/wk` : "";
  return { main, yearly: `≈ ${range(pay.yearly_low, pay.yearly_high, pay.currency)}/yr${at}` };
}
