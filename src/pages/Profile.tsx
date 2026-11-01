import { useState, useEffect, useCallback, useId } from "react";
import { Save } from "lucide-react";
import { getProfile, saveProfile, getCountries } from "../lib/api";
import type { CandidateProfile, CountryConfig } from "../lib/types";
import { PageHeader, LoadingSpinner, Section } from "../components/ui";
import TagInputShared from "../components/ui/TagInput";
import JobSearchForm from "../components/JobSearchForm";
import { DEFAULT_JOB_SEARCH, withDefaults } from "../lib/jobSearch";
import { useTranslation } from "react-i18next";

const defaultProfile: CandidateProfile = {
  name: "",
  email: "",
  phone: "",
  phone_country_code: "+1",
  country: "US",
  address: { street: "", city: "", state: "", zip: "", country: "USA" },
  work_authorization: "",
  visa_sponsorship_needed: false,
  willing_to_relocate: false,
  preferred_work_mode: "hybrid",
  years_of_experience: 0,
  education: { degree: "", major: "", major_alternative: "", school: "", start_date: "", graduation: "" },
  current_role: "",
  target_job_titles: [],
  target_locations: [],
  languages: ["English"],
  skills: [],
  salary_expectation: { min: 50000, max: 100000, currency: "USD", period: "annual" },
  notice_period: "",
  nationality: "",
  date_of_birth: "",
  cover_letter: "",
  date_format: "MM/DD/YYYY",
  notes: "",
  screening: {
    start_date: "", gpa: "", linkedin_url: "", portfolio_url: "",
    gender: "", race_ethnicity: "", veteran_status: "", disability_status: "",
  },
  search_area: { center_zip: "", radius: "any" },
  job_search: DEFAULT_JOB_SEARCH,
};

export default function Profile() {
  const uid = useId();  // links each label to its field
  const { t } = useTranslation("profile");
  const [profile, setProfile] = useState<CandidateProfile>(defaultProfile);
  const [countries, setCountries] = useState<Record<string, CountryConfig>>({});
  const [noticePeriodOptions, setNoticePeriodOptions] = useState<string[]>([]);
  const [newTitle, setNewTitle] = useState("");
  const [newSkill, setNewSkill] = useState("");
  const [newLocation, setNewLocation] = useState("");
  const [newLanguage, setNewLanguage] = useState("");
  const [saved, setSaved] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // The profile couldn't be read: no form (saving its blanks would replace it), just Retry.
  const [loadFailed, setLoadFailed] = useState(false);

  const countryConfig = profile.country ? countries[profile.country] : null;

  const load = useCallback(() => {
    Promise.all([
      getProfile().then((data) => setProfile({
        ...defaultProfile,
        ...data,
        screening: { ...defaultProfile.screening!, ...(data.screening || {}) },
        search_area: { ...defaultProfile.search_area!, ...(data.search_area || {}) },
        job_search: withDefaults(data.job_search),
        education: { ...defaultProfile.education, ...(data.education || {}) },
      })).catch(() => setLoadFailed(true)),
      getCountries().then((data) => {
        setCountries(data.countries);
        setNoticePeriodOptions(data.notice_period_options || []);
      }).catch(() => {}),
    ]).finally(() => setLoading(false));
  }, []);
  useEffect(() => { load(); }, [load]);
  const retry = () => {
    setLoading(true);
    setLoadFailed(false);
    load();
  };

  const handleCountryChange = (code: string) => {
    const config = countries[code];
    if (!config) return;
    setProfile((p) => {
      // The old country's defaults follow the country; anything the user picked stays.
      // (Switching from the US to Germany used to keep USD but switch the period.)
      const previous = countries[p.country];
      const salary = p.salary_expectation;
      return {
        ...p,
        country: code,
        phone_country_code: config.phone_prefix,
        date_format: config.date_format,
        salary_expectation: {
          ...salary,
          currency: !salary.currency || salary.currency === previous?.currency ? config.currency : salary.currency,
          period: (!salary.period || salary.period === previous?.salary_period
            ? config.salary_period : salary.period) as "annual" | "monthly",
        },
      };
    });
    setSaved(false);
  };

  const update = (field: string, value: unknown) => {
    setProfile((p) => ({ ...p, [field]: value }));
    setSaved(false);
  };

  const updateNested = (parent: keyof Pick<CandidateProfile, "address" | "education" | "salary_expectation" | "screening" | "search_area">, field: string, value: unknown) => {
    setProfile((p) => {
      const current = p[parent];
      if (typeof current === "object" && current !== null) {
        return { ...p, [parent]: { ...current, [field]: value } };
      }
      return p;
    });
    setSaved(false);
  };

  const addToList = (field: keyof CandidateProfile, value: string, setter: (v: string) => void) => {
    if (!value.trim()) return;
    const list = profile[field] as string[];
    if (!list.includes(value.trim())) {
      update(field, [...list, value.trim()]);
    }
    setter("");
  };

  const removeFromList = (field: keyof CandidateProfile, value: string) => {
    update(field, (profile[field] as string[]).filter((v) => v !== value));
  };

  const handleSave = async () => {
    try {
      setSaveError(null);
      await saveProfile(profile);
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch (e) {
      setSaveError(t("saveError"));
      console.error("Failed to save profile:", e);
    }
  };

  if (loading) return <LoadingSpinner />;

  if (loadFailed) {
    return (
      <div className="max-w-3xl">
        <div role="alert" className="error-banner flex items-center justify-between gap-3">
          <span>{t("loadError")}</span>
          <button type="button" onClick={retry} className="btn-secondary">{t("retry")}</button>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-3xl">
      <PageHeader
        title={t("title")}
        subtitle={t("subtitle")}
        actions={
          <button onClick={handleSave} className="btn-primary">
            <Save className="w-4 h-4" />
            {saved ? t("saved") : t("save")}
          </button>
        }
      />
      {saveError && (
        <div className="error-banner mb-5">{saveError}</div>
      )}

      {/* Country Selection */}
      <Section title={t("country.title")}>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f1`}>{t("country.label")}</label>
            <select id={`${uid}-f1`}
              value={profile.country}
              onChange={(e) => handleCountryChange(e.target.value)}
              className="w-full px-3 py-2 border border-border rounded-lg text-sm bg-card"
            >
              <option value="">{t("country.selectPlaceholder")}</option>
              {Object.entries(countries).map(([code, config]) => (
                <option key={code} value={code}>{config.flag} {config.name}</option>
              ))}
            </select>
          </div>
          {countryConfig && (
            <div className="flex items-end">
              <p className="text-sm text-muted-foreground">
                {t("country.dateFormat", { format: countryConfig.date_format })} &middot; {t("country.currency", { currency: countryConfig.currency })}
              </p>
            </div>
          )}
        </div>
      </Section>

      {/* Personal Info */}
      <Section title={t("personal.title")}>
        <div className="grid grid-cols-2 gap-4">
          <Input label={t("personal.fullName")} value={profile.name} onChange={(v) => update("name", v)} />
          <div>
            <Input label={t("personal.email")} type="email" value={profile.email} onChange={(v) => update("email", v)} />
            {profile.email.trim() !== "" && !looksLikeEmail(profile.email) && (
              <p role="alert" className="text-[12px] text-destructive mt-1">{t("personal.emailInvalid")}</p>
            )}
          </div>
          <div>
            <label className="block text-sm font-semibold text-foreground mb-1.5">{t("personal.phone")}</label>
            <div className="flex gap-2">
              <input
                type="text"
                value={profile.phone_country_code}
                onChange={(e) => update("phone_country_code", e.target.value)}
                className="input-base w-20"
                placeholder={t("personal.phonePrefix")}
              />
              <input
                type="text"
                value={profile.phone}
                onChange={(e) => update("phone", e.target.value)}
                className="input-base flex-1"
                placeholder={t("personal.phonePlaceholder")}
              />
            </div>
          </div>
          <Input label={t("personal.currentRole")} value={profile.current_role} onChange={(v) => update("current_role", v)} />
          {countryConfig?.show_nationality && (
            <Input label={t("personal.nationality")} value={profile.nationality} onChange={(v) => update("nationality", v)} />
          )}
          <Input label={t("personal.dateOfBirth")} value={profile.date_of_birth} onChange={(v) => update("date_of_birth", v)} placeholder={countryConfig?.date_format || "MM/DD/YYYY"} />
        </div>
      </Section>

      {/* Address */}
      <Section title={t("address.title")}>
        <div className="grid grid-cols-2 gap-4">
          <Input label={t("address.street")} value={profile.address.street} onChange={(v) => updateNested("address", "street", v)} className="col-span-2" />
          <Input label={t("address.city")} value={profile.address.city} onChange={(v) => updateNested("address", "city", v)} />
          <Input label={countryConfig?.address_labels?.state || t("address.state")} value={profile.address.state} onChange={(v) => updateNested("address", "state", v)} />
          <Input label={countryConfig?.address_labels?.zip || t("address.zip")} value={profile.address.zip} onChange={(v) => updateNested("address", "zip", v)} />
          <Input label={t("address.country")} value={profile.address.country} onChange={(v) => updateNested("address", "country", v)} />
        </div>
      </Section>

      {/* Work Preferences */}
      <Section title={t("work.title")}>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-semibold text-foreground mb-1.5">{t("work.workAuth")}</label>
            {countryConfig?.work_auth_options ? (
              <select
                value={profile.work_authorization}
                onChange={(e) => update("work_authorization", e.target.value)}
                className="w-full px-3 py-2 border border-border rounded-lg text-sm bg-card"
              >
                <option value="">{t("work.workAuthPlaceholder")}</option>
                {countryConfig.work_auth_options.map((opt) => (
                  <option key={opt} value={opt}>{opt}</option>
                ))}
              </select>
            ) : (
              <input
                type="text"
                value={profile.work_authorization}
                onChange={(e) => update("work_authorization", e.target.value)}
                className="input-base"
              />
            )}
          </div>
          <div>
            <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f2`}>{t("work.workMode")}</label>
            <select id={`${uid}-f2`}
              value={profile.preferred_work_mode}
              onChange={(e) => update("preferred_work_mode", e.target.value)}
              className="w-full px-3 py-2 border border-border rounded-lg text-sm bg-card"
            >
              <option value="remote">{t("work.remote")}</option>
              <option value="hybrid">{t("work.hybrid")}</option>
              <option value="onsite">{t("work.onsite")}</option>
              <option value="any">{t("work.any")}</option>
            </select>
          </div>
          <Checkbox label={t("work.visaSponsorship")} checked={profile.visa_sponsorship_needed} onChange={(v) => update("visa_sponsorship_needed", v)} />
          <Checkbox label={t("work.willingToRelocate")} checked={profile.willing_to_relocate} onChange={(v) => update("willing_to_relocate", v)} />
          <Input label={t("work.yearsOfExperience")} type="number" value={String(profile.years_of_experience)} onChange={(v) => update("years_of_experience", Number(v))} />
          {countryConfig?.show_notice_period && (
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f3`}>{t("work.noticePeriod")}</label>
              <select id={`${uid}-f3`}
                value={profile.notice_period}
                onChange={(e) => update("notice_period", e.target.value)}
                className="w-full px-3 py-2 border border-border rounded-lg text-sm bg-card"
              >
                <option value="">{t("work.noticePeriodPlaceholder")}</option>
                {noticePeriodOptions.map((opt) => (
                  <option key={opt} value={opt}>{opt}</option>
                ))}
              </select>
            </div>
          )}
        </div>
      </Section>

      {/* Search area */}
      <Section title={t("searchArea.title")}>
        <p className="text-[13px] text-muted-foreground mb-3">{t("searchArea.hint")}</p>
        <div className="grid grid-cols-2 gap-4">
          <Input
            label={t("searchArea.centerZip")}
            value={profile.search_area?.center_zip ?? ""}
            placeholder={profile.address.zip || "e.g. 10001"}
            onChange={(v) => updateNested("search_area", "center_zip", v)}
          />
          <div>
            <label htmlFor="search-radius" className="block text-sm font-semibold text-foreground mb-1.5">{t("searchArea.radius")}</label>
            <select
              id="search-radius"
              value={profile.search_area?.radius ?? "any"}
              onChange={(e) => updateNested("search_area", "radius", e.target.value)}
              className="input-base"
            >
              <option value="25">{t("searchArea.under", { miles: 25 })}</option>
              <option value="50">{t("searchArea.under", { miles: 50 })}</option>
              <option value="100">{t("searchArea.under", { miles: 100 })}</option>
              <option value="any">{t("searchArea.any")}</option>
            </select>
          </div>
        </div>
      </Section>

      {/* Education */}
      <Section title={t("education.title")}>
        <div className="grid grid-cols-2 gap-4">
          <Input label={t("education.degree")} value={profile.education.degree} onChange={(v) => updateNested("education", "degree", v)} />
          <Input label={t("education.major")} value={profile.education.major ?? ""} onChange={(v) => updateNested("education", "major", v)} />
          <Input label={t("education.majorAlternative")} value={profile.education.major_alternative ?? ""} placeholder={t("education.majorAlternativePlaceholder")} onChange={(v) => updateNested("education", "major_alternative", v)} />
          <Input label={t("education.school")} value={profile.education.school} onChange={(v) => updateNested("education", "school", v)} />
          <Input label={t("education.startDate")} value={profile.education.start_date ?? ""} placeholder={t("education.startDatePlaceholder")} onChange={(v) => updateNested("education", "start_date", v)} />
          <Input label={t("education.graduation")} value={profile.education.graduation} onChange={(v) => updateNested("education", "graduation", v)} />
        </div>
      </Section>

      {/* Salary */}
      <Section title={t("salary.title")}>
        <div className="grid grid-cols-2 gap-4">
          <Input label={t("salary.minimum")} type="number" value={String(profile.salary_expectation.min)} onChange={(v) => updateNested("salary_expectation", "min", Number(v))} />
          <Input label={t("salary.maximum")} type="number" value={String(profile.salary_expectation.max)} onChange={(v) => updateNested("salary_expectation", "max", Number(v))} />
          <Input label={t("salary.currency")} value={profile.salary_expectation.currency} onChange={(v) => updateNested("salary_expectation", "currency", v)} />
          <div>
            <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f4`}>{t("salary.period")}</label>
            <select id={`${uid}-f4`}
              value={profile.salary_expectation.period}
              onChange={(e) => updateNested("salary_expectation", "period", e.target.value)}
              className="w-full px-3 py-2 border border-border rounded-lg text-sm bg-card"
            >
              <option value="annual">{t("salary.annual")}</option>
              <option value="monthly">{t("salary.monthly")}</option>
              <option value="hourly">{t("salary.hourly")}</option>
            </select>
          </div>
        </div>
      </Section>

      {/* Screening answers */}
      <Section title={t("screening.title")}>
        <p className="text-[13px] text-muted-foreground mb-3">{t("screening.hint")}</p>
        <div className="grid grid-cols-2 gap-4">
          <Input label={t("screening.startDate")} value={profile.screening?.start_date ?? ""} onChange={(v) => updateNested("screening", "start_date", v)} placeholder={t("screening.startDatePlaceholder")} />
          <Input label={t("screening.gpa")} value={profile.screening?.gpa ?? ""} onChange={(v) => updateNested("screening", "gpa", v)} placeholder="3.6" />
          <Input label={t("screening.linkedin")} value={profile.screening?.linkedin_url ?? ""} onChange={(v) => updateNested("screening", "linkedin_url", v)} placeholder="https://linkedin.com/in/..." />
          <Input label={t("screening.portfolio")} value={profile.screening?.portfolio_url ?? ""} onChange={(v) => updateNested("screening", "portfolio_url", v)} placeholder="https://github.com/..." />
          <Input label={t("screening.gender")} value={profile.screening?.gender ?? ""} onChange={(v) => updateNested("screening", "gender", v)} placeholder={t("screening.declinePlaceholder")} />
          <Input label={t("screening.race")} value={profile.screening?.race_ethnicity ?? ""} onChange={(v) => updateNested("screening", "race_ethnicity", v)} placeholder={t("screening.declinePlaceholder")} />
          <Input label={t("screening.veteran")} value={profile.screening?.veteran_status ?? ""} onChange={(v) => updateNested("screening", "veteran_status", v)} placeholder={t("screening.declinePlaceholder")} />
          <Input label={t("screening.disability")} value={profile.screening?.disability_status ?? ""} onChange={(v) => updateNested("screening", "disability_status", v)} placeholder={t("screening.declinePlaceholder")} />
        </div>
      </Section>

      {/* Cover Letter (conditional) */}
      {countryConfig?.show_cover_letter && (
        <Section title={t("coverLetter.title")}>
          <p className="text-sm text-muted-foreground mb-2">
            {t("coverLetter.description")}
          </p>
          <textarea value={profile.cover_letter} onChange={(e) => update("cover_letter", e.target.value)}
            rows={6} className="input-base"
            placeholder={t("coverLetter.placeholder")} />
        </Section>
      )}

      {/* Tag Lists */}
      <Section title={t("targetJobTitles.title")}>
        <TagInputShared tags={profile.target_job_titles} value={newTitle} onChange={setNewTitle}
          onAdd={() => addToList("target_job_titles", newTitle, setNewTitle)}
          onRemove={(v) => removeFromList("target_job_titles", v)} placeholder={t("targetJobTitles.placeholder")} />
      </Section>

      <Section title={t("targetLocations.title")}>
        <TagInputShared tags={profile.target_locations} value={newLocation} onChange={setNewLocation}
          onAdd={() => addToList("target_locations", newLocation, setNewLocation)}
          onRemove={(v) => removeFromList("target_locations", v)} placeholder={t("targetLocations.placeholder")} />
      </Section>

      <Section title={t("jobSearch.title")}>
        <p className="text-[13px] text-muted-foreground mb-3">{t("jobSearch.hint")}</p>
        <JobSearchForm value={withDefaults(profile.job_search)} onChange={(v) => update("job_search", v)}
          currency={profile.salary_expectation.currency || "USD"} />
      </Section>

      <Section title={t("skills.title")}>
        <TagInputShared tags={Array.isArray(profile.skills) ? profile.skills : []} value={newSkill} onChange={setNewSkill}
          onAdd={() => addToList("skills", newSkill, setNewSkill)}
          onRemove={(v) => removeFromList("skills", v)} placeholder={t("skills.placeholder")} />
      </Section>

      <Section title={t("languages.title")}>
        <TagInputShared tags={profile.languages} value={newLanguage} onChange={setNewLanguage}
          onAdd={() => addToList("languages", newLanguage, setNewLanguage)}
          onRemove={(v) => removeFromList("languages", v)} placeholder={t("languages.placeholder")} />
      </Section>

      {/* Notes */}
      <Section title={t("notes.title")}>
        <p className="text-[13px] text-muted-foreground mb-2">{t("notes.hint")}</p>
        <textarea value={profile.notes} onChange={(e) => update("notes", e.target.value)}
          rows={8} className="input-base"
          placeholder={t("notes.placeholder")} />
      </Section>
    </div>
  );
}

/** Same rule the apply agent uses (cli/apply_jobs.py _looks_like_email). */
function looksLikeEmail(value: string): boolean {
  return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value.trim());
}

function Input({ label, value, onChange, type = "text", className = "", placeholder = "" }: {
  label: string; value: string; onChange: (v: string) => void; type?: string; className?: string; placeholder?: string;
}) {
  const id = useId();
  return (
    <div className={className}>
      <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={id}>{label}</label>
      <input id={id} type={type} value={value} onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder} className="input-base" />
    </div>
  );
}

function Checkbox({ label, checked, onChange }: {
  label: string; checked: boolean; onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex items-center gap-2.5 cursor-pointer">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)}
        className="w-4 h-4 rounded-md border-border accent-primary" />
      <span className="text-sm text-foreground">{label}</span>
    </label>
  );
}
