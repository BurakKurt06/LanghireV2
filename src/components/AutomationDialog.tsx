import type { ReactNode } from "react";
import { AlertTriangle, Clock, Globe, X } from "lucide-react";
import { useDialog } from "../hooks/useDialog";

interface AutomationDialogProps {
  open: boolean;
  title: string;
  onConfirm: () => void;
  onCancel: () => void;
  /** Extra options shown above the buttons (e.g. Full auto / Trial run). */
  children?: ReactNode;
}

export default function AutomationDialog({ open, title, onConfirm, onCancel, children }: AutomationDialogProps) {
  const panel = useDialog<HTMLDivElement>(open, onCancel);
  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={onCancel}>
      <div ref={panel} role="dialog" aria-modal="true" aria-labelledby="automation-dialog-title" tabIndex={-1}
        className="bg-card rounded-2xl shadow-xl w-full max-w-lg mx-4 overflow-hidden" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-6 pt-6 pb-0">
          <h2 id="automation-dialog-title" className="text-lg font-bold text-foreground">{title}</h2>
          <button onClick={onCancel} aria-label="Close" className="p-1.5 rounded-lg hover:bg-secondary transition-colors cursor-pointer">
            <X className="w-5 h-5 text-muted-foreground" />
          </button>
        </div>

        <div className="px-6 py-5 space-y-4">
          <div className="flex gap-3 p-4 bg-amber-50 border border-amber-200 rounded-xl">
            <Clock className="w-5 h-5 text-amber-600 flex-shrink-0 mt-0.5" />
            <div className="text-sm text-amber-900">
              <strong className="block mb-1">This may take a while</strong>
              The automation needs time to launch a browser, load pages, and interact with websites.
              Please be patient and <strong>do not close the app</strong> while it's running.
            </div>
          </div>

          <div className="flex gap-3 p-4 bg-blue-50 border border-blue-200 rounded-xl">
            <Globe className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
            <div className="text-sm text-blue-900">
              <strong className="block mb-1">Log in first</strong>
              Sign in to the boards you use from the app before you start. Collection reuses those
              sessions. If a session expires, sign in again from the app and retry that board.
            </div>
          </div>

          <div className="flex gap-3 p-4 bg-[#F7F7F7] border border-border rounded-xl">
            <AlertTriangle className="w-5 h-5 text-muted-foreground flex-shrink-0 mt-0.5" />
            <div className="text-sm text-foreground">
              <strong className="block mb-1">Keep the browser window open</strong>
              A browser window will appear: this is the AI agent working. Don't close it. To fix
              something yourself, press Pause in the History tab first, so the agent keeps your changes.
            </div>
          </div>
          {children}
        </div>

        <div className="flex justify-end gap-3 px-6 pb-6">
          <button onClick={onCancel} className="btn-secondary">Cancel</button>
          <button onClick={onConfirm} className="btn-dark">Got it, Start</button>
        </div>
      </div>
    </div>
  );
}
