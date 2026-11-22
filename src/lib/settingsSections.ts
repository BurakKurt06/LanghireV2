/** Settings categories, in order; `/settings?section=<id>` opens one (other screens link to them). */
export const SETTINGS_SECTIONS = [
  { id: "ai-model", labelKey: "sections.aiModel", label: "AI model" },
  { id: "job-search", labelKey: "sections.jobSearch", label: "Job search" },
  { id: "applications", labelKey: "sections.applications", label: "Applications" },
  { id: "accounts", labelKey: "sections.accounts", label: "Accounts & email" },
  { id: "app", labelKey: "sections.app", label: "App & browser" },
] as const;
export type SettingsSectionId = (typeof SETTINGS_SECTIONS)[number]["id"];

/** Categories merged into another one (2026-11): their old links still open the right place. */
export const SECTION_ALIASES: Record<string, SettingsSectionId> = {
  "job-sources": "job-search",
  browser: "app",
  appearance: "app",
  logs: "app",
  feedback: "app",
};

/** The category a `?section=` value opens (the first one when it names none). */
export function resolveSection(requested: string | null): SettingsSectionId {
  if (SETTINGS_SECTIONS.some((s) => s.id === requested)) return requested as SettingsSectionId;
  return (requested && SECTION_ALIASES[requested]) || "ai-model";
}

/** Link to one Settings category (HashRouter: works from any screen, with or without a router). */
export function settingsHref(id: SettingsSectionId): string {
  return `#/settings?section=${id}`;
}
