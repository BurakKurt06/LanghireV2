import { Mail } from "lucide-react";
import { useTranslation } from "react-i18next";

// Where feedback should be sent. Change this one line to use a different address.
const FEEDBACK_EMAIL = "xyz@gmail.com";

export default function Feedback() {
  const { t } = useTranslation("feedback");

  return (
    <div className="max-w-2xl">
      <div className="mb-1">
        <h1 className="text-2xl font-bold text-foreground tracking-tight">{t("title")}</h1>
        <p className="text-[13px] text-muted-foreground mt-1">{t("subtitle")}</p>
      </div>

      <div className="mt-6 card text-center py-10">
        <Mail className="w-12 h-12 text-muted-foreground mx-auto mb-4" />
        <h2 className="text-lg font-bold mb-2">Send feedback by email</h2>
        <p className="text-sm text-muted-foreground mb-5">
          Found a bug, or want to request a feature? Email it over.
        </p>
        <a href={`mailto:${FEEDBACK_EMAIL}`} className="btn-primary text-xs">
          <Mail className="w-3.5 h-3.5" />
          {FEEDBACK_EMAIL}
        </a>
      </div>
    </div>
  );
}
