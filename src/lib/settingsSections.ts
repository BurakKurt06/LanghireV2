/** Settings categories, in order; `/settings?section=<id>` opens one (other screens link to them). */
export const SETTINGS_SECTIONS = [
  { id: "ai-model", labelKey: "sections.aiModel", label: "AI model" },
  { id: "job-search", labelKey: "sections.jobSearch", label: "Job search" },
  { id: "applications", labelKey: "sections.applications", label: "Applications" },
  { id: "accounts", labelKey: "sections.accounts", label: "Accounts & email" },
  { id: "job-sources", labelKey: "sections.jobSources", label: "Job sources" },
  { id: "browser", labelKey: "sections.browser", label: "Browser & automation" },
  { id: "appearance", labelKey: "sections.appearance", label: "Appearance & privacy" },
] as const;
export type SettingsSectionId = (typeof SETTINGS_SECTIONS)[number]["id"];

/** Link to one Settings category (HashRouter: works from any screen, with or without a router). */
export function settingsHref(id: SettingsSectionId): string {
  return `#/settings?section=${id}`;
}
