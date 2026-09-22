import { useState, useEffect, useRef } from "react";
import { Play, Square, Terminal, Loader2, Plus, ClipboardList, Upload } from "lucide-react";
import {
  startJobCollection,
  stopJobCollection,
  getCollectionStatus,
  getPlugins,
  getProfile,
  addJob,
  addJobsBulk,
  getSettings,
  type BulkAddResult,
} from "../../lib/api";
import { trackEvent } from "../../lib/analytics";
import type { PluginConfig } from "../../lib/types";
import LogLine from "../../components/LogLine";
import AutomationDialog from "../../components/AutomationDialog";
import { ProgressBar } from "../../components/ui";
import { useTranslation } from "react-i18next";

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
          }
          wasRunning = s.running;
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
  }, [collecting, onJobsChanged]);

  // Auto-scroll log
  useEffect(() => {
    const el = logRef.current;
    if (el) {
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      if (nearBottom) el.scrollTop = el.scrollHeight;
    }
  }, [collectLog]);

  const handleStartCollect = () => {
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
        collectSources
      );
      if (res.success) {
        setCollecting(true);
        setCollectLog([t("collector.startingCollection")]);
        trackEvent("collection_started", {
          title: collectTitle || "all",
          max_jobs: collectMaxJobs || "unlimited",
          source: collectSources.join(","),
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
        {/* Dynamic filters from selected plugin */}
        {selectedPlugin?.filters && selectedPlugin.filters.length > 0 && (
          <div className="grid grid-cols-2 gap-3 mb-4">
            {selectedPlugin.filters.map((filter) => (
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
              disabled={collectSources.length === 0 || !collectionEnabled}
              title={!collectionEnabled ? "Job board collection is turned off in Settings" : undefined}
              className="btn-dark disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Play className="w-4 h-4" /> {t("collector.start")}
            </button>
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
