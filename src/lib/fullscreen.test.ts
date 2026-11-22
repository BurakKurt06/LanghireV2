import { describe, it, expect, vi, afterEach } from "vitest";

// The app window (Tauri): fullscreen is asked of the window itself.
const win = vi.hoisted(() => {
  const state = { on: false, calls: [] as boolean[] };
  return {
    state,
    isFullscreen: async () => state.on,
    // Slow, like a real window: a second press arriving meanwhile must wait for this one.
    setFullscreen: async (value: boolean) => {
      await new Promise((r) => setTimeout(r, 20));
      state.calls.push(value);
      state.on = value;
    },
  };
});
vi.mock("@tauri-apps/api/window", () => ({ getCurrentWindow: () => win }));

import { leaveFullscreen, toggleFullscreen } from "./fullscreen";

const inApp = () => { (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {}; };

describe("fullscreen", () => {
  afterEach(() => {
    win.state.on = false;
    win.state.calls.length = 0;
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  });

  it("two quick F11 presses go on, then off (never on twice)", async () => {
    inApp();
    await Promise.all([toggleFullscreen(), toggleFullscreen()]);
    expect(win.state.calls).toEqual([true, false]);
    expect(win.state.on).toBe(false);
  });

  it("Esc leaves fullscreen only when the window is in it", async () => {
    inApp();
    await leaveFullscreen();
    expect(win.state.calls).toEqual([]);
    await toggleFullscreen();
    await leaveFullscreen();
    expect(win.state.calls).toEqual([true, false]);
  });
});
