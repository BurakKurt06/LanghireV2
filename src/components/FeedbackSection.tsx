import { Mail } from "lucide-react";

// Where feedback should be sent. Change this one line to use a different address.
const FEEDBACK_EMAIL = "t1467635@gmail.com";

/** Settings → App & browser → Send feedback. */
export default function FeedbackSection() {
  return (
    <div className="flex items-center justify-between gap-4">
      <p className="text-[13px] text-muted-foreground">
        Found a bug, or want to request a feature? Email it over. For a problem with one job, open that job's
        View log and paste what it shows.
      </p>
      <a href={`mailto:${FEEDBACK_EMAIL}`} className="btn-primary text-xs flex-shrink-0">
        <Mail className="w-3.5 h-3.5" />
        {FEEDBACK_EMAIL}
      </a>
    </div>
  );
}
