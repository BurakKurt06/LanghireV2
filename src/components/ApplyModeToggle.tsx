import { APPLY_MODE_LABELS, type ApplyMode } from "../lib/applyMode";

interface ApplyModeToggleProps {
  value: ApplyMode;
  onChange: (mode: ApplyMode) => void;
  /** One line, for the floating batch bar. */
  compact?: boolean;
}

/** Full auto, or the same live flow with the agent's final-submit action stopped. */
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
        {option("auto", APPLY_MODE_LABELS.auto)}
        {option("trial", APPLY_MODE_LABELS.trial)}
      </div>
      {!compact && (
        <p className="text-xs text-muted-foreground">
          {value === "trial"
            ? "Live trial: signs in, uploads, saves and moves through Next like Full auto, but the agent stops before pressing the final Submit application button. No approval prompts. The site can still save data or submit during intermediate steps or through its own scripts; the job stays pending for you to check."
            : "Applies on its own, final Submit included. Account details are typed only on the employer's own site; any other sign-in becomes Needs you."}
        </p>
      )}
    </div>
  );
}
