/** "Full auto" or "Trial run" (wait for approval before each submit), remembered between runs. */
export type ApplyMode = "auto" | "trial";

const STORAGE_KEY = "langhire_apply_mode";

export function loadApplyMode(): ApplyMode {
  try {
    return localStorage.getItem(STORAGE_KEY) === "trial" ? "trial" : "auto";
  } catch {
    return "auto";
  }
}

export function saveApplyMode(mode: ApplyMode) {
  try {
    localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    /* private mode: the choice just isn't remembered */
  }
}
