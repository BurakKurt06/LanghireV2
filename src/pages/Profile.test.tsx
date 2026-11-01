import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Profile from "./Profile";
import { getProfile, saveProfile, getCountries } from "../lib/api";
import type { CandidateProfile, CountryConfig } from "../lib/types";

// ── i18n: identity translator (returns the raw key) ────────────────────────
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (k: string) => k,
    i18n: { changeLanguage: vi.fn() },
  }),
}));

vi.mock("../lib/api", () => ({
  getProfile: vi.fn(),
  saveProfile: vi.fn(),
  getCountries: vi.fn(),
}));

const mockGetProfile = vi.mocked(getProfile);
const mockSaveProfile = vi.mocked(saveProfile);
const mockGetCountries = vi.mocked(getCountries);

const usConfig: CountryConfig = {
  name: "United States",
  flag: "🇺🇸",
  date_format: "MM/DD/YYYY",
  currency: "USD",
  salary_period: "annual",
  address_labels: { state: "State", zip: "ZIP" },
  work_auth_options: ["Citizen", "Green Card", "H-1B"],
  show_notice_period: false,
  show_nationality: false,
  show_cover_letter: false,
  show_photo: false,
  show_date_of_birth: false,
  phone_prefix: "+1",
  default_sources: ["linkedin"],
};

const deConfig: CountryConfig = {
  name: "Germany",
  flag: "🇩🇪",
  date_format: "DD.MM.YYYY",
  currency: "EUR",
  salary_period: "annual",
  address_labels: { state: "Bundesland", zip: "PLZ" },
  work_auth_options: ["EU Citizen", "Blue Card"],
  show_notice_period: true,
  show_nationality: true,
  show_cover_letter: true,
  show_photo: true,
  show_date_of_birth: true,
  phone_prefix: "+49",
  default_sources: ["linkedin"],
};

const sampleProfile: Partial<CandidateProfile> = {
  name: "Ada Lovelace",
  email: "ada@example.com",
  country: "US",
  current_role: "Engineer",
  target_job_titles: ["Backend Engineer"],
  skills: ["Python", "Rust"],
  languages: ["English"],
};

describe("Profile page", () => {
  beforeEach(() => {
    mockGetProfile.mockResolvedValue(sampleProfile as CandidateProfile);
    mockGetCountries.mockResolvedValue({
      success: true,
      countries: { US: usConfig, DE: deConfig },
      notice_period_options: ["Immediate", "1 month", "3 months"],
    } as never);
    mockSaveProfile.mockResolvedValue({ success: true } as never);
  });

  it("shows a loading spinner before data resolves, then renders the form", async () => {
    render(<Profile />);
    // The save button (a stable form landmark) is not present while loading.
    expect(screen.queryByRole("button", { name: "save" })).not.toBeInTheDocument();

    await waitFor(() => expect(mockGetProfile).toHaveBeenCalled());
    expect(await screen.findByRole("button", { name: "save" })).toBeInTheDocument();
  });

  it("edits and saves screening answers", async () => {
    mockGetProfile.mockResolvedValue({ ...sampleProfile, screening: { gpa: "3.2" } } as CandidateProfile);
    const user = userEvent.setup();
    render(<Profile />);
    expect(await screen.findByText("screening.title")).toBeInTheDocument();
    const gpa = screen.getByDisplayValue("3.2");
    await user.clear(gpa);
    await user.type(gpa, "3.6");
    await user.click(screen.getByRole("button", { name: "save" }));
    await waitFor(() =>
      expect(mockSaveProfile).toHaveBeenCalledWith(
        expect.objectContaining({ screening: expect.objectContaining({ gpa: "3.6", start_date: "" }) }),
      ),
    );
  });

  it("shows date of birth for US profiles too; the AI instructions and job search are linked in Settings", async () => {
    mockGetProfile.mockResolvedValue({ ...sampleProfile, notes: "Use Apply with resume first." } as CandidateProfile);
    render(<Profile />);
    expect(await screen.findByText("personal.dateOfBirth")).toBeInTheDocument();
    // Moved to Settings (Applications, Job search); tested in Settings.test.tsx.
    expect(screen.queryByText("notes.title")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("Use Apply with resume first.")).not.toBeInTheDocument();
    expect(screen.queryByText("jobSearch.title")).not.toBeInTheDocument();
    expect(screen.queryByText("searchArea.title")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "movedJobSearch" })).toHaveAttribute("href", "#/settings?section=job-search");
    expect(screen.getByRole("link", { name: "movedInstructions" })).toHaveAttribute("href", "#/settings?section=applications");
  });

  it("renders fetched profile values into the inputs", async () => {
    render(<Profile />);
    expect(await screen.findByDisplayValue("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByDisplayValue("ada@example.com")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Engineer")).toBeInTheDocument();
    // Tags from the loaded profile render (target job titles are in Settings → Job search now).
    expect(screen.getByText("Python")).toBeInTheDocument();
  });

  it("warns when the email isn't an email address, and not when it is", async () => {
    const user = userEvent.setup();
    render(<Profile />);
    const email = await screen.findByDisplayValue("ada@example.com");
    expect(screen.queryByText("personal.emailInvalid")).not.toBeInTheDocument();

    await user.clear(email);
    await user.type(email, "Zork");
    expect(screen.getByRole("alert")).toHaveTextContent("personal.emailInvalid");

    await user.type(email, "@example.com");
    expect(screen.queryByText("personal.emailInvalid")).not.toBeInTheDocument();
  });

  it("populates the country dropdown from getCountries and reflects the selection", async () => {
    render(<Profile />);
    await screen.findByRole("button", { name: "save" });

    const countrySelect = screen.getByDisplayValue("🇺🇸 United States") as HTMLSelectElement;
    expect(countrySelect.value).toBe("US");
    expect(within(countrySelect).getByRole("option", { name: /Germany/ })).toBeInTheDocument();
  });

  it("edits a field and saves, calling saveProfile with the updated payload", async () => {
    const user = userEvent.setup();
    render(<Profile />);
    await screen.findByDisplayValue("Ada Lovelace");

    const nameInput = screen.getByDisplayValue("Ada Lovelace");
    await user.clear(nameInput);
    await user.type(nameInput, "Grace Hopper");

    await user.click(screen.getByRole("button", { name: "save" }));

    await waitFor(() =>
      expect(mockSaveProfile).toHaveBeenCalledWith(
        expect.objectContaining({ name: "Grace Hopper" }),
      ),
    );
    // Success state flips the button label to the "saved" key.
    expect(await screen.findByRole("button", { name: "saved" })).toBeInTheDocument();
  });

  it("adds and removes a tag, persisting the change on save", async () => {
    const user = userEvent.setup();
    render(<Profile />);
    await screen.findByDisplayValue("Ada Lovelace");

    // Add a new skill via the skills TagInput.
    const skillInput = screen.getByPlaceholderText("skills.placeholder");
    await user.type(skillInput, "TypeScript{Enter}");
    expect(await screen.findByText("TypeScript")).toBeInTheDocument();

    // Remove an existing skill ("Python") — its remove button is the sibling of the tag text.
    const pythonTag = screen.getByText("Python").closest("span")!;
    await user.click(within(pythonTag).getByRole("button"));
    await waitFor(() => expect(screen.queryByText("Python")).not.toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "save" }));
    await waitFor(() => {
      const payload = mockSaveProfile.mock.calls.at(-1)![0];
      expect(payload.skills).toContain("TypeScript");
      expect(payload.skills).not.toContain("Python");
    });
  });

  it("changing country reveals country-conditional fields (Germany: notice period, nationality)", async () => {
    const user = userEvent.setup();
    render(<Profile />);
    await screen.findByRole("button", { name: "save" });

    const countrySelect = screen.getByDisplayValue("🇺🇸 United States");
    await user.selectOptions(countrySelect, "DE");

    // Germany config turns on nationality + notice period.
    expect(await screen.findByText("personal.nationality")).toBeInTheDocument();
    expect(screen.getByText("work.noticePeriod")).toBeInTheDocument();
    expect(screen.getByText("coverLetter.title")).toBeInTheDocument();
  });

  it("a new country brings its currency unless the user picked one", async () => {
    const user = userEvent.setup();
    render(<Profile />);
    await screen.findByRole("button", { name: "save" });
    await user.selectOptions(screen.getByDisplayValue("🇺🇸 United States"), "DE");
    await user.click(screen.getByRole("button", { name: "save" }));
    await waitFor(() => expect(mockSaveProfile).toHaveBeenCalled());
    const saved = mockSaveProfile.mock.calls[0][0];
    expect(saved.salary_expectation.currency).toBe("EUR");     // was the US default: follows the country
    expect(saved.salary_expectation.period).toBe("annual");
  });

  it("a currency the user chose stays when the country changes", async () => {
    mockGetProfile.mockResolvedValue({
      ...sampleProfile, salary_expectation: { min: 1, max: 2, currency: "GBP", period: "annual" },
    } as CandidateProfile);
    const user = userEvent.setup();
    render(<Profile />);
    await screen.findByRole("button", { name: "save" });
    await user.selectOptions(screen.getByDisplayValue("🇺🇸 United States"), "DE");
    await user.click(screen.getByRole("button", { name: "save" }));
    await waitFor(() => expect(mockSaveProfile).toHaveBeenCalled());
    expect(mockSaveProfile.mock.calls[0][0].salary_expectation.currency).toBe("GBP");
  });

  it("shows an error banner when saveProfile rejects (page stays mounted)", async () => {
    mockSaveProfile.mockRejectedValueOnce(new Error("network down"));
    const user = userEvent.setup();
    render(<Profile />);
    await screen.findByRole("button", { name: "save" });

    await user.click(screen.getByRole("button", { name: "save" }));

    expect(await screen.findByText("saveError")).toBeInTheDocument();
    // Form is still present.
    expect(screen.getByDisplayValue("Ada Lovelace")).toBeInTheDocument();
  });

  it("a failed profile load offers Retry, never a form whose blanks could be saved", async () => {
    const user = userEvent.setup();
    mockGetProfile.mockRejectedValueOnce(new Error("boom"));
    render(<Profile />);

    expect(await screen.findByText("loadError")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "save" })).not.toBeInTheDocument();
    expect(mockSaveProfile).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "retry" }));
    expect(await screen.findByDisplayValue("Ada Lovelace")).toBeInTheDocument();
  });

  it("a failed country list alone still shows the profile", async () => {
    mockGetCountries.mockRejectedValueOnce(new Error("boom"));
    render(<Profile />);
    expect(await screen.findByDisplayValue("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "save" })).toBeInTheDocument();
  });

  it("sets a field-of-study fallback (the search area is in Settings → Job search)", async () => {
    const user = userEvent.setup();
    render(<Profile />);
    await screen.findByDisplayValue("Ada Lovelace");
    await user.type(screen.getByPlaceholderText("education.majorAlternativePlaceholder"), "Finance");
    await user.click(screen.getByRole("button", { name: "save" }));
    await waitFor(() =>
      expect(mockSaveProfile).toHaveBeenCalledWith(expect.objectContaining({
        education: expect.objectContaining({ major_alternative: "Finance" }),
      })),
    );
  });
});

describe("Profile labels", () => {
  beforeEach(() => {
    mockGetProfile.mockResolvedValue(sampleProfile as CandidateProfile);
  });

  it("each field is named by its label (screen readers)", async () => {
    render(<Profile />);
    await screen.findByRole("button", { name: "save" });
    expect(screen.getByLabelText("country.label")).toHaveValue("US");
    expect(screen.getAllByLabelText(/./).length).toBeGreaterThan(5);
  });

  it("saving keeps what Settings saved meanwhile (job search, AI instructions): only Profile's own fields", async () => {
    const user = userEvent.setup();
    const elsewhere = {
      ...sampleProfile,
      target_job_titles: ["Data Analyst"],
      target_locations: ["Remote"],
      search_area: { center_zip: "02139", radius: "25" },
      job_search: { mode: "detailed", notes: "Mornings only" },
      notes: "New instructions",
    };
    mockGetProfile
      .mockResolvedValueOnce({ ...sampleProfile } as CandidateProfile)
      .mockResolvedValueOnce(elsewhere as unknown as CandidateProfile);
    render(<Profile />);
    const name = await screen.findByDisplayValue("Ada Lovelace");
    await user.clear(name);
    await user.type(name, "Grace Hopper");
    await user.click(screen.getByRole("button", { name: "save" }));
    await waitFor(() => expect(mockSaveProfile).toHaveBeenCalledTimes(1));
    const saved = mockSaveProfile.mock.calls[0][0];
    expect(saved.name).toBe("Grace Hopper");
    expect(saved.target_job_titles).toEqual(["Data Analyst"]);
    expect(saved.target_locations).toEqual(["Remote"]);
    expect(saved.search_area).toEqual({ center_zip: "02139", radius: "25" });
    expect(saved.job_search).toEqual({ mode: "detailed", notes: "Mornings only" });
    expect(saved.notes).toBe("New instructions");
  });
});
