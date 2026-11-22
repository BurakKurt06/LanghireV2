import { useState, useEffect, useMemo } from "react";
import { Briefcase, Building2, Globe } from "lucide-react";
import { getJobs } from "../lib/api";
import { dailyTrend } from "../lib/trend";
import type { Job } from "../lib/types";
import { useTranslation } from "react-i18next";

/** Count occurrences of a field across jobs, returning the top N as sorted pairs. */
function topCounts(items: string[], n: number): Array<[string, number]> {
  const counts = new Map<string, number>();
  for (const raw of items) {
    const key = (raw || "").trim();
    if (!key) continue;
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, n);
}

// Every job (not the newest 1000), with only the fields counted here.
const ANALYTICS_FIELDS = ["status", "source", "company", "applied_at", "collected_at"];

interface StatCardProps {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: string | number;
}

function StatCard({ icon: Icon, label, value }: StatCardProps) {
  return (
    <div className="card flex items-center gap-4">
      <div className="w-10 h-10 rounded-xl bg-secondary flex items-center justify-center">
        <Icon className="w-5 h-5 text-foreground" />
      </div>
      <div>
        <p className="text-2xl font-bold text-foreground leading-tight">{value}</p>
        <p className="text-[13px] text-muted-foreground">{label}</p>
      </div>
    </div>
  );
}

/** Horizontal labeled bar (no chart dependency). */
function Bar({ label, value, max }: { label: string; value: number; max: number }) {
  const pct = max > 0 ? Math.round((value / max) * 100) : 0;
  return (
    <div className="flex items-center gap-3 text-[13px]">
      <span className="w-40 truncate text-foreground" title={label}>{label}</span>
      <div className="flex-1 h-2.5 rounded-full bg-secondary overflow-hidden">
        <div className="h-full bg-primary rounded-full" style={{ width: `${pct}%` }} />
      </div>
      <span className="w-8 text-right text-muted-foreground tabular-nums">{value}</span>
    </div>
  );
}

/**
 * Where the jobs stand and where they came from, under the Dashboard's headline numbers.
 * (Was the separate Analytics page, whose top row repeated the Dashboard's cards.)
 */
export default function Insights() {
  const { t } = useTranslation("dashboard");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [failed, setFailed] = useState(false);

  // The app's engine may start after the Dashboard (it recovers then): keep trying for a minute.
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = (attempt: number) => {
      getJobs({ limit: 1_000_000, fields: ANALYTICS_FIELDS })
        .then((j) => {
          if (!active) return;
          setJobs(Array.isArray(j) ? j : []);
          setFailed(false);
        })
        .catch(() => {
          if (!active) return;
          setFailed(true);
          if (attempt < 20) timer = setTimeout(() => load(attempt + 1), 3000);
        });
    };
    load(0);
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, []);

  const metrics = useMemo(() => {
    const now = new Date();
    const weekAgo = new Date(now.getTime() - 7 * 24 * 3600 * 1000).toISOString();
    const monthAgo = new Date(now.getTime() - 30 * 24 * 3600 * 1000).toISOString();
    const count = (status: string) => jobs.filter((j) => j.status === status).length;
    const byStatus: Array<[string, number]> = [
      ["Applied", count("applied")],
      ["Ready to apply", count("pending")],
      ["Needs you", count("needs_you")],
      ["Failed", count("failed")],
      ["Blocked", count("blocked")],
      ["Skipped", count("skipped")],
      ["Closed", count("closed")],
    ];
    return {
      total: jobs.length,
      appliedThisWeek: jobs.filter((j) => (j.applied_at || "") >= weekAgo).length,
      appliedThisMonth: jobs.filter((j) => (j.applied_at || "") >= monthAgo).length,
      bySource: topCounts(jobs.map((j) => j.source || "unknown"), 6),
      byCompany: topCounts(jobs.map((j) => j.company), 6),
      byStatus,
      trend: dailyTrend(jobs, 14),
    };
  }, [jobs]);

  if (failed) {
    return <div role="alert" className="error-banner mb-6">{t("analytics.loadFailed", "Couldn't load your jobs, so there's nothing to count right now.")}</div>;
  }
  if (metrics.total === 0) return null;

  return (
    <section aria-label="Insights" className="space-y-5 mb-6">
      <div className="grid grid-cols-2 gap-5">
        <StatCard icon={Briefcase} label={t("analytics.appliedThisWeek", "Applied this week")} value={metrics.appliedThisWeek} />
        <StatCard icon={Briefcase} label={t("analytics.appliedThisMonth", "Applied this month")} value={metrics.appliedThisMonth} />
      </div>
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-5">
        <div className="card">
          <h3 className="section-title mb-4">{t("analytics.byStatus", "By status")}</h3>
          <div className="space-y-2.5">
            {metrics.byStatus.map(([label, value]) => (
              <Bar key={label} label={label} value={value} max={metrics.total} />
            ))}
          </div>
        </div>
        <div className="card">
          <h3 className="section-title mb-4 flex items-center gap-2"><Globe className="w-4 h-4" /> {t("analytics.bySource", "By source")}</h3>
          <div className="space-y-2.5">
            {metrics.bySource.map(([label, value]) => (
              <Bar key={label} label={label} value={value} max={metrics.bySource[0]?.[1] || 1} />
            ))}
          </div>
        </div>
        <div className="card">
          <h3 className="section-title mb-4 flex items-center gap-2"><Building2 className="w-4 h-4" /> {t("analytics.topCompanies", "Top companies")}</h3>
          <div className="space-y-2.5">
            {metrics.byCompany.map(([label, value]) => (
              <Bar key={label} label={label} value={value} max={metrics.byCompany[0]?.[1] || 1} />
            ))}
          </div>
        </div>
      </div>
      <div className="card">
        <h3 className="section-title mb-4">{t("analytics.trend", "Applications, last 14 days")}</h3>
        <div className="flex items-end gap-1.5 h-32">
          {metrics.trend.map(({ day, count }) => {
            const maxCount = Math.max(...metrics.trend.map((d) => d.count), 1);
            const h = Math.round((count / maxCount) * 100);
            return (
              <div key={day} className="flex-1 flex flex-col items-center justify-end gap-1" title={`${day}: ${count}`}>
                <div className="w-full bg-primary/80 rounded-t" style={{ height: `${h}%` }} />
                <span className="text-[9px] text-muted-foreground rotate-0">{day.slice(5)}</span>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
