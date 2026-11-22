import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import Insights from "./Insights";
import { dailyTrend } from "../lib/trend";
import { getJobs } from "../lib/api";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (_k: string, d?: string) => d ?? _k, i18n: { changeLanguage: vi.fn() } }),
}));

vi.mock("../lib/api", () => ({
  getJobs: vi.fn(),
}));

const mockGetJobs = vi.mocked(getJobs);

const jobs = [
  { url: "u1", title: "Eng", company: "Acme", location: "Remote", easy_apply: true, status: "applied", source: "linkedin", applied_at: "2026-06-20T10:00:00Z", collected_at: "2026-06-19T10:00:00Z" },
  { url: "u2", title: "Eng2", company: "Acme", location: "NYC", easy_apply: false, status: "applied", source: "indeed", applied_at: "2026-06-21T10:00:00Z", collected_at: "2026-06-20T10:00:00Z" },
  { url: "u3", title: "Eng3", company: "Globex", location: "LA", easy_apply: true, status: "failed", source: "linkedin", applied_at: "2026-06-22T10:00:00Z", collected_at: "2026-06-21T10:00:00Z" },
  { url: "u4", title: "Eng4", company: "Initech", location: "SF", easy_apply: true, status: "pending", source: "indeed", collected_at: "2026-06-22T10:00:00Z" },
];

describe("Insights (lower half of the Dashboard)", () => {
  beforeEach(() => {
    mockGetJobs.mockResolvedValue(structuredClone(jobs) as never);
  });

  it("counts every status, including the ones that need you", async () => {
    render(<Insights />);
    expect(await screen.findByText("By status")).toBeInTheDocument();
    expect(screen.getByText("Ready to apply")).toBeInTheDocument();
    expect(screen.getByText("Needs you")).toBeInTheDocument();
  });

  it("renders source, company, and status breakdowns", async () => {
    render(<Insights />);
    await screen.findByText("By status");
    expect(screen.getByText("By status")).toBeInTheDocument();
    expect(screen.getByText("By source")).toBeInTheDocument();
    expect(screen.getByText("Top companies")).toBeInTheDocument();
    // Acme appears as a top company (2 jobs)
    expect(screen.getByText("Acme")).toBeInTheDocument();
    // Sources present
    expect(screen.getByText("linkedin")).toBeInTheDocument();
    expect(screen.getByText("indeed")).toBeInTheDocument();
  });

  it("shows nothing at all when there are no jobs (the Dashboard says so already)", async () => {
    mockGetJobs.mockResolvedValue([] as never);
    const { container } = render(<Insights />);
    await new Promise((r) => setTimeout(r, 0));
    expect(container).toBeEmptyDOMElement();
  });

  it("says it couldn't load, rather than 'no data', when the API rejects", async () => {
    mockGetJobs.mockRejectedValue(new Error("down"));
    render(<Insights />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load your jobs");
    expect(screen.queryByText("No data yet")).not.toBeInTheDocument();
  });

  it("loads again by itself when the app's engine answers late (as the Dashboard does)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      mockGetJobs.mockRejectedValueOnce(new Error("down")).mockRejectedValueOnce(new Error("down"));
      render(<Insights />);
      expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load your jobs");
      await vi.advanceTimersByTimeAsync(3000);
      await vi.advanceTimersByTimeAsync(3000);
      expect(await screen.findByText("By status")).toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(mockGetJobs).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("counts every job, asking only for the fields it needs", async () => {
    render(<Insights />);
    await screen.findByText("By status");
    const params = mockGetJobs.mock.calls[0][0]!;
    expect(params.limit).toBeGreaterThanOrEqual(1_000_000);
    expect(params.fields).toEqual(expect.arrayContaining(["status", "applied_at"]));
  });
});

describe("dailyTrend", () => {
  const now = new Date(2026, 5, 22, 15, 0);   // 22 June 2026, local time
  const at = (d: number, h = 12) => new Date(2026, 5, d, h).toISOString();

  it("covers the last 14 calendar days, empty days included", () => {
    const trend = dailyTrend([{ applied_at: at(20) }, { applied_at: at(20, 9) }, { applied_at: at(1) }] as never, 14, now);
    expect(trend).toHaveLength(14);
    expect(trend[13]).toEqual({ day: "2026-06-22", count: 0 });
    expect(trend[0].day).toBe("2026-06-09");
    expect(trend.find((d) => d.day === "2026-06-20")!.count).toBe(2);
    expect(trend.reduce((s, d) => s + d.count, 0)).toBe(2);   // June 1 is outside the window
  });

  it("counts applications only, not collected jobs", () => {
    const trend = dailyTrend([{ collected_at: at(21), status: "pending" }] as never, 14, now);
    expect(trend.every((d) => d.count === 0)).toBe(true);
  });
});
