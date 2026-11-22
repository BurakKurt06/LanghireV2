import { useCallback, useRef, useState } from "react";
import { checkBeforeStart } from "../lib/api";
import type { StartCheck } from "../lib/types";
import StartCheckDialog from "../components/StartCheckDialog";

/**
 * `confirmStart(urls)` runs the check for the jobs about to run and resolves true when the run
 * may start: at once when there is nothing to say (or the check itself couldn't be made), else
 * after the user's choice in `dialog` (render it somewhere on the page).
 */
export function useStartCheck() {
  const [check, setCheck] = useState<StartCheck | null>(null);
  const decide = useRef<((start: boolean) => void) | null>(null);

  const confirmStart = useCallback(async (urls: string[]): Promise<boolean> => {
    let result: StartCheck;
    try {
      result = await checkBeforeStart(urls);
    } catch {
      return true;            // the check couldn't be made: start as before, the run itself reports problems
    }
    if (!(result.problems ?? []).length && !(result.questions ?? []).length) return true;
    return new Promise<boolean>((resolve) => {
      decide.current = resolve;
      setCheck(result);
    });
  }, []);

  const onDecide = (start: boolean) => {
    decide.current?.(start);
    decide.current = null;
    setCheck(null);
  };

  return { confirmStart, dialog: check ? <StartCheckDialog check={check} onDecide={onDecide} /> : null };
}
