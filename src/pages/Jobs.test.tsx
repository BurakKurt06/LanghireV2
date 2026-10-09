import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import JobsPage from "./Jobs";

function Jobs({ url = "/jobs" }: { url?: string }) {
  return (
    <MemoryRouter initialEntries={[url]}>
      <JobsPage />
    </MemoryRouter>
  );
}
import { getJobStats, getApplyStatus } from "../lib/api";

// i18n: identity translator so assertions can use raw keys.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { changeLanguage: vi.fn() } }),
}));

// Mock the child tabs so this test focuses on the Jobs shell (tab switching + stats).
vi.mock("./jobs/CollectTab", () => ({
  default: ({ onJobsChanged }: { onJobsChanged: () => void }) => (
    <div data-testid="collect-tab">
      <button onClick={onJobsChanged}>collect-refresh</button>
    </div>
  ),
}));
vi.mock("./jobs/PendingTab", () => ({
  default: () => <div data-testid="pending-tab" />,
}));
vi.mock("./jobs/HistoryTab", () => ({
  default: () => <div data-testid="history-tab" />,
}));

vi.mock("../lib/api", () => ({
  getJobStats: vi.fn(),
  getApplyStatus: vi.fn(),
}));

const mockGetJobStats = vi.mocked(getJobStats);
const mockGetApplyStatus = vi.mocked(getApplyStatus);

const stats = {
  total: 10,
  pending: 4,
  applied: 3,
  failed: 2,
  blocked: 1,
  in_progress: 0,
};

describe("Jobs", () => {
  beforeEach(() => {
    mockGetJobStats.mockResolvedValue(structuredClone(stats) as never);
    mockGetApplyStatus.mockResolvedValue({ running: false } as never);
  });

  it("fetches stats on mount and renders the header", async () => {
    render(<Jobs />);
    expect(screen.getByText("title")).toBeInTheDocument();
    await waitFor(() => expect(mockGetJobStats).toHaveBeenCalled());
  });

  it("opens the tab named in the URL (the attention pill links to ?tab=history)", async () => {
    render(<Jobs url="/jobs?tab=history" />);
    expect(await screen.findByTestId("history-tab")).toBeInTheDocument();
  });

  it("renders the Collect tab by default", async () => {
    render(<Jobs />);
    expect(await screen.findByTestId("collect-tab")).toBeInTheDocument();
    expect(screen.queryByTestId("pending-tab")).not.toBeInTheDocument();
  });

  it("switches to the Pending tab", async () => {
    const user = userEvent.setup();
    render(<Jobs />);
    await waitFor(() => expect(mockGetJobStats).toHaveBeenCalled());

    await user.click(screen.getByRole("button", { name: /Review & Apply/ }));
    expect(await screen.findByTestId("pending-tab")).toBeInTheDocument();
    expect(screen.queryByTestId("collect-tab")).not.toBeInTheDocument();
  });

  it("switches to the History tab", async () => {
    const user = userEvent.setup();
    render(<Jobs />);
    await waitFor(() => expect(mockGetJobStats).toHaveBeenCalled());

    await user.click(screen.getByRole("button", { name: /History/ }));
    expect(await screen.findByTestId("history-tab")).toBeInTheDocument();
  });

  it("re-fetches stats when a child reports jobs changed", async () => {
    const user = userEvent.setup();
    render(<Jobs />);
    await waitFor(() => expect(mockGetJobStats).toHaveBeenCalledTimes(1));

    await user.click(screen.getByRole("button", { name: "collect-refresh" }));
    await waitFor(() => expect(mockGetJobStats).toHaveBeenCalledTimes(2));
  });

  it("renders tab count badges derived from stats", async () => {
    render(<Jobs />);
    await waitFor(() => expect(mockGetJobStats).toHaveBeenCalled());
    // collect count = total (10); pending tab = pending + failed (6); history = applied (3)
    // The count badges carry a rounded-full class distinguishing them from the step number circles.
    await screen.findByText("10");
    const badges = screen
      .getAllByText(/^(10|6|3)$/)
      .filter((el) => el.className.includes("rounded-full") && el.className.includes("px-1.5"));
    const counts = badges.map((b) => b.textContent).sort();
    expect(counts).toEqual(["10", "3", "6"]);
  });

  it("does not crash when getJobStats rejects", async () => {
    mockGetJobStats.mockRejectedValue(new Error("down"));
    render(<Jobs />);
    expect(await screen.findByTestId("collect-tab")).toBeInTheDocument();
  });

  it("re-counts while a run goes (even through jobs that never open a browser), and not while idle", async () => {
    const job = (url: string) => ({ worker: 1, url, title: "Intern", company: "Acme", step: 1, max_steps: 70,
      started_at: new Date().toISOString() });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<Jobs />);
      await waitFor(() => expect(mockGetJobStats).toHaveBeenCalledTimes(1));
      await vi.advanceTimersByTimeAsync(6000);
      expect(mockGetJobStats).toHaveBeenCalledTimes(1); // nothing running: no re-counting

      // Running, but the jobs end before a browser opens (closed, fit-skipped): no current job.
      mockGetApplyStatus.mockResolvedValue({ running: true, current: [] } as never);
      await vi.advanceTimersByTimeAsync(2100);
      await waitFor(() => expect(mockGetJobStats).toHaveBeenCalledTimes(2)); // the run started
      await vi.advanceTimersByTimeAsync(5100);
      await waitFor(() => expect(mockGetJobStats).toHaveBeenCalledTimes(3)); // and keeps re-counting

      mockGetApplyStatus.mockResolvedValue({ running: true, current: [job("https://jobs.example/2")] } as never);
      await vi.advanceTimersByTimeAsync(2100);
      await waitFor(() => expect(mockGetJobStats.mock.calls.length).toBeGreaterThanOrEqual(4)); // the next job

      mockGetApplyStatus.mockResolvedValue({ running: false } as never);
      await vi.advanceTimersByTimeAsync(2100);
      await vi.advanceTimersByTimeAsync(500); // let the "run ended" re-count land
      const ended = mockGetJobStats.mock.calls.length;
      await vi.advanceTimersByTimeAsync(11000);
      expect(mockGetJobStats).toHaveBeenCalledTimes(ended); // the run ended: no more re-counting
    } finally {
      vi.useRealTimers();
    }
  });
});
