/**
 * Real fullscreen for the app window (F11 toggles it, Esc leaves it).
 *
 * In the desktop app the window belongs to Tauri: the web view's own F11 does nothing, so the
 * window itself is asked. In a plain browser (development) the page's fullscreen is used.
 */
type AppWindow = { isFullscreen(): Promise<boolean>; setFullscreen(on: boolean): Promise<void> };

async function appWindow(): Promise<AppWindow | null> {
  if (!("__TAURI_INTERNALS__" in window)) return null;
  try {
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    return getCurrentWindow();
  } catch {
    return null;
  }
}

export async function isFullscreen(): Promise<boolean> {
  const win = await appWindow();
  if (win) {
    try {
      return await win.isFullscreen();
    } catch {
      return false;
    }
  }
  return document.fullscreenElement != null;
}

export async function setFullscreen(on: boolean): Promise<void> {
  const win = await appWindow();
  try {
    if (win) {
      await win.setFullscreen(on);
    } else if (on) {
      await document.documentElement.requestFullscreen?.();
    } else if (document.fullscreenElement) {
      await document.exitFullscreen?.();
    }
  } catch {
    /* the window refused: nothing to do */
  }
}

// One change at a time: two quick presses toggle twice (on, then off), never both "on".
let queue: Promise<void> = Promise.resolve();
function inTurn(change: () => Promise<void>): Promise<void> {
  queue = queue.then(change, change);
  return queue;
}

export function toggleFullscreen(): Promise<void> {
  return inTurn(async () => setFullscreen(!(await isFullscreen())));
}

/** Esc: leave fullscreen if the window is in it (otherwise nothing happens). */
export function leaveFullscreen(): Promise<void> {
  return inTurn(async () => {
    if (await isFullscreen()) await setFullscreen(false);
  });
}
