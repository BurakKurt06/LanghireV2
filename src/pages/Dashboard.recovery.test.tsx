import { it, expect, vi } from 'vitest';
import { render, act, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Dashboard from './Dashboard';
import * as api from '../lib/api';

vi.mock('react-i18next', () => ({useTranslation: () => ({t: (key: string) => key})}));
vi.mock('../lib/api', () => ({
  checkHealth: vi.fn(), getDashboardData: vi.fn(),
  getSetupStatus: vi.fn(async () => ({})), getQAStats: vi.fn(async () => ({})),
  getChromiumStatus: vi.fn(async () => ({state: 'ready', message: ''})),
  // The Analytics page became <Insights/> on the Dashboard (decision 3); it loads the jobs.
  getJobs: vi.fn(async () => []),
}));
vi.mock('../lib/analytics', () => ({trackEvent: vi.fn()}));
vi.mock('../lib/perf', () => ({markStart: vi.fn(), measureAndTrack: vi.fn()}));

it('Dashboard recovers (review R13) when the backend starts after its initial health retries', async () => {
  vi.useFakeTimers();
  try {
    vi.mocked(api.checkHealth).mockRejectedValue(new Error('synthetic not ready'));
    render(<MemoryRouter><Dashboard /></MemoryRouter>);
    await act(() => vi.advanceTimersByTimeAsync(20_000));
    expect(api.checkHealth).toHaveBeenCalledTimes(10);
    // Decision 3: the not-connected message is plain words now.
    expect(screen.getByText(/The app's engine isn't answering yet/)).toBeInTheDocument();
    vi.mocked(api.checkHealth).mockResolvedValue({} as never);
    vi.mocked(api.getDashboardData).mockResolvedValue({jobs: {}, memory: {}} as never);
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(api.checkHealth).toHaveBeenCalledTimes(11);
    expect(api.getDashboardData).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/The app's engine isn't answering yet/)).not.toBeInTheDocument();
  } finally {
    vi.useRealTimers();
  }
});
