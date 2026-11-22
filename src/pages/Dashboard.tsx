import { useState, useEffect } from "react";
import {
  Briefcase,
  CheckCircle,
  XCircle,
  Clock,
  Brain,
  TrendingUp,
  BookOpen,
  User,
  Cpu,
  FileText,
  ArrowRight,
} from "lucide-react";
import { getDashboardData, checkHealth, getSetupStatus, getChromiumStatus, getQAStats, type SetupStatus } from "../lib/api";
import { trackEvent } from "../lib/analytics";
import { markStart, measureAndTrack } from "../lib/perf";
import { Download, Loader2 as Spinner } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { PageHeader, LoadingSpinner, ProgressBar } from "../components/ui";
import Insights from "../components/Insights";

export default function Dashboard() {
  const { t } = useTranslation("dashboard");
  const [stats, setStats] = useState({
    totalJobs: 0, applied: 0, notApplied: 0, failed: 0, needsYou: 0, blocked: 0, unknown: 0, pending: 0,
    successRate: null as number | null, finished: 0, totalMemories: 0,
  });
  const [backendOk, setBackendOk] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(true);
  const [setupStatus, setSetupStatus] = useState<SetupStatus | null>(null);
  const [chromiumState, setChromiumState] = useState<{ state: string; message: string } | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    let cancelled = false;
    // The data has been loaded (or a load is under way): a backend that starts late is loaded
    // once its health check succeeds (review R13).
    let dataRequested = false;

    async function init() {
      markStart("dashboard_load");
      for (let i = 0; i < 10; i++) {
        try {
          await checkHealth();
          if (!cancelled) setBackendOk(true);
          break;
        } catch {
          if (i === 9) {
            // Not up yet: show the "not connected" state instead of an endless spinner; the
            // health poll below loads the data when the backend answers.
            if (!cancelled) { setBackendOk(false); setLoading(false); }
            return;
          }
          await new Promise((r) => setTimeout(r, 1500));
        }
      }
      await loadData();
    }

    async function loadData() {
      dataRequested = true;
      try {
        const [data, setup, qaStats] = await Promise.all([
          getDashboardData(),
          getSetupStatus().catch(() => null),
          getQAStats().catch(() => ({ total: 0, answered: 0, unanswered: 0 })),
        ]);

        const jobs = data.jobs || { total: 0, applied: 0, failed: 0, pending: 0, blocked: 0, in_progress: 0 };
        const mem = data.memory || { total_memories: 0, unique_domains: 0, by_category: {} };
        const total = jobs.total || 0;
        const applied = jobs.applied || 0;
        // Every attempt that ended without an application counts: an error, a job left for you
        // ("Needs you", which includes an unknown outcome) and a blocked one. The card used to count
        // only "failed", and showed "Failed 0, Success rate 100%" over a list full of errors.
        const failed = jobs.failed || 0;
        const needsYou = jobs.needs_you || 0;
        const blocked = jobs.blocked || 0;
        const notApplied = jobs.not_successful ?? failed + needsYou + blocked;
        const finished = jobs.finished ?? applied + notApplied;
        if (!cancelled) {
          setStats({
            totalJobs: total,
            applied,
            notApplied,
            failed,
            needsYou,
            blocked,
            unknown: jobs.outcome_unknown || 0,
            pending: jobs.pending || 0,
            // Applied out of all finished attempts; no rate at all before the first one.
            successRate: finished > 0 ? Math.round((applied / finished) * 100) : null,
            finished,
            totalMemories: mem.total_memories || 0,
          });
          if (setup) setSetupStatus(setup);
          trackEvent("dashboard_stats", {
            jobs_collected: total,
            jobs_applied: applied,
            jobs_failed: notApplied,
            jobs_pending: jobs.pending || 0,
            jobs_in_progress: jobs.in_progress || 0,
            memories_created: mem.total_memories || 0,
            questions_extracted: qaStats.total || 0,
          });
        }
      } catch { /* API failures handled gracefully */ }
      if (!cancelled) {
        setLoading(false);
        measureAndTrack("dashboard_load");
      }
    }

    init();

    const healthPoll = setInterval(() => {
      if (cancelled) return;
      checkHealth()
        .then(() => {
          if (cancelled) return;
          setBackendOk(true);
          if (!dataRequested) void loadData();
        })
        .catch(() => { if (!cancelled) setBackendOk(false); });
    }, 30000);

    return () => { cancelled = true; clearInterval(healthPoll); };
  }, []);

  useEffect(() => {
    let active = true;
    const poll = () => {
      getChromiumStatus()
        .then((s) => {
          if (!active) return;
          setChromiumState(s);
          if (s.state === "installing" || s.state === "checking") {
            setTimeout(poll, 2000);
          }
        })
        .catch(() => {
          if (active) setTimeout(poll, 3000);
        });
    };
    poll();
    return () => { active = false; };
  }, []);

  // Re-check onboarding progress when the user returns to the Dashboard or
  // finishes the setup wizard — the wizard is an overlay (no route change), so
  // without this the "Getting Started" checklist stays stale until a restart.
  useEffect(() => {
    let active = true;
    const refreshSetup = () => {
      getSetupStatus()
        .then((s) => { if (active) setSetupStatus(s); })
        .catch(() => {});
    };
    window.addEventListener("focus", refreshSetup);
    window.addEventListener("langhire:setup-updated", refreshSetup);
    document.addEventListener("visibilitychange", refreshSetup);
    return () => {
      active = false;
      window.removeEventListener("focus", refreshSetup);
      window.removeEventListener("langhire:setup-updated", refreshSetup);
      document.removeEventListener("visibilitychange", refreshSetup);
    };
  }, []);

  const cards = [
    { label: t("cards.totalJobs"), value: stats.totalJobs, icon: Briefcase, color: "text-[#222222]", bg: "bg-[#F7F7F7]", detail: "" },
    { label: t("cards.applied"), value: stats.applied, icon: CheckCircle, color: "text-success", bg: "bg-[#F0FFF0]", detail: "" },
    { label: t("cards.failed"), value: stats.notApplied, icon: XCircle, color: "text-destructive", bg: "bg-[#FFF0F0]",
      detail: stats.notApplied > 0
        ? [stats.failed && `${stats.failed} failed`,
           stats.needsYou && `${stats.needsYou} need you${stats.unknown ? ` (${stats.unknown} may have been sent)` : ""}`,
           stats.blocked && `${stats.blocked} blocked`].filter(Boolean).join(" · ")
        : "" },
    { label: t("cards.pending"), value: stats.pending, icon: Clock, color: "text-warning", bg: "bg-[#FFF8F0]", detail: "" },
    { label: t("cards.successRate"), value: stats.successRate === null ? "—" : `${stats.successRate}%`, icon: TrendingUp,
      color: "text-primary", bg: "bg-[#FFF0F3]",
      detail: stats.finished > 0 ? `${stats.applied} of ${stats.finished} finished attempts` : "No finished attempts yet" },
    { label: t("cards.memories"), value: stats.totalMemories, icon: Brain, color: "text-[#6B21A8]", bg: "bg-[#F5F0FF]", detail: "" },
  ];

  const setupSteps = [
    { label: t("setup.configureAI"), done: setupStatus?.llm, path: "/settings?section=ai-model", icon: Cpu, required: true },
    { label: t("setup.setResume"), done: setupStatus?.resume, path: "/settings?section=applications", icon: FileText, required: true },
    { label: t("setup.setupProfile"), done: setupStatus?.profile, path: "/profile", icon: User, required: true },
  ];

  const allRequiredDone = setupStatus?.all_required_done;
  const completedCount = setupSteps.filter((s) => s.done).length;

  if (loading) {
    return (
      <div className="page-wide">
        <PageHeader title={t("title")} subtitle={t("subtitle")} />
        {/* Show Chromium progress even while loading */}
        {chromiumState && chromiumState.state === "installing" && (
          <div className="card mb-6 flex items-center gap-4">
            <div className="bg-[#F7F7F7] p-3 rounded-2xl">
              <Download className="w-5 h-5 text-foreground" />
            </div>
            <div className="flex-1">
              <p className="text-sm font-semibold text-foreground">{t("chromium.installing")}</p>
              <p className="text-[13px] text-muted-foreground mt-0.5">{chromiumState.message}</p>
            </div>
            <Spinner className="w-5 h-5 animate-spin text-muted-foreground" />
          </div>
        )}
        {chromiumState && chromiumState.state === "failed" && (
          <div className="error-banner mb-6">{chromiumState.message}</div>
        )}
        <LoadingSpinner />
      </div>
    );
  }

  return (
    <div className="page-wide">
      <PageHeader title={t("title")} subtitle={t("subtitle")} />

      {backendOk === false && (
        <div className="error-banner mb-6 flex items-center gap-2">
          <XCircle className="w-4 h-4 text-destructive flex-shrink-0" />
          <span>
            The app's engine isn't answering yet. It usually starts within a few seconds; if this stays, close LangHireV2 and open it again.
          </span>
        </div>
      )}

      {/* Chromium Install Progress */}
      {chromiumState && chromiumState.state === "installing" && (
        <div className="card mb-6 flex items-center gap-4">
          <div className="bg-[#F7F7F7] p-3 rounded-2xl">
            <Download className="w-5 h-5 text-foreground" />
          </div>
          <div className="flex-1">
            <p className="text-sm font-semibold text-foreground">{t("chromium.installing")}</p>
            <p className="text-[13px] text-muted-foreground mt-0.5">{chromiumState.message}</p>
          </div>
          <Spinner className="w-5 h-5 animate-spin text-muted-foreground" />
        </div>
      )}

      {chromiumState && chromiumState.state === "failed" && (
        <div className="error-banner mb-6 flex items-center gap-2">
          <XCircle className="w-4 h-4 text-destructive flex-shrink-0" />
          <span>The app's own browser couldn't be downloaded ({chromiumState.message}). Check your internet connection and open LangHireV2 again, or pick an installed browser in Settings → App &amp; browser.</span>
        </div>
      )}

      {/* Stats Grid */}
      <div className="grid grid-cols-3 2xl:grid-cols-6 gap-5 mb-8">
        {cards.map(({ label, value, icon: Icon, color, bg, detail }) => (
          <div key={label} className="card flex items-center gap-4">
            <div className={`${bg} p-3.5 rounded-2xl`}>
              <Icon className={`w-5 h-5 ${color}`} />
            </div>
            <div className="min-w-0">
              <p className="text-[13px] text-muted-foreground font-medium">{label}</p>
              <p className="text-2xl font-extrabold text-foreground">{value}</p>
              {detail && <p className="text-[11px] text-muted-foreground truncate" title={detail}>{detail}</p>}
            </div>
          </div>
        ))}
      </div>

      <Insights />

      {/* Quick Actions */}
      <div className="card mb-6">
        <h3 className="section-title mb-5">{t("quickActions.title")}</h3>
        <div className="flex gap-3">
          <button onClick={() => navigate("/jobs")} className="btn-primary">
            {t("quickActions.collectJobs")}
            <ArrowRight className="w-4 h-4" />
          </button>
          <button onClick={() => navigate("/apply")} className="btn-secondary">
            {t("quickActions.startApplying")}
            <ArrowRight className="w-4 h-4" />
          </button>
          <button onClick={() => navigate("/memory")} className="btn-ghost">
            {t("quickActions.viewMemories")}
          </button>
        </div>
      </div>

      {/* Live Setup Checklist */}
      <div className="card">
        <div className="flex items-center justify-between mb-5">
          <h3 className="section-title">
            {allRequiredDone ? t("setup.complete") : t("setup.gettingStarted")}
          </h3>
          <button
            onClick={() => navigate("/guide")}
            className="flex items-center gap-1.5 text-[13px] text-primary font-semibold hover:underline"
          >
            <BookOpen className="w-3.5 h-3.5" />
            {t("setup.viewGuide")}
          </button>
        </div>

        {allRequiredDone ? (
          <div className="bg-[#F0FFF0] rounded-2xl p-5 flex items-start gap-3">
            <CheckCircle className="w-5 h-5 text-success mt-0.5 flex-shrink-0" />
            <div>
              <p className="text-sm font-semibold text-foreground">
                {t("setup.allDone")}
              </p>
              <p className="text-[13px] text-muted-foreground mt-1">
                {t("setup.needHelp").split("<guide>")[0]}
                <button onClick={() => navigate("/guide")} className="underline font-semibold text-primary">{t("nav.guide", { ns: "common" })}</button>
                {t("setup.needHelp").split("</guide>")[1]}
              </p>
            </div>
          </div>
        ) : (
          <>
            <div className="mb-5">
              <ProgressBar
                percent={(completedCount / setupSteps.length) * 100}
                label={t("setup.stepsComplete", { completed: completedCount, total: setupSteps.length })}
              />
            </div>

            <div className="space-y-1">
              {setupSteps.map((step) => {
                const Icon = step.icon;
                return (
                  <div key={step.label} className="flex items-center justify-between py-2.5 px-3 rounded-xl hover:bg-secondary transition-colors">
                    <div className="flex items-center gap-3">
                      {step.done ? (
                        <CheckCircle className="w-5 h-5 text-success" />
                      ) : (
                        <div className="w-5 h-5 rounded-full border-2 border-border" />
                      )}
                      <Icon className="w-4 h-4 text-muted-foreground" />
                      <span className={`text-sm font-medium ${step.done ? "text-muted-foreground line-through" : "text-foreground"}`}>
                        {step.label}
                      </span>
                    </div>
                    {!step.done && step.path !== "/" && (
                      <button onClick={() => navigate(step.path)} className="text-[13px] text-primary font-semibold hover:underline">
                        {t("setup.setUp")}
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
