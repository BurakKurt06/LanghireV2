import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import SettingsPage from "./Settings";
import {
  getSettings,
  saveSettings,
  getPlugins,
  togglePlugin,
  removePlugin,
  importPlugin,
  getBrowsers,
  getEmployerPasswordSites,
  revealEmployerPassword,
  getProfile,
  saveProfile,
  getInbox,
  setInboxSchedule,
} from "../lib/api";
import { setTelemetryEnabled } from "../lib/analytics";
import { open as openDialog } from "@tauri-apps/plugin-dialog";

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

vi.mock("../lib/analytics", () => ({
  setTelemetryEnabled: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  getAuthStatus: vi.fn(async () => ({ linkedin: { logged_in: false }, gmail: { logged_in: true } })),
  launchLogin: vi.fn(),
  getSettings: vi.fn(),
  saveSettings: vi.fn(),
  getPlugins: vi.fn(),
  togglePlugin: vi.fn(),
  removePlugin: vi.fn(),
  importPlugin: vi.fn(),
  getBrowsers: vi.fn(),
  getEmployerPasswordSites: vi.fn(async () => ({ sites: [] })),
  revealEmployerPassword: vi.fn(),
  getProfile: vi.fn(),
  saveProfile: vi.fn(),
  getInbox: vi.fn(),
  setInboxSchedule: vi.fn(),
}));

// The AI model form has its own tests (LLMSettingsForm.test.tsx); here only where it appears.
vi.mock("../components/forms/LLMSettingsForm", () => ({
  default: () => <div data-testid="llm-settings-form" />,
}));

/** Settings, opened on one category (the default is Applications, where the resume is). */
function renderSettings(section = "applications") {
  return render(
    <MemoryRouter initialEntries={[`/settings?section=${section}`]}>
      <SettingsPage />
    </MemoryRouter>,
  );
}

const mockGetSettings = vi.mocked(getSettings);
const mockSaveSettings = vi.mocked(saveSettings);
const mockGetPlugins = vi.mocked(getPlugins);
const mockTogglePlugin = vi.mocked(togglePlugin);
const mockRemovePlugin = vi.mocked(removePlugin);
const mockImportPlugin = vi.mocked(importPlugin);
const mockGetBrowsers = vi.mocked(getBrowsers);
const mockSetTelemetry = vi.mocked(setTelemetryEnabled);
const mockOpenDialog = vi.mocked(openDialog);
const mockGetProfile = vi.mocked(getProfile);
const mockSaveProfile = vi.mocked(saveProfile);
const mockGetInbox = vi.mocked(getInbox);
const mockSetInboxSchedule = vi.mocked(setInboxSchedule);

const baseProfile = {
  name: "Ada Lovelace",
  email: "ada@example.com",
  address: { street: "", city: "", state: "", zip: "10001", country: "USA" },
  salary_expectation: { min: 1, max: 2, currency: "USD", period: "annual" },
  target_job_titles: ["Backend Engineer"],
  target_locations: ["Boston, MA"],
  search_area: { center_zip: "", radius: "any" },
  notes: "Use Apply with resume first.",
  skills: ["Python"],
};

const baseSettings = {
  resume_path: "/home/me/resume.pdf",
  blocked_domains: ["spam.com"],
  sensitive_data: { email: "me@example.com", password: "secret" },
  max_failures: 8,
  data_dir: "/data",
  telemetry_enabled: true,
};

const builtinPlugin = {
  name: "linkedin",
  display_name: "LinkedIn",
  version: "1.0.0",
  author: "core",
  description: "LinkedIn jobs",
  countries: ["US", "GB"],
  website: "https://linkedin.com",
  requires_login: true,
  login_url: "https://linkedin.com/login",
  is_builtin: true,
  enabled: true,
  filters: [],
};

const communityPlugin = {
  ...builtinPlugin,
  name: "acme",
  display_name: "Acme Jobs",
  is_builtin: false,
  enabled: false,
};

describe("SettingsPage", () => {
  beforeEach(() => {
    installLocalStorage();
    mockGetSettings.mockResolvedValue(structuredClone(baseSettings) as never);
    mockSaveSettings.mockResolvedValue({ success: true } as never);
    mockGetPlugins.mockResolvedValue({
      success: true,
      plugins: [structuredClone(builtinPlugin), structuredClone(communityPlugin)],
    } as never);
    mockTogglePlugin.mockResolvedValue({ success: true } as never);
    mockRemovePlugin.mockResolvedValue({ success: true } as never);
    mockImportPlugin.mockResolvedValue({
      success: true,
      plugin: { name: "new", display_name: "New" },
    } as never);
    mockOpenDialog.mockResolvedValue(null as never);
    mockGetProfile.mockResolvedValue(structuredClone(baseProfile) as never);
    mockSaveProfile.mockResolvedValue({ success: true } as never);
    mockGetInbox.mockResolvedValue({ schedule: "weekly", emails: [] } as never);
    mockSetInboxSchedule.mockResolvedValue({ success: true } as never);
    mockGetBrowsers.mockResolvedValue({
      browsers: [
        { id: "chrome", name: "Google Chrome", path: "C:/chrome.exe", is_default: false },
        { id: "msedge", name: "Microsoft Edge", path: "C:/msedge.exe", is_default: true },
      ],
    } as never);
  });

  it("has an Accounts section to sign in to Gmail and LinkedIn once", async () => {
    renderSettings("accounts");
    expect(await screen.findByRole("heading", { name: "sections.accounts" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Sign in to LinkedIn/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Sign in again/ })).toBeInTheDocument(); // Gmail already signed in
  });

  it("lists installed browsers, marks the default, and saves the choice", async () => {
    const user = userEvent.setup();
    renderSettings("browser");
    const select = await screen.findByLabelText("Browser the agent uses");
    expect(within(select).getByRole("option", { name: "Microsoft Edge (your default browser)" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "Google Chrome" })).toBeInTheDocument();

    await user.selectOptions(select, "msedge");
    await user.click(screen.getByRole("button", { name: /Save Settings/ }));
    await waitFor(() =>
      expect(mockSaveSettings).toHaveBeenCalledWith(expect.objectContaining({ browser: "msedge" })),
    );
  });

  it("custom browser choice reveals a path input that is saved", async () => {
    const user = userEvent.setup();
    renderSettings("browser");
    await user.selectOptions(await screen.findByLabelText("Browser the agent uses"), "custom");
    await user.type(screen.getByPlaceholderText(/browser\.exe/), "D:/Opera/opera.exe");
    await user.click(screen.getByRole("button", { name: /Save Settings/ }));
    await waitFor(() =>
      expect(mockSaveSettings).toHaveBeenCalledWith(
        expect.objectContaining({ browser: "custom", browser_path: "D:/Opera/opera.exe" }),
      ),
    );
  });

  it("shows a loading spinner before settings load", () => {
    renderSettings();
    // Save Settings button is not rendered while loading.
    expect(screen.queryByRole("button", { name: /Save Settings/ })).not.toBeInTheDocument();
  });

  it("loads and renders the settings values", async () => {
    renderSettings();
    expect(await screen.findByDisplayValue("/home/me/resume.pdf")).toBeInTheDocument();
    expect(screen.getByDisplayValue("me@example.com")).toBeInTheDocument();
    expect(screen.getByDisplayValue("secret")).toBeInTheDocument();
    expect(screen.getByDisplayValue(8)).toBeInTheDocument();
    // Blocked domain tag renders.
    expect(screen.getByText("spam.com")).toBeInTheDocument();
  });

  it("renders plugins, builtin badge, and the community remove button", async () => {
    renderSettings("job-sources");
    expect(await screen.findByText("LinkedIn")).toBeInTheDocument();
    expect(screen.getByText("Acme Jobs")).toBeInTheDocument();
    expect(screen.getByText("Built-in")).toBeInTheDocument();
  });

  it("sets how many jobs run at the same time (1 by default, up to 4)", async () => {
    const user = userEvent.setup();
    renderSettings();
    const select = await screen.findByLabelText("Jobs at the same time");
    expect(select).toHaveValue("1");
    expect([...(select as HTMLSelectElement).options].map((o) => o.value)).toEqual(["1", "2", "3", "4"]);
    await user.selectOptions(select, "3");
    await user.click(screen.getByRole("button", { name: /Save Settings/ }));
    await waitFor(() =>
      expect(mockSaveSettings).toHaveBeenCalledWith(expect.objectContaining({ parallel_jobs: 3 })),
    );
  });

  it("saves settings with the current field values", async () => {
    const user = userEvent.setup();
    renderSettings();
    await screen.findByDisplayValue("/home/me/resume.pdf");

    await user.click(screen.getByRole("button", { name: /Save Settings/ }));

    await waitFor(() =>
      expect(mockSaveSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          resume_path: "/home/me/resume.pdf",
          blocked_domains: ["spam.com"],
          sensitive_data: { email: "me@example.com", password: "secret" },
          max_failures: 8,
          telemetry_enabled: true,
        }),
      ),
    );
    // Button text flips to "Saved".
    expect(await screen.findByText("Saved")).toBeInTheDocument();
  });

  it("shows an error banner when saving fails", async () => {
    mockSaveSettings.mockRejectedValue(new Error("boom"));
    const user = userEvent.setup();
    renderSettings();
    await screen.findByDisplayValue("/home/me/resume.pdf");

    await user.click(screen.getByRole("button", { name: /Save Settings/ }));
    expect(await screen.findByText("Failed to save settings. Please try again.")).toBeInTheDocument();
  });

  it("toggles telemetry and updates the analytics module", async () => {
    const user = userEvent.setup();
    renderSettings("appearance");
    await screen.findByDisplayValue("/home/me/resume.pdf");

    const telemetryToggle = screen.getByRole("switch", { name: "Usage Analytics" });
    expect(telemetryToggle).toBeChecked();
    await user.click(telemetryToggle);
    expect(mockSetTelemetry).toHaveBeenCalledWith(false);
  });

  it("toggles a plugin on/off via the API", async () => {
    const user = userEvent.setup();
    renderSettings("job-sources");
    await screen.findByText("Acme Jobs");

    // The community plugin starts disabled (unchecked); toggle it on.
    const checkboxes = screen.getAllByRole("checkbox");
    const acmeToggle = checkboxes.find((c) => !(c as HTMLInputElement).checked);
    await user.click(acmeToggle!);
    await waitFor(() => expect(mockTogglePlugin).toHaveBeenCalledWith("acme", true));
  });

  it("removes a community plugin", async () => {
    const user = userEvent.setup();
    renderSettings("job-sources");
    await screen.findByText("Acme Jobs");

    // Only the community plugin has a remove (Trash) button.
    const removeButtons = screen
      .getAllByRole("button")
      .filter((b) => b.querySelector("svg.lucide-trash2") || b.className.includes("text-destructive"));
    await user.click(removeButtons[0]);
    await waitFor(() => expect(mockRemovePlugin).toHaveBeenCalledWith("acme"));
    await waitFor(() => expect(screen.queryByText("Acme Jobs")).not.toBeInTheDocument());
  });

  it("adds a blocked domain via the tag input", async () => {
    const user = userEvent.setup();
    renderSettings("job-search");
    await screen.findByDisplayValue("/home/me/resume.pdf");

    const tagInput = screen.getByPlaceholderText("example.com");
    await user.type(tagInput, "blocked.io{Enter}");
    expect(await screen.findByText("blocked.io")).toBeInTheDocument();
  });

  it("imports a plugin when a file is chosen", async () => {
    mockOpenDialog.mockResolvedValue("/path/to/plugin.yaml" as never);
    const user = userEvent.setup();
    renderSettings("job-sources");
    await screen.findByText("Acme Jobs");

    await user.click(screen.getByRole("button", { name: /Import Plugin/ }));
    await waitFor(() => expect(mockImportPlugin).toHaveBeenCalledWith("/path/to/plugin.yaml"));
    // Plugins reload after import.
    await waitFor(() => expect(mockGetPlugins).toHaveBeenCalledTimes(2));
  });

  it("browses for a resume file and updates the path", async () => {
    mockOpenDialog.mockResolvedValue("/new/resume.pdf" as never);
    const user = userEvent.setup();
    renderSettings();
    await screen.findByDisplayValue("/home/me/resume.pdf");

    await user.click(screen.getByRole("button", { name: /Browse/ }));
    expect(await screen.findByDisplayValue("/new/resume.pdf")).toBeInTheDocument();
  });

  it("renders the theme toggle and persists a selection (#41)", async () => {
    const user = userEvent.setup();
    renderSettings("appearance");
    await screen.findByDisplayValue("/home/me/resume.pdf");

    const dark = screen.getByRole("radio", { name: /appearance\.dark/ });
    expect(screen.getByRole("radio", { name: /appearance\.light/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /appearance\.system/ })).toBeInTheDocument();

    await user.click(dark);
    // Applies immediately and mirrors to the backend.
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    await waitFor(() => expect(mockSaveSettings).toHaveBeenCalledWith({ theme: "dark" }));
    expect(dark).toHaveAttribute("aria-checked", "true");

    // Clean up the global <html> class so other tests aren't affected.
    document.documentElement.classList.remove("dark");
  });

  it("applies a theme loaded from backend settings (#41)", async () => {
    mockGetSettings.mockResolvedValue({ ...baseSettings, theme: "dark" } as never);
    renderSettings();
    await screen.findByDisplayValue("/home/me/resume.pdf");
    await waitFor(() => expect(document.documentElement.classList.contains("dark")).toBe(true));
    document.documentElement.classList.remove("dark");
  });

  // ── New feature switches ────────────────────────────────────────────────
  describe("feature switches", () => {
    const findSwitch = (name: string) =>
      screen.getByRole("switch", { name });

    it("defaults job board collection on and the rest off", async () => {
      const user = userEvent.setup();
      renderSettings("job-sources");
      await screen.findByDisplayValue("/home/me/resume.pdf");
      expect(findSwitch("Job board collection")).toBeChecked();
      await user.click(screen.getByRole("button", { name: "sections.browser" }));
      expect(findSwitch("Stealth mode")).not.toBeChecked();
      expect(findSwitch("Human-like delays")).not.toBeChecked();
      expect(findSwitch("Solve CAPTCHAs with 2Captcha")).not.toBeChecked();
    });

    it("reflects saved switch values from the backend", async () => {
      mockGetSettings.mockResolvedValue({
        ...baseSettings,
        job_collection_enabled: false,
        stealth_enabled: true,
        human_delay_enabled: true,
        captcha_solver_enabled: true,
      } as never);
      const user = userEvent.setup();
      renderSettings("job-sources");
      await screen.findByDisplayValue("/home/me/resume.pdf");
      expect(findSwitch("Job board collection")).not.toBeChecked();
      await user.click(screen.getByRole("button", { name: "sections.browser" }));
      expect(findSwitch("Stealth mode")).toBeChecked();
      expect(findSwitch("Human-like delays")).toBeChecked();
      expect(findSwitch("Solve CAPTCHAs with 2Captcha")).toBeChecked();
    });

    it("persists every switch on save", async () => {
      const user = userEvent.setup();
      renderSettings("job-sources");
      await screen.findByDisplayValue("/home/me/resume.pdf");

      await user.click(findSwitch("Job board collection"));
      // Edits are kept while switching categories; one Save saves them all.
      await user.click(screen.getByRole("button", { name: "sections.browser" }));
      await user.click(findSwitch("Stealth mode"));
      await user.click(screen.getByRole("button", { name: /Save Settings/ }));

      await waitFor(() => expect(mockSaveSettings).toHaveBeenCalled());
      const saved = mockSaveSettings.mock.calls.at(-1)![0];
      expect(saved.job_collection_enabled).toBe(false);
      expect(saved.stealth_enabled).toBe(true);
    });

    it("shows the delay window only when delays are enabled", async () => {
      const user = userEvent.setup();
      renderSettings("browser");
      await screen.findByDisplayValue("/home/me/resume.pdf");
      expect(screen.queryByLabelText("Min seconds")).not.toBeInTheDocument();

      await user.click(findSwitch("Human-like delays"));
      expect(await screen.findByLabelText("Min seconds")).toHaveValue(5);
      expect(screen.getByLabelText("Max seconds")).toHaveValue(15);
    });

    it("warns when the delay window is inverted", async () => {
      const user = userEvent.setup();
      renderSettings("browser");
      await screen.findByDisplayValue("/home/me/resume.pdf");
      await user.click(findSwitch("Human-like delays"));

      const min = await screen.findByLabelText("Min seconds");
      await user.clear(min);
      await user.type(min, "30");
      expect(await screen.findByText(/Max must be at least the min/)).toBeInTheDocument();
    });

    it("shows the CAPTCHA key field only when the solver is on, and masks it", async () => {
      const user = userEvent.setup();
      renderSettings("browser");
      await screen.findByDisplayValue("/home/me/resume.pdf");
      expect(screen.queryByLabelText("2Captcha API key")).not.toBeInTheDocument();

      await user.click(findSwitch("Solve CAPTCHAs with 2Captcha"));
      const key = await screen.findByLabelText("2Captcha API key");
      expect(key).toHaveAttribute("type", "password");
    });

    it("persists the CAPTCHA API key", async () => {
      const user = userEvent.setup();
      renderSettings("browser");
      await screen.findByDisplayValue("/home/me/resume.pdf");
      await user.click(findSwitch("Solve CAPTCHAs with 2Captcha"));
      await user.type(await screen.findByLabelText("2Captcha API key"), "abc123");
      await user.click(screen.getByRole("button", { name: /Save Settings/ }));

      await waitFor(() => expect(mockSaveSettings).toHaveBeenCalled());
      const saved = mockSaveSettings.mock.calls.at(-1)![0];
      expect(saved.captcha_api_key).toBe("abc123");
      expect(saved.captcha_solver_enabled).toBe(true);
    });

    it("keeps the account password field working", async () => {
      renderSettings("accounts");
      await screen.findByDisplayValue("/home/me/resume.pdf");
      const pw = screen.getByDisplayValue("secret");
      expect(pw).toHaveAttribute("type", "password");
    });

    it("hides the language picker in this English-only build", async () => {
      renderSettings();
      await screen.findByDisplayValue("/home/me/resume.pdf");
      expect(screen.queryByText("language.title")).not.toBeInTheDocument();
    });
  });

  it("saves performance mode, the fit check, cover-letter boxes and other documents", async () => {
    const user = userEvent.setup();
    renderSettings();
    const perf = await screen.findByRole("switch", { name: "Performance mode" });
    const fit = screen.getByRole("switch", { name: "Fit check before applying" });
    expect(fit).toBeChecked();
    await user.click(perf);
    expect(fit).toBeDisabled();
    expect(fit).not.toBeChecked();
    await user.click(screen.getByRole("switch", { name: "Fill cover-letter boxes" }));
    await user.click(screen.getByRole("button", { name: "+ Add document" }));
    await user.type(screen.getByRole("textbox", { name: "Document name" }), "Transcript");
    await user.type(screen.getByRole("textbox", { name: "Document file" }), "C:/docs/transcript.pdf");
    await user.click(screen.getByRole("button", { name: /Save Settings/ }));
    await waitFor(() =>
      expect(mockSaveSettings).toHaveBeenCalledWith(expect.objectContaining({
        performance_mode: true,
        fit_check_enabled: true,
        cover_letter_boxes: true,
        other_documents: [{ label: "Transcript", path: "C:/docs/transcript.pdf" }],
      })),
    );
  });

  it("a failed settings load offers Retry, never a form whose defaults could be saved", async () => {
    const user = userEvent.setup();
    mockGetSettings.mockRejectedValueOnce(new Error("boom"));
    renderSettings();
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load your settings");
    expect(screen.queryByRole("button", { name: /Save Settings/ })).not.toBeInTheDocument();
    expect(mockSaveSettings).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: /Save Settings/ })).toBeInTheDocument();
  });

  it("a failed plugin list alone still shows the settings", async () => {
    mockGetPlugins.mockRejectedValueOnce(new Error("boom"));
    renderSettings();
    expect(await screen.findByRole("button", { name: /Save Settings/ })).toBeInTheDocument();
  });

  it("lists the employer passwords the app made, each shown only on request", async () => {
    const user = userEvent.setup();
    vi.mocked(getEmployerPasswordSites).mockResolvedValue({ sites: ["acme.wd5.myworkdayjobs.com"] });
    vi.mocked(revealEmployerPassword).mockResolvedValue({ site: "acme.wd5.myworkdayjobs.com", password: "Fake-Pass-123" });
    renderSettings("accounts");
    expect(await screen.findByText("acme.wd5.myworkdayjobs.com")).toBeInTheDocument();
    expect(screen.queryByText("Fake-Pass-123")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show the password for acme.wd5.myworkdayjobs.com" }));
    expect(await screen.findByText("Fake-Pass-123")).toBeInTheDocument();
    expect(revealEmployerPassword).toHaveBeenCalledWith("acme.wd5.myworkdayjobs.com");
    await user.click(screen.getByRole("button", { name: "Hide the password for acme.wd5.myworkdayjobs.com" }));
    expect(screen.queryByText("Fake-Pass-123")).not.toBeInTheDocument();
  });
});

describe("Settings categories (reorganization, user-approved 11-01)", () => {
  beforeEach(() => {
    installLocalStorage();
    mockGetSettings.mockResolvedValue(structuredClone(baseSettings) as never);
    mockSaveSettings.mockResolvedValue({ success: true } as never);
    mockGetPlugins.mockResolvedValue({ success: true, plugins: [] } as never);
    mockGetBrowsers.mockResolvedValue({ browsers: [] } as never);
    mockGetProfile.mockResolvedValue(structuredClone(baseProfile) as never);
    mockSaveProfile.mockResolvedValue({ success: true } as never);
    mockGetInbox.mockResolvedValue({ schedule: "weekly", emails: [] } as never);
    mockSetInboxSchedule.mockResolvedValue({ success: true } as never);
  });

  it("lists the seven categories and shows one at a time", async () => {
    const user = userEvent.setup();
    renderSettings("applications");
    const nav = await screen.findByRole("navigation", { name: "Settings categories" });
    expect(within(nav).getAllByRole("button").map((b) => b.textContent)).toEqual([
      "sections.aiModel", "sections.jobSearch", "sections.applications", "sections.accounts",
      "sections.jobSources", "sections.browser", "sections.appearance",
    ]);
    expect(within(nav).getByRole("button", { name: "sections.applications" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("heading", { name: "sections.applications" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "sections.browser" })).not.toBeInTheDocument();  // hidden
    await user.click(within(nav).getByRole("button", { name: "sections.browser" }));
    expect(screen.getByRole("heading", { name: "sections.browser" })).toBeVisible();
    expect(screen.getByLabelText("Browser the agent uses")).toBeVisible();
  });

  it("opens the AI model by default (and for an unknown link), with the form's own Save only", async () => {
    renderSettings("nonsense");
    expect(await screen.findByRole("heading", { name: "sections.aiModel" })).toBeVisible();
    expect(screen.getByTestId("llm-settings-form")).toBeVisible();
    expect(screen.queryByRole("button", { name: /Save Settings/ })).not.toBeInTheDocument();
  });

  it("Job search edits the profile's search fields and saves only those, on the profile as saved now", async () => {
    const user = userEvent.setup();
    // Profile was saved elsewhere after this page loaded: its change must survive this save.
    mockGetProfile
      .mockResolvedValueOnce(structuredClone(baseProfile) as never)
      .mockResolvedValueOnce({ ...structuredClone(baseProfile), name: "Changed On Profile" } as never);
    renderSettings("job-search");
    expect(await screen.findByText("Backend Engineer")).toBeVisible();
    expect(screen.getByText("Boston, MA")).toBeVisible();
    expect(screen.getByPlaceholderText("10001")).toBeVisible();             // the profile's ZIP as the hint
    await user.type(screen.getByPlaceholderText("targetJobTitles.placeholder"), "Data Analyst{Enter}");
    await user.selectOptions(screen.getByLabelText("searchArea.radius"), "50");
    await user.click(screen.getByRole("checkbox", { name: "Part-time" }));
    await user.click(screen.getByRole("button", { name: /Save Settings/ }));
    await waitFor(() => expect(mockSaveProfile).toHaveBeenCalledTimes(1));
    const saved = mockSaveProfile.mock.calls[0][0];
    expect(saved.name).toBe("Changed On Profile");
    expect(saved.skills).toEqual(["Python"]);
    expect(saved.target_job_titles).toEqual(["Backend Engineer", "Data Analyst"]);
    expect(saved.target_locations).toEqual(["Boston, MA"]);
    expect(saved.search_area).toEqual({ center_zip: "", radius: "50" });
    expect(saved.job_search).toEqual(expect.objectContaining({ job_types: ["part_time"], mode: "quick" }));
    expect(saved.notes).toBe("Use Apply with resume first.");
    expect(mockSaveSettings).toHaveBeenCalled();
  });

  it("the profile isn't written when none of its fields changed", async () => {
    const user = userEvent.setup();
    renderSettings("job-search");
    await screen.findByText("Backend Engineer");
    await user.click(screen.getByRole("button", { name: /Save Settings/ }));
    await waitFor(() => expect(mockSaveSettings).toHaveBeenCalled());
    expect(mockSaveProfile).not.toHaveBeenCalled();
  });

  it("Applications has the instructions for the AI, saved with the profile", async () => {
    const user = userEvent.setup();
    renderSettings("applications");
    const notes = await screen.findByRole("textbox", { name: "notes.title" });
    expect(notes).toHaveValue("Use Apply with resume first.");
    await user.type(notes, " Then check every field.");
    await user.click(screen.getByRole("button", { name: /Save Settings/ }));
    await waitFor(() => expect(mockSaveProfile).toHaveBeenCalledWith(expect.objectContaining({
      notes: "Use Apply with resume first. Then check every field.",
      name: "Ada Lovelace",
    })));
  });

  it("a profile that couldn't be read hides its fields and is never written; the settings still save", async () => {
    const user = userEvent.setup();
    mockGetProfile.mockRejectedValueOnce(new Error("boom"));
    renderSettings("job-search");
    expect(await screen.findByRole("alert")).toHaveTextContent("loadError");
    expect(screen.queryByPlaceholderText("targetJobTitles.placeholder")).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText("example.com")).toBeVisible();      // blocked domains are settings
    await user.click(screen.getByRole("button", { name: /Save Settings/ }));
    await waitFor(() => expect(mockSaveSettings).toHaveBeenCalled());
    expect(mockSaveProfile).not.toHaveBeenCalled();
  });

  it("tailoring options moved here: saved at once in the same browser storage", async () => {
    const user = userEvent.setup();
    renderSettings("applications");
    await user.click(await screen.findByRole("checkbox", { name: /Job Title/ }));
    await user.click(screen.getByRole("checkbox", { name: /Auto-tailor before applying/ }));
    expect(JSON.parse(localStorage.getItem("langhire_tailor_options") || "{}")).toEqual(
      { skills: true, title: true, overview: false, experience: false });
    expect(localStorage.getItem("langhire_auto_tailor")).toBe("true");
    expect(mockSaveSettings).not.toHaveBeenCalled();
  });

  it("the Inbox's automatic checks moved here: changed at once through the same call", async () => {
    const user = userEvent.setup();
    renderSettings("accounts");
    const schedule = await screen.findByLabelText("Check automatically");
    await waitFor(() => expect(schedule).toHaveValue("weekly"));
    await user.selectOptions(schedule, "daily");
    expect(mockSetInboxSchedule).toHaveBeenCalledWith("daily");
    expect(mockSaveSettings).not.toHaveBeenCalled();
  });

  it("an Inbox schedule that can't be saved goes back and says why", async () => {
    const user = userEvent.setup();
    mockSetInboxSchedule.mockRejectedValueOnce(new Error("schedule must be off, daily or weekly"));
    renderSettings("accounts");
    const schedule = await screen.findByLabelText("Check automatically");
    await waitFor(() => expect(schedule).toHaveValue("weekly"));
    await user.selectOptions(schedule, "daily");
    expect(await screen.findByText("schedule must be off, daily or weekly")).toBeVisible();
    expect(schedule).toHaveValue("weekly");
  });
});
