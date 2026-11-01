import type { Job } from "./types";

const localDay = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

/** Applications per calendar day for the last `days` days, ending today (days without any show 0).
 *  It used to take the last `days` days that had data, and count collected jobs as well. */
export function dailyTrend(jobs: Job[], days: number, now = new Date()): Array<{ day: string; count: number }> {
  const buckets = new Map<string, number>();
  for (const j of jobs) {
    if (!j.applied_at) continue;
    const when = new Date(j.applied_at);
    if (Number.isNaN(when.getTime())) continue;
    const day = localDay(when);
    buckets.set(day, (buckets.get(day) || 0) + 1);
  }
  return Array.from({ length: days }, (_, i) => {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - (days - 1 - i));
    const day = localDay(d);
    return { day, count: buckets.get(day) || 0 };
  });
}
