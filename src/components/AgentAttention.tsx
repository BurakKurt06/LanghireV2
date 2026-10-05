import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { CircleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";
import { getApplyStatus } from "../lib/api";

const POLL_MS = 3000;

/**
 * App-wide "❗ the agent needs you" pill: shown on every page while an
 * application waits for the user's review or help, and a ❗ in the window title.
 */
export default function AgentAttention() {
  const { t } = useTranslation("apply");
  const navigate = useNavigate();
  const location = useLocation();
  const [waiting, setWaiting] = useState(0);

  useEffect(() => {
    let active = true;
    const check = () =>
      getApplyStatus()
        .then((s) => {
          if (active) setWaiting(s.running ? (s.help?.length ?? 0) + (s.reviews?.length ?? 0) : 0);
        })
        .catch(() => {});
    check();
    const timer = setInterval(check, POLL_MS);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    const base = document.title.replace(/^❗ /, "");
    document.title = waiting > 0 ? `❗ ${base}` : base;
  }, [waiting]);

  const onHistory = location.pathname === "/jobs" && new URLSearchParams(location.search).get("tab") === "history";
  if (waiting === 0 || onHistory) return null;

  return (
    <button
      onClick={() => navigate("/jobs?tab=history")}
      className="fixed top-4 right-4 z-50 inline-flex items-center gap-2 rounded-full bg-amber-500 px-4 py-2 text-sm font-semibold text-white shadow-lg hover:bg-amber-600"
    >
      <CircleAlert className="h-4 w-4" />
      {t("attention", { count: waiting })}
    </button>
  );
}
