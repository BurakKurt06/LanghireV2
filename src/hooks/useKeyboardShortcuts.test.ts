import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useKeyboardShortcuts, SHORTCUT_ROUTES } from "./useKeyboardShortcuts";

const mockNavigate = vi.fn();
vi.mock("react-router-dom", () => ({
  useNavigate: () => mockNavigate,
}));

const fullscreen = vi.hoisted(() => ({ toggle: vi.fn(async () => {}), leave: vi.fn(async () => {}) }));
vi.mock("../lib/fullscreen", () => ({
  toggleFullscreen: fullscreen.toggle,
  leaveFullscreen: fullscreen.leave,
}));

function press(key: string, opts: Partial<KeyboardEventInit> & { target?: EventTarget } = {}) {
  const ev = new KeyboardEvent("keydown", { key, metaKey: true, bubbles: true, cancelable: true, ...opts });
  if (opts.target) Object.defineProperty(ev, "target", { value: opts.target });
  window.dispatchEvent(ev);
  return ev;
}

describe("useKeyboardShortcuts", () => {
  beforeEach(() => {
    mockNavigate.mockClear();
    fullscreen.toggle.mockClear();
    fullscreen.leave.mockClear();
  });

  it("F11 toggles real fullscreen once per press, even while typing; a held key doesn't flicker", () => {
    renderHook(() => useKeyboardShortcuts());
    const typing = document.createElement("input");
    const ev = press("F11", { metaKey: false, target: typing });
    expect(ev.defaultPrevented).toBe(true);            // the web view's own F11 never runs
    expect(fullscreen.toggle).toHaveBeenCalledTimes(1);
    press("F11", { metaKey: false, repeat: true });
    press("F11", { metaKey: false, repeat: true });
    expect(fullscreen.toggle).toHaveBeenCalledTimes(1);
  });

  it("Esc leaves fullscreen, but only closes a dialog when one is open", () => {
    renderHook(() => useKeyboardShortcuts());
    press("Escape", { metaKey: false });
    expect(fullscreen.leave).toHaveBeenCalledTimes(1);

    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    document.body.appendChild(dialog);
    const ev = press("Escape", { metaKey: false });
    expect(ev.defaultPrevented).toBe(false);           // the dialog still gets its Esc
    expect(fullscreen.leave).toHaveBeenCalledTimes(1);
    dialog.remove();

    // A dialog that already handled Esc (and is closing) doesn't take the window out of fullscreen either.
    const handled = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    handled.preventDefault();
    window.dispatchEvent(handled);
    expect(fullscreen.leave).toHaveBeenCalledTimes(1);
  });

  it("navigates to each page on Cmd/Ctrl+1..8", () => {
    renderHook(() => useKeyboardShortcuts());
    for (let i = 1; i <= SHORTCUT_ROUTES.length; i++) {
      press(String(i));
      expect(mockNavigate).toHaveBeenLastCalledWith(SHORTCUT_ROUTES[i - 1]);
    }
  });

  it("opens settings on Cmd/Ctrl+,", () => {
    renderHook(() => useKeyboardShortcuts());
    press(",");
    expect(mockNavigate).toHaveBeenCalledWith("/settings");
  });

  it("dispatches a langhire:export event on Cmd/Ctrl+E", () => {
    renderHook(() => useKeyboardShortcuts());
    const spy = vi.fn();
    window.addEventListener("langhire:export", spy);
    press("e");
    expect(spy).toHaveBeenCalled();
    window.removeEventListener("langhire:export", spy);
  });

  it("ignores shortcuts without the modifier key", () => {
    renderHook(() => useKeyboardShortcuts());
    press("1", { metaKey: false, ctrlKey: false });
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("ignores shortcuts while typing in an input", () => {
    renderHook(() => useKeyboardShortcuts());
    const input = document.createElement("input");
    document.body.appendChild(input);
    press("1", { target: input });
    expect(mockNavigate).not.toHaveBeenCalled();
    input.remove();
  });

  it("ignores out-of-range number keys (9, 0)", () => {
    renderHook(() => useKeyboardShortcuts());
    press("9");
    press("0");
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("removes its listener on unmount", () => {
    const { unmount } = renderHook(() => useKeyboardShortcuts());
    unmount();
    press("1");
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});
