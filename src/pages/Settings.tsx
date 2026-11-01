import { useState, useEffect, useCallback } from "react";
import { Save, FolderOpen, X, Upload, Trash2, Sun, Moon, Monitor } from "lucide-react";
import {
  getSettings, saveSettings, getPlugins, togglePlugin, removePlugin, importPlugin, getBrowsers,
  getEmployerPasswordSites, revealEmployerPassword,
} from "../lib/api";
import { setTelemetryEnabled as setAnalyticsTelemetry } from "../lib/analytics";
import { getStoredTheme, setTheme, type ThemeMode } from "../lib/theme";
import { open } from "@tauri-apps/plugin-dialog";
import { useTranslation } from "react-i18next";
import { PageHeader, LoadingSpinner, Section, PasswordInput } from "../components/ui";
import TagInput from "../components/ui/TagInput";
import LoginCards from "../components/LoginCards";
import type { PluginConfig, InstalledBrowser, OtherDocument } from "../lib/types";

export default function SettingsPage() {
  const { t } = useTranslation("settings");
  const [resumePath, setResumePath] = useState("");
  const [blockedDomains, setBlockedDomains] = useState<string[]>([]);
  const [newDomain, setNewDomain] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [maxFailures, setMaxFailures] = useState(8);
  const [jobCollectionEnabled, setJobCollectionEnabled] = useState(true);
  const [stealthEnabled, setStealthEnabled] = useState(false);
  const [humanDelayEnabled, setHumanDelayEnabled] = useState(false);
  const [humanDelayMin, setHumanDelayMin] = useState(5);
  const [humanDelayMax, setHumanDelayMax] = useState(15);
  const [captchaEnabled, setCaptchaEnabled] = useState(false);
  const [captchaApiKey, setCaptchaApiKey] = useState("");
  const [telemetryEnabled, setTelemetryEnabled] = useState(false);
  const [theme, setThemeState] = useState<ThemeMode>(getStoredTheme());
  const [saved, setSaved] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // Settings couldn't be read: no form (saving its defaults would replace them), just Retry.
  const [loadFailed, setLoadFailed] = useState(false);
  const [plugins, setPlugins] = useState<PluginConfig[]>([]);
  const [browser, setBrowser] = useState("");
  const [browserPath, setBrowserPath] = useState("");
  const [browsers, setBrowsers] = useState<InstalledBrowser[]>([]);
  const [otherDocuments, setOtherDocuments] = useState<OtherDocument[]>([]);
  const [performanceMode, setPerformanceMode] = useState(false);
  const [fitCheckEnabled, setFitCheckEnabled] = useState(true);
  const [coverLetterBoxes, setCoverLetterBoxes] = useState(false);
  const [parallelJobs, setParallelJobs] = useState(1);

  const load = useCallback(() => {
    Promise.all([
      getSettings().then((data) => {
        setResumePath(data.resume_path || "");
        setOtherDocuments(data.other_documents || []);
        setPerformanceMode(data.performance_mode === true);
        setFitCheckEnabled(data.fit_check_enabled !== false);
        setCoverLetterBoxes(data.cover_letter_boxes === true);
        setParallelJobs(Math.max(1, Math.min(4, data.parallel_jobs || 1)));
        setBlockedDomains(data.blocked_domains || []);
        setMaxFailures(data.max_failures || 8);
        setTelemetryEnabled(data.telemetry_enabled === true);  // opt-in
        // Reconcile theme: a value saved on the backend wins over the local
        // default and is applied immediately.
        if (data.theme && data.theme !== getStoredTheme()) {
          setThemeState(data.theme);
          setTheme(data.theme);
        }
        const sens = data.sensitive_data || { email: "", password: "" };
        setEmail(sens.email || "");
        setPassword(sens.password || "");
        // New switches: fall back to the shipped defaults when absent, so an
        // older settings.json keeps today's behaviour.
        setJobCollectionEnabled(data.job_collection_enabled !== false);
        setStealthEnabled(data.stealth_enabled === true);
        setHumanDelayEnabled(data.human_delay_enabled === true);
        setHumanDelayMin(data.human_delay_min ?? 5);
        setHumanDelayMax(data.human_delay_max ?? 15);
        setCaptchaEnabled(data.captcha_solver_enabled === true);
        setCaptchaApiKey(data.captcha_api_key || "");
        setBrowser(data.browser || "");
        setBrowserPath(data.browser_path || "");
      }).catch(() => setLoadFailed(true)),
      getPlugins().then((res) => setPlugins(res.plugins || [])).catch(() => {}),
      getBrowsers().then((res) => setBrowsers(res.browsers || [])).catch(() => {}),
    ]).finally(() => setLoading(false));
  }, []);
  useEffect(() => { load(); }, [load]);
  const retry = () => {
    setLoading(true);
    setLoadFailed(false);
    load();
  };

  const handleTogglePlugin = async (name: string, enabled: boolean) => {
    await togglePlugin(name, enabled);
    setPlugins((prev) => prev.map((p) => p.name === name ? { ...p, enabled } : p));
  };

  const handleRemovePlugin = async (name: string) => {
    await removePlugin(name);
    setPlugins((prev) => prev.filter((p) => p.name !== name));
  };

  const handleImportPlugin = async () => {
    const file = await open({ filters: [{ name: "YAML Plugin", extensions: ["yaml", "yml"] }] });
    if (file) {
      try {
        await importPlugin(file as string);
        const res = await getPlugins();
        setPlugins(res.plugins || []);
      } catch (e) {
        alert(e instanceof Error ? e.message : "Failed to import plugin");
      }
    }
  };

  const handleThemeChange = (mode: ThemeMode) => {
    setThemeState(mode);
    setTheme(mode); // apply + persist locally immediately
    saveSettings({ theme: mode }).catch(() => {}); // mirror to backend
  };

  const addDomain = () => {
    if (newDomain.trim() && !blockedDomains.includes(newDomain.trim())) {
      setBlockedDomains([...blockedDomains, newDomain.trim()]);
      setNewDomain("");
    }
  };

  const removeDomain = (d: string) => {
    setBlockedDomains(blockedDomains.filter((x) => x !== d));
  };

  const handleSave = async () => {
    try {
      setSaveError(null);
      await saveSettings({
        resume_path: resumePath,
        blocked_domains: blockedDomains,
        sensitive_data: { email, password },
        max_failures: maxFailures,
        telemetry_enabled: telemetryEnabled,
        theme,
        job_collection_enabled: jobCollectionEnabled,
        stealth_enabled: stealthEnabled,
        human_delay_enabled: humanDelayEnabled,
        human_delay_min: humanDelayMin,
        human_delay_max: humanDelayMax,
        captcha_solver_enabled: captchaEnabled,
        captcha_api_key: captchaApiKey,
        browser,
        browser_path: browserPath,
        other_documents: otherDocuments.filter((d) => d.path.trim()),
        performance_mode: performanceMode,
        fit_check_enabled: fitCheckEnabled,
        cover_letter_boxes: coverLetterBoxes,
        parallel_jobs: parallelJobs,
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch (e) {
      setSaveError("Failed to save settings. Please try again.");
      console.error("Failed to save settings:", e);
    }
  };

  if (loading) return <LoadingSpinner />;

  if (loadFailed) {
    return (
      <div className="max-w-2xl">
        <PageHeader title="Settings" subtitle="General application settings" />
        <div role="alert" className="error-banner flex items-center justify-between gap-3">
          <span>Couldn't load your settings, so they can't be changed right now (saving could replace them with defaults).</span>
          <button type="button" onClick={retry} className="btn-secondary">Try again</button>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-2xl">
      <PageHeader
        title="Settings"
        subtitle="General application settings"
        actions={
          <button onClick={handleSave} className="btn-primary">
            <Save className="w-4 h-4" />
            {saved ? "Saved" : "Save Settings"}
          </button>
        }
      />
      {saveError && (
        <div className="error-banner mb-5 flex items-center justify-between">
          {saveError}
          <button onClick={() => setSaveError(null)} aria-label="Dismiss" className="text-destructive hover:text-destructive/80"><X className="w-4 h-4" /></button>
        </div>
      )}

      {/* Appearance / Theme */}
      <Section title={t("appearance.title", "Appearance")}>
        <label className="block text-sm font-semibold text-foreground mb-2">{t("appearance.theme", "Theme")}</label>
        <div className="flex gap-2" role="radiogroup" aria-label={t("appearance.theme", "Theme")}>
          {([
            { mode: "light" as const, label: t("appearance.light", "Light"), Icon: Sun },
            { mode: "dark" as const, label: t("appearance.dark", "Dark"), Icon: Moon },
            { mode: "system" as const, label: t("appearance.system", "System"), Icon: Monitor },
          ]).map(({ mode, label, Icon }) => (
            <button
              key={mode}
              role="radio"
              aria-checked={theme === mode}
              onClick={() => handleThemeChange(mode)}
              className={`filter-tab flex items-center gap-1.5 ${theme === mode ? "filter-tab-active" : "filter-tab-inactive"}`}
            >
              <Icon className="w-3.5 h-3.5" /> {label}
            </button>
          ))}
        </div>
        <p className="text-[13px] text-muted-foreground mt-2">{t("appearance.description", "Choose light, dark, or follow your system setting.")}</p>
      </Section>

      {/* Resume */}
      <Section title="Resume">
        <div className="flex gap-2">
          <input value={resumePath} onChange={(e) => setResumePath(e.target.value)}
            placeholder="/path/to/your/resume.pdf"
            className="input-base flex-1" />
          <button onClick={async () => {
              const file = await open({ filters: [{ name: "PDF", extensions: ["pdf"] }] });
              if (file) setResumePath(file as string);
            }}
            className="btn-secondary">
            <FolderOpen className="w-4 h-4" /> Browse
          </button>
        </div>
        <p className="text-[13px] text-muted-foreground mt-2">PDF resume file used for job applications</p>

        <div className="mt-5">
          <p className="text-sm font-semibold text-foreground">Other documents</p>
          <p className="text-[13px] text-muted-foreground mt-1 mb-2">
            Files the agent may upload when a form asks for them, like a transcript or a writing sample.
            When a form requires a file you haven't added, the job is marked "Needs you".
          </p>
          {otherDocuments.map((doc, i) => (
            <div key={i} className="flex gap-2 mb-2">
              <input
                value={doc.label}
                aria-label="Document name"
                placeholder="e.g. Transcript"
                onChange={(e) => setOtherDocuments(otherDocuments.map((d, j) => (j === i ? { ...d, label: e.target.value } : d)))}
                className="input-base !w-44"
              />
              <input
                value={doc.path}
                aria-label="Document file"
                placeholder="/path/to/file.pdf"
                onChange={(e) => setOtherDocuments(otherDocuments.map((d, j) => (j === i ? { ...d, path: e.target.value } : d)))}
                className="input-base flex-1"
              />
              <button onClick={async () => {
                  const file = await open({ filters: [{ name: "Documents", extensions: ["pdf", "doc", "docx", "txt"] }] });
                  if (file) setOtherDocuments(otherDocuments.map((d, j) => (j === i ? { ...d, path: file as string } : d)));
                }}
                className="btn-secondary">
                <FolderOpen className="w-4 h-4" /> Browse
              </button>
              <button
                onClick={() => setOtherDocuments(otherDocuments.filter((_, j) => j !== i))}
                className="p-2 text-muted-foreground hover:text-destructive"
                aria-label={`Remove ${doc.label || "document"}`}
              >
                <X className="w-4 h-4" />
              </button>
            </div>
          ))}
          <button onClick={() => setOtherDocuments([...otherDocuments, { label: "", path: "" }])} className="btn-secondary text-xs">
            + Add document
          </button>
        </div>
      </Section>

      {/* AI features */}
      <Section title="AI Features">
        <SettingSwitch
          label="Performance mode"
          checked={performanceMode}
          onChange={setPerformanceMode}
          description={
            "Turns off optional AI work that doesn't change your answers: the fit check, job naming by the agent, " +
            "the agent's per-step learning notes and interview prep sheets. Screenshots, form checks and your " +
            "saved answers stay on, so applications are filled just as carefully. In a test it saved one AI " +
            "request and about 10 seconds per application, and also skips the one automatic retry after a failure."
          }
        />
        <SettingSwitch
          label="Fit check before applying"
          checked={fitCheckEnabled && !performanceMode}
          disabled={performanceMode}
          onChange={setFitCheckEnabled}
          description={
            "Reads each posting first (one small AI request) and skips jobs with a hard requirement you don't meet: " +
            "degree level (MBA / Master's / PhD only), graduation year, citizenship or clearance, or location outside " +
            "your search area. Skipped jobs show the reason, and you can still apply by hand." +
            (performanceMode ? " Off while performance mode is on." : "")
          }
        />
        <SettingSwitch
          label="Fill cover-letter boxes"
          checked={coverLetterBoxes}
          onChange={setCoverLetterBoxes}
          description={
            "When a form has a cover letter box or upload, write a short letter for that job from your profile, " +
            "resume and the posting. Plain text, no formatting, no AI-sounding phrases."
          }
        />
      </Section>

      {/* Gmail / LinkedIn sign-in for the agent's browser */}
      <Section title="Accounts">
        <LoginCards />
      </Section>

      {/* Sensitive Data */}
      <Section title="Account Credentials">
        <p className="text-[13px] text-muted-foreground mb-4">
          The email is used for accounts on employer application sites. Each site the agent
          creates an account on gets its own password, made by the app and listed below. The
          password here is only used to sign in to accounts you created earlier with it.
          Both are saved encrypted for your Windows account. Prefer SSO (Sign in with
          LinkedIn/Google) when a site offers it.
        </p>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label htmlFor="account-email" className="block text-sm font-semibold text-foreground mb-1.5">Email</label>
            <input id="account-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)}
              className="input-base" />
          </div>
          <div>
            <label htmlFor="account-password" className="block text-sm font-semibold text-foreground mb-1.5">Earlier password (sign-in only)</label>
            <PasswordInput id="account-password" value={password} onChange={(e) => setPassword(e.target.value)}
              className="input-base" />
          </div>
        </div>
        <EmployerPasswords />
      </Section>

      {/* Browser */}
      <Section title="Browser">
        <div>
          <label htmlFor="browser-choice" className="block text-sm font-semibold text-foreground mb-1.5">
            Browser the agent uses
          </label>
          <select
            id="browser-choice"
            value={browser}
            onChange={(e) => setBrowser(e.target.value)}
            className="w-full px-3 py-2 border border-border rounded-lg text-sm bg-card max-w-sm"
          >
            <option value="">Automatic (Chrome if installed)</option>
            {browsers.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}{b.is_default ? " (your default browser)" : ""}
              </option>
            ))}
            {/* Keep a saved choice visible even if that browser is no longer found. */}
            {browser && browser !== "custom" && !browsers.some((b) => b.id === browser) && (
              <option value={browser}>{browser} (not found, using Automatic)</option>
            )}
            <option value="custom">Other Chromium browser (enter path)…</option>
          </select>
          {browser === "custom" && (
            <input
              value={browserPath}
              onChange={(e) => setBrowserPath(e.target.value)}
              placeholder="C:\Program Files\...\browser.exe"
              className="input-base font-mono mt-2"
            />
          )}
          <p className="text-[13px] text-muted-foreground mt-1.5">
            Only Chromium-based browsers (Chrome, Edge, Brave, Vivaldi…) can be automated. The agent opens its own
            window with a separate profile for each browser, not your everyday one, so log in to LinkedIn again
            after switching.
          </p>
        </div>
      </Section>

      {/* Agent Settings */}
      <Section title="Agent Settings">
        <div className="mb-5">
          <label htmlFor="parallel-jobs" className="block text-sm font-semibold text-foreground mb-1.5">Jobs at the same time</label>
          <select id="parallel-jobs" value={parallelJobs} onChange={(e) => setParallelJobs(Number(e.target.value))}
            className="input-base !w-32">
            {[1, 2, 3, 4].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <p className="text-[13px] text-muted-foreground mt-1.5">
            How many applications run at once, each in its own browser window with your sign-ins. 2-3 is a good
            range: each window uses about 1 GB of memory, and your AI plan's usage goes up as fast. Two jobs on the
            same site never run at once. Applies from the next run.
          </p>
        </div>
        <div>
          <label className="block text-sm font-semibold text-foreground mb-1.5">Max Failures Per Job</label>
          <input type="number" value={maxFailures} onChange={(e) => {
              const val = Number(e.target.value);
              setMaxFailures(Math.max(1, Math.min(50, isNaN(val) ? 8 : val)));
            }}
            min={1} max={50} className="input-base !w-32" />
          <p className="text-[13px] text-muted-foreground mt-1.5">
            Agent stops trying after this many consecutive failures on a single job.
          </p>
        </div>
      </Section>

      {/* Job Sources */}
      <Section title="Job Sources">
        <SettingSwitch
          label="Job board collection"
          checked={jobCollectionEnabled}
          onChange={setJobCollectionEnabled}
          description="Let the agent search job boards (LinkedIn, Indeed, Handshake, Intern Insider and the other plugins) for listings. When off, the Collect controls are disabled and the collect script exits — pasting links still works."
          className=""
        />
      </Section>

      {/* Anti-detection */}
      <Section title="Anti-Detection">
        <SettingSwitch
          label="Stealth mode"
          checked={stealthEnabled}
          onChange={setStealthEnabled}
          description="Hide the signals that mark the browser as automated (navigator.webdriver, missing plugins, headless WebGL fingerprints) and drop the automation launch flags. The browser already runs visibly, not headless."
          className=""
        />

        <SettingSwitch
          label="Human-like delays"
          checked={humanDelayEnabled}
          onChange={setHumanDelayEnabled}
          description="Wait a random amount of time between applications instead of starting the next one immediately. Slows a run down by roughly the average delay per job."
          className="mt-5 pt-5 border-t border-border"
        />

        {humanDelayEnabled && (
          <div className="flex items-end gap-3 mt-4">
            <div>
              <label htmlFor="delay-min" className="block text-sm font-semibold text-foreground mb-1.5">
                Min seconds
              </label>
              <input
                id="delay-min"
                type="number"
                min={0}
                max={300}
                value={humanDelayMin}
                onChange={(e) => {
                  const v = Number(e.target.value);
                  setHumanDelayMin(Math.max(0, Math.min(300, isNaN(v) ? 5 : v)));
                }}
                className="input-base !w-28"
              />
            </div>
            <div>
              <label htmlFor="delay-max" className="block text-sm font-semibold text-foreground mb-1.5">
                Max seconds
              </label>
              <input
                id="delay-max"
                type="number"
                min={0}
                max={300}
                value={humanDelayMax}
                onChange={(e) => {
                  const v = Number(e.target.value);
                  setHumanDelayMax(Math.max(0, Math.min(300, isNaN(v) ? 15 : v)));
                }}
                className="input-base !w-28"
              />
            </div>
            {humanDelayMax < humanDelayMin && (
              <p className="text-[13px] text-destructive pb-2">Max must be at least the min.</p>
            )}
          </div>
        )}
      </Section>

      {/* CAPTCHA */}
      <Section title="CAPTCHA Solving">
        <SettingSwitch
          label="Solve CAPTCHAs with 2Captcha"
          checked={captchaEnabled}
          onChange={setCaptchaEnabled}
          description="When a CAPTCHA appears, send it to 2Captcha (max 2 attempts per application). If solving fails — or this is off — the job is marked Needs you (so you can apply yourself), and the agent moves to the next job. Your API key is never included in prompts or logs."
          className=""
        />
        {captchaEnabled && (
          <div className="mt-4">
            <label htmlFor="captcha-key" className="block text-sm font-semibold text-foreground mb-1.5">
              2Captcha API key
            </label>
            <PasswordInput
              id="captcha-key"
              value={captchaApiKey}
              onChange={(e) => setCaptchaApiKey(e.target.value)}
              placeholder="Your 2Captcha API key"
              className="input-base"
            />
          </div>
        )}
      </Section>

      {/* Privacy & Telemetry */}
      <Section title="Privacy & Telemetry">
        <SettingSwitch
          label="Usage Analytics"
          checked={telemetryEnabled}
          onChange={(on) => {
            setTelemetryEnabled(on);
            setAnalyticsTelemetry(on);
          }}
          description="Off unless you turn it on. Sends which features you use and error reports (an error message can include a page or file name). Never your profile, resume, answers or job list. Only in builds set up with analytics keys."
          className=""
        />
      </Section>

      {/* Blocked Domains */}
      <Section title={t("blockedDomains.title")} className="">
        <p className="text-[13px] text-muted-foreground mb-4">{t("blockedDomains.description")}</p>
        <TagInput
          tags={blockedDomains}
          value={newDomain}
          onChange={setNewDomain}
          onAdd={addDomain}
          onRemove={removeDomain}
          placeholder="example.com"
          variant="destructive"
        />
        {blockedDomains.length === 0 && (
          <p className="text-[13px] text-muted-foreground mt-2">No blocked domains</p>
        )}
      </Section>

      {/* Plugins */}
      <Section title="Job Source Plugins">
        <p className="text-[13px] text-muted-foreground mb-4">
          Manage job source plugins. Built-in plugins provide LinkedIn, Indeed, SEEK, Naukri, Reed, and StepStone.
          Import community plugins (.yaml files) for additional job sites.
        </p>
        <div className="space-y-3 mb-4">
          {plugins.map((plugin) => (
            <div key={plugin.name} className="flex items-center justify-between p-3 border border-border rounded-lg">
              <div className="flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-semibold text-foreground">{plugin.display_name}</span>
                  <span className="text-[11px] px-1.5 py-0.5 rounded bg-secondary text-muted-foreground">
                    v{plugin.version}
                  </span>
                  {plugin.is_builtin && (
                    <span className="text-[11px] px-1.5 py-0.5 rounded bg-primary/10 text-primary">Built-in</span>
                  )}
                </div>
                <p className="text-[12px] text-muted-foreground mt-0.5">
                  {plugin.description} &middot; {plugin.countries.join(", ")}
                </p>
              </div>
              <div className="flex items-center gap-2 ml-4">
                <label className="relative inline-flex items-center cursor-pointer">
                  <input
                    type="checkbox"
                    checked={plugin.enabled}
                    onChange={(e) => handleTogglePlugin(plugin.name, e.target.checked)}
                    className="sr-only peer"
                  />
                  <div className="w-9 h-5 bg-secondary peer-focus:ring-2 peer-focus:ring-primary/20 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-primary"></div>
                </label>
                {!plugin.is_builtin && (
                  <button onClick={() => handleRemovePlugin(plugin.name)} aria-label={`Remove ${plugin.name}`} className="text-destructive hover:text-destructive/80">
                    <Trash2 className="w-4 h-4" />
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
        <button onClick={handleImportPlugin} className="btn-secondary">
          <Upload className="w-4 h-4" /> Import Plugin
        </button>
      </Section>
    </div>
  );
}


function SettingSwitch({ label, description, checked, onChange, disabled = false, className = "py-2" }: {
  label: string; description: string; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean;
  className?: string;
}) {
  return (
    <div className={`flex items-center justify-between ${className}`}>
      <div>
        <p className="text-sm font-semibold text-foreground">{label}</p>
        <p className="text-[13px] text-muted-foreground mt-1">{description}</p>
      </div>
      <label className={`relative inline-flex items-center flex-shrink-0 ml-4 ${disabled ? "opacity-50 cursor-not-allowed" : "cursor-pointer"}`}>
        <input
          type="checkbox"
          role="switch"
          aria-label={label}
          checked={checked}
          disabled={disabled}
          onChange={(e) => onChange(e.target.checked)}
          className="sr-only peer"
        />
        <div className="w-11 h-6 bg-secondary peer-focus:ring-2 peer-focus:ring-primary/20 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-primary"></div>
      </label>
    </div>
  );
}


/** The passwords the app made for employer sites: names listed, each shown only when asked. */
function EmployerPasswords() {
  const [sites, setSites] = useState<string[]>([]);
  const [shown, setShown] = useState<Record<string, string>>({});
  const [error, setError] = useState("");

  useEffect(() => {
    getEmployerPasswordSites().then((r) => setSites(r.sites || [])).catch(() => {});
  }, []);

  const toggle = async (site: string) => {
    setError("");
    if (shown[site]) {
      setShown((prev) => {
        const next = { ...prev };
        delete next[site];
        return next;
      });
      return;
    }
    try {
      const { password } = await revealEmployerPassword(site);
      setShown((prev) => ({ ...prev, [site]: password }));
    } catch {
      setError(`Couldn't read the password for ${site}.`);
    }
  };

  if (sites.length === 0) return null;
  return (
    <div className="mt-5">
      <h3 className="text-sm font-semibold text-foreground mb-1.5">Passwords the app made for employer sites</h3>
      {error && <p role="alert" className="text-[13px] text-destructive mb-2">{error}</p>}
      <ul className="space-y-1.5">
        {sites.map((site) => (
          <li key={site} className="flex items-center justify-between gap-3 text-[13px]">
            <span className="font-mono truncate">{site}</span>
            <span className="flex items-center gap-2">
              {shown[site] && <code className="font-mono select-all">{shown[site]}</code>}
              <button type="button" className="btn-secondary" onClick={() => toggle(site)}
                aria-label={`${shown[site] ? "Hide" : "Show"} the password for ${site}`}>
                {shown[site] ? "Hide" : "Show"}
              </button>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
