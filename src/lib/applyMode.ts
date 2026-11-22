/**
 * "Full auto" (final Submit included) or "Trial run" (the same live flow with
 * the agent's final-submit action blocked, no approval), remembered
 * between runs. A remembered "training" (the trial run's old name) opens as a trial run.
 */
export type ApplyMode = "auto" | "trial";

export const APPLY_MODE_LABELS: Record<ApplyMode, string> = {
  auto: "Full auto",
  trial: "Trial run",
};

const STORAGE_KEY = "langhire_apply_mode";

/** Full auto unless the user chose Trial run (the user's decision, 2026-11: submit by default). */
export function loadApplyMode(): ApplyMode {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored === "trial" || stored === "training" ? "trial" : "auto";
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
