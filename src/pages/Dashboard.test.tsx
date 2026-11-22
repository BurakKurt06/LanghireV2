import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import Dashboard from "./Dashboard";
import {
  getDashboardData,
  checkHealth,
  getSetupStatus,
  getChromiumStatus,
  getQAStats,
  getJobs,
} from "../lib/api";
import { trackEvent } from "../lib/analytics";

const navigateMock = vi.fn();

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (k: string, o?: Record<string, unknown>) =>
      o ? `${k}:${JSON.stringify(o)}` : k,
    i18n: { changeLanguage: vi.fn() },
  }),
}));

vi.mock("react-router-dom", async (orig) => {
  const actual = await orig<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => navigateMock };
});

vi.mock("../lib/analytics", () => ({ trackEvent: vi.fn() }));
vi.mock("../lib/perf", () => ({
  markStart: vi.fn(),
  measureAndTrack: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  getDashboardData: vi.fn(),
  checkHealth: vi.fn(),
  getSetupStatus: vi.fn(),
  getChromiumStatus: vi.fn(),
  getQAStats: vi.fn(),
  // The Analytics page became <Insights/> on the Dashboard (decision 3); it loads the jobs.
  getJobs: vi.fn(),
}));

const mockGetDashboardData = vi.mocked(getDashboardData);
const mockCheckHealth = vi.mocked(checkHealth);
const mockGetSetupStatus = vi.mocked(getSetupStatus);
const mockGetChromiumStatus = vi.mocked(getChromiumStatus);
const mockGetQAStats = vi.mocked(getQAStats);
const mockGetJobs = vi.mocked(getJobs);
const mockTrackEvent = vi.mocked(trackEvent);

const dashboardData = {
  jobs: { total: 10, pending: 3, applied: 6, failed: 1, blocked: 0, in_progress: 0 },
  memory: { total_memories: 42, unique_domains: 5, by_category: {} },
};

const setupIncomplete = {
  profile: true,
  llm: false,
  resume: false,
  chromium: true,
  linkedin: true,
  gmail: true,
  onboarding_completed: false,
  all_required_done: false,
};

function renderDashboard() {
  return render(
    <MemoryRouter>
      <Dashboard />
    </MemoryRouter>,
  );
}

describe("Dashboard page", () => {
  beforeEach(() => {
    mockCheckHealth.mockResolvedValue({} as never);
    mockGetDashboardData.mockResolvedValue(dashboardData as never);
    mockGetSetupStatus.mockResolvedValue(setupIncomplete as never);
    mockGetQAStats.mockResolvedValue({ total: 7, answered: 4, unanswered: 3 } as never);
    mockGetChromiumStatus.mockResolvedValue({ state: "ready", message: "" } as never);
    // No jobs listed → the Insights section renders nothing, so the cards' numbers stay unique.
    mockGetJobs.mockResolvedValue([] as never);
  });

  it("renders the loading spinner header before data resolves", () => {
    // Keep the dashboard fetch pending so we observe the loading branch.
    mockGetDashboardData.mockReturnValue(new Promise(() => {}) as never);
    renderDashboard();
    expect(screen.getByText("title")).toBeInTheDocument();
    // The stats grid / quick actions only show after loading completes.
    expect(screen.queryByText("quickActions.title")).not.toBeInTheDocument();
  });

  it("renders stat cards computed from dashboard data after load", async () => {
    renderDashboard();
    // successRate = applied / finished attempts (applied + failed + needs_you + blocked) = round(6/7*100) = 86%
    expect(await screen.findByText("86%")).toBeInTheDocument();
    expect(screen.getByText("6 of 7 finished attempts")).toBeInTheDocument();
    expect(screen.getByText("10")).toBeInTheDocument(); // total jobs
    expect(screen.getByText("42")).toBeInTheDocument(); // memories
    expect(screen.getByText("cards.applied")).toBeInTheDocument();
    expect(screen.getByText("cards.pending")).toBeInTheDocument();
    // Decision 3: the old Analytics page is the Dashboard's lower half and loads the jobs.
    // (its first load starts right after the cards render)
    await waitFor(() => expect(mockGetJobs).toHaveBeenCalled());
  });

  // Decision 3: the "Failed" card counts every attempt that ended without an application
  // (failed + needs_you + blocked), and the backend's not_successful / finished win when sent.
  it("counts failed, needs-you and blocked jobs on the not-applied card", async () => {
    mockGetDashboardData.mockResolvedValue({
      jobs: { total: 20, pending: 2, applied: 4, failed: 1, needs_you: 3, blocked: 2, in_progress: 0 },
      memory: { total_memories: 0, unique_domains: 0, by_category: {} },
    } as never);
    renderDashboard();
    // notApplied = 1 + 3 + 2 = 6; finished = 4 + 6 = 10 → 40%.
    expect(await screen.findByText("40%")).toBeInTheDocument();
    expect(screen.getByText("4 of 10 finished attempts")).toBeInTheDocument();
    expect(screen.getByText("6")).toBeInTheDocument();
    // The detail adds up to the card's number: blocked jobs are named too.
    expect(screen.getByText("1 failed · 3 need you · 2 blocked")).toBeInTheDocument();
  });

  it("says how many of the jobs that need you may have been sent", async () => {
    mockGetDashboardData.mockResolvedValue({
      jobs: { total: 9, pending: 0, applied: 2, failed: 0, needs_you: 4, blocked: 0, in_progress: 0, outcome_unknown: 1 },
      memory: { total_memories: 0, unique_domains: 0, by_category: {} },
    } as never);
    renderDashboard();
    expect(await screen.findByText("4 need you (1 may have been sent)")).toBeInTheDocument();
  });

  it("uses the backend's not_successful and finished counts when present", async () => {
    mockGetDashboardData.mockResolvedValue({
      jobs: {
        total: 30, pending: 0, applied: 5, failed: 1, needs_you: 0, blocked: 0, in_progress: 0,
        not_successful: 15, finished: 20,
      },
      memory: { total_memories: 0, unique_domains: 0, by_category: {} },
    } as never);
    renderDashboard();
    // 5 / 20 = 25%, not 5 / (5 + 1).
    expect(await screen.findByText("25%")).toBeInTheDocument();
    expect(screen.getByText("15")).toBeInTheDocument();
    expect(screen.getByText("5 of 20 finished attempts")).toBeInTheDocument();
  });

  it("tracks a dashboard_stats analytics event with aggregated counts", async () => {
    renderDashboard();
    await waitFor(() =>
      expect(mockTrackEvent).toHaveBeenCalledWith(
        "dashboard_stats",
        expect.objectContaining({
          jobs_collected: 10,
          jobs_applied: 6,
          memories_created: 42,
          questions_extracted: 7,
        }),
      ),
    );
  });

  it("navigates from quick action buttons", async () => {
    const user = userEvent.setup();
    renderDashboard();
    await screen.findByText("quickActions.title");

    await user.click(screen.getByRole("button", { name: /quickActions\.collectJobs/ }));
    expect(navigateMock).toHaveBeenCalledWith("/jobs");

    await user.click(screen.getByRole("button", { name: /quickActions\.startApplying/ }));
    expect(navigateMock).toHaveBeenCalledWith("/apply");
  });

  it("renders the setup checklist with a 'set up' link for incomplete required steps", async () => {
    const user = userEvent.setup();
    renderDashboard();
    await screen.findByText("setup.gettingStarted");

    // LLM not done → its set-up button opens Settings → AI model.
    const setUpButtons = await screen.findAllByRole("button", { name: "setup.setUp" });
    expect(setUpButtons.length).toBeGreaterThan(0);
    await user.click(setUpButtons[0]);
    expect(navigateMock).toHaveBeenCalledWith("/settings?section=ai-model");
  });

  it("shows the all-done state when all required steps are complete", async () => {
    mockGetSetupStatus.mockResolvedValue({
      ...setupIncomplete,
      llm: true,
      resume: true,
      all_required_done: true,
    } as never);
    renderDashboard();
    expect(await screen.findByText("setup.allDone")).toBeInTheDocument();
    expect(screen.getByText("setup.complete")).toBeInTheDocument();
  });

  it("does not show the backend-not-connected banner when health is OK", async () => {
    renderDashboard();
    await screen.findByText("quickActions.title");
    // Healthy backend → the connection-error banner stays hidden.
    // Decision 3: the banner is plain words now ("The app's engine isn't answering yet").
    expect(screen.queryByText(/engine isn't answering/i)).not.toBeInTheDocument();
  });

  it("renders the Chromium install progress card while installing", async () => {
    mockGetChromiumStatus.mockResolvedValue({
      state: "installing",
      message: "Downloading 45%",
    } as never);
    renderDashboard();
    expect(await screen.findByText("Downloading 45%")).toBeInTheDocument();
    expect(screen.getByText("chromium.installing")).toBeInTheDocument();
  });

  it("renders gracefully when getDashboardData rejects (defaults to zeros)", async () => {
    mockGetDashboardData.mockRejectedValue(new Error("500"));
    renderDashboard();
    // Loading completes and quick actions render even though dashboard data failed.
    expect(await screen.findByText("quickActions.title")).toBeInTheDocument();
    // Stat cards still render their labels with zeroed values.
    expect(screen.getByText("cards.successRate")).toBeInTheDocument();
    // Decision 3: no success rate before any finished attempt ("—"), not "0%".
    const rateCard = screen.getByText("cards.successRate").parentElement as HTMLElement;
    expect(within(rateCard).getByText("—")).toBeInTheDocument();
    expect(within(rateCard).getByText("No finished attempts yet")).toBeInTheDocument();
    expect(within(rateCard).queryByText("0%")).not.toBeInTheDocument();
  });

  it("re-fetches setup status on the langhire:setup-updated event", async () => {
    // Initial load: nothing required done → 0 of 3 complete.
    mockGetSetupStatus.mockResolvedValue({ ...setupIncomplete, llm: false, resume: false, profile: false } as never);
    renderDashboard();
    await screen.findByText("quickActions.title");
    const initialCalls = mockGetSetupStatus.mock.calls.length;

    // Wizard finished → everything configured. Fire the event and expect a refetch.
    mockGetSetupStatus.mockResolvedValue({ ...setupIncomplete, llm: true, resume: true, profile: true, all_required_done: true } as never);
    window.dispatchEvent(new CustomEvent("langhire:setup-updated"));

    await waitFor(() =>
      expect(mockGetSetupStatus.mock.calls.length).toBeGreaterThan(initialCalls),
    );
  });
});
