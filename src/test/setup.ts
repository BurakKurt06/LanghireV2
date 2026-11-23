/**
 * Global Vitest setup, loaded before every test file (see vite.config.ts).
 *
 * - Registers @testing-library/jest-dom matchers (toBeInTheDocument, etc.).
 * - Mocks the Tauri runtime (`@tauri-apps/api/*`), which is unavailable in
 *   jsdom — `invoke` is replaced with a vi.fn() that tests can override.
 * - Cleans up the React tree and resets mocks between tests.
 */
import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup, configure } from "@testing-library/react";

// findBy*/waitFor give up after 3 s instead of 1 s: on the slower Windows CI runner a page's first
// load and debounce could take longer than 1 s (11-22). They still return as soon as it's there.
configure({ asyncUtilTimeout: 3000 });

// ── Mock the Tauri core bridge ──────────────────────────────────────────────
// api.ts does `invoke<string>("get_api_token")`; default to a dummy token so
// the request helper doesn't throw. Individual tests can `vi.mocked(invoke)...`.
vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(async (cmd: string) => {
    if (cmd === "get_api_token") return "test-token";
    return undefined;
  }),
}));

// Some components import the dialog/opener plugins; stub them defensively.
vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: vi.fn(async () => null),
  save: vi.fn(async () => null),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
