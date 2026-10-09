/**
 * "Full auto", "Trial run" (wait for approval before each submit) or "Training"
 * (fill everything in, never submit), remembered between runs.
 */
export type ApplyMode = "auto" | "trial" | "training";

export const APPLY_MODE_LABELS: Record<ApplyMode, string> = {
  auto: "Full auto",
  trial: "Trial run",
  training: "Training",
};

const STORAGE_KEY = "langhire_apply_mode";

export function loadApplyMode(): ApplyMode {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored === "trial" || stored === "training" ? stored : "auto";
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
