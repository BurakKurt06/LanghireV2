import type { ApplyMode } from "../lib/applyMode";

interface ApplyModeToggleProps {
  value: ApplyMode;
  onChange: (mode: ApplyMode) => void;
  /** One line, for the floating batch bar. */
  compact?: boolean;
}

/** Full auto, a trial run that waits for the user's OK before each Submit, or training (never submits). */
export default function ApplyModeToggle({ value, onChange, compact }: ApplyModeToggleProps) {
  const option = (mode: ApplyMode, label: string) => (
    <button
      type="button"
      role="radio"
      aria-checked={value === mode}
      onClick={() => onChange(mode)}
      className={`px-3 py-1.5 text-xs font-semibold rounded-lg transition-colors ${
        value === mode ? "bg-foreground text-background" : "text-muted-foreground hover:text-foreground"
      }`}
    >
      {label}
    </button>
  );

  return (
    <div className={compact ? "inline-flex items-center" : "space-y-1.5"}>
      <div role="radiogroup" aria-label="Apply mode" className="inline-flex gap-1 p-1 rounded-xl border border-border">
        {option("auto", "Full auto")}
        {option("trial", "Trial run")}
        {option("training", "Training")}
      </div>
      {!compact && (
        <p className="text-xs text-muted-foreground">
          {value === "trial"
            ? "Fills in each application, then waits before Submit so you can check it (History tab: Approve & submit, or Skip)."
            : value === "training"
            ? "Practice: fills in each application up to the final Submit, never submits, then closes it. Jobs stay pending; answers and site tips are saved for the real run."
            : "Fills in and submits each application on its own."}
        </p>
      )}
    </div>
  );
}
