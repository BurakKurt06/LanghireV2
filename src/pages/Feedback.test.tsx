import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// i18n: identity translator so assertions can use raw keys.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { changeLanguage: vi.fn() } }),
}));

import Feedback from "./Feedback";

const EMAIL = "t1467635@gmail.com";

describe("Feedback", () => {
  it("renders the page heading and subtitle", () => {
    render(<Feedback />);
    expect(screen.getByText("title")).toBeInTheDocument();
    expect(screen.getByText("subtitle")).toBeInTheDocument();
  });

  it("shows the feedback email as a mailto link", () => {
    render(<Feedback />);
    const link = screen.getByRole("link", { name: new RegExp(EMAIL) });
    expect(link).toHaveAttribute("href", `mailto:${EMAIL}`);
  });

  it("does not render a submission form", () => {
    render(<Feedback />);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("makes no network requests", () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy as unknown as typeof fetch);
    render(<Feedback />);
    expect(fetchSpy).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
