import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import PendingTab from "./PendingTab";
import {
  getJobs,
  startApplying,
  getApplyStatus,
  generateCoverLetter,
  saveJobCoverLetter,
  saveProfile,
  getProfile,
  updateJobStatus,
  deleteJobs,
  tailorResumes,
  saveJobNote,
  refineTailoredResume,
  getTailoredResumeContent,
} from "../../lib/api";
import type { JobStats } from "../../lib/types";

// jsdom in this config does not provide a working localStorage; polyfill it.
function installLocalStorage() {
  const store = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() {
      return store.size;
    },
  });
}

// i18n: identity translator so assertions can use raw keys.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { changeLanguage: vi.fn() } }),
}));

// perf -> analytics; stub perf to avoid analytics side effects in tests.
vi.mock("../../lib/perf", () => ({
  markStart: vi.fn(),
  measureAndTrack: vi.fn(),
}));

vi.mock("../../lib/api", () => ({
  getJobs: vi.fn(),
  startApplying: vi.fn(),
  getApplyStatus: vi.fn(),
  generateCoverLetter: vi.fn(),
  saveJobCoverLetter: vi.fn(async () => ({ success: true })),
  saveProfile: vi.fn(),
  getProfile: vi.fn(),
  updateJobStatus: vi.fn(),
  deleteJobs: vi.fn(),
  tailorResumes: vi.fn(),
  saveJobNote: vi.fn(),
  refineTailoredResume: vi.fn(),
  getTailoredResumeContent: vi.fn(),
}));

const mockGetJobs = vi.mocked(getJobs);
const mockStartApplying = vi.mocked(startApplying);
const mockGetApplyStatus = vi.mocked(getApplyStatus);
const mockGenerateCoverLetter = vi.mocked(generateCoverLetter);
const mockSaveProfile = vi.mocked(saveProfile);
const mockGetProfile = vi.mocked(getProfile);
const mockUpdateJobStatus = vi.mocked(updateJobStatus);
const mockDeleteJobs = vi.mocked(deleteJobs);
const mockTailorResumes = vi.mocked(tailorResumes);
const mockSaveJobNote = vi.mocked(saveJobNote);
const mockRefineTailoredResume = vi.mocked(refineTailoredResume);
const mockGetTailoredResumeContent = vi.mocked(getTailoredResumeContent);

const idleApply = { running: false, mode: null, workers: 0, log: [], error: null, finished_at: null };

const pendingJob = {
  url: "https://job.example.com/pending",
  title: "Backend Engineer",
  company: "Acme",
  location: "Remote",
  easy_apply: true,
  status: "pending" as const,
  collected_at: "2026-06-01T10:00:00Z",
  description: "Build APIs",
};

const failedJob = {
  url: "https://job.example.com/failed",
  title: "Frontend Engineer",
  company: "Globex",
  location: "NYC",
  easy_apply: false,
  status: "failed" as const,
  error: "Form error",
};

const tailoredJob = {
  url: "https://job.example.com/tailored",
  title: "Staff Engineer",
  company: "Initech",
  location: "SF",
  easy_apply: true,
  status: "pending" as const,
  tailored_resume_path: "/tmp/resume.pdf",
};

const stats: JobStats = {
  total: 10,
  pending: 3,
  applied: 2,
  failed: 1,
  blocked: 0,
  in_progress: 0,
};

describe("PendingTab", () => {
  let onJobsChanged: ReturnType<typeof vi.fn<() => void>>;

  beforeEach(() => {
    installLocalStorage();
    vi.stubGlobal("alert", vi.fn());
    vi.stubGlobal("confirm", vi.fn(() => true));
    onJobsChanged = vi.fn();
    mockGetJobs.mockResolvedValue([
      structuredClone(pendingJob),
      structuredClone(failedJob),
      structuredClone(tailoredJob),
    ] as never);
    mockGetApplyStatus.mockResolvedValue(structuredClone(idleApply) as never);
    mockStartApplying.mockResolvedValue({ success: true, message: "ok" } as never);
    mockGenerateCoverLetter.mockResolvedValue({ success: true, cover_letter: "Dear Hiring Manager" } as never);
    mockGetProfile.mockResolvedValue({ name: "Me", cover_letter: "" } as never);
    mockSaveProfile.mockResolvedValue({ success: true } as never);
    mockUpdateJobStatus.mockResolvedValue({ success: true } as never);
    mockDeleteJobs.mockResolvedValue({ success: true, deleted: 1 } as never);
    mockTailorResumes.mockResolvedValue({
      success: true,
      results: [{ url: tailoredJob.url, status: "done", content: "Tailored!" }],
    } as never);
    mockRefineTailoredResume.mockResolvedValue({ success: true, content: "Refined!", path: "/x" } as never);
    mockGetTailoredResumeContent.mockResolvedValue({ success: true, content: "Resume body", path: "/x" } as never);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows a spinner then renders the pending job list", async () => {
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    expect(await screen.findByText("Backend Engineer")).toBeInTheDocument();
    expect(screen.getByText("Frontend Engineer")).toBeInTheDocument();
    expect(screen.getByText("Staff Engineer")).toBeInTheDocument();
  });

  it("renders the empty state when no jobs are returned", async () => {
    mockGetJobs.mockResolvedValue([] as never);
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    expect(await screen.findByText("emptyState.noJobsYet")).toBeInTheDocument();
  });

  it("filters out applied jobs when no status filter is set", async () => {
    mockGetJobs.mockResolvedValue([
      structuredClone(pendingJob),
      { ...structuredClone(pendingJob), url: "u2", title: "Applied One", status: "applied" },
    ] as never);
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    expect(await screen.findByText("Backend Engineer")).toBeInTheDocument();
    expect(screen.queryByText("Applied One")).not.toBeInTheDocument();
  });

  it("opens the apply confirm dialog and starts a single apply on confirm", async () => {
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Backend Engineer");

    // The pending job row has an Apply button (jobItem.apply key).
    const applyButtons = screen.getAllByRole("button", { name: /jobItem\.apply/ });
    await user.click(applyButtons[0]);

    await user.click(await screen.findByRole("button", { name: "Got it, Start" }));
    await waitFor(() =>
      expect(mockStartApplying).toHaveBeenCalledWith(
        expect.objectContaining({ job_url: pendingJob.url, workers: 1, mode: "all" }),
      ),
    );
  });

  it("changes a job status via the three-dot menu", async () => {
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Backend Engineer");

    const menuButtons = screen.getAllByRole("button", { name: "Change status" });
    await user.click(menuButtons[0]);

    const markApplied = await screen.findByRole("button", { name: "Mark as Applied" });
    await user.click(markApplied);
    await waitFor(() =>
      expect(mockUpdateJobStatus).toHaveBeenCalledWith(pendingJob.url, "applied"),
    );
    expect(onJobsChanged).toHaveBeenCalled();
  });

  it("batch-selects jobs and deletes them after confirmation", async () => {
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Backend Engineer");

    // Select All (pending + failed = 3 selectable: pending, failed, tailored).
    await user.click(screen.getByRole("button", { name: /Select All/ }));

    const deleteBtn = await screen.findByRole("button", { name: /^Delete \d/ });
    await user.click(deleteBtn);

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => expect(mockDeleteJobs).toHaveBeenCalled());
    expect(mockDeleteJobs.mock.calls[0][0].length).toBeGreaterThan(0);
  });

  it("does not delete when confirmation is declined", async () => {
    vi.stubGlobal("confirm", vi.fn(() => false));
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Backend Engineer");

    await user.click(screen.getByRole("button", { name: /Select All/ }));
    await user.click(await screen.findByRole("button", { name: /^Delete \d/ }));
    expect(mockDeleteJobs).not.toHaveBeenCalled();
  });

  it("batch-applies selected jobs", async () => {
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Backend Engineer");

    await user.click(screen.getByRole("button", { name: /Select All/ }));
    const applyBatch = await screen.findByRole("button", { name: /Apply to \d+ job/ });
    await user.click(applyBatch);

    await waitFor(() =>
      expect(mockStartApplying).toHaveBeenCalledWith(
        expect.objectContaining({ job_urls: expect.any(Array), mode: "all" }),
      ),
    );
    // No worker count: the backend uses Settings → Jobs at the same time.
    expect(mockStartApplying.mock.calls[0][0]).not.toHaveProperty("workers");
  });

  it("auto-tailor on: tailors untailored jobs before a batch apply", async () => {
    localStorage.setItem("langhire_auto_tailor", "true");
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Backend Engineer");

    await user.click(screen.getByRole("button", { name: /Select All/ }));
    await user.click(await screen.findByRole("button", { name: /Apply to \d+ job/ }));

    await waitFor(() => expect(mockStartApplying).toHaveBeenCalled());
    expect(mockTailorResumes).toHaveBeenCalledTimes(1);
    const [urls] = mockTailorResumes.mock.calls[0];
    expect(urls).toContain(pendingJob.url);
    expect(urls).not.toContain(tailoredJob.url); // already has a tailored resume
    expect(mockTailorResumes.mock.invocationCallOrder[0]).toBeLessThan(mockStartApplying.mock.invocationCallOrder[0]);
  });

  it("trial run chosen in the start dialog is sent with the apply", async () => {
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Backend Engineer");
    await user.click(screen.getAllByRole("button", { name: /jobItem\.apply/ })[0]);
    await user.click(await screen.findByRole("radio", { name: "Trial run" }));
    await user.click(screen.getByRole("button", { name: "Got it, Start" }));
    await waitFor(() =>
      expect(mockStartApplying).toHaveBeenCalledWith(expect.objectContaining({ run_mode: "trial" })),
    );
    expect(localStorage.getItem("langhire_apply_mode")).toBe("trial");
  });

  it("a note typed in the start dialog is saved on the job before it starts", async () => {
    mockSaveJobNote.mockResolvedValue({ success: true });
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Backend Engineer");
    await user.click(screen.getAllByRole("button", { name: /jobItem\.apply/ })[0]);
    await user.type(await screen.findByLabelText(/Note for the agent/), "Tick the terms box first");
    await user.click(screen.getByRole("button", { name: "Got it, Start" }));
    await waitFor(() => expect(mockStartApplying).toHaveBeenCalled());
    expect(mockSaveJobNote).toHaveBeenCalledWith(pendingJob.url, "Tick the terms box first");
    expect(mockSaveJobNote.mock.invocationCallOrder[0]).toBeLessThan(mockStartApplying.mock.invocationCallOrder[0]);
  });

  it("saves a failed job's note without starting and supplies it when Retry opens", async () => {
    mockSaveJobNote.mockResolvedValue({ success: true });
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await user.click(await screen.findByRole("button", { name: "Edit retry note for Frontend Engineer" }));
    await user.type(screen.getByLabelText("Retry note for Frontend Engineer"), "Choose the saved education option");
    await user.click(screen.getByRole("button", { name: "Save note" }));
    await waitFor(() => expect(screen.queryByLabelText("Retry note for Frontend Engineer")).not.toBeInTheDocument());
    expect(mockSaveJobNote).toHaveBeenCalledWith(failedJob.url, "Choose the saved education option");
    expect(mockStartApplying).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "jobItem.retry" }));
    expect(await screen.findByLabelText(/Note for the agent/)).toHaveValue("Choose the saved education option");
  });

  it("keeps an unsaved retry note visible after a save failure", async () => {
    mockSaveJobNote.mockRejectedValue(new Error("Disk unavailable"));
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await user.click(await screen.findByRole("button", { name: "Edit retry note for Frontend Engineer" }));
    await user.type(screen.getByLabelText("Retry note for Frontend Engineer"), "Keep this draft");
    await user.click(screen.getByRole("button", { name: "Save note" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Disk unavailable");
    expect(screen.getByLabelText("Retry note for Frontend Engineer")).toHaveValue("Keep this draft");
    expect(mockStartApplying).not.toHaveBeenCalled();
  });

  it("a remembered training mode opens as a trial run, the only other mode next to full auto", async () => {
    localStorage.setItem("langhire_apply_mode", "training");
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Backend Engineer");
    await user.click(screen.getAllByRole("button", { name: /jobItem\.apply/ })[0]);
    expect(await screen.findByRole("radio", { name: "Trial run" })).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByRole("radio", { name: "Training" })).not.toBeInTheDocument();
    expect(screen.getByText(/No approval prompts/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Got it, Start" }));
    await waitFor(() =>
      expect(mockStartApplying).toHaveBeenCalledWith(expect.objectContaining({ run_mode: "trial" })),
    );
  });

  it("marks a job a trial run filled in and skipped at review", async () => {
    mockGetJobs.mockResolvedValue([{ ...structuredClone(pendingJob), practised_at: "2026-10-01T10:00:00Z" }] as never);
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    expect(await screen.findByText(/trial, not submitted/)).toBeInTheDocument();
  });

  it("a job that needs the user can be retried and explains why, without looking like an error", async () => {
    mockGetJobs.mockResolvedValue([{ ...structuredClone(pendingJob), status: "needs_you",
      error: "This site asks you to confirm you're a real person." }] as never);
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    const reason = await screen.findByText(/confirm you're a real person/);
    expect(reason.closest("div")?.className).toContain("text-amber-700");
    expect(screen.getByRole("button", { name: /jobItem\.retry/ })).toBeInTheDocument();
    expect(screen.getAllByText("status.needsYou").length).toBeGreaterThan(0);
  });

  it("a closed posting can't be applied to", async () => {
    mockGetJobs.mockResolvedValue([{ ...structuredClone(pendingJob), status: "closed",
      error: "The posting was taken down." }] as never);
    render(<PendingTab onJobsChanged={onJobsChanged} stats={{ ...stats, closed: 1 }} />);
    expect(await screen.findByText("The posting was taken down.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /jobItem\.(apply|retry)$/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /status\.closed \(1\)/ })).toBeInTheDocument();
  });

  it("trial request approval is the default", async () => {
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Backend Engineer");
    await user.click(screen.getAllByRole("button", { name: /jobItem\.apply/ })[0]);
    expect(await screen.findByRole("radio", { name: "Trial run" })).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("button", { name: "Got it, Start" }));
    await waitFor(() =>
      expect(mockStartApplying).toHaveBeenCalledWith(expect.objectContaining({ run_mode: "trial" })),
    );
  });

  it("does not retry an uncertain submission when duplicate-risk confirmation is declined", async () => {
    vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    mockGetJobs.mockResolvedValue([{ ...structuredClone(pendingJob), status: "needs_you", submit_attempted_at: "2026-10-16T10:00:00Z" }] as never);
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await user.click(await screen.findByRole("button", { name: /jobItem\.retry/ }));
    await user.click(screen.getByRole("button", { name: "Got it, Start" }));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("duplicate risk"));
    expect(mockStartApplying).not.toHaveBeenCalled();
  });

  it("sends explicit acknowledgement only after the user confirms an uncertain retry", async () => {
    vi.spyOn(window, "confirm").mockReturnValueOnce(true);
    mockGetJobs.mockResolvedValue([{ ...structuredClone(pendingJob), status: "needs_you", submit_attempted_at: "2026-10-16T10:00:00Z" }] as never);
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await user.click(await screen.findByRole("button", { name: /jobItem\.retry/ }));
    await user.click(screen.getByRole("button", { name: "Got it, Start" }));
    await waitFor(() => expect(mockStartApplying).toHaveBeenCalledWith(expect.objectContaining({ acknowledge_unknown: true })));
  });

  it("auto-tailor off: applies without tailoring", async () => {
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Backend Engineer");
    await user.click(screen.getAllByRole("button", { name: /jobItem\.apply/ })[0]);
    await user.click(await screen.findByRole("button", { name: "Got it, Start" }));
    await waitFor(() => expect(mockStartApplying).toHaveBeenCalled());
    expect(mockTailorResumes).not.toHaveBeenCalled();
  });

  it("batch-tailors selected resumes", async () => {
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Backend Engineer");

    await user.click(screen.getByRole("button", { name: /Select All/ }));
    await user.click(await screen.findByRole("button", { name: /Tailor Resume/ }));

    await waitFor(() => expect(mockTailorResumes).toHaveBeenCalled());
    await waitFor(() => expect(window.alert).toHaveBeenCalled());
  });

  it("opens the cover letter modal and generates a letter from the description", async () => {
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Backend Engineer");

    const coverButtons = screen.getAllByRole("button", { name: /jobItem\.coverLetter/ });
    await user.click(coverButtons[0]);

    expect(await screen.findByText("coverLetterModal.title")).toBeInTheDocument();
    await waitFor(() =>
      expect(mockGenerateCoverLetter).toHaveBeenCalledWith("Build APIs", "Backend Engineer", "Acme"),
    );
    expect(await screen.findByDisplayValue("Dear Hiring Manager")).toBeInTheDocument();
  });

  it("saves a generated cover letter on that job, never as the profile's letter for every job", async () => {
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Backend Engineer");

    const coverButtons = screen.getAllByRole("button", { name: /jobItem\.coverLetter/ });
    await user.click(coverButtons[0]);
    await screen.findByDisplayValue("Dear Hiring Manager");

    await user.click(screen.getByRole("button", { name: /coverLetterModal\.saveToProfile/ }));
    await waitFor(() =>
      expect(vi.mocked(saveJobCoverLetter)).toHaveBeenCalledWith("https://job.example.com/pending", "Dear Hiring Manager"),
    );
    expect(mockSaveProfile).not.toHaveBeenCalled();
  });

  it("expands a tailored job, loads content, and refines it", async () => {
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Staff Engineer");

    await user.click(screen.getByRole("button", { name: /View tailored resume/ }));
    await waitFor(() => expect(mockGetTailoredResumeContent).toHaveBeenCalledWith(tailoredJob.url));
    expect(await screen.findByText("Resume body")).toBeInTheDocument();

    const refineInput = screen.getByPlaceholderText(/Refine:/);
    await user.type(refineInput, "emphasize AWS");
    await user.click(screen.getByRole("button", { name: /^Refine$/ }));

    await waitFor(() =>
      expect(mockRefineTailoredResume).toHaveBeenCalledWith(tailoredJob.url, "emphasize AWS"),
    );
    expect(await screen.findByText("Refined!")).toBeInTheDocument();
  });

  it("filters by status sub-tab (re-queries with the chosen status)", async () => {
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Backend Engineer");

    mockGetJobs.mockClear();
    await user.click(screen.getByRole("button", { name: /status\.failed/ }));
    await waitFor(() =>
      expect(mockGetJobs).toHaveBeenCalledWith(expect.objectContaining({ status: "failed" })),
    );
  });

  it("searches jobs via the search form", async () => {
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Backend Engineer");

    await user.type(screen.getByPlaceholderText("searchPlaceholder"), "engineer");
    mockGetJobs.mockClear();
    await user.click(screen.getByRole("button", { name: "search" }));
    await waitFor(() =>
      expect(mockGetJobs).toHaveBeenCalledWith(expect.objectContaining({ search: "engineer" })),
    );
  });

  it("alerts when a single apply rejects", async () => {
    mockStartApplying.mockRejectedValueOnce(new Error("apply boom"));
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Backend Engineer");

    const applyButtons = screen.getAllByRole("button", { name: /jobItem\.apply/ });
    await user.click(applyButtons[0]);
    await user.click(await screen.findByRole("button", { name: "Got it, Start" }));
    await waitFor(() => expect(window.alert).toHaveBeenCalledWith("apply boom"));
  });

  it("persists tailoring options to localStorage when toggled", async () => {
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Backend Engineer");

    await user.click(screen.getByRole("button", { name: /Tailoring Options/ }));
    const titleCheckbox = await screen.findByRole("checkbox", { name: /Job Title/ });
    await user.click(titleCheckbox);

    await waitFor(() => {
      const stored = JSON.parse(localStorage.getItem("langhire_tailor_options") || "{}");
      expect(stored.title).toBe(true);
    });
  });

  it("renders the easy-apply and tailored badges", async () => {
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("Backend Engineer");
    expect(screen.getAllByText("jobItem.easyApply").length).toBeGreaterThan(0);
    // Tailored job shows the "Tailored" badge.
    const tailoredRow = screen.getByText("Staff Engineer").closest("div");
    expect(within(tailoredRow!.parentElement as HTMLElement).getByText("Tailored")).toBeInTheDocument();
  });

  it("shows a failure's first line as a bold title above the paragraph", async () => {
    mockGetJobs.mockResolvedValue([{ ...structuredClone(failedJob),
      error: "Stopped at Review: Field of Study\nThe form had no Statistics option." }] as never);
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    const title = await screen.findByText(/Stopped at Review: Field of Study/);
    expect(title.className).toContain("font-semibold");
    expect(screen.getByText("The form had no Statistics option.")).toBeInTheDocument();
  });

  it("a skipped job shows why and can still be applied to", async () => {
    mockGetJobs.mockResolvedValue([{ ...structuredClone(pendingJob), status: "skipped",
      error: "Requires an MBA\nThe posting is for MBA students only." }] as never);
    render(<PendingTab onJobsChanged={onJobsChanged} stats={{ ...stats, skipped: 1 }} />);
    expect(await screen.findByText("Requires an MBA")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /jobItem\.retry/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /status\.skipped \(1\)/ })).toBeInTheDocument();
  });

  it("Select All leaves out skipped jobs (applying to one overrides the fit check)", async () => {
    const skipped = { ...structuredClone(pendingJob), url: "https://job.example/skipped", title: "MBA Associate",
      status: "skipped" as const, error: "Requires an MBA\nMBA students only." };
    mockGetJobs.mockResolvedValue([structuredClone(pendingJob), skipped] as never);
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await screen.findByText("MBA Associate");
    await user.click(screen.getByRole("button", { name: /Select All \(\s*1\)/ }));
    await user.click(await screen.findByRole("button", { name: /Apply to 1 job/ }));
    await waitFor(() =>
      expect(mockStartApplying).toHaveBeenCalledWith(expect.objectContaining({ job_urls: [pendingJob.url] })),
    );
  });

  it("reloads the list when the counts change (a run moved jobs along)", async () => {
    const { rerender } = render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    await waitFor(() => expect(mockGetJobs).toHaveBeenCalledTimes(1));
    rerender(<PendingTab onJobsChanged={onJobsChanged} stats={{ ...stats }} />);
    expect(mockGetJobs).toHaveBeenCalledTimes(1); // same numbers: no reload
    rerender(<PendingTab onJobsChanged={onJobsChanged} stats={{ ...stats, pending: stats.pending - 1, failed: stats.failed + 1 }} />);
    await waitFor(() => expect(mockGetJobs).toHaveBeenCalledTimes(2));
  });

  it("shows what the collector read and sorts by pay or match", async () => {
    const pay = { low: 22, high: 26, currency: "USD", period: "hour" as const, text: "$22 - $26 an hour", hours: 40,
                  yearly_low: 45760, yearly_high: 54080, estimated: true };
    mockGetJobs.mockResolvedValue([
      { ...structuredClone(pendingJob), url: "u1", title: "Scored Low", match: 6, match_source: "source_rules", job_types: ["full_time"] },
      { ...structuredClone(pendingJob), url: "u2", title: "Paid Well", pay, match: 8, work_mode: "remote", match_reason: "Fits",
        match_source: "source_rules", model_match: 0, model_reason: "Advisory only" },
      { ...structuredClone(pendingJob), url: "u3", title: "Plain Pasted Link" },
    ] as never);
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={onJobsChanged} stats={stats} />);
    expect(await screen.findByText("Paid Well")).toBeInTheDocument();
    expect(screen.getByText("$22–$26/hr")).toBeInTheDocument();
    expect(screen.getAllByText("jobItem.sourceFit")).toHaveLength(2);
    expect(screen.getByText("jobItem.aiSuggestion")).toBeInTheDocument();
    expect(screen.getByText(/≈ \$45,760–\$54,080\/yr/)).toBeInTheDocument();
    expect(screen.getByText("jobItem.payNotListed")).toBeInTheDocument();   // only the collected job without pay
    const titles = () => screen.getAllByRole("heading", { level: 4 }).map((h) => h.textContent);
    await user.selectOptions(screen.getByLabelText("sort.label"), "pay");
    expect(titles()).toEqual(["Paid Well", "Scored Low", "Plain Pasted Link"]);
    await user.selectOptions(screen.getByLabelText("sort.label"), "match");
    expect(titles()).toEqual(["Paid Well", "Scored Low", "Plain Pasted Link"]);
    await user.selectOptions(screen.getByLabelText("sort.label"), "newest");
    expect(titles()).toEqual(["Scored Low", "Paid Well", "Plain Pasted Link"]);
  });
});
