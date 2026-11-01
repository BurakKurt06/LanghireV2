/** Tailoring options (Settings → Applications), kept in browser storage under the same keys as before. */
import type { TailorOptions } from "./types";

export const TAILOR_OPTION_LABELS: ReadonlyArray<{ key: keyof TailorOptions; label: string }> = [
  { key: "skills", label: "Skills" },
  { key: "title", label: "Job Title" },
  { key: "overview", label: "Overview/Summary" },
  { key: "experience", label: "Job Experience" },
];

export function loadTailorOptions(): TailorOptions {
  try {
    const stored = localStorage.getItem("langhire_tailor_options");
    if (stored) return JSON.parse(stored);
  } catch {
    /* ignore */
  }
  return { skills: true, title: false, overview: false, experience: false };
}

export function saveTailorOptions(options: TailorOptions) {
  localStorage.setItem("langhire_tailor_options", JSON.stringify(options));
}

export function loadAutoTailor(): boolean {
  try {
    return localStorage.getItem("langhire_auto_tailor") === "true";
  } catch {
    return false;
  }
}

export function saveAutoTailor(value: boolean) {
  localStorage.setItem("langhire_auto_tailor", value ? "true" : "false");
}
