import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// i18n: identity translator so assertions can use raw keys.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { changeLanguage: vi.fn() } }),
}));

import FeedbackSection from "./FeedbackSection";

const EMAIL = "t1467635@gmail.com";

describe("Send feedback (Settings -> App & browser)", () => {
  it("says what to send and points at a job's own log", () => {
    render(<FeedbackSection />);
    expect(screen.getByText(/Found a bug, or want to request a feature/)).toBeInTheDocument();
    expect(screen.getByText(/View log/)).toBeInTheDocument();
  });

  it("shows the feedback email as a mailto link", () => {
    render(<FeedbackSection />);
    const link = screen.getByRole("link", { name: new RegExp(EMAIL) });
    expect(link).toHaveAttribute("href", `mailto:${EMAIL}`);
  });

  it("does not render a submission form", () => {
    render(<FeedbackSection />);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("makes no network requests", () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy as unknown as typeof fetch);
    render(<FeedbackSection />);
    expect(fetchSpy).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
