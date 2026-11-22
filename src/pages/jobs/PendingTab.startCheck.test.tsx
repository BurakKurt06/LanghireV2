import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import PendingTab from "./PendingTab";
import {
  getJobs,
  startApplying,
  getApplyStatus,
  checkBeforeStart,
  getWaitingQuestions,
  answerQuestionEverywhere,
  getJobScreenshot,
} from "../../lib/api";
import type { JobStats } from "../../lib/types";

// What Start says before the first job, the questions several jobs wait on (answered once), and
// the picture of a job's last page. Made-up jobs; the backend is a stand-in.

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { changeLanguage: vi.fn() } }),
}));
vi.mock("../../lib/perf", () => ({ markStart: vi.fn(), measureAndTrack: vi.fn() }));
vi.mock("../../lib/api", () => ({
  getJobs: vi.fn(),
  startApplying: vi.fn(),
  getApplyStatus: vi.fn(),
  generateCoverLetter: vi.fn(),
  saveJobCoverLetter: vi.fn(),
  updateJobStatus: vi.fn(),
  deleteJobs: vi.fn(),
  tailorResumes: vi.fn(),
  saveJobNote: vi.fn(),
  refineTailoredResume: vi.fn(),
  getTailoredResumeContent: vi.fn(),
  answerJobQuestions: vi.fn(),
  getJobLogs: vi.fn(),
  checkBeforeStart: vi.fn(),
  getWaitingQuestions: vi.fn(),
  answerQuestionEverywhere: vi.fn(),
  getJobScreenshot: vi.fn(),
}));

const mockGetJobs = vi.mocked(getJobs);
const mockStart = vi.mocked(startApplying);
const mockCheck = vi.mocked(checkBeforeStart);
const mockWaiting = vi.mocked(getWaitingQuestions);
const mockAnswer = vi.mocked(answerQuestionEverywhere);
const mockPicture = vi.mocked(getJobScreenshot);

const ready = (n: number) => ({
  url: `https://jobs.example/ready-${n}`, title: `Ready Job ${n}`, company: "Example Works", location: "Remote",
  easy_apply: false, status: "pending" as const,
});
const stats: JobStats = { total: 3, pending: 2, applied: 0, failed: 1, blocked: 0, in_progress: 0 };
const SCHOOL = "Your school before university (high school): its name and the qualification you followed";
const nothing = { ok: true, problems: [], questions: [], jobs: 2 };

function installLocalStorage() {
  const store = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() { return store.size; },
  });
}

describe("PendingTab: before a run starts", () => {
  beforeEach(() => {
    installLocalStorage();
    vi.stubGlobal("alert", vi.fn());
    vi.stubGlobal("confirm", vi.fn(() => true));
    mockGetJobs.mockResolvedValue([ready(1), ready(2)] as never);
    vi.mocked(getApplyStatus).mockResolvedValue({ running: false, mode: null, workers: 0, log: [], error: null, finished_at: null } as never);
    mockStart.mockResolvedValue({ success: true, message: "ok" } as never);
    mockCheck.mockResolvedValue(nothing as never);
    mockWaiting.mockResolvedValue({ questions: [] } as never);
    mockAnswer.mockResolvedValue({ success: true, ready: 0, still_waiting: 0, continued: 0 } as never);
    mockPicture.mockResolvedValue(null as never);
    mockStart.mockClear();
    mockCheck.mockClear();
    mockAnswer.mockClear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const pressStart = async (user: ReturnType<typeof userEvent.setup>) => {
    render(<PendingTab onJobsChanged={vi.fn()} stats={stats} />);
    await user.click(await screen.findByRole("button", { name: /Start \(2 ready\)/ }));
  };

  it("checks the jobs about to run and starts at once when nothing is missing", async () => {
    const user = userEvent.setup();
    await pressStart(user);
    await waitFor(() => expect(mockStart).toHaveBeenCalledTimes(1));
    expect(mockCheck).toHaveBeenCalledWith(["https://jobs.example/ready-1", "https://jobs.example/ready-2"]);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("does not start when something required is missing, and says everything in one message", async () => {
    mockCheck.mockResolvedValue({ ok: false, jobs: 2, questions: [], problems: [
      { id: "ai_limit", message: "ChatGPT limit: 98% of the 5-hour limit is used, resets in about 2 hours.", required: true },
      { id: "gmail", message: "Gmail isn't connected (Settings → Accounts & email).", required: false },
    ] } as never);
    const user = userEvent.setup();
    await pressStart(user);
    const dialog = await screen.findByRole("dialog", { name: "The run can't start yet" });
    expect(within(dialog).getByText(/98% of the 5-hour limit is used/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Gmail isn't connected/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Start anyway" })).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(mockStart).not.toHaveBeenCalled();
  });

  it("names what would leave jobs waiting and starts only if the user says so", async () => {
    mockCheck.mockResolvedValue({ ok: false, jobs: 2, questions: [], problems: [
      { id: "captcha", message: "Your 2Captcha balance is empty (add funds at 2captcha.com).", required: false },
    ] } as never);
    const user = userEvent.setup();
    await pressStart(user);
    const dialog = await screen.findByRole("dialog", { name: "Before the run starts: something is missing" });
    expect(within(dialog).getByText(/2Captcha balance is empty/)).toBeInTheDocument();
    expect(mockStart).not.toHaveBeenCalled();                    // nothing started by itself
    await user.click(within(dialog).getByRole("button", { name: "Start anyway" }));
    await waitFor(() => expect(mockStart).toHaveBeenCalledTimes(1));
  });

  it("Cancel leaves everything as it was", async () => {
    mockCheck.mockResolvedValue({ ok: false, jobs: 2, questions: [], problems: [
      { id: "gmail", message: "Gmail isn't connected.", required: false }] } as never);
    const user = userEvent.setup();
    await pressStart(user);
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(mockStart).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("asks once what these jobs' sites will ask, saves the answer and then starts", async () => {
    mockCheck.mockResolvedValue({ ok: true, jobs: 2, problems: [], questions: [
      { question: SCHOOL, section: "", options: [], field_type: "text", jobs: 2,
        why: "tal.net sites ask every applicant about pre-university education" }] } as never);
    const user = userEvent.setup();
    await pressStart(user);
    const dialog = await screen.findByRole("dialog", { name: "Before you walk away" });
    expect(within(dialog).getByText(/2 of these jobs: tal.net sites ask every applicant/)).toBeInTheDocument();
    const save = within(dialog).getByRole("button", { name: "Save answers and start" });
    expect(save).toBeDisabled();                                 // nothing typed yet
    await user.type(within(dialog).getByLabelText(SCHOOL), "Springfield High School, US High School Diploma");
    await user.click(save);
    await waitFor(() => expect(mockStart).toHaveBeenCalledTimes(1));
    expect(mockAnswer).toHaveBeenCalledWith(SCHOOL, "Springfield High School, US High School Diploma");
  });

  it("can start without answering", async () => {
    mockCheck.mockResolvedValue({ ok: true, jobs: 2, problems: [], questions: [
      { question: SCHOOL, section: "", options: [], field_type: "text", jobs: 1, why: "this site asked it in an earlier run" }] } as never);
    const user = userEvent.setup();
    await pressStart(user);
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Start without answering" }));
    await waitFor(() => expect(mockStart).toHaveBeenCalledTimes(1));
    expect(mockAnswer).not.toHaveBeenCalled();
  });

  it("starts as before when the check itself can't be made", async () => {
    mockCheck.mockRejectedValue(new Error("API Error 500"));
    const user = userEvent.setup();
    await pressStart(user);
    await waitFor(() => expect(mockStart).toHaveBeenCalledTimes(1));
  });

  it("shows a question several jobs wait on once, and one answer goes to all of them", async () => {
    mockWaiting.mockResolvedValue({ questions: [
      { question: "What is your notice period in weeks?", section: "About you", options: [], field_type: "text",
        jobs: [{ url: "https://jobs.example/a", title: "A", company: "X" }, { url: "https://jobs.example/b", title: "B", company: "Y" }] },
      { question: "Only one job asks this", section: "", options: [], field_type: "text",
        jobs: [{ url: "https://jobs.example/c", title: "C", company: "Z" }] },
    ] } as never);
    mockAnswer.mockResolvedValue({ success: true, ready: 2, still_waiting: 0, continued: 0 } as never);
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={vi.fn()} stats={stats} />);
    const panel = await screen.findByRole("region", { name: "Questions your jobs are waiting on" });
    expect(within(panel).getByText("2 jobs wait on this")).toBeInTheDocument();
    expect(within(panel).queryByText("Only one job asks this")).not.toBeInTheDocument();     // that one is on its own card
    await user.type(within(panel).getByLabelText("What is your notice period in weeks?"), "2 weeks");
    await user.click(within(panel).getByRole("button", { name: "Answer for all" }));
    await waitFor(() => expect(mockAnswer).toHaveBeenCalledWith("What is your notice period in weeks?", "2 weeks"));
    expect(await screen.findByText("Saved. 2 jobs are ready to apply again.")).toBeInTheDocument();
  });

  it("a job that didn't finish offers the picture of its last page", async () => {
    mockGetJobs.mockResolvedValue([
      { ...ready(1), status: "failed", error: "Stopped at Review", screenshot_at: "2026-11-16T10:00:00+00:00" },
      { ...ready(2), status: "failed", error: "No picture for this one" },
    ] as never);
    mockPicture.mockResolvedValue(new Blob(["made-up picture"], { type: "image/jpeg" }) as never);
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => "blob:made-up"), revokeObjectURL: vi.fn() }));
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={vi.fn()} stats={stats} />);
    await screen.findByText("Ready Job 1");
    const buttons = screen.getAllByRole("button", { name: /^Last page of / });
    expect(buttons).toHaveLength(1);                             // only the job that has one
    await user.click(buttons[0]);
    const dialog = await screen.findByRole("dialog", { name: "Last page of Ready Job 1" });
    expect(mockPicture).toHaveBeenCalledWith("https://jobs.example/ready-1");
    expect(await within(dialog).findByRole("img")).toHaveAttribute("src", "blob:made-up");
  });

  it("says so when no picture is kept any more", async () => {
    mockGetJobs.mockResolvedValue([
      { ...ready(1), status: "needs_you", error: "Needs you", screenshot_at: "2026-11-16T10:00:00+00:00" }] as never);
    mockPicture.mockResolvedValue(null as never);
    const user = userEvent.setup();
    render(<PendingTab onJobsChanged={vi.fn()} stats={stats} />);
    await user.click(await screen.findByRole("button", { name: /^Last page of / }));
    expect(await screen.findByText("No picture of this job's last page is kept.")).toBeInTheDocument();
  });
});
