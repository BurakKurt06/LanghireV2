import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
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
  let onJobsChanged: ReturnType<typeof vi.fn>;

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
    mockGetApplyStatus.mockResolvedValue({ ...idleApply, running: true, log: ["Applying to job..."] } as never);
    mockPauseApplying.mockResolvedValue({ success: true, paused: true });
    mockResumeApplying.mockResolvedValue({ success: true, paused: false });
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

  it("shows an application waiting for review, and approves it", async () => {
    const review = { url: "https://jobs.example/1", title: "Intern", company: "Acme", summary: "Filled 3 pages." };
    mockGetApplyStatus.mockResolvedValue({ ...idleApply, running: true, log: ["x"], reviews: [review] } as never);
    mockReviewApplication.mockResolvedValue({ success: true });
    const user = userEvent.setup();
    render(<HistoryTab onJobsChanged={onJobsChanged} />);

    expect(await screen.findByText("review.title")).toBeInTheDocument();
    expect(screen.getByText("Filled 3 pages.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "review.approve" }));
    expect(mockReviewApplication).toHaveBeenCalledWith("https://jobs.example/1", true);
  });

  it("a stuck trial run shows a help card; the message goes with Continue", async () => {
    const help = { url: "https://jobs.example/3", title: "Intern", company: "Cyberdyne", problem: "Save and Continue is disabled." };
    mockGetApplyStatus.mockResolvedValue({ ...idleApply, running: true, log: ["x"], help: [help] } as never);
    mockAnswerHelp.mockResolvedValue({ success: true });
    const user = userEvent.setup();
    render(<HistoryTab onJobsChanged={onJobsChanged} />);

    expect(await screen.findByText("help.title")).toBeInTheDocument();
    expect(screen.getByText("Save and Continue is disabled.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("help.messageLabel"), "Tick the terms box first");
    await user.click(screen.getByRole("button", { name: "help.continue" }));
    expect(mockAnswerHelp).toHaveBeenCalledWith("https://jobs.example/3", "Tick the terms box first", false);
  });

  it("gives up on a stuck trial run", async () => {
    const help = { url: "https://jobs.example/4", title: "Intern", company: "Cyberdyne", problem: "Stuck." };
    mockGetApplyStatus.mockResolvedValue({ ...idleApply, running: true, log: ["x"], help: [help] } as never);
    mockAnswerHelp.mockResolvedValue({ success: true });
    const user = userEvent.setup();
    render(<HistoryTab onJobsChanged={onJobsChanged} />);
    await user.click(await screen.findByRole("button", { name: "help.giveUp" }));
    expect(mockAnswerHelp).toHaveBeenCalledWith("https://jobs.example/4", "", true);
  });

  it("skips an application waiting for review", async () => {
    const review = { url: "https://jobs.example/2", title: "Intern", company: "Acme", summary: "" };
    mockGetApplyStatus.mockResolvedValue({ ...idleApply, running: true, log: ["x"], reviews: [review] } as never);
    mockReviewApplication.mockResolvedValue({ success: true });
    const user = userEvent.setup();
    render(<HistoryTab onJobsChanged={onJobsChanged} />);
    await user.click(await screen.findByRole("button", { name: "review.skip" }));
    expect(mockReviewApplication).toHaveBeenCalledWith("https://jobs.example/2", false);
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
