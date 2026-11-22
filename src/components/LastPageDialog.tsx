import { useEffect, useState } from "react";
import { Image, Loader2, X } from "lucide-react";
import { getJobScreenshot } from "../lib/api";
import { useDialog } from "../hooks/useDialog";

/**
 * The picture of a job's last page: what the page looked like when the job stopped (failed,
 * needs you, or not confirmed). The browser took it itself when the application ended; a later
 * try replaces it, and it is deleted once the job is applied to or removed.
 */
export default function LastPageDialog({ url, title, onClose }: { url: string; title: string; onClose: () => void }) {
  const [picture, setPicture] = useState<string | null>(null);
  const [state, setState] = useState<"loading" | "shown" | "none" | "failed">("loading");

  useEffect(() => {
    let active = true;
    let made: string | null = null;
    getJobScreenshot(url)
      .then((blob) => {
        if (!active) return;
        if (!blob) {
          setState("none");
          return;
        }
        made = URL.createObjectURL(blob);
        setPicture(made);
        setState("shown");
      })
      .catch(() => { if (active) setState("failed"); });
    return () => {
      active = false;
      if (made) URL.revokeObjectURL(made);
    };
  }, [url]);

  const panel = useDialog<HTMLDivElement>(true, onClose);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6" onClick={onClose}>
      <div ref={panel} tabIndex={-1} role="dialog" aria-modal="true" aria-label={`Last page of ${title}`}
        className="bg-card rounded-2xl shadow-xl w-full max-w-5xl max-h-[90vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between gap-3 px-5 py-4 border-b border-border">
          <h3 className="text-sm font-bold text-foreground flex items-center gap-2 min-w-0">
            <Image className="w-4 h-4 flex-shrink-0" />
            <span className="truncate">Last page: {title}</span>
          </h3>
          <button onClick={onClose} aria-label="Close" className="text-muted-foreground hover:text-foreground">
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="flex-1 overflow-auto p-4">
          {state === "loading" && (
            <div className="flex items-center justify-center h-32"><Loader2 className="w-6 h-6 animate-spin text-muted-foreground" /></div>
          )}
          {state === "none" && (
            <p className="text-muted-foreground text-center mt-10">No picture of this job's last page is kept.</p>
          )}
          {state === "failed" && (
            <p role="alert" className="text-destructive text-center mt-10">Couldn't load the picture. Try again in a moment.</p>
          )}
          {state === "shown" && picture && (
            <>
              <p className="mb-2 text-[12px] text-muted-foreground">
                What the page looked like when this job stopped. The next try replaces this picture.
              </p>
              <img src={picture} alt={`The last page of the application to ${title}`} className="w-full rounded-lg border border-border" />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
