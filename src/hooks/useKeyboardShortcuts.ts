/**
 * Global keyboard shortcuts for power users (issue #46).
 *
 * - Cmd/Ctrl + 1..8  → navigate to the Nth page (order matches the sidebar)
 * - Cmd/Ctrl + ,     → open Settings
 * - Cmd/Ctrl + L     → open the logs (Settings → App & browser → Logs)
 * - Cmd/Ctrl + E     → trigger an export (dispatches a `langhire:export` event)
 * - F11              → real fullscreen on / off; Esc leaves it (works while typing too)
 *
 * Shortcuts are ignored while the user is typing in an input/textarea/select
 * or a contentEditable element, so they never clobber normal text entry.
 */
import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { leaveFullscreen, toggleFullscreen } from "../lib/fullscreen";

/** Page order for Cmd/Ctrl+1..N — kept in sync with the sidebar nav. */
export const SHORTCUT_ROUTES: string[] = [
  "/", // 1 Dashboard
  "/profile", // 2 Profile
  "/jobs", // 3 Jobs
  "/inbox", // 4 Inbox
  "/memory", // 5 Memory
  "/qa", // 6 Q&A
  "/guide", // 7 Guide
  "/settings", // 8 Settings
];

function isTypingTarget(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  const tag = el.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    el.isContentEditable
  );
}

export function useKeyboardShortcuts(): void {
  const navigate = useNavigate();

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "F11") {
        e.preventDefault();
        if (!e.repeat) void toggleFullscreen();   // a held key toggles once
        return;
      }
      if (e.key === "Escape") {
        // Esc that closes a dialog (it handles the key first, or is still open) only closes the
        // dialog; Esc anywhere else leaves fullscreen. Nothing is prevented here.
        if (!e.defaultPrevented && !document.querySelector('[role="dialog"][aria-modal="true"]')) {
          void leaveFullscreen();
        }
        return;
      }
      const mod = e.metaKey || e.ctrlKey;
      if (!mod) return;
      if (isTypingTarget(e.target)) return;

      // Cmd/Ctrl + , → Settings
      if (e.key === ",") {
        e.preventDefault();
        navigate("/settings");
        return;
      }

      // Cmd/Ctrl + L → the logs
      if (e.key.toLowerCase() === "l") {
        e.preventDefault();
        navigate("/settings?section=logs");
        return;
      }

      // Cmd/Ctrl + E → export (page decides what to do)
      if (e.key.toLowerCase() === "e") {
        e.preventDefault();
        window.dispatchEvent(new CustomEvent("langhire:export"));
        return;
      }

      // Cmd/Ctrl + 1..8 → page navigation
      if (/^[1-8]$/.test(e.key)) {
        const idx = Number(e.key) - 1;
        const route = SHORTCUT_ROUTES[idx];
        if (route) {
          e.preventDefault();
          navigate(route);
        }
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [navigate]);
}
