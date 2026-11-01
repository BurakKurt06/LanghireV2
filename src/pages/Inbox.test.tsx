import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Inbox from "./Inbox";
import { getInbox, checkInbox, setInboxSchedule, makePrepSheet } from "../lib/api";
import type { InboxStatus } from "../lib/types";
import { INBOX_SCHEDULE_LABELS } from "../lib/inbox";

vi.mock("../lib/api", () => ({
  getInbox: vi.fn(),
  checkInbox: vi.fn(async () => ({ success: true })),
  setInboxSchedule: vi.fn(async () => ({ success: true })),
  makePrepSheet: vi.fn(async () => ({ success: true, prep: "THE INTERVIEW" })),
}));

const mockGetInbox = vi.mocked(getInbox);

const base: InboxStatus = {
  checking: false, requested: false, status: "", error: "", schedule: "off", last_checked: null, busy: false,
  performance_mode: false,
  emails: [
    { id: "a", date: "2026-10-08T11:05", sender: "Stark Global", sender_email: "r@stark.example", subject: "Interview invitation",
      snippet: "", category: "interview", company: "Stark Global", role: "Analytics Intern",
      gmail_url: "https://mail.google.com/mail/u/0/#all/a", job_url: "https://stark/q" },
    { id: "b", date: "2026-10-07T09:30", sender: "Northwind", sender_email: "r@northwind.example", subject: "Update on your application",
      snippet: "", category: "rejected", company: "Northwind", role: "", gmail_url: "https://mail.google.com/mail/u/0/#all/b", job_url: null },
  ],
};

describe("Inbox", () => {
  beforeEach(() => {
    mockGetInbox.mockResolvedValue(structuredClone(base) as never);
  });

  it("lists sorted emails with filters and Gmail links", async () => {
    const user = userEvent.setup();
    render(<Inbox />);
    expect(await screen.findByText("Stark Global")).toBeInTheDocument();
    expect(screen.getByText("Northwind")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Interview (1)" })).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Open in Gmail" })[0]).toHaveAttribute("href", base.emails[0].gmail_url);
    await user.click(screen.getByRole("button", { name: "Rejected (1)" }));
    expect(screen.queryByText("Stark Global")).not.toBeInTheDocument();
    expect(screen.getByText("Northwind")).toBeInTheDocument();
  });

  it("checks now, and shows the automatic schedule with a link to change it in Settings", async () => {
    const user = userEvent.setup();
    render(<Inbox />);
    await user.click(await screen.findByRole("button", { name: /Check now/ }));
    expect(checkInbox).toHaveBeenCalled();
    // The schedule is changed in Settings → Accounts & email (tested in Settings.test.tsx).
    expect(screen.queryByRole("combobox", { name: "Check automatically" })).not.toBeInTheDocument();
    expect(screen.getByText(/Check automatically:/).parentElement).toHaveTextContent(INBOX_SCHEDULE_LABELS[base.schedule]);
    expect(screen.getByRole("link", { name: "Change in Settings" })).toHaveAttribute("href", "#/settings?section=accounts");
    expect(setInboxSchedule).not.toHaveBeenCalled();
  });

  it("shows progress and errors", async () => {
    mockGetInbox.mockResolvedValue({ ...structuredClone(base), checking: true, status: "Reading Gmail…" } as never);
    render(<Inbox />);
    expect(await screen.findByRole("status")).toHaveTextContent("Reading Gmail…");
    expect(screen.getByRole("button", { name: /Checking/ })).toBeDisabled();
  });

  it("makes a prep sheet only for interviews and offers, only when asked", async () => {
    const user = userEvent.setup();
    render(<Inbox />);
    const buttons = await screen.findAllByRole("button", { name: /Make prep sheet/ });
    expect(buttons).toHaveLength(1);               // the interview, not the rejection
    expect(makePrepSheet).not.toHaveBeenCalled();
    await user.click(buttons[0]);
    await waitFor(() => expect(makePrepSheet).toHaveBeenCalledWith("a"));
  });

  it("hides prep sheets in performance mode", async () => {
    mockGetInbox.mockResolvedValue({ ...structuredClone(base), performance_mode: true } as never);
    render(<Inbox />);
    await screen.findByText("Stark Global");
    expect(screen.queryByRole("button", { name: /Make prep sheet/ })).not.toBeInTheDocument();
  });

  it("opens a saved prep sheet", async () => {
    const withPrep = structuredClone(base);
    withPrep.emails[0].prep = "THE INTERVIEW\n- 30 minutes";
    mockGetInbox.mockResolvedValue(withPrep as never);
    const user = userEvent.setup();
    render(<Inbox />);
    await user.click(await screen.findByRole("button", { name: /Prep sheet/ }));
    expect(screen.getByText(/30 minutes/)).toBeInTheDocument();
  });
});
