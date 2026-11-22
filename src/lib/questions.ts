/** The answers that were actually given (blank ones left out). */
export function answeredOnly(answers: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(answers).filter(([, value]) => value.trim() !== ""));
}

/** "m:ss" for a number of seconds (the run panel's countdown). */
export function minutesSeconds(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
