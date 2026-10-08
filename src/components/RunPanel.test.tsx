import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
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
} from "../lib/api";

vi.mock("../lib/api", () => ({
  getApplyStatus: vi.fn(),
  pauseApplying: vi.fn(async () => ({ success: true, paused: true })),
  resumeApplying: vi.fn(async () => ({ success: true, paused: false })),
  stopApplying: vi.fn(async () => ({ success: true })),
  skipJob: vi.fn(async () => ({ success: true })),
  answerHelp: vi.fn(async () => ({ success: true })),
  reviewApplication: vi.fn(async () => ({ success: true })),
}));

const mockStatus = vi.mocked(getApplyStatus);
const JOB = {
  worker: 1, url: "https://jobs.example/1", title: "Data Intern", company: "Contoso",
  step: 12, max_steps: 100, started_at: new Date(Date.now() - 95_000).toISOString(),
};
const running = (extra = {}) => ({ running: true, mode: "all", workers: 1, log: [], error: null, finished_at: null,
  run_mode: "auto", current: [JOB], ...extra });

describe("RunPanel", () => {
  beforeEach(() => {
    mockStatus.mockResolvedValue(running() as never);
  });

  it("shows nothing when no run is going", async () => {
    mockStatus.mockResolvedValue({ running: false } as never);
    const { container } = render(<RunPanel />);
    await waitFor(() => expect(mockStatus).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the current job, mode, step and time", async () => {
    render(<RunPanel />);
    expect(await screen.findByText("Data Intern")).toBeInTheDocument();
    expect(screen.getByText("Contoso")).toBeInTheDocument();
    expect(screen.getByText("Full auto")).toBeInTheDocument();
    expect(screen.getByText("Step 12")).toBeInTheDocument();
    expect(screen.getByText(/^1:3\d$/)).toBeInTheDocument();
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
  });

  it("answers a help request with a message, or gives up", async () => {
    mockStatus.mockResolvedValue(running({ run_mode: "trial", help: [{ url: JOB.url, title: "Data Intern", company: "Contoso",
      problem: "The phone field keeps rejecting the number." }] }) as never);
    const user = userEvent.setup();
    render(<RunPanel />);
    expect(await screen.findByText(/phone field keeps rejecting/)).toBeInTheDocument();
    expect(screen.getByText("Trial run")).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "Message to the agent" }), "Use 555 123 4567");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(answerHelp).toHaveBeenCalledWith(JOB.url, "Use 555 123 4567", false);
    await user.click(screen.getByRole("button", { name: "Give up" }));
    expect(answerHelp).toHaveBeenCalledWith(JOB.url, "", true);
  });

  it("approves or skips a trial review", async () => {
    mockStatus.mockResolvedValue(running({ reviews: [{ url: JOB.url, title: "Data Intern", company: "Contoso", summary: "All set" }] }) as never);
    const user = userEvent.setup();
    render(<RunPanel />);
    await user.click(await screen.findByRole("button", { name: /Approve/ }));
    expect(reviewApplication).toHaveBeenCalledWith(JOB.url, true);
    await user.click(screen.getByRole("button", { name: /^Skip$/ }));
    expect(reviewApplication).toHaveBeenCalledWith(JOB.url, false);
  });
});
