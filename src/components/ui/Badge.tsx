import type { ReactNode } from "react";

type BadgeVariant = "success" | "error" | "warning" | "info" | "muted" | "primary";

const VARIANT_STYLES: Record<BadgeVariant, string> = {
  success: "bg-[#F0FFF0] text-success dark:bg-green-500/15 dark:text-green-400",
  error: "bg-[#FFF0F0] text-destructive dark:bg-red-500/15 dark:text-red-400",
  warning: "bg-[#FFF8F0] text-warning dark:bg-orange-500/15 dark:text-orange-300",
  info: "bg-[#F0F4FF] text-[#3B5998] dark:bg-blue-500/15 dark:text-blue-300",
  muted: "bg-[#F7F7F7] text-muted-foreground dark:bg-muted",
  primary: "bg-[#FFF0F3] text-primary dark:bg-primary/15",
};

interface BadgeProps {
  variant?: BadgeVariant;
  children: ReactNode;
  className?: string;
}

export default function Badge({ variant = "muted", children, className = "" }: BadgeProps) {
  return (
    <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold ${VARIANT_STYLES[variant]} ${className}`}>
      {children}
    </span>
  );
}
