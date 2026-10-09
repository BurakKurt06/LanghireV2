import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { getApplyStatus } from "../lib/api";
import { useApplyStatus } from "./useApplyStatus";

vi.mock("../lib/api", () => ({ getApplyStatus: vi.fn() }));
const mockStatus = vi.mocked(getApplyStatus);

function Show({ name }: { name: string }) {
  const status = useApplyStatus();
  return <p>{name}: {status ? (status.running ? "running" : "idle") : "…"}</p>;
}

describe("useApplyStatus", () => {
  beforeEach(() => {
    mockStatus.mockReset();
    mockStatus.mockResolvedValue({ running: true } as never);
  });

  it("shares one poll between every component that shows the status", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<><Show name="panel" /><Show name="pill" /></>);
      expect(await screen.findByText("panel: running")).toBeInTheDocument();
      expect(screen.getByText("pill: running")).toBeInTheDocument();
      expect(mockStatus).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(2100);
      expect(mockStatus).toHaveBeenCalledTimes(2); // one request per tick, not one per component
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops polling when nothing shows it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { unmount } = render(<Show name="panel" />);
      await waitFor(() => expect(mockStatus).toHaveBeenCalledTimes(1));
      unmount();
      await vi.advanceTimersByTimeAsync(6000);
      expect(mockStatus).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps no stale status from a request that finished after everyone left", async () => {
    let answer: (value: unknown) => void = () => {};
    mockStatus.mockReturnValueOnce(new Promise((resolve) => { answer = resolve; }) as never);
    const { unmount } = render(<Show name="panel" />);
    unmount();
    answer({ running: true });
    await Promise.resolve();
    mockStatus.mockResolvedValue({ running: false } as never);
    render(<Show name="pill" />);
    expect(screen.getByText("pill: …")).toBeInTheDocument(); // not the old run's "running"
    expect(await screen.findByText("pill: idle")).toBeInTheDocument();
  });
});
