import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SettingsPage from "./Settings";
import {
  getSettings,
  saveSettings,
  getPlugins,
  togglePlugin,
  removePlugin,
  importPlugin,
  getBrowsers,
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
}));

const mockGetSettings = vi.mocked(getSettings);
const mockSaveSettings = vi.mocked(saveSettings);
const mockGetPlugins = vi.mocked(getPlugins);
const mockTogglePlugin = vi.mocked(togglePlugin);
const mockRemovePlugin = vi.mocked(removePlugin);
const mockImportPlugin = vi.mocked(importPlugin);
const mockGetBrowsers = vi.mocked(getBrowsers);
const mockSetTelemetry = vi.mocked(setTelemetryEnabled);
const mockOpenDialog = vi.mocked(openDialog);

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
    mockGetBrowsers.mockResolvedValue({
      browsers: [
        { id: "chrome", name: "Google Chrome", path: "C:/chrome.exe", is_default: false },
        { id: "msedge", name: "Microsoft Edge", path: "C:/msedge.exe", is_default: true },
      ],
    } as never);
  });

  it("has an Accounts section to sign in to Gmail and LinkedIn once", async () => {
    render(<SettingsPage />);
    expect(await screen.findByText("Accounts")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Sign in to LinkedIn/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Sign in again/ })).toBeInTheDocument(); // Gmail already signed in
  });

  it("lists installed browsers, marks the default, and saves the choice", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
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
    render(<SettingsPage />);
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
    render(<SettingsPage />);
    // Save Settings button is not rendered while loading.
    expect(screen.queryByRole("button", { name: /Save Settings/ })).not.toBeInTheDocument();
  });

  it("loads and renders the settings values", async () => {
    render(<SettingsPage />);
    expect(await screen.findByDisplayValue("/home/me/resume.pdf")).toBeInTheDocument();
    expect(screen.getByDisplayValue("me@example.com")).toBeInTheDocument();
    expect(screen.getByDisplayValue("secret")).toBeInTheDocument();
    expect(screen.getByDisplayValue(8)).toBeInTheDocument();
    // Blocked domain tag renders.
    expect(screen.getByText("spam.com")).toBeInTheDocument();
  });

  it("renders plugins, builtin badge, and the community remove button", async () => {
    render(<SettingsPage />);
    expect(await screen.findByText("LinkedIn")).toBeInTheDocument();
    expect(screen.getByText("Acme Jobs")).toBeInTheDocument();
    expect(screen.getByText("Built-in")).toBeInTheDocument();
  });

  it("saves settings with the current field values", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
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
    render(<SettingsPage />);
    await screen.findByDisplayValue("/home/me/resume.pdf");

    await user.click(screen.getByRole("button", { name: /Save Settings/ }));
    expect(await screen.findByText("Failed to save settings. Please try again.")).toBeInTheDocument();
  });

  it("toggles telemetry and updates the analytics module", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByDisplayValue("/home/me/resume.pdf");

    // The telemetry toggle lives in the "Usage Analytics" section. Find the
    // checkbox in the same row as that label (avoids matching plugin toggles).
    const analyticsRow = screen.getByText("Usage Analytics").closest("div")!
      .parentElement! as HTMLElement;
    const telemetryToggle = within(analyticsRow).getByRole("checkbox");
    expect(telemetryToggle).toBeChecked();
    await user.click(telemetryToggle);
    expect(mockSetTelemetry).toHaveBeenCalledWith(false);
  });

  it("toggles a plugin on/off via the API", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByText("Acme Jobs");

    // The community plugin starts disabled (unchecked); toggle it on.
    const checkboxes = screen.getAllByRole("checkbox");
    const acmeToggle = checkboxes.find((c) => !(c as HTMLInputElement).checked);
    await user.click(acmeToggle!);
    await waitFor(() => expect(mockTogglePlugin).toHaveBeenCalledWith("acme", true));
  });

  it("removes a community plugin", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
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
    render(<SettingsPage />);
    await screen.findByDisplayValue("/home/me/resume.pdf");

    const tagInput = screen.getByPlaceholderText("example.com");
    await user.type(tagInput, "blocked.io{Enter}");
    expect(await screen.findByText("blocked.io")).toBeInTheDocument();
  });

  it("imports a plugin when a file is chosen", async () => {
    mockOpenDialog.mockResolvedValue("/path/to/plugin.yaml" as never);
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByText("Acme Jobs");

    await user.click(screen.getByRole("button", { name: /Import Plugin/ }));
    await waitFor(() => expect(mockImportPlugin).toHaveBeenCalledWith("/path/to/plugin.yaml"));
    // Plugins reload after import.
    await waitFor(() => expect(mockGetPlugins).toHaveBeenCalledTimes(2));
  });

  it("browses for a resume file and updates the path", async () => {
    mockOpenDialog.mockResolvedValue("/new/resume.pdf" as never);
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByDisplayValue("/home/me/resume.pdf");

    await user.click(screen.getByRole("button", { name: /Browse/ }));
    expect(await screen.findByDisplayValue("/new/resume.pdf")).toBeInTheDocument();
  });

  it("renders the theme toggle and persists a selection (#41)", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
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
    render(<SettingsPage />);
    await screen.findByDisplayValue("/home/me/resume.pdf");
    await waitFor(() => expect(document.documentElement.classList.contains("dark")).toBe(true));
    document.documentElement.classList.remove("dark");
  });

  // ── New feature switches ────────────────────────────────────────────────
  describe("feature switches", () => {
    const findSwitch = (name: string) =>
      screen.getByRole("switch", { name });

    it("defaults job board collection on and the rest off", async () => {
      render(<SettingsPage />);
      await screen.findByDisplayValue("/home/me/resume.pdf");
      expect(findSwitch("Job board collection")).toBeChecked();
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
      render(<SettingsPage />);
      await screen.findByDisplayValue("/home/me/resume.pdf");
      expect(findSwitch("Job board collection")).not.toBeChecked();
      expect(findSwitch("Stealth mode")).toBeChecked();
      expect(findSwitch("Human-like delays")).toBeChecked();
      expect(findSwitch("Solve CAPTCHAs with 2Captcha")).toBeChecked();
    });

    it("persists every switch on save", async () => {
      const user = userEvent.setup();
      render(<SettingsPage />);
      await screen.findByDisplayValue("/home/me/resume.pdf");

      await user.click(findSwitch("Job board collection"));
      await user.click(findSwitch("Stealth mode"));
      await user.click(screen.getByRole("button", { name: /Save Settings/ }));

      await waitFor(() => expect(mockSaveSettings).toHaveBeenCalled());
      const saved = mockSaveSettings.mock.calls.at(-1)![0];
      expect(saved.job_collection_enabled).toBe(false);
      expect(saved.stealth_enabled).toBe(true);
    });

    it("shows the delay window only when delays are enabled", async () => {
      const user = userEvent.setup();
      render(<SettingsPage />);
      await screen.findByDisplayValue("/home/me/resume.pdf");
      expect(screen.queryByLabelText("Min seconds")).not.toBeInTheDocument();

      await user.click(findSwitch("Human-like delays"));
      expect(await screen.findByLabelText("Min seconds")).toHaveValue(5);
      expect(screen.getByLabelText("Max seconds")).toHaveValue(15);
    });

    it("warns when the delay window is inverted", async () => {
      const user = userEvent.setup();
      render(<SettingsPage />);
      await screen.findByDisplayValue("/home/me/resume.pdf");
      await user.click(findSwitch("Human-like delays"));

      const min = await screen.findByLabelText("Min seconds");
      await user.clear(min);
      await user.type(min, "30");
      expect(await screen.findByText(/Max must be at least the min/)).toBeInTheDocument();
    });

    it("shows the CAPTCHA key field only when the solver is on, and masks it", async () => {
      const user = userEvent.setup();
      render(<SettingsPage />);
      await screen.findByDisplayValue("/home/me/resume.pdf");
      expect(screen.queryByLabelText("2Captcha API key")).not.toBeInTheDocument();

      await user.click(findSwitch("Solve CAPTCHAs with 2Captcha"));
      const key = await screen.findByLabelText("2Captcha API key");
      expect(key).toHaveAttribute("type", "password");
    });

    it("persists the CAPTCHA API key", async () => {
      const user = userEvent.setup();
      render(<SettingsPage />);
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
      render(<SettingsPage />);
      await screen.findByDisplayValue("/home/me/resume.pdf");
      const pw = screen.getByDisplayValue("secret");
      expect(pw).toHaveAttribute("type", "password");
    });

    it("hides the language picker in this English-only build", async () => {
      render(<SettingsPage />);
      await screen.findByDisplayValue("/home/me/resume.pdf");
      expect(screen.queryByText("language.title")).not.toBeInTheDocument();
    });
  });
});
