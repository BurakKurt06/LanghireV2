import { useEffect } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { CircleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useApplyStatus } from "../hooks/useApplyStatus";

/**
 * App-wide "❗ the agent needs you" pill: shown on every page while an
 * application waits for the user's review or help, and a ❗ in the window title.
 */
export default function AgentAttention() {
  const { t } = useTranslation("apply");
  const navigate = useNavigate();
  const location = useLocation();
  const status = useApplyStatus();
  const waiting = status?.running ? (status.help?.length ?? 0) + (status.reviews?.length ?? 0) : 0;

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
