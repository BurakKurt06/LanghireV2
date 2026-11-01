import { useState, useEffect, useRef, useCallback } from "react";
import { Play, Square, Terminal, Loader2, Plus, ClipboardList, Upload, ChevronDown, ChevronRight, Bookmark, X, LogIn } from "lucide-react";
import {
  startJobCollection,
  stopJobCollection,
  getCollectionStatus,
  getPlugins,
  getProfile,
  saveProfile,
  addJob,
  addJobsBulk,
  getSettings,
  getSavedSearches,
  saveSavedSearches,
  getFilteredJobs,
  keepFilteredJob,
  clearFilteredJobs,
  launchLogin,
  type BulkAddResult,
} from "../../lib/api";
import { trackEvent } from "../../lib/analytics";
import type { FilteredJob, JobSearchPrefs, PluginConfig, SavedSearch } from "../../lib/types";
import LogLine from "../../components/LogLine";
import AutomationDialog from "../../components/AutomationDialog";
import JobSearchForm, { ModeChoice } from "../../components/JobSearchForm";
import { ProgressBar } from "../../components/ui";
import {
  DEFAULT_JOB_SEARCH, JOB_TYPE_LABELS, PERIOD_LABELS, WORK_MODE_LABELS, money, withDefaults,
} from "../../lib/jobSearch";
import { useTranslation } from "react-i18next";

/** "Part-time · Remote · at least $20 an hour · match 5+" for the closed filters panel. */
function summary(search: JobSearchPrefs, currency: string): string {
  const parts = [
    ...search.job_types.map((x) => JOB_TYPE_LABELS[x]),
    ...search.work_modes.map((x) => WORK_MODE_LABELS[x]),
  ];
  if (search.min_pay.amount) {
    parts.push(`at least ${money(search.min_pay.amount, search.min_pay.currency || currency)} ${PERIOD_LABELS[search.min_pay.period]}`);
  }
  if (search.min_match) parts.push(`match ${search.min_match}+`);
  const excluded = search.exclude_companies.length + search.exclude_title_words.length + search.dealbreakers.length;
  if (excluded) parts.push(`${excluded} left out`);
  if (search.notes.trim()) parts.push("notes");
  return parts.length ? parts.join(" · ") : "Any job type, arrangement and pay";
}

interface CollectTabProps {
  onJobsChanged: () => void;
}

export default function CollectTab({ onJobsChanged }: CollectTabProps) {
  const { t } = useTranslation("jobs");

  // Collection state
  const [collecting, setCollecting] = useState(false);
  const [collectTitle, setCollectTitle] = useState("");
  const [collectMaxJobs, setCollectMaxJobs] = useState<number | "">(20);
  const [collectSources, setCollectSources] = useState<string[]>(["linkedin"]);
  const [availableSources, setAvailableSources] = useState<PluginConfig[]>([]);
  const [collectLog, setCollectLog] = useState<string[]>([]);
  const [collected, setCollected] = useState(0);
  const [collectFilters, setCollectFilters] = useState<Record<string, string>>({});
  const [showConfirmDialog, setShowConfirmDialog] = useState(false);
  const [loginWindowOpen, setLoginWindowOpen] = useState(false);
  const [launchingLogin, setLaunchingLogin] = useState<string | null>(null);
  const [loginMessage, setLoginMessage] = useState<{ text: string; error: boolean } | null>(null);

  const signIn = async (service: string) => {
    setLaunchingLogin(service);
    setLoginMessage(null);
    try {
      const result = await launchLogin(service);
      setLoginMessage({ text: result.message, error: !result.success });
      if (result.success) setLoginWindowOpen(true);
    } catch (error) {
      setLoginMessage({ text: error instanceof Error ? error.message : "Couldn't open the sign-in window.", error: true });
    } finally {
      setLaunchingLogin(null);
    }
  };

  // This search's preferences, starting from Settings → Job search.
  const [search, setSearch] = useState<JobSearchPrefs>(DEFAULT_JOB_SEARCH);
  const [profileSearch, setProfileSearch] = useState<JobSearchPrefs>(DEFAULT_JOB_SEARCH);
  const [currency, setCurrency] = useState("USD");
  const [showSearch, setShowSearch] = useState(false);
  const [savedDefault, setSavedDefault] = useState(false);
  const [savedSearches, setSavedSearches] = useState<SavedSearch[]>([]);
  const [naming, setNaming] = useState<string | null>(null);
  const [leftOut, setLeftOut] = useState<FilteredJob[]>([]);
  const [leftOutTotal, setLeftOutTotal] = useState(0);
  const [showLeftOut, setShowLeftOut] = useState(false);

  const loadLeftOut = useCallback(() => {
    getFilteredJobs(100)
      .then((res) => {
        setLeftOut(res.jobs || []);
        setLeftOutTotal(res.total || 0);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    getSavedSearches().then((res) => setSavedSearches(res.searches || [])).catch(() => {});
    loadLeftOut();
  }, [loadLeftOut]);

  // Add job form state
  const [showAddJobForm, setShowAddJobForm] = useState(false);
  const [addJobUrl, setAddJobUrl] = useState("");
  const [addJobTitle, setAddJobTitle] = useState("");
  const [addJobCompany, setAddJobCompany] = useState("");
  const [addingJob, setAddingJob] = useState(false);

  // Bulk paste / file upload state
  const [showPasteForm, setShowPasteForm] = useState(false);
  const [pasteText, setPasteText] = useState("");
  const [pasting, setPasting] = useState(false);
  const [pasteResult, setPasteResult] = useState<BulkAddResult | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Job board collection switch (Settings). Default on.
  const [collectionEnabled, setCollectionEnabled] = useState(true);
  useEffect(() => {
    getSettings()
      .then((s) => setCollectionEnabled(s.job_collection_enabled !== false))
      .catch(() => {});
  }, []);

  const logRef = useRef<HTMLDivElement>(null);

  // Filters are plugin-specific, so they are only offered when a single source
  // is selected — applying LinkedIn's filter params to Handshake would be wrong.
  const selectedPlugin =
    collectSources.length === 1
      ? availableSources.find((s) => s.name === collectSources[0])
      : undefined;

  const toggleSource = (name: string) => {
    setCollectSources((prev) =>
      prev.includes(name) ? prev.filter((s) => s !== name) : [...prev, name],
    );
    // Changing the source set invalidates plugin-specific filters. Kept outside
    // the updater above: updater functions must be pure, and React may run them
    // twice in StrictMode.
    setCollectFilters({});
  };

  // Load available plugins/sources
  useEffect(() => {
    getProfile()
      .then((p) => {
        const prefs = withDefaults(p.job_search);
        setSearch(prefs);
        setProfileSearch(prefs);
        setCurrency(p.salary_expectation?.currency || "USD");
        const country = p.country || "US";
        getPlugins(country)
          .then((res) => {
            if (res.plugins) setAvailableSources(res.plugins);
          })
          .catch(() => {});
      })
      .catch(() => {
        getPlugins()
          .then((res) => {
            if (res.plugins) setAvailableSources(res.plugins);
          })
          .catch(() => {});
      });
  }, []);

  // Check if collection is already running on mount
  useEffect(() => {
    getCollectionStatus()
      .then((s) => {
        setLoginWindowOpen(Boolean(s.login_window_open));
        if (s.running) {
          setCollecting(true);
          setCollectLog(s.log || []);
          setCollected(s.collected || 0);
        }
      })
      .catch(() => {});
  }, []);

  // Poll collection status
  useEffect(() => {
    let active = true;
    let wasRunning = collecting;
    const poll = setInterval(() => {
      getCollectionStatus()
        .then((s) => {
          if (!active) return;
          if (wasRunning && !s.running) {
            trackEvent("collection_completed", { jobs_collected: s.collected || 0 });
            onJobsChanged();
            loadLeftOut();
          }
          wasRunning = s.running;
          setLoginWindowOpen(Boolean(s.login_window_open));
          setCollecting(s.running);
          setCollectLog(s.log || []);
          setCollected(s.collected || 0);
        })
        .catch(() => {});
    }, 2000);
    return () => {
      active = false;
      clearInterval(poll);
    };
  }, [collecting, onJobsChanged, loadLeftOut]);

  // Auto-scroll log
  useEffect(() => {
    const el = logRef.current;
    if (el) {
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      if (nearBottom) el.scrollTop = el.scrollHeight;
    }
  }, [collectLog]);

  const handleStartCollect = () => {
    if (loginWindowOpen || launchingLogin) return;
    setShowConfirmDialog(true);
  };

  const confirmStartCollect = async () => {
    setShowConfirmDialog(false);
    try {
      const res = await startJobCollection(
        collectTitle || undefined,
        collectMaxJobs ? Number(collectMaxJobs) : undefined,
        collectSources[0],
        collectFilters,
        collectSources,
        search,
      );
      if (res.success) {
        setCollecting(true);
        setCollectLog([t("collector.startingCollection")]);
        trackEvent("collection_started", {
          has_title: Boolean(collectTitle),
          max_jobs: collectMaxJobs || "unlimited",
          source: collectSources.join(","),
          mode: search.mode,
        });
      } else {
        alert(res.message);
      }
    } catch (e) {
      alert(e instanceof Error ? e.message : "Failed to start collection");
    }
  };

  const handleStopCollect = async () => {
    await stopJobCollection();
    setCollecting(false);
  };

  const saveAsDefault = async () => {
    try {
      const profile = await getProfile();
      await saveProfile({ ...profile, job_search: search });
      setProfileSearch(search);
      setSavedDefault(true);
      setTimeout(() => setSavedDefault(false), 2500);
    } catch (e) {
      alert(e instanceof Error ? e.message : "Couldn't save to Profile");
    }
  };

  const storeSearches = async (next: Omit<SavedSearch, "id">[] | SavedSearch[]) => {
    try {
      const res = await saveSavedSearches(next);
      setSavedSearches(res.searches || []);
    } catch (e) {
      alert(e instanceof Error ? e.message : "Couldn't save the search");
    }
  };

  const saveThisSearch = async () => {
    const name = (naming || "").trim();
    if (!name) return;
    await storeSearches([
      ...savedSearches,
      { name, title: collectTitle, sources: collectSources, filters: collectFilters,
        max_jobs: collectMaxJobs ? Number(collectMaxJobs) : 0, search },
    ]);
    setNaming(null);
  };

  const loadSearch = (saved: SavedSearch) => {
    setCollectTitle(saved.title);
    const known = saved.sources.filter((s) => availableSources.some((p) => p.name === s));
    setCollectSources(known.length ? known : collectSources);
    setCollectFilters(saved.filters || {});
    setCollectMaxJobs(saved.max_jobs || "");
    setSearch(withDefaults(saved.search));
  };

  const keepJob = async (url: string) => {
    try {
      await keepFilteredJob(url);
      loadLeftOut();
      onJobsChanged();
    } catch (e) {
      alert(e instanceof Error ? e.message : "Couldn't add it");
    }
  };

  const slowBoards = availableSources
    .filter((s) => collectSources.includes(s.name) && s.quick === false)
    .map((s) => s.display_name);
  const boardFilters = (selectedPlugin?.filters || []).filter((f) => !f.shared);

  // Add job manually
  const handleAddJob = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!addJobUrl.trim()) return;
    setAddingJob(true);
    try {
      const res = await addJob(
        addJobUrl.trim(),
        addJobTitle.trim() || undefined,
        addJobCompany.trim() || undefined
      );
      if (res.success) {
        setAddJobUrl("");
        setAddJobTitle("");
        setAddJobCompany("");
        setShowAddJobForm(false);
        onJobsChanged();
      }
    } catch (e2) {
      alert(e2 instanceof Error ? e2.message : "Failed to add job");
    } finally {
      setAddingJob(false);
    }
  };

  const submitPastedLinks = async (text: string) => {
    if (!text.trim()) return;
    setPasting(true);
    setPasteResult(null);
    try {
      const res = await addJobsBulk(text);
      setPasteResult(res);
      if (res.added > 0) {
        setPasteText("");
        onJobsChanged();
      }
    } catch (e) {
      alert(e instanceof Error ? e.message : "Failed to add links");
    } finally {
      setPasting(false);
    }
  };

  // The backend caps the pasted text at 200k characters. Check here too so an
  // oversized file fails with a clear message instead of an opaque 422 after
  // being read into memory and uploaded.
  const MAX_UPLOAD_BYTES = 200_000;

  const handleFilePicked = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (file.size > MAX_UPLOAD_BYTES) {
      alert(
        `That file is ${Math.round(file.size / 1024)} KB, over the ${MAX_UPLOAD_BYTES / 1000} KB limit. ` +
          `Split it up — one job URL per line is only ~60 bytes, so this holds a few thousand links.`,
      );
      if (fileInputRef.current) fileInputRef.current.value = "";
      return;
    }
    const text = await file.text();
    setPasteText(text);
    await submitPastedLinks(text);
    // Allow re-picking the same file straight after.
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  return (
    <div>
      {/* Action buttons */}
      <div className="flex items-center gap-2 mb-5">
        <button
          onClick={() => setShowAddJobForm(!showAddJobForm)}
          className="btn-secondary"
        >
          <Plus className="w-4 h-4" /> Add Job
        </button>
        <button
          onClick={() => setShowPasteForm(!showPasteForm)}
          className="btn-secondary"
        >
          <ClipboardList className="w-4 h-4" /> Paste Links
        </button>
      </div>

      {/* Paste / upload many links */}
      {showPasteForm && (
        <div className="card mb-5">
          <h3 className="section-title mb-3">Paste or upload job links</h3>
          <p className="text-[13px] text-muted-foreground mb-3">
            One URL per line. Duplicates and invalid links are skipped. These are
            treated as external applications, so they run in the External and All
            apply modes.
          </p>
          <textarea
            value={pasteText}
            onChange={(e) => setPasteText(e.target.value)}
            placeholder={"https://careers.example.com/job/123\nhttps://boards.greenhouse.io/acme/jobs/456"}
            rows={8}
            className="input-base resize-y font-mono text-[12px]"
            aria-label="Job links, one per line"
          />
          <div className="flex items-center gap-2 mt-3">
            <button
              onClick={() => submitPastedLinks(pasteText)}
              disabled={pasting || !pasteText.trim()}
              className="btn-primary disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {pasting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
              Add Links
            </button>
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={pasting}
              className="btn-secondary"
            >
              <Upload className="w-4 h-4" /> Upload File
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept=".txt,.csv,.text,text/plain,text/csv"
              onChange={handleFilePicked}
              className="hidden"
              data-testid="bulk-file-input"
            />
            <button type="button" onClick={() => setShowPasteForm(false)} className="btn-secondary">
              Close
            </button>
          </div>
          {pasteResult && (
            <div className="info-box mt-3 text-[13px]">
              Added <strong>{pasteResult.added}</strong>
              {pasteResult.skipped_duplicate > 0 && <> · skipped {pasteResult.skipped_duplicate} duplicate{pasteResult.skipped_duplicate === 1 ? "" : "s"}</>}
              {pasteResult.skipped_invalid > 0 && <> · skipped {pasteResult.skipped_invalid} invalid</>}
              {pasteResult.invalid_samples.length > 0 && (
                <div className="mt-1.5 text-muted-foreground">
                  Rejected: {pasteResult.invalid_samples.slice(0, 3).join(", ")}
                  {pasteResult.skipped_invalid > 3 ? ` +${pasteResult.skipped_invalid - 3} more` : ""}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Add Job Form */}
      {showAddJobForm && (
        <div className="card mb-5">
          <h3 className="section-title mb-3">Add Job Manually</h3>
          <form onSubmit={handleAddJob} className="flex flex-col gap-3">
            <input
              value={addJobUrl}
              onChange={(e) => setAddJobUrl(e.target.value)}
              placeholder="Job URL (required)"
              className="input-base"
              required
            />
            <div className="flex gap-3">
              <input
                value={addJobTitle}
                onChange={(e) => setAddJobTitle(e.target.value)}
                placeholder="Job title (optional)"
                className="input-base flex-1"
              />
              <input
                value={addJobCompany}
                onChange={(e) => setAddJobCompany(e.target.value)}
                placeholder="Company (optional)"
                className="input-base flex-1"
              />
            </div>
            <div className="flex gap-2">
              <button
                type="submit"
                disabled={addingJob || !addJobUrl.trim()}
                className="btn-primary"
              >
                {addingJob ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Plus className="w-4 h-4" />
                )}
                Add Job
              </button>
              <button
                type="button"
                onClick={() => setShowAddJobForm(false)}
                className="btn-secondary"
              >
                Cancel
              </button>
            </div>
          </form>
        </div>
      )}

      {/* Job Collector Panel */}
      {!collectionEnabled && (
        <div className="info-box mb-5 text-[13px]">
          Job board collection is turned <strong>off</strong> in Settings. Pasting and
          uploading links still works. Turn it back on in Settings to collect from job
          boards again.
        </div>
      )}
      <div className="card mb-5">
        <h3 className="section-title mb-3">{t("collector.title")}</h3>
        <p className="text-[13px] text-muted-foreground mb-4">
          {t("collector.description")}
        </p>
        <div
          className="info-box mb-4"
          dangerouslySetInnerHTML={{ __html: t("collector.loginInfo") }}
        />
        <div className="mb-4">
          <div className="flex flex-wrap gap-2" role="group" aria-label="Job board sign-in">
            {availableSources.filter((source) => source.is_builtin && source.enabled && source.login_url && collectSources.includes(source.name)).map((source) => (
              <button
                key={source.name}
                type="button"
                className="btn-secondary"
                disabled={collecting || loginWindowOpen || launchingLogin !== null}
                onClick={() => void signIn(source.name)}
              >
                {launchingLogin === source.name ? <Loader2 className="w-4 h-4 animate-spin" /> : <LogIn className="w-4 h-4" />}
                Sign in to {source.display_name}
              </button>
            ))}
          </div>
          {loginMessage && <p role="status" className={`mt-2 text-[13px] ${loginMessage.error ? "text-red-600" : "text-muted-foreground"}`}>{loginMessage.text}</p>}
          {loginWindowOpen && <p className="mt-2 text-[13px] text-muted-foreground">Close the sign-in browser window when you finish, then start collection.</p>}
        </div>
        {/* Source selector — multi-select; each title is collected from every
            checked source in turn. */}
        {availableSources.length > 0 && (
          <div className="mb-4">
            <label className="block text-sm font-semibold text-foreground mb-1.5">
              {t("collector.sourcePlatform")}
            </label>
            <div className="flex gap-2 flex-wrap" role="group" aria-label="Job sources">
              {availableSources.map((source) => {
                const checked = collectSources.includes(source.name);
                return (
                  <button
                    key={source.name}
                    role="checkbox"
                    aria-checked={checked}
                    onClick={() => toggleSource(source.name)}
                    disabled={collecting}
                    className={`px-3 py-1.5 rounded-lg text-sm font-medium border transition-colors disabled:opacity-50 ${
                      checked
                        ? "border-primary bg-primary/10 text-primary"
                        : "border-border bg-card text-foreground hover:border-primary/50"
                    }`}
                  >
                    {checked && <span aria-hidden="true">✓ </span>}
                    {source.display_name}
                  </button>
                );
              })}
            </div>
            <p className="text-[12px] text-muted-foreground mt-1.5">
              {collectSources.length === 0
                ? "Pick at least one source."
                : collectSources.length === 1
                  ? "Each job title is collected from this source."
                  : `Each job title is collected from all ${collectSources.length} sources, one after another. Source-specific filters are hidden while more than one is selected.`}
            </p>
          </div>
        )}
        {/* Saved searches */}
        {(savedSearches.length > 0 || naming !== null) && (
          <div className="mb-4">
            <p className="text-sm font-semibold text-foreground mb-1.5">{t("collector.savedSearches")}</p>
            <div className="flex gap-2 flex-wrap">
              {savedSearches.map((saved) => (
                <span key={saved.id} className="inline-flex items-center rounded-lg border border-border bg-card text-sm">
                  <button type="button" onClick={() => loadSearch(saved)} disabled={collecting}
                    aria-label={t("collector.loadSearch", { name: saved.name })}
                    className="inline-flex items-center gap-1.5 pl-3 pr-2 py-1.5 hover:text-primary disabled:opacity-50">
                    <Bookmark className="w-3.5 h-3.5" /> {saved.name}
                  </button>
                  <button type="button" disabled={collecting}
                    onClick={() => storeSearches(savedSearches.filter((s) => s.id !== saved.id))}
                    aria-label={t("collector.deleteSearch", { name: saved.name })}
                    className="pr-2 py-1.5 text-muted-foreground hover:text-destructive disabled:opacity-50">
                    <X className="w-3.5 h-3.5" />
                  </button>
                </span>
              ))}
            </div>
          </div>
        )}

        {/* How to collect, and this search's filters (from Settings → Job search) */}
        <div className="mb-4">
          <ModeChoice value={search.mode} onChange={(mode) => setSearch((s) => ({ ...s, mode }))} />
          {search.mode === "quick" && slowBoards.length > 0 && (
            <p className="text-[12px] text-muted-foreground mt-1.5">
              {t("collector.oneAtATimeHint", { boards: slowBoards.join(", ") })}
            </p>
          )}
        </div>
        <div className="mb-4 rounded-xl border border-border">
          <button type="button" onClick={() => setShowSearch((v) => !v)} aria-expanded={showSearch}
            className="w-full flex items-center justify-between gap-3 px-4 py-3 text-left">
            <span>
              <span className="block text-sm font-semibold text-foreground">{t("collector.searchSettings")}</span>
              <span className="block text-[12px] text-muted-foreground">{summary(search, currency)}</span>
            </span>
            {showSearch ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
          </button>
          {showSearch && (
            <div className="px-4 pb-4">
              <p className="text-[12px] text-muted-foreground mb-3">{t("collector.searchSettingsHint")}</p>
              <JobSearchForm value={search} onChange={setSearch} currency={currency} showMode={false} disabled={collecting} />
              <div className="flex gap-2 mt-4">
                <button type="button" className="btn-secondary" disabled={collecting} onClick={() => setSearch(profileSearch)}>
                  {t("collector.resetToProfile")}
                </button>
                <button type="button" className="btn-secondary" disabled={collecting} onClick={saveAsDefault}>
                  {savedDefault ? t("collector.savedAsDefault") : t("collector.saveAsDefault")}
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Board-specific filters the shared ones don't cover */}
        {boardFilters.length > 0 && (
          <div className="mb-4">
            <p className="text-xs font-semibold text-muted-foreground mb-1.5">
              {t("collector.boardFilters", { board: selectedPlugin?.display_name })}
            </p>
          <div className="grid grid-cols-2 gap-3">
            {boardFilters.map((filter) => (
              <div key={filter.key}>
                <label className="block text-xs font-medium text-muted-foreground mb-1">
                  {filter.label}
                </label>
                {filter.type === "select" ? (
                  <select
                    value={collectFilters[filter.key] || filter.default || ""}
                    onChange={(e) =>
                      setCollectFilters((prev) => ({
                        ...prev,
                        [filter.key]: e.target.value,
                      }))
                    }
                    disabled={collecting}
                    className="w-full px-2.5 py-1.5 border border-border rounded-lg text-sm bg-card"
                  >
                    <option value="">Any</option>
                    {filter.options.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    type="text"
                    value={collectFilters[filter.key] || ""}
                    onChange={(e) =>
                      setCollectFilters((prev) => ({
                        ...prev,
                        [filter.key]: e.target.value,
                      }))
                    }
                    disabled={collecting}
                    className="input-base !py-1.5 text-sm"
                    placeholder={filter.label}
                  />
                )}
              </div>
            ))}
          </div>
          </div>
        )}
        <div className="flex gap-3 mb-4">
          <input
            value={collectTitle}
            onChange={(e) => setCollectTitle(e.target.value)}
            placeholder={t("collector.jobTitlePlaceholder")}
            className="input-base flex-1"
            disabled={collecting}
          />
          <input
            type="number"
            value={collectMaxJobs}
            onChange={(e) =>
              setCollectMaxJobs(e.target.value ? Number(e.target.value) : "")
            }
            placeholder={t("collector.maxJobsPlaceholder")}
            min={1}
            max={200}
            className="input-base !w-32 !flex-initial"
            disabled={collecting}
            title={t("collector.maxJobsTitle")}
          />
          {collecting ? (
            <button onClick={handleStopCollect} className="btn-destructive">
              <Square className="w-4 h-4" /> {t("collector.stop")}
            </button>
          ) : (
            <button
              onClick={handleStartCollect}
              disabled={collectSources.length === 0 || !collectionEnabled || loginWindowOpen || launchingLogin !== null}
              title={!collectionEnabled ? "Job board collection is turned off in Settings" : undefined}
              className="btn-dark disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Play className="w-4 h-4" /> {t("collector.start")}
            </button>
          )}
        </div>
        {/* Save this search (title, boards, filters) to run again later */}
        <div className="mb-4">
          {naming === null ? (
            <button type="button" onClick={() => setNaming(collectTitle || "")} disabled={collecting}
              className="inline-flex items-center gap-1.5 text-[13px] text-muted-foreground hover:text-foreground disabled:opacity-50">
              <Bookmark className="w-3.5 h-3.5" /> {t("collector.saveSearch")}
            </button>
          ) : (
            <div className="flex gap-2 items-center">
              <input value={naming} onChange={(e) => setNaming(e.target.value)} maxLength={60} autoFocus
                onKeyDown={(e) => e.key === "Enter" && saveThisSearch()}
                aria-label={t("collector.searchName")} placeholder={t("collector.searchName")}
                className="input-base !py-1.5 text-sm flex-1" />
              <button type="button" className="btn-primary" onClick={saveThisSearch} disabled={!naming.trim()}>
                {t("collector.save")}
              </button>
              <button type="button" className="btn-secondary" onClick={() => setNaming(null)}>{t("collector.cancel")}</button>
            </div>
          )}
        </div>
        {/* Progress bar */}
        {collecting && collectMaxJobs && (
          <div className="mb-4">
            <ProgressBar
              percent={
                collectMaxJobs > 0
                  ? (collected / Number(collectMaxJobs)) * 100
                  : 0
              }
              label={t("collector.collectedProgress", {
                collected,
                max: collectMaxJobs,
              })}
            />
          </div>
        )}
        {/* Log output */}
        {collectLog.length > 0 && (
          <div ref={logRef} className="log-viewer">
            <div className="flex items-center gap-2 mb-2 text-muted-foreground">
              <Terminal className="w-3.5 h-3.5" /> {t("collector.collectionLog")}
              {collecting && (
                <Loader2 className="w-3.5 h-3.5 animate-spin text-green-400" />
              )}
            </div>
            {collectLog.map((line, i) => (
              <LogLine key={i} line={line} />
            ))}
          </div>
        )}
        {/* What the search left out, and why */}
        {leftOutTotal > 0 && (
          <div className="mt-4 rounded-xl border border-border">
            <button type="button" onClick={() => setShowLeftOut((v) => !v)} aria-expanded={showLeftOut}
              className="w-full flex items-center justify-between gap-3 px-4 py-3 text-left">
              <span className="text-sm font-semibold text-foreground">{t("collector.leftOut", { count: leftOutTotal })}</span>
              {showLeftOut ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
            </button>
            {showLeftOut && (
              <div className="px-4 pb-4">
                <p className="text-[12px] text-muted-foreground mb-2">{t("collector.leftOutHint")}</p>
                <ul className="divide-y divide-border">
                  {leftOut.map((job) => (
                    <li key={job.url} className="flex items-center justify-between gap-3 py-2">
                      <div className="min-w-0">
                        <a href={job.url} target="_blank" rel="noopener noreferrer"
                          className="block text-sm font-medium text-foreground truncate hover:text-primary">
                          {job.title || "Untitled"} · {job.company || "Unknown"}
                        </a>
                        <p className="text-[12px] text-muted-foreground truncate">{job.reason}</p>
                      </div>
                      <button type="button" className="btn-secondary !py-1 text-[12px] flex-shrink-0"
                        onClick={() => keepJob(job.url)}>{t("collector.keep")}</button>
                    </li>
                  ))}
                </ul>
                <button type="button" className="mt-2 text-[12px] text-muted-foreground hover:text-destructive"
                  onClick={() => clearFilteredJobs().then(loadLeftOut).catch(() => {})}>
                  {t("collector.clearLeftOut")}
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      <AutomationDialog
        open={showConfirmDialog}
        title={t("dialog.startCollecting")}
        onConfirm={confirmStartCollect}
        onCancel={() => setShowConfirmDialog(false)}
      />
    </div>
  );
}
