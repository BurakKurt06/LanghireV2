import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import JobLogDialog from "./JobLogDialog";
import { getJobLogs } from "../lib/api";

vi.mock("../lib/api", () => ({ getJobLogs: vi.fn() }));

function Opener() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>View log</button>
      {open && <JobLogDialog url="https://jobs.example/1" title="Data Intern" onClose={() => setOpen(false)} />}
    </>
  );
}

describe("JobLogDialog (a job card's View log)", () => {
  beforeEach(() => {
    vi.mocked(getJobLogs).mockResolvedValue([
      { id: 1, run_id: "r1", job_url: "https://jobs.example/1", timestamp: "2026-11-01T10:00:00Z", level: "INFO",
        message: "Opened the application form", created_at: "2026-11-01T10:00:00Z" },
    ] as never);
  });

  it("takes the keyboard focus, keeps Tab inside, closes on Esc and gives the focus back", async () => {
    const user = userEvent.setup();
    render(<Opener />);
    const opener = screen.getByRole("button", { name: "View log" });
    await user.click(opener);
    const dialog = await screen.findByRole("dialog", { name: "Log for Data Intern" });
    expect(await screen.findByText("Opened the application form")).toBeInTheDocument();
    const close = screen.getByRole("button", { name: "Close" });
    expect(close).toHaveFocus();                        // the only control in it
    await user.tab();
    expect(close).toHaveFocus();                        // Tab stays inside the dialog
    expect(dialog).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(opener).toHaveFocus();
  });

  it("says when there is no log yet, and when it can't be loaded", async () => {
    vi.mocked(getJobLogs).mockResolvedValueOnce([] as never);
    const { unmount } = render(<JobLogDialog url="https://jobs.example/2" title="Risk Intern" onClose={() => {}} />);
    expect(await screen.findByText(/No log for this job yet/)).toBeInTheDocument();
    unmount();
    vi.mocked(getJobLogs).mockRejectedValueOnce(new Error("down"));
    render(<JobLogDialog url="https://jobs.example/3" title="Risk Intern" onClose={() => {}} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load this job's log");
  });
});
