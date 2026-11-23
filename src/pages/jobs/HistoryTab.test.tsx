import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import HistoryTab from "./HistoryTab";
import {
  stopApplying,
  pauseApplying,
  resumeApplying,
  reviewApplication,
  answerHelp,
  getApplyStatus,
  getJobs,
  getMetricRuns,
  getSettings,
  exportJobsCsv,
  saveJobsCsv,
  getJobLogs,
} from "../../lib/api";
import { save as saveDialog } from "@tauri-apps/plugin-dialog";

// i18n: identity translator so assertions can use raw keys.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { changeLanguage: vi.fn() } }),
}));

vi.mock("../../lib/analytics", () => ({ trackEvent: vi.fn() }));

vi.mock("../../lib/api", () => ({
  stopApplying: vi.fn(),
  pauseApplying: vi.fn(),
  resumeApplying: vi.fn(),
  reviewApplication: vi.fn(),
  answerHelp: vi.fn(),
  getApplyStatus: vi.fn(),
  getJobs: vi.fn(),
  getMetricRuns: vi.fn(),
  getSettings: vi.fn(),
  exportJobsCsv: vi.fn(),
  saveJobsCsv: vi.fn(),
  getJobLogs: vi.fn(),
}));

const mockStopApplying = vi.mocked(stopApplying);
const mockPauseApplying = vi.mocked(pauseApplying);
const mockResumeApplying = vi.mocked(resumeApplying);
const mockReviewApplication = vi.mocked(reviewApplication);
const mockAnswerHelp = vi.mocked(answerHelp);
const mockGetApplyStatus = vi.mocked(getApplyStatus);
const mockGetJobs = vi.mocked(getJobs);
const mockGetMetricRuns = vi.mocked(getMetricRuns);
const mockGetSettings = vi.mocked(getSettings);
const mockExportJobsCsv = vi.mocked(exportJobsCsv);
const mockSaveJobsCsv = vi.mocked(saveJobsCsv);
const mockGetJobLogs = vi.mocked(getJobLogs);

const idleApply = { running: false, mode: null, workers: 0, log: [], error: null, finished_at: null };

const appliedJob = {
  url: "https://job.example.com/applied",
  title: "Senior Engineer",
  company: "Acme",
  location: "Remote",
  easy_apply: true,
  status: "applied" as const,
  applied_at: "2026-06-01T10:00:00Z",
  collected_at: "2026-05-31T10:00:00Z",
};

const failedJob = {
  url: "https://job.example.com/failed",
  title: "Staff Engineer",
  company: "Globex",
  location: "NYC",
  easy_apply: false,
  status: "failed" as const,
  applied_at: "2026-06-02T10:00:00Z",
  error: "Captcha blocked",
};

const metric = {
  id: 1,
  job_url: "https://job.example.com/applied",
  job_title: "Senior Engineer",
  company: "Acme",
  website_domain: "example.com",
  ats_platform: null,
  success: true,
  error_message: null,
  started_at: "2026-06-01T10:00:00Z",
  finished_at: "2026-06-01T10:02:00Z",
  duration_seconds: 125,
  step_count: 12,
  memories_injected: 4,
  memories_extracted: 2,
  cost_usd: 0.0123,
  created_at: "2026-06-01T10:02:00Z",
};

describe("HistoryTab", () => {
  let onJobsChanged: ReturnType<typeof vi.fn<() => void>>;

  beforeEach(() => {
    onJobsChanged = vi.fn();
    mockGetApplyStatus.mockResolvedValue(structuredClone(idleApply) as never);
    mockGetJobs.mockImplementation(async (params?: { status?: string }) => {
      if (params?.status === "applied") return [structuredClone(appliedJob)] as never;
      if (params?.status === "failed") return [structuredClone(failedJob)] as never;
      return [] as never;
    });
    mockGetMetricRuns.mockResolvedValue([structuredClone(metric)] as never);
    mockGetSettings.mockResolvedValue({ resume_path: "/r.pdf" } as never);
    mockStopApplying.mockResolvedValue({ success: true } as never);
    mockExportJobsCsv.mockResolvedValue("Job Title,Company\nSenior Engineer,Acme\n" as never);
  });

  it("shows a spinner then renders the applied jobs list", async () => {
    render(<HistoryTab onJobsChanged={onJobsChanged} />);
    expect(await screen.findByText("Senior Engineer")).toBeInTheDocument();
    expect(screen.getByText("Staff Engineer")).toBeInTheDocument();
    // Both applied + failed jobs counted.
    expect(screen.getByText("(2)")).toBeInTheDocument();
  });

  it("renders the empty state when there are no applied/failed jobs", async () => {
    mockGetJobs.mockResolvedValue([] as never);
    render(<HistoryTab onJobsChanged={onJobsChanged} />);
    expect(
      await screen.findByText("No applications yet. Start applying to see results here."),
    ).toBeInTheDocument();
  });

  it("expands a job row to show metrics detail", async () => {
    const user = userEvent.setup();
    render(<HistoryTab onJobsChanged={onJobsChanged} />);
    const row = await screen.findByText("Senior Engineer");

    await user.click(row);
    // Metric fields appear after expanding.
    expect(await screen.findByText("Memories Injected")).toBeInTheDocument();
    expect(screen.getByText("Steps (LLM Calls)")).toBeInTheDocument();
    expect(screen.getByText("$0.0123")).toBeInTheDocument();
  });

  it("shows the failed-job error message in the expanded detail", async () => {
    const user = userEvent.setup();
    render(<HistoryTab onJobsChanged={onJobsChanged} />);
    const row = await screen.findByText("Staff Engineer");

    await user.click(row);
    expect(await screen.findByText("Captcha blocked")).toBeInTheDocument();
  });

  it("renders the live log and a stop button when applying is running", async () => {
    mockGetApplyStatus.mockResolvedValue({
      ...idleApply,
      running: true,
      log: ["Applying to job..."],
    } as never);
    const user = userEvent.setup();
    render(<HistoryTab onJobsChanged={onJobsChanged} />);

    expect(await screen.findByText("Applying to job...")).toBeInTheDocument();
    const stopBtn = screen.getByRole("button", { name: /controls\.stop/ });
    await user.click(stopBtn);
    expect(mockStopApplying).toHaveBeenCalled();
  });

  it("pauses so the user can take over, then resumes", async () => {
    // The status follows the pause, as the backend's does (the 2 s poll would otherwise undo it
    // between the two clicks on a slow machine).
    let paused = false;
    mockGetApplyStatus.mockImplementation(async () =>
      ({ ...idleApply, running: true, paused, log: ["Applying to job..."] }) as never);
    mockPauseApplying.mockImplementation(async () => { paused = true; return { success: true, paused: true }; });
    mockResumeApplying.mockImplementation(async () => { paused = false; return { success: true, paused: false }; });
    const user = userEvent.setup();
    render(<HistoryTab onJobsChanged={onJobsChanged} />);

    expect(await screen.findByText("controls.pauseHint")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /controls\.pause/ }));
    expect(mockPauseApplying).toHaveBeenCalled();
    expect(await screen.findByText("controls.pausedHint")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /controls\.resume/ }));
    expect(mockResumeApplying).toHaveBeenCalled();
    expect(await screen.findByText("controls.pauseHint")).toBeInTheDocument();
  });

  it("leftover review entries show no approve/skip card here", async () => {
    // Decision 1: Trial "review" approve/skip cards were removed (was: review.title card with approve/skip buttons).
    const review = { url: "https://jobs.example/1", title: "Intern", company: "Acme", summary: "Filled 3 pages." };
    mockGetApplyStatus.mockResolvedValue({ ...idleApply, running: true, log: ["Applying to job..."], reviews: [review] } as never);
    render(<HistoryTab onJobsChanged={onJobsChanged} />);

    expect(await screen.findByText("Applying to job...")).toBeInTheDocument();
    expect(screen.queryByText("review.title")).not.toBeInTheDocument();
    expect(screen.queryByText("Filled 3 pages.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "review.approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "review.skip" })).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(mockReviewApplication).not.toHaveBeenCalled();
  });

  it("a job waiting for help points to the run panel instead of being answered here", async () => {
    // Decision 5: HistoryTab no longer answers help requests (was: help card with a message box and Continue).
    const help = { url: "https://jobs.example/3", title: "Intern", company: "Cyberdyne", problem: "Save and Continue is disabled." };
    mockGetApplyStatus.mockResolvedValue({ ...idleApply, running: true, log: ["x"], help: [help] } as never);
    render(<HistoryTab onJobsChanged={onJobsChanged} />);

    expect(await screen.findByRole("status")).toHaveTextContent(
      "A job is waiting for you. Answer it in the run panel on the left.",
    );
    expect(screen.queryByText("help.title")).not.toBeInTheDocument();
    expect(screen.queryByText("Save and Continue is disabled.")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("help.messageLabel")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "help.continue" })).not.toBeInTheDocument();
    expect(mockAnswerHelp).not.toHaveBeenCalled();
  });

  it("several jobs waiting for help are counted in the run-panel note, with no give-up button here", async () => {
    // Decision 5: giving up moved to the run panel's help request (was: help.giveUp button calling answerHelp(url, "", true)).
    const help = [
      { url: "https://jobs.example/4", title: "Intern", company: "Cyberdyne", problem: "Stuck." },
      { url: "https://jobs.example/5", title: "Analyst", company: "Initech", problem: "Needs a code." },
    ];
    mockGetApplyStatus.mockResolvedValue({ ...idleApply, running: true, log: ["x"], help } as never);
    render(<HistoryTab onJobsChanged={onJobsChanged} />);

    expect(await screen.findByRole("status")).toHaveTextContent(
      "2 jobs are waiting for you. Answer them in the run panel on the left.",
    );
    expect(screen.queryByRole("button", { name: "help.giveUp" })).not.toBeInTheDocument();
    expect(mockAnswerHelp).not.toHaveBeenCalled();
  });

  it("no run-panel note once the run is not running", async () => {
    const help = { url: "https://jobs.example/6", title: "Intern", company: "Cyberdyne", problem: "Stuck." };
    mockGetApplyStatus.mockResolvedValue({ ...idleApply, running: false, log: ["Run finished"], help: [help] } as never);
    render(<HistoryTab onJobsChanged={onJobsChanged} />);
    expect(await screen.findByText("Run finished")).toBeInTheDocument();
    expect(screen.queryByText(/waiting for you/)).not.toBeInTheDocument();
  });

  it("each row's View log opens that job's own log, without expanding the row", async () => {
    // Decision 5: each history row has a "View log" button (JobLogDialog), on the row itself.
    mockGetJobLogs.mockResolvedValue([
      { id: 7, run_id: "r2", job_url: failedJob.url, timestamp: "2026-06-02T10:00:00Z", level: "ERROR",
        message: "Captcha blocked the form", created_at: "2026-06-02T10:00:00Z" },
    ]);
    const user = userEvent.setup();
    render(<HistoryTab onJobsChanged={onJobsChanged} />);
    const rowToggle = (await screen.findByText("Staff Engineer")).closest("button")!;
    expect(rowToggle).toHaveAttribute("aria-expanded", "false");
    await user.click(screen.getByRole("button", { name: "View log for Staff Engineer" }));
    expect(rowToggle).toHaveAttribute("aria-expanded", "false");   // the log opens; the row stays as it was

    const dialog = await screen.findByRole("dialog", { name: "Log for Staff Engineer" });
    expect(mockGetJobLogs).toHaveBeenCalledWith(failedJob.url);
    expect(await within(dialog).findByText("Captcha blocked the form")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows the paused state reported by the backend", async () => {
    mockGetApplyStatus.mockResolvedValue({ ...idleApply, running: true, paused: true, log: ["x"] } as never);
    render(<HistoryTab onJobsChanged={onJobsChanged} />);
    expect(await screen.findByRole("button", { name: /controls\.resume/ })).toBeInTheDocument();
  });

  it("does not crash when loadAppliedJobs rejects", async () => {
    mockGetJobs.mockRejectedValue(new Error("db down"));
    render(<HistoryTab onJobsChanged={onJobsChanged} />);
    // Falls through to the empty-state card.
    await waitFor(() => expect(mockGetJobs).toHaveBeenCalled());
    expect(
      await screen.findByText("No applications yet. Start applying to see results here."),
    ).toBeInTheDocument();
  });

  it("asks where to save the CSV and says where it went", async () => {
    vi.mocked(saveDialog).mockResolvedValueOnce("C:/Users/me/Desktop/jobs.csv");
    mockSaveJobsCsv.mockResolvedValue({ success: true, path: "C:/Users/me/Desktop/jobs.csv", rows: 2 } as never);
    const user = userEvent.setup();
    render(<HistoryTab onJobsChanged={onJobsChanged} />);
    await screen.findByText("Senior Engineer");

    await user.click(screen.getByRole("button", { name: /controls\.exportCsv/ }));
    await waitFor(() => expect(mockSaveJobsCsv).toHaveBeenCalledWith("C:/Users/me/Desktop/jobs.csv"));
    expect(await screen.findByRole("status")).toHaveTextContent("controls.exportSaved");
    expect(mockExportJobsCsv).not.toHaveBeenCalled();
  });

  it("does nothing when the save dialog is cancelled", async () => {
    const user = userEvent.setup();
    render(<HistoryTab onJobsChanged={onJobsChanged} />);
    await screen.findByText("Senior Engineer");
    await user.click(screen.getByRole("button", { name: /controls\.exportCsv/ }));
    await waitFor(() => expect(saveDialog).toHaveBeenCalled());
    expect(mockSaveJobsCsv).not.toHaveBeenCalled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("outside the app it downloads the file and says so", async () => {
    vi.mocked(saveDialog).mockRejectedValueOnce(new Error("not in Tauri"));
    mockExportJobsCsv.mockResolvedValue("Job Title,Company" as never);
    const createObjectURL = vi.fn(() => "blob:mock");
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL: vi.fn() });
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const user = userEvent.setup();
    render(<HistoryTab onJobsChanged={onJobsChanged} />);
    await screen.findByText("Senior Engineer");

    await user.click(screen.getByRole("button", { name: /controls\.exportCsv/ }));
    await waitFor(() => expect(clickSpy).toHaveBeenCalled());
    expect(createObjectURL).toHaveBeenCalled();
    expect(await screen.findByRole("status")).toHaveTextContent("controls.exportDownloaded");
    clickSpy.mockRestore();
    vi.unstubAllGlobals();
  });

  it("shows the error when saving fails, and re-enables the button", async () => {
    vi.mocked(saveDialog).mockResolvedValueOnce("C:/jobs.csv");
    mockSaveJobsCsv.mockRejectedValue(new Error("disk full"));
    const user = userEvent.setup();
    render(<HistoryTab onJobsChanged={onJobsChanged} />);
    await screen.findByText("Senior Engineer");

    const exportBtn = screen.getByRole("button", { name: /controls\.exportCsv/ });
    await user.click(exportBtn);
    expect(await screen.findByRole("status")).toHaveTextContent("controls.exportFailed");
    await waitFor(() => expect(exportBtn).not.toBeDisabled());
  });
});
