/** Profile fields edited in Settings (Job search, Applications); the Profile page edits the rest.
 *  Each page re-reads the profile before saving and writes back only its own fields. */
export const PROFILE_SETTINGS_FIELDS = [
  "search_area",
  "target_job_titles",
  "target_locations",
  "job_search",
  "notes",
] as const;
