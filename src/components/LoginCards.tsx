import { useState, useEffect, useCallback } from "react";
import { CheckCircle, XCircle, LogIn, Loader2, RefreshCw } from "lucide-react";
import { getAuthStatus, launchLogin } from "../lib/api";

type Service = "linkedin" | "gmail";

interface AuthStatus {
  linkedin: { logged_in: boolean };
  gmail: { logged_in: boolean };
  login_window_open?: boolean;
}

const SERVICES: { id: Service; name: string; reason: string }[] = [
  {
    id: "gmail",
    name: "Gmail",
    reason:
      "Many application sites email a verification code or link. When Gmail is signed in, the agent opens it and enters the code for you.",
  },
  {
    id: "linkedin",
    name: "LinkedIn",
    reason: "Needed for LinkedIn job postings and Easy Apply.",
  },
];

/** Sign in once to Gmail / LinkedIn in the agents' own browser profile; it lasts across runs. */
export default function LoginCards() {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [launching, setLaunching] = useState<Service | null>(null);
  const [message, setMessage] = useState<{ service: Service; text: string; error: boolean } | null>(null);

  const fetchStatus = useCallback(async () => {
    try {
      setStatus(await getAuthStatus());
    } catch {
      // backend not ready yet
    }
  }, []);

  useEffect(() => {
    void fetchStatus();
    const interval = setInterval(() => void fetchStatus(), 5000);
    return () => clearInterval(interval);
  }, [fetchStatus]);

  const handleLogin = async (service: Service) => {
    setLaunching(service);
    setMessage(null);
    try {
      const res = await launchLogin(service);
      setMessage({ service, text: res.message, error: !res.success });
      void fetchStatus();
    } catch (e) {
      setMessage({ service, text: e instanceof Error ? e.message : "Couldn't open the browser", error: true });
    } finally {
      setLaunching(null);
    }
  };

  if (!status) return null;
  const windowOpen = Boolean(status.login_window_open);

  return (
    <div className="space-y-3">
      <p className="text-[13px] text-muted-foreground">
        Sign in once in the agent's browser; it stays signed in for every run. A window opens: sign in
        there, then close it.
      </p>
      {SERVICES.map(({ id, name, reason }) => {
        const loggedIn = status[id]?.logged_in;
        return (
          <div key={id} className="flex items-start justify-between gap-4 rounded-lg border border-border p-4">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="font-semibold text-foreground">{name}</span>
                {loggedIn ? (
                  <span className="inline-flex items-center gap-1 rounded-full bg-green-500/10 px-2 py-0.5 text-xs font-medium text-green-600">
                    <CheckCircle className="h-3 w-3" /> Signed in
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/10 px-2 py-0.5 text-xs font-medium text-amber-600">
                    <XCircle className="h-3 w-3" /> Not signed in
                  </span>
                )}
              </div>
              <p className="mt-1 text-[13px] text-muted-foreground">{reason}</p>
              {message?.service === id && (
                <p className={`mt-1 text-[13px] ${message.error ? "text-red-600" : "text-foreground"}`} role="status">
                  {message.text}
                </p>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <button
                onClick={() => void fetchStatus()}
                className="p-1.5 text-muted-foreground hover:text-foreground"
                title="Check again"
                aria-label={`Check ${name} status again`}
              >
                <RefreshCw className="h-3.5 w-3.5" />
              </button>
              <button
                onClick={() => void handleLogin(id)}
                disabled={launching !== null || windowOpen}
                className={loggedIn ? "btn-secondary" : "btn-primary"}
              >
                {launching === id ? <Loader2 className="h-4 w-4 animate-spin" /> : <LogIn className="h-4 w-4" />}
                {launching === id ? "Opening..." : loggedIn ? "Sign in again" : `Sign in to ${name}`}
              </button>
            </div>
          </div>
        );
      })}
      {windowOpen && (
        <p className="text-[13px] text-muted-foreground">
          A sign-in window is open. Close it when you're done; applications can't start while it's open.
        </p>
      )}
    </div>
  );
}
