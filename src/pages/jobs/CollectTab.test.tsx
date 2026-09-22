import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CollectTab from "./CollectTab";
import {
  startJobCollection,
  stopJobCollection,
  getCollectionStatus,
  getPlugins,
  getProfile,
  addJob,
  addJobsBulk,
  getSettings,
} from "../../lib/api";
import { trackEvent } from "../../lib/analytics";

// i18n: identity translator so assertions can use raw keys.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { changeLanguage: vi.fn() } }),
}));

vi.mock("../../lib/analytics", () => ({ trackEvent: vi.fn() }));

vi.mock("../../lib/api", () => ({
  startJobCollection: vi.fn(),
  stopJobCollection: vi.fn(),
  getCollectionStatus: vi.fn(),
  getPlugins: vi.fn(),
  getProfile: vi.fn(),
  addJob: vi.fn(),
  addJobsBulk: vi.fn(),
  getSettings: vi.fn(),
}));

const mockStart = vi.mocked(startJobCollection);
const mockStop = vi.mocked(stopJobCollection);
const mockStatus = vi.mocked(getCollectionStatus);
const mockGetPlugins = vi.mocked(getPlugins);
const mockGetProfile = vi.mocked(getProfile);
const mockAddJob = vi.mocked(addJob);
const mockAddJobsBulk = vi.mocked(addJobsBulk);
const mockGetSettings = vi.mocked(getSettings);
const mockTrackEvent = vi.mocked(trackEvent);

const idleStatus = {
  running: false,
  title: null,
  log: [],
  collected: 0,
  max_jobs: 0,
  error: null,
  finished_at: null,
};

const linkedinPlugin = {
  name: "linkedin",
  display_name: "LinkedIn",
  version: "1.0.0",
  author: "core",
  description: "LinkedIn",
  countries: ["US"],
  website: "x",
  requires_login: true,
  login_url: "x",
  is_builtin: true,
  enabled: true,
  filters: [],
};
const indeedPlugin = { ...linkedinPlugin, name: "indeed", display_name: "Indeed" };

describe("CollectTab", () => {
  let onJobsChanged: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.stubGlobal("alert", vi.fn());
    onJobsChanged = vi.fn();
    mockGetProfile.mockResolvedValue({ country: "US" } as never);
    mockGetPlugins.mockResolvedValue({
      success: true,
      plugins: [structuredClone(linkedinPlugin), structuredClone(indeedPlugin)],
    } as never);
    mockStatus.mockResolvedValue(structuredClone(idleStatus) as never);
    mockStart.mockResolvedValue({ success: true, message: "started" } as never);
    mockStop.mockResolvedValue({ success: true } as never);
    mockAddJob.mockResolvedValue({ success: true } as never);
    mockGetSettings.mockResolvedValue({ job_collection_enabled: true } as never);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the collector panel and loads source plugins", async () => {
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    expect(screen.getByText("collector.title")).toBeInTheDocument();
    await waitFor(() => expect(mockGetProfile).toHaveBeenCalled());
    // >1 source → source selector buttons render.
    expect(await screen.findByRole("checkbox", { name: "LinkedIn" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Indeed" })).toBeInTheDocument();
  });

  it("opens the confirm dialog and starts collection on confirm", async () => {
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await screen.findByRole("checkbox", { name: "LinkedIn" });

    await user.type(screen.getByPlaceholderText("collector.jobTitlePlaceholder"), "Engineer");
    await user.click(screen.getByRole("button", { name: /collector\.start/ }));

    // AutomationDialog appears.
    const confirm = await screen.findByRole("button", { name: "Got it, Start" });
    await user.click(confirm);

    await waitFor(() =>
      expect(mockStart).toHaveBeenCalledWith("Engineer", 20, "linkedin", {}, ["linkedin"]),
    );
    expect(mockTrackEvent).toHaveBeenCalledWith(
      "collection_started",
      expect.objectContaining({ source: "linkedin" }),
    );
  });

  it("alerts when starting collection rejects", async () => {
    mockStart.mockRejectedValue(new Error("no login"));
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await screen.findByRole("checkbox", { name: "LinkedIn" });

    await user.click(screen.getByRole("button", { name: /collector\.start/ }));
    await user.click(await screen.findByRole("button", { name: "Got it, Start" }));

    await waitFor(() => expect(window.alert).toHaveBeenCalledWith("no login"));
  });

  it("selects LinkedIn by default", async () => {
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    const linkedin = await screen.findByRole("checkbox", { name: "LinkedIn" });
    expect(linkedin).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("checkbox", { name: "Indeed" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
  });

  it("adds a second source without deselecting the first", async () => {
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    const indeed = await screen.findByRole("checkbox", { name: "Indeed" });
    await user.click(indeed);
    await waitFor(() => expect(indeed).toHaveAttribute("aria-checked", "true"));
    // Multi-select: picking Indeed must not turn LinkedIn off.
    expect(screen.getByRole("checkbox", { name: "LinkedIn" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("deselects a source when clicked again", async () => {
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    const linkedin = await screen.findByRole("checkbox", { name: "LinkedIn" });
    await user.click(linkedin);
    await waitFor(() => expect(linkedin).toHaveAttribute("aria-checked", "false"));
  });

  it("disables Start when no source is selected", async () => {
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    const linkedin = await screen.findByRole("checkbox", { name: "LinkedIn" });
    await user.click(linkedin); // the only selected source, now off
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /start/i })).toBeDisabled(),
    );
  });

  it("sends every selected source to the backend", async () => {
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await user.click(await screen.findByRole("checkbox", { name: "Indeed" }));
    await user.click(screen.getByRole("button", { name: /collector\.start/ }));
    await user.click(await screen.findByRole("button", { name: "Got it, Start" }));
    await waitFor(() => expect(mockStart).toHaveBeenCalled());
    // 5th argument is the full source list, in selection order.
    expect(mockStart.mock.calls[0][4]).toEqual(["linkedin", "indeed"]);
  });

  // ── Bulk paste / file upload ──────────────────────────────────────────
  const bulkOk = (over = {}) => ({
    success: true,
    added: 2,
    skipped_duplicate: 0,
    skipped_invalid: 0,
    invalid_samples: [],
    total_submitted: 2,
    ...over,
  });

  it("sends pasted links to the bulk endpoint", async () => {
    mockAddJobsBulk.mockResolvedValue(bulkOk() as never);
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);

    await user.click(screen.getByRole("button", { name: /Paste Links/ }));
    const box = screen.getByLabelText("Job links, one per line");
    await user.type(box, "https://a.example/1{Enter}https://b.example/2");
    await user.click(screen.getByRole("button", { name: /Add Links/ }));

    await waitFor(() => expect(mockAddJobsBulk).toHaveBeenCalled());
    expect(mockAddJobsBulk.mock.calls[0][0]).toContain("https://a.example/1");
    expect(onJobsChanged).toHaveBeenCalled();
  });

  it("reports added and skipped counts back to the user", async () => {
    mockAddJobsBulk.mockResolvedValue(
      bulkOk({ added: 3, skipped_duplicate: 2, skipped_invalid: 1, invalid_samples: ["junk"], total_submitted: 6 }) as never,
    );
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);

    await user.click(screen.getByRole("button", { name: /Paste Links/ }));
    await user.type(screen.getByLabelText("Job links, one per line"), "https://a.example/1");
    await user.click(screen.getByRole("button", { name: /Add Links/ }));

    expect(await screen.findByText(/skipped 2 duplicates/)).toBeInTheDocument();
    expect(screen.getByText(/skipped 1 invalid/)).toBeInTheDocument();
    expect(screen.getByText(/junk/)).toBeInTheDocument();
  });

  it("disables Add Links until something is typed", async () => {
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await user.click(screen.getByRole("button", { name: /Paste Links/ }));
    expect(screen.getByRole("button", { name: /Add Links/ })).toBeDisabled();
  });

  it("uploads a file of links and submits its contents", async () => {
    mockAddJobsBulk.mockResolvedValue(bulkOk() as never);
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await user.click(screen.getByRole("button", { name: /Paste Links/ }));

    const file = new File(
      ["https://a.example/1\nhttps://b.example/2\n"],
      "jobs.txt",
      { type: "text/plain" },
    );
    await user.upload(screen.getByTestId("bulk-file-input") as HTMLInputElement, file);

    await waitFor(() => expect(mockAddJobsBulk).toHaveBeenCalled());
    expect(mockAddJobsBulk.mock.calls[0][0]).toContain("https://b.example/2");
  });

  it("rejects an oversized file without uploading it", async () => {
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await user.click(screen.getByRole("button", { name: /Paste Links/ }));

    // 300 KB, over the 200 KB cap the backend enforces.
    const big = new File(["x".repeat(300_000)], "huge.txt", { type: "text/plain" });
    await user.upload(screen.getByTestId("bulk-file-input") as HTMLInputElement, big);

    await waitFor(() => expect(window.alert).toHaveBeenCalled());
    expect(mockAddJobsBulk).not.toHaveBeenCalled();
  });

  it("alerts when the bulk add fails", async () => {
    mockAddJobsBulk.mockRejectedValue(new Error("backend down"));
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await user.click(screen.getByRole("button", { name: /Paste Links/ }));
    await user.type(screen.getByLabelText("Job links, one per line"), "https://a.example/1");
    await user.click(screen.getByRole("button", { name: /Add Links/ }));
    await waitFor(() => expect(window.alert).toHaveBeenCalledWith("backend down"));
  });

  // ── Job board collection switch ───────────────────────────────────────
  it("disables Start and explains when collection is switched off", async () => {
    mockGetSettings.mockResolvedValue({ job_collection_enabled: false } as never);
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    expect(await screen.findByText(/turned/)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /collector\.start/ })).toBeDisabled(),
    );
  });

  it("still allows pasting links when collection is switched off", async () => {
    mockGetSettings.mockResolvedValue({ job_collection_enabled: false } as never);
    mockAddJobsBulk.mockResolvedValue(bulkOk() as never);
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);

    await user.click(screen.getByRole("button", { name: /Paste Links/ }));
    await user.type(screen.getByLabelText("Job links, one per line"), "https://a.example/1");
    await user.click(screen.getByRole("button", { name: /Add Links/ }));
    await waitFor(() => expect(mockAddJobsBulk).toHaveBeenCalled());
  });

  it("leaves Start enabled when collection is on", async () => {
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await screen.findByRole("checkbox", { name: "LinkedIn" });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /collector\.start/ })).toBeEnabled(),
    );
  });

  it("resumes display when a collection is already running on mount", async () => {
    mockStatus.mockResolvedValue({
      ...idleStatus,
      running: true,
      collected: 5,
      log: ["line 1"],
    } as never);
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    // Stop button (destructive) shows when running.
    expect(await screen.findByRole("button", { name: /collector\.stop/ })).toBeInTheDocument();
    expect(screen.getByText("line 1")).toBeInTheDocument();
  });

  it("stops a running collection", async () => {
    mockStatus.mockResolvedValue({ ...idleStatus, running: true } as never);
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);

    await user.click(await screen.findByRole("button", { name: /collector\.stop/ }));
    expect(mockStop).toHaveBeenCalled();
  });

  it("adds a job manually via the add-job form", async () => {
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await screen.findByRole("checkbox", { name: "LinkedIn" });

    await user.click(screen.getByRole("button", { name: /Add Job/ }));
    await user.type(
      screen.getByPlaceholderText("Job URL (required)"),
      "https://jobs.example.com/1",
    );
    await user.type(screen.getByPlaceholderText("Job title (optional)"), "Dev");

    // Submit the add-job form.
    const addButtons = screen.getAllByRole("button", { name: /Add Job/ });
    await user.click(addButtons[addButtons.length - 1]);

    await waitFor(() =>
      expect(mockAddJob).toHaveBeenCalledWith("https://jobs.example.com/1", "Dev", undefined),
    );
    expect(onJobsChanged).toHaveBeenCalled();
  });

  it("falls back to no-country plugins when getProfile rejects", async () => {
    mockGetProfile.mockRejectedValue(new Error("no profile"));
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await waitFor(() => expect(mockGetPlugins).toHaveBeenCalled());
    expect(await screen.findByRole("checkbox", { name: "LinkedIn" })).toBeInTheDocument();
  });
});
