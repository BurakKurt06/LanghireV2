import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import AutomationDialog from "./AutomationDialog";

function renderDialog(overrides: Partial<React.ComponentProps<typeof AutomationDialog>> = {}) {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  const props = {
    open: true,
    title: "Start Automation",
    onConfirm,
    onCancel,
    ...overrides,
  };
  render(<AutomationDialog {...props} />);
  return { onConfirm, onCancel };
}

describe("AutomationDialog", () => {
  it("renders nothing when open is false", () => {
    const { onConfirm, onCancel } = renderDialog({ open: false });
    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
    expect(onConfirm).not.toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("renders the title and informational sections when open", () => {
    renderDialog({ title: "Apply to Jobs" });
    expect(screen.getByRole("heading", { name: "Apply to Jobs" })).toBeInTheDocument();
    expect(screen.getByText("This may take a while")).toBeInTheDocument();
    expect(screen.getByText("Log in first")).toBeInTheDocument();
    expect(screen.getByText("Keep the browser window open")).toBeInTheDocument();
  });

  // Dark mode (Codex's note, 2026-11-17): the third card had a fixed near-white surface under
  // theme-colored (near-white) text. Each card's surface, text and icon now follow the theme;
  // measured in a browser: text 12:1 or better in dark mode, unchanged in light mode.
  it("gives every notice card colors that follow the theme", () => {
    renderDialog();
    for (const title of ["This may take a while", "Log in first", "Keep the browser window open"]) {
      const card = screen.getByText(title).closest("div.flex") as HTMLElement;
      const classes = [card, ...Array.from(card.querySelectorAll("*"))].map((el) => el.getAttribute("class") || "").join(" ");
      expect(classes).not.toMatch(/bg-\[#/);                                   // no fixed color surface
      const themed = /\bbg-secondary\b/.test(card.className) || /\bdark:bg-/.test(card.className);
      expect(themed, `${title}: surface follows the theme`).toBe(true);
      if (!/\bbg-secondary\b/.test(card.className)) {
        expect(classes, `${title}: text and icon have dark-mode colors`).toMatch(/dark:text-\w+-100/);
        expect(classes).toMatch(/dark:text-\w+-400/);
      }
    }
  });

  it("calls onConfirm when the start button is clicked", async () => {
    const user = userEvent.setup();
    const { onConfirm, onCancel } = renderDialog();
    await user.click(screen.getByRole("button", { name: "Got it, Start" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("calls onCancel when the Cancel button is clicked", async () => {
    const user = userEvent.setup();
    const { onCancel, onConfirm } = renderDialog();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("calls onCancel when the X (close) button is clicked", async () => {
    const user = userEvent.setup();
    const { onCancel } = renderDialog();
    // The X button is the first button (icon-only, no accessible name beyond the svg).
    const buttons = screen.getAllByRole("button");
    // Close (X), Cancel, Got it, Start
    await user.click(buttons[0]);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("calls onCancel when clicking the backdrop overlay", async () => {
    const user = userEvent.setup();
    const { onCancel } = renderDialog();
    // Backdrop is the outermost container; clicking the heading region must NOT close.
    const heading = screen.getByRole("heading", { name: "Start Automation" });
    const backdrop = heading.closest("div.fixed");
    expect(backdrop).not.toBeNull();
    await user.click(backdrop as HTMLElement);
    expect(onCancel).toHaveBeenCalled();
  });

  it("does NOT call onCancel when clicking inside the dialog body (stopPropagation)", async () => {
    const user = userEvent.setup();
    const { onCancel } = renderDialog();
    await user.click(screen.getByText("This may take a while"));
    expect(onCancel).not.toHaveBeenCalled();
  });
});

describe("AutomationDialog keyboard and screen-reader behaviour", () => {
  it("is a labelled modal dialog with a named close button", () => {
    renderDialog({ title: "Apply to Jobs" });
    const dialog = screen.getByRole("dialog", { name: "Apply to Jobs" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("button", { name: "Close" })).toBeInTheDocument();
  });

  it("moves focus inside, keeps Tab inside, and Escape cancels", async () => {
    const user = userEvent.setup();
    const { onCancel } = renderDialog();
    const dialog = screen.getByRole("dialog");
    expect(dialog.contains(document.activeElement)).toBe(true);
    for (let i = 0; i < 6; i++) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
    await user.keyboard("{Escape}");
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("gives focus back to what had it when it closes", () => {
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();
    const props = { title: "T", onConfirm: vi.fn(), onCancel: vi.fn() };
    const { rerender } = render(<AutomationDialog open {...props} />);
    expect(document.activeElement).not.toBe(opener);
    rerender(<AutomationDialog open={false} {...props} />);
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });
});
