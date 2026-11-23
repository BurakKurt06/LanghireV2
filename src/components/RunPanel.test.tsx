import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import RunPanel from "./RunPanel";
import {
  getApplyStatus,
  pauseApplying,
  resumeApplying,
  stopApplying,
  skipJob,
  answerHelp,
  reviewApplication,
  decideBrowserRequests,
  stopJobCollection,
  continueAfterLimit,
} from "../lib/api";

vi.mock("../lib/api", () => ({
  continueAfterLimit: vi.fn(async () => ({ success: true })),
  getApplyStatus: vi.fn(),
  pauseApplying: vi.fn(async () => ({ success: true, paused: true })),
  resumeApplying: vi.fn(async () => ({ success: true, paused: false })),
  stopApplying: vi.fn(async () => ({ success: true })),
  skipJob: vi.fn(async () => ({ success: true })),
  answerHelp: vi.fn(async () => ({ success: true })),
  reviewApplication: vi.fn(async () => ({ success: true })),
  decideBrowserRequests: vi.fn(async () => ({ success: true })),
  stopJobCollection: vi.fn(async () => ({ success: true })),
}));

const mockStatus = vi.mocked(getApplyStatus);
const JOB = {
  worker: 1, url: "https://jobs.example/1", title: "Data Intern", company: "Contoso",
  step: 12, max_steps: 100, started_at: new Date(Date.now() - 95_000).toISOString(),
};
const running = (extra = {}) => ({ running: true, mode: "all", workers: 1, log: [], error: null, finished_at: null,
  run_mode: "auto", current: [JOB], ...extra });

describe("RunPanel", () => {
  const requestRow = { id: "a".repeat(32), job_url: JOB.url, title: JOB.title, company: JOB.company,
    mode: "trial", kind: "credentials", origin: "https://login.careers.example", tenant: "acme",
    url: "https://login.careers.example/acme/sign-in", method: "", body: "",
    description: "Allow account details on this exact site and tenant for this job?" };
  beforeEach(() => {
    mockStatus.mockResolvedValue(running() as never);
  });

  it("shows nothing when no run is going", async () => {
    mockStatus.mockResolvedValue({ running: false } as never);
    const { container } = render(<RunPanel />);
    await waitFor(() => expect(mockStatus).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  // Decision 1: approval prompts were removed, so a site asking for account details shows no
  // Allow/Block card any more; the run panel only shows the running job.
  it("shows no account-details approval card for a site asking for account details", async () => {
    mockStatus.mockResolvedValue(running({ requests: [requestRow] }) as never);
    render(<RunPanel />);
    expect(await screen.findByText("Data Intern")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Review https://login.careers.example" })).not.toBeInTheDocument();
    expect(screen.queryByText("Tenant: acme")).not.toBeInTheDocument();
    expect(screen.queryByText("https://login.careers.example/acme/sign-in")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Allow account details" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Block" })).not.toBeInTheDocument();
    expect(decideBrowserRequests).not.toHaveBeenCalled();
  });

  // Decision 1: collection approvals are no longer shown in the run panel; it renders nothing
  // when no application run is going (collection has its own Stop on the Jobs page).
  it("shows nothing for collection requests when no application is running", async () => {
    mockStatus.mockResolvedValue(running({ running: false, requests: [requestRow], current: [] }) as never);
    const { container } = render(<RunPanel />);
    await waitFor(() => expect(mockStatus).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("region", { name: "Account details approval" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
    expect(stopJobCollection).not.toHaveBeenCalled();
  });

  it("shows the current job, mode, step and time", async () => {
    // Started 95 s before this render (JOB's own time is from when the file loaded).
    const job = { ...JOB, started_at: new Date(Date.now() - 95_000).toISOString() };
    mockStatus.mockResolvedValue(running({ current: [job] }) as never);
    render(<RunPanel />);
    expect(await screen.findByText("Data Intern")).toBeInTheDocument();
    expect(screen.getByText("Contoso")).toBeInTheDocument();
    expect(screen.getByText("Full auto")).toBeInTheDocument();
    expect(screen.getByText("Step 12")).toBeInTheDocument();
    expect(screen.getByText(/^1:3\d$/)).toBeInTheDocument();
    expect(screen.getByText("Applying")).toBeInTheDocument();
  });

  it("pauses, skips this job and stops", async () => {
    const user = userEvent.setup();
    render(<RunPanel />);
    await user.click(await screen.findByRole("button", { name: /Pause/ }));
    expect(pauseApplying).toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Skip this job" }));
    expect(skipJob).toHaveBeenCalledWith("https://jobs.example/1");
    await user.click(screen.getByRole("button", { name: "Stop" }));
    expect(stopApplying).toHaveBeenCalled();
  });

  it("resumes when paused", async () => {
    mockStatus.mockResolvedValue(running({ paused: true }) as never);
    const user = userEvent.setup();
    render(<RunPanel />);
    expect(await screen.findByText("Paused")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Resume/ }));
    expect(resumeApplying).toHaveBeenCalled();
    expect(pauseApplying).not.toHaveBeenCalled();
  });

  // Decision 1: the "You stepped in" note (current[].user_active) replaces the old flow; the
  // agent waits, and the Pause button becomes Resume.
  it("says when the user stepped in and offers Resume instead of Pause", async () => {
    mockStatus.mockResolvedValue(running({ current: [{ ...JOB, user_active: true }] }) as never);
    const user = userEvent.setup();
    render(<RunPanel />);
    expect(await screen.findByText("Waiting for you")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/^You stepped in, so the agent is waiting\./);
    expect(screen.queryByRole("button", { name: /Pause/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Resume/ }));
    expect(resumeApplying).toHaveBeenCalled();
    expect(pauseApplying).not.toHaveBeenCalled();
  });

  // Decision 1: current[].note (what the user's own activity led to) is shown on the job.
  it("shows the note about what the user's activity led to", async () => {
    mockStatus.mockResolvedValue(running({ current: [{ ...JOB, note: "Continuing from your page, keeping your 3 edits." }] }) as never);
    render(<RunPanel />);
    expect(await screen.findByRole("status")).toHaveTextContent("Continuing from your page, keeping your 3 edits.");
    expect(screen.getByText("Applying")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Pause/ })).toBeInTheDocument();
  });

  // Decision 1: answerHelp has a 4th argument (the answers) and "Give up" became "Next job".
  it("answers a help request with a message, or moves on to the next job", async () => {
    mockStatus.mockResolvedValue(running({ run_mode: "trial", help: [{ url: JOB.url, title: "Data Intern", company: "Contoso",
      problem: "The phone field keeps rejecting the number." }] }) as never);
    const user = userEvent.setup();
    render(<RunPanel />);
    expect(await screen.findByText(/phone field keeps rejecting/)).toBeInTheDocument();
    expect(screen.getByText("Trial run")).toBeInTheDocument();
    expect(screen.getByText("Needs you")).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "Message to the agent" }), "Use 555 123 4567");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(answerHelp).toHaveBeenCalledWith(JOB.url, "Use 555 123 4567", false, {});
    expect(screen.queryByRole("button", { name: "Give up" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next job" }));
    expect(answerHelp).toHaveBeenCalledWith(JOB.url, "", true, {});
  });

  // Decision 1: questions are shown whole (section, full text, choices) and the answers go as answerHelp's 4th argument.
  it("shows each question whole and sends only the given answers", async () => {
    const authQuestion = "Are you legally allowed to work in the country of this job without visa sponsorship?";
    const whyQuestion = "Why do you want this internship?";
    mockStatus.mockResolvedValue(running({ help: [{ url: JOB.url, title: "Data Intern", company: "Contoso", kind: "questions",
      problem: "Two questions have no saved answer.", seconds_left: 125,
      questions: [
        { section: "Work authorization", question: authQuestion, options: ["Yes", "No", ""] },
        { question: whyQuestion },
      ] }] }) as never);
    const user = userEvent.setup();
    render(<RunPanel />);
    expect(await screen.findByText("Your answer is needed")).toBeInTheDocument();
    expect(screen.getByText("Work authorization")).toBeInTheDocument();
    const select = screen.getByRole("combobox", { name: authQuestion });
    // Review 2026-11-12: any list of choices also takes a typed answer (a choice the list leaves out).
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["Choose…", "Yes", "No", "Something else (type it)"]);
    expect(screen.getByRole("textbox", { name: whyQuestion })).toBeInTheDocument();
    // ...and the optional message stays, for what a field can't hold (review 2026-11-12).
    expect(screen.getByRole("textbox", { name: "Message to the agent" })).toBeInTheDocument();
    expect(screen.queryByText("Two questions have no saved answer.")).not.toBeInTheDocument();
    expect(screen.getByText(/moves on to the next job in 2:05\./)).toBeInTheDocument();

    const answer = screen.getByRole("button", { name: "Answer and continue" });
    expect(answer).toBeDisabled();
    await user.selectOptions(select, "Yes");
    expect(answer).toBeEnabled();
    await user.click(answer);
    expect(answerHelp).toHaveBeenCalledTimes(1);
    expect(answerHelp).toHaveBeenCalledWith(JOB.url, "", false, { [authQuestion]: "Yes" });
  });

  it("a tick-all-that-apply question gets tick boxes; a choice the list leaves out can be typed", async () => {
    const which = "Which of these locations would you work in?";
    const office = "Which office is closest to you?";
    mockStatus.mockResolvedValue(running({ help: [{ url: JOB.url, title: "Data Intern", company: "Contoso", kind: "questions",
      problem: "", questions: [
        { question: which, field_type: "checkbox", options: ["New York", "Chicago", "Remote"] },
        { question: office, field_type: "dropdown", options: ["Boston", "Austin"] },
      ] }] }) as never);
    const user = userEvent.setup();
    render(<RunPanel />);
    const group = await screen.findByRole("group", { name: which });
    await user.click(within(group).getByRole("checkbox", { name: "Remote" }));
    await user.click(within(group).getByRole("checkbox", { name: "New York" }));
    await user.selectOptions(screen.getByRole("combobox", { name: office }), "Something else (type it)");
    await user.type(screen.getByRole("textbox", { name: `Your answer: ${office}` }), "Denver");
    await user.click(screen.getByRole("button", { name: "Answer and continue" }));
    // Ticked choices in the form's own order; the typed answer as typed.
    expect(answerHelp).toHaveBeenCalledWith(JOB.url, "", false, { [which]: "New York, Remote", [office]: "Denver" });
  });

  it("a message alone can answer: 'I filled it in myself'", async () => {
    const which = "Which of these apply to you?";
    mockStatus.mockResolvedValue(running({ help: [{ url: JOB.url, title: "Data Intern", company: "Contoso", kind: "questions",
      problem: "", questions: [{ question: which, field_type: "checkbox", options: ["A", "B"] }] }] }) as never);
    const user = userEvent.setup();
    render(<RunPanel />);
    const answer = await screen.findByRole("button", { name: "Answer and continue" });
    expect(answer).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "Message to the agent" }), "I ticked both myself");
    expect(answer).toBeEnabled();
    await user.click(answer);
    expect(answerHelp).toHaveBeenCalledWith(JOB.url, "I ticked both myself", false, {});
  });

  // Decision 1: a CAPTCHA help request asks the user to solve it in the browser (no message box).
  it("asks the user to solve a CAPTCHA in the browser", async () => {
    mockStatus.mockResolvedValue(running({ help: [{ url: JOB.url, title: "Data Intern", company: "Contoso", kind: "captcha",
      problem: "The site shows a picture puzzle." }] }) as never);
    const user = userEvent.setup();
    render(<RunPanel />);
    expect(await screen.findByText("Solve the CAPTCHA in the browser")).toBeInTheDocument();
    expect(screen.getByText("The site shows a picture puzzle.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByText(/moves on to the next job in/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "I solved it" }));
    expect(answerHelp).toHaveBeenCalledWith(JOB.url, "", false, {});
  });

  // Decision 1: a verification help request has a code box and Continue sends the code.
  it("sends a verification code", async () => {
    mockStatus.mockResolvedValue(running({ help: [{ url: JOB.url, title: "Data Intern", company: "Contoso", kind: "verification",
      problem: "The site sent a code to your email.", seconds_left: 59 }] }) as never);
    const user = userEvent.setup();
    render(<RunPanel />);
    expect(await screen.findByText("Verification code needed")).toBeInTheDocument();
    expect(screen.getByText(/moves on to the next job in 0:59\./)).toBeInTheDocument();
    const code = screen.getByRole("textbox", { name: "Verification code" });
    expect(code).toHaveAttribute("placeholder", "Type the code from the email");
    await user.type(code, "246810");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(answerHelp).toHaveBeenCalledWith(JOB.url, "246810", false, {});
  });

  // Decision 1: Trial "review" approve/skip cards were removed; Trial goes on without a Submit prompt.
  it("shows no trial review card", async () => {
    mockStatus.mockResolvedValue(running({ run_mode: "trial", reviews: [{ url: JOB.url, title: "Data Intern", company: "Contoso", summary: "All set" }] }) as never);
    render(<RunPanel />);
    expect(await screen.findByText("Trial run")).toBeInTheDocument();
    expect(screen.queryByText("All set")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Submit$/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Skip$/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Skip this job" })).toBeInTheDocument();
    expect(reviewApplication).not.toHaveBeenCalled();
  });

  it("lists every job running at the same time, each with its own Skip", async () => {
    const second = { ...JOB, worker: 2, url: "https://jobs.example/2", title: "Risk Intern", company: "Northwind", step: 3 };
    mockStatus.mockResolvedValue(running({ workers: 2, current: [JOB, second],
      help: [{ url: second.url, problem: "Which office?" }] }) as never);
    const user = userEvent.setup();
    render(<RunPanel />);
    expect(await screen.findByText("Data Intern")).toBeInTheDocument();
    expect(screen.getByText("Risk Intern")).toBeInTheDocument();
    expect(screen.getByText("Step 3")).toBeInTheDocument();
    expect(screen.getByText(/Needs you: Risk Intern/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Skip Risk Intern" }));
    expect(skipJob).toHaveBeenCalledWith("https://jobs.example/2");
  });

  // The AI plan's limit: the run says why it waits or stopped starting jobs, and keeps saying
  // why it ended after it is over (the user walked away).
  const LIMIT = "Stopped: ChatGPT limit: 98% of the 5-hour limit is used and the next job needs about 5%, resets in about 2 hours";

  it("says while running why no further job is starting", async () => {
    mockStatus.mockResolvedValue(running({ notice: { text: LIMIT, kind: "limit_stop" } }) as never);
    render(<RunPanel />);
    expect(await screen.findByText(`${LIMIT}. Jobs already running finish; the rest stay ready.`)).toBeInTheDocument();
    expect(screen.getByText("Data Intern")).toBeInTheDocument();
  });

  // A full plan pauses the run (the user's wish, 2026-11-20): a countdown to the reset, the run
  // goes on by itself then, and Continue now for a limit that was reset early.
  const WAIT = "Waiting: ChatGPT limit: 98% of the 5-hour limit is used and the next job needs about 5%, resets in about 2 hours";

  it("while the run waits for the limit it counts down to the reset and offers Continue now", async () => {
    const resetsAt = Math.floor(Date.now() / 1000) + 2 * 3600 + 5 * 60 + 30;
    mockStatus.mockResolvedValue(running({ current: [], notice: { text: WAIT, kind: "limit_wait", resets_at: resetsAt } }) as never);
    const user = userEvent.setup();
    render(<RunPanel />);
    const box = await screen.findByRole("status", { name: "Waiting for the ChatGPT limit" });
    expect(within(box).getByText(/^Waiting: ChatGPT limit: 98%/)).toBeInTheDocument();
    expect(within(box).getByText(/^2 h 5 min$/)).toBeInTheDocument();
    expect(screen.getByText("Waiting for the ChatGPT limit", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
    await user.click(within(box).getByRole("button", { name: "Continue now" }));
    expect(continueAfterLimit).toHaveBeenCalled();
  });

  it("a weekly limit is counted down in days", async () => {
    const resetsAt = Math.floor(Date.now() / 1000) + 3 * 86400 + 4 * 3600 + 120;
    mockStatus.mockResolvedValue(running({ current: [], notice: { text: WAIT, kind: "limit_wait", resets_at: resetsAt } }) as never);
    render(<RunPanel />);
    const box = await screen.findByRole("status", { name: "Waiting for the ChatGPT limit" });
    expect(within(box).getByText(/^3 d 4 h$/)).toBeInTheDocument();
  });

  it("control: a run with room on the plan shows no limit box and no Continue now", async () => {
    render(<RunPanel />);
    expect(await screen.findByText("Data Intern")).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "Waiting for the ChatGPT limit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Continue now" })).not.toBeInTheDocument();
  });

  it("keeps the reason on screen after a run the limit stopped", async () => {
    mockStatus.mockResolvedValue({ running: false, mode: null, workers: 0, log: [], error: null, finished_at: null,
      notice: { text: LIMIT, kind: "limit_stop" } } as never);
    render(<RunPanel />);
    const section = await screen.findByRole("region", { name: "Why the last run stopped" });
    expect(within(section).getByText(`${LIMIT}. The jobs that weren't started are still ready.`)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
  });

  it("control: after an ordinary run, or one that only waited, nothing is shown", async () => {
    for (const notice of [{ text: "", kind: "" }, { text: "Waiting: ChatGPT limit reached", kind: "limit_wait" }, undefined]) {
      mockStatus.mockResolvedValue({ running: false, mode: null, workers: 0, log: [], error: null, finished_at: null, notice } as never);
      const { container, unmount } = render(<RunPanel />);
      await waitFor(() => expect(mockStatus).toHaveBeenCalled());
      expect(container).toBeEmptyDOMElement();
      unmount();
    }
  });
});
