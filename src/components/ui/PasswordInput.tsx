import { useState, type InputHTMLAttributes } from "react";
import { Eye, EyeOff } from "lucide-react";
import { SECRET_MASK } from "../../lib/secrets";

type PasswordInputProps = Omit<InputHTMLAttributes<HTMLInputElement>, "type"> & {
  /**
   * Fetches the saved secret in full. The page only ever receives a row of dots for a saved
   * password or key, so without this the eye button could only "show" those dots.
   */
  reveal?: () => Promise<string>;
  /** Called with the saved secret once it was fetched, so the field holds (and shows) it. */
  onRevealed?: (value: string) => void;
};

/** Password / API-key input with an eye button to show or hide it. */
export default function PasswordInput({ className = "", reveal, onRevealed, ...props }: PasswordInputProps) {
  const [visible, setVisible] = useState(false);
  const [failed, setFailed] = useState(false);

  const toggle = async () => {
    setFailed(false);
    if (!visible && props.value === SECRET_MASK && reveal) {
      try {
        onRevealed?.(await reveal());
      } catch {
        setFailed(true);   // still dots: say so instead of pretending to show it
        return;
      }
    }
    setVisible((v) => !v);
  };

  return (
    <div>
      <div className="relative">
        <input {...props} type={visible ? "text" : "password"} className={`${className} pr-10`} />
        <button
          type="button"
          onClick={() => void toggle()}
          aria-label={visible ? "Hide" : "Show"}
          aria-pressed={visible}
          title={visible ? "Hide" : "Show"}
          className="absolute inset-y-0 right-0 flex items-center px-3 text-muted-foreground hover:text-foreground"
        >
          {visible ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
        </button>
      </div>
      {failed && <p role="alert" className="text-[12px] text-destructive mt-1">Couldn't read the saved value. Type it again to change it.</p>}
    </div>
  );
}
