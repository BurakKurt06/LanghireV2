import { describe, it, expect } from "vitest";
import { DEFAULT_JOB_SEARCH, formatPay, noPayBar, sortJobs, withDefaults } from "./jobSearch";
import type { Job, JobPay } from "./types";

const hourly: JobPay = {
  low: 22, high: 26, currency: "USD", period: "hour", text: "$22 - $26 an hour", hours: 20,
  yearly_low: 22880, yearly_high: 27040, estimated: true,
};

function job(url: string, extra: Partial<Job> = {}): Job {
  return { url, title: url, company: "", location: "", easy_apply: null, status: "pending", ...extra };
}

describe("jobSearch", () => {
  it("shows hourly pay with its yearly estimate and the hours it assumes", () => {
    expect(formatPay(hourly)).toEqual({ main: "$22–$26/hr", yearly: "≈ $22,880–$27,040/yr at 20 h/wk" });
    expect(formatPay({ ...hourly, period: "year", low: 60000, high: 60000 })).toEqual({ main: "$60,000/yr" });
    expect(formatPay({ ...hourly, currency: "GBP", period: "month", low: 3000, high: 3000, yearly_low: 36000, yearly_high: 36000 }))
      .toEqual({ main: "£3,000/mo", yearly: "≈ £36,000/yr" });
    expect(formatPay(null)).toBeNull();
  });

  it("sorts by match and pay with unscored and unpaid jobs last, and keeps the list order for newest", () => {
    const jobs = [job("a"), job("b", { match: 6, match_source: "source_rules", pay: { ...hourly, yearly_high: 50000 } }), job("c", { match: 9, match_source: "source_rules" }),
                  job("d", { company: "acme", pay: { ...hourly, yearly_high: 90000 } })];
    expect(sortJobs(jobs, "newest")).toBe(jobs);
    expect(sortJobs(jobs, "match").map((j) => j.url)).toEqual(["c", "b", "a", "d"]);
    expect(sortJobs(jobs, "pay").map((j) => j.url)).toEqual(["d", "b", "a", "c"]);
    expect(sortJobs([job("x", { company: "Zeta" }), job("y", { company: "alpha" })], "company").map((j) => j.url))
      .toEqual(["y", "x"]);
  });

  it("marks board estimates and guessed periods separately from stated pay", () => {
    expect(formatPay({ ...hourly, period: "year", source_estimate: true })?.main).toBe("≈ $22–$26/yr");
    expect(formatPay({ ...hourly, period_inferred: true })?.main).toBe("≈ $22–$26/hr");
    expect(formatPay(hourly)?.main).toBe("$22–$26/hr");
  });

  it("ranks notes-driven AI relevance by score, retaining historical scores", () => {
    expect(sortJobs([job("old", { match: 10 }), job("source", { match: 8, match_source: "source_rules" })], "match").map(j => j.url))
      .toEqual(["old", "source"]);
  });

  it("fills in preferences saved by an older version", () => {
    const prefs = withDefaults({ job_types: ["part_time"] });
    expect(prefs.job_types).toEqual(["part_time"]);
    expect(prefs.min_pay).toEqual(DEFAULT_JOB_SEARCH.min_pay);
    expect(withDefaults(null)).toEqual(DEFAULT_JOB_SEARCH);
  });

  it("raises the match bar halfway to 10 for jobs without pay when a minimum is set", () => {
    expect(noPayBar({ ...DEFAULT_JOB_SEARCH, min_match: 5, min_pay: { amount: 20, period: "hour" } })).toBe(7.5);
    expect(noPayBar({ ...DEFAULT_JOB_SEARCH, min_match: 5 })).toBe(5);
    expect(noPayBar({ ...DEFAULT_JOB_SEARCH, min_match: 0, min_pay: { amount: 20, period: "hour" } })).toBe(0);
  });
});
