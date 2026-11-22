import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import Sidebar from "./Sidebar";
import { checkHealth } from "../../lib/api";

// Identity translator so assertions use raw i18n keys.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (k: string) => k,
    i18n: { changeLanguage: vi.fn(), dir: () => "ltr", language: "en" },
  }),
}));

vi.mock("../../lib/api", () => ({
  checkHealth: vi.fn(),
  // The run panel polls this; no run is going in these tests.
  getApplyStatus: vi.fn(async () => ({ running: false })),
  pauseApplying: vi.fn(),
  resumeApplying: vi.fn(),
  stopApplying: vi.fn(),
  skipJob: vi.fn(),
  answerHelp: vi.fn(),
  // Decision 1: Trial review cards were removed (no reviewApplication); the panel can stop a collection.
  stopJobCollection: vi.fn(),
}));

const mockCheckHealth = vi.mocked(checkHealth);

function renderSidebar(initialEntries: string[] = ["/"]) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <Sidebar />
    </MemoryRouter>,
  );
}

describe("Sidebar", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockCheckHealth.mockResolvedValue({ build_id: "abc123" } as never);
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
  });

  it("renders the app title/tagline and all navigation items", () => {
    renderSidebar();
    expect(screen.getByText("app.title")).toBeInTheDocument();
    expect(screen.getByText("app.tagline")).toBeInTheDocument();

    // Decision 4: new order; Analytics, Logs and Feedback left the sidebar (Logs and Feedback
    // are Settings → App & browser sections, Analytics is the Dashboard's lower half).
    const expectedNav = [
      "nav.dashboard",
      "nav.profile",
      "nav.jobs",
      "nav.inbox",
      "nav.memory",
      "nav.qa",
      "nav.guide",
      "nav.settings",
    ];
    const links = screen.getAllByRole("link");
    expect(links.map((a) => a.textContent)).toEqual(expectedNav);
    for (const removed of ["nav.logs", "nav.feedback", "nav.analytics"]) {
      expect(screen.queryByText(removed)).not.toBeInTheDocument();
    }
    // The AI model is a Settings category now (/llm still opens it).
    expect(screen.queryByText("nav.llmSettings")).not.toBeInTheDocument();
  });

  it("renders nav items as links pointing at the right routes", () => {
    renderSidebar();
    expect(screen.getByRole("link", { name: "nav.dashboard" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("link", { name: "nav.jobs" })).toHaveAttribute("href", "/jobs");
    expect(screen.getByRole("link", { name: "nav.settings" })).toHaveAttribute("href", "/settings");
    expect(screen.getByRole("link", { name: "nav.inbox" })).toHaveAttribute("href", "/inbox");
    expect(screen.getByRole("link", { name: "nav.guide" })).toHaveAttribute("href", "/guide");
  });

  it("marks the active route's NavLink (aria-current) based on the current location", () => {
    renderSidebar(["/jobs"]);
    const activeLink = screen.getByRole("link", { name: "nav.jobs" });
    expect(activeLink).toHaveAttribute("aria-current", "page");
    expect(activeLink).toHaveClass("bg-foreground");

    const inactiveLink = screen.getByRole("link", { name: "nav.dashboard" });
    expect(inactiveLink).not.toHaveAttribute("aria-current", "page");
  });

  it("shows connecting state initially then connected with build id after health check", async () => {
    renderSidebar();
    // Before the promise resolves, the connecting label is shown.
    expect(screen.getByText("status.connecting")).toBeInTheDocument();

    await waitFor(() =>
      expect(screen.getByText(/status\.connected/)).toBeInTheDocument(),
    );
    // Build id is appended.
    expect(screen.getByText(/abc123/)).toBeInTheDocument();
  });

  it("shows the starting-backend state when the health check fails", async () => {
    mockCheckHealth.mockRejectedValue(new Error("backend down"));
    renderSidebar();
    await waitFor(() =>
      expect(screen.getByText("status.startingBackend")).toBeInTheDocument(),
    );
  });

  it("renders connected without a build id when none is returned", async () => {
    mockCheckHealth.mockResolvedValue({ build_id: "" } as never);
    renderSidebar();
    await waitFor(() => expect(screen.getByText("status.connected")).toBeInTheDocument());
    expect(screen.queryByText(/·/)).not.toBeInTheDocument();
  });
});
