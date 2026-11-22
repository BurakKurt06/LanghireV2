import { useEffect, useState } from "react";
import { FolderOpen } from "lucide-react";
import { open } from "@tauri-apps/plugin-dialog";
import { getSettings, saveSettings, apiErrorMessage } from "../lib/api";

/** The resume file input with a Browse button (PDF). */
export function ResumePathInput({ id, value, onChange, onCommit }: {
  id: string;
  value: string;
  onChange: (path: string) => void;
  /** Called with the final value when the field loses focus or a file is picked. */
  onCommit?: (path: string) => void;
}) {
  return (
    <div className="flex gap-2">
      <input id={id} value={value} onChange={(e) => onChange(e.target.value)}
        onBlur={(e) => onCommit?.(e.target.value)}
        placeholder="/path/to/your/resume.pdf" className="input-base flex-1" />
      <button type="button" onClick={async () => {
          let file: string | string[] | null = null;
          try {
            file = await open({ filters: [{ name: "PDF", extensions: ["pdf"] }] });
          } catch {
            return; // not inside the app (browser preview): type the path instead
          }
          if (typeof file === "string" && file) {
            onChange(file);
            onCommit?.(file);
          }
        }}
        className="btn-secondary">
        <FolderOpen className="w-4 h-4" /> Browse
      </button>
    </div>
  );
}

/**
 * Profile → Resume. The same stored value as Settings → Applications → Resume (settings
 * "resume_path"): change it in either place and the other shows the change. Saved at once.
 */
export default function ProfileResume() {
  const [path, setPath] = useState("");
  const [saved, setSaved] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getSettings()
      .then((s) => {
        setPath(s.resume_path || "");
        setSaved(s.resume_path || "");
      })
      .catch(() => setError("Couldn't read the resume setting."))
      .finally(() => setLoaded(true));
  }, []);

  const commit = async (value: string) => {
    const next = value.trim();
    if (next === saved) return;
    setError(null);
    try {
      await saveSettings({ resume_path: next });   // a partial save: only this setting changes
      setSaved(next);
      setStatus("Saved");
    } catch (e) {
      setError(apiErrorMessage(e, "Couldn't save the resume path. Try again."));
    }
  };

  if (!loaded) return null;
  return (
    <div>
      <label htmlFor="profile-resume-path" className="block text-sm font-semibold text-foreground mb-1.5">Resume file (PDF)</label>
      <ResumePathInput id="profile-resume-path" value={path} onChange={(v) => { setPath(v); setStatus(null); }} onCommit={commit} />
      <p className="text-[13px] text-muted-foreground mt-2">
        The resume the agent uploads to applications. It is the same file as in Settings → Applications.
        {status && <span role="status" className="ml-2 font-semibold text-foreground">{status}</span>}
      </p>
      {error && <p role="alert" className="text-sm text-destructive mt-1">{error}</p>}
    </div>
  );
}
