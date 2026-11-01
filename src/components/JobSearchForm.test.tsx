import { describe, it, expect, vi } from "vitest";
import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import JobSearchForm from "./JobSearchForm";
import { DEFAULT_JOB_SEARCH } from "../lib/jobSearch";
import type { JobSearchPrefs } from "../lib/types";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (k: string, o?: Record<string, unknown>) => (o && "bar" in o ? `${k}:${o.bar}` : k),
    i18n: { changeLanguage: vi.fn() },
  }),
}));

function Harness({ onChange, initial = DEFAULT_JOB_SEARCH }: { onChange: (v: JobSearchPrefs) => void; initial?: JobSearchPrefs }) {
  const [value, setValue] = useState(initial);
  return (
    <JobSearchForm value={value} currency="USD"
      onChange={(v) => { setValue(v); onChange(v); }} />
  );
}

describe("JobSearchForm", () => {
  it("picks job types and work arrangements, and picks one off again", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<Harness onChange={onChange} />);
    await user.click(screen.getByRole("checkbox", { name: "Part-time" }));
    await user.click(screen.getByRole("checkbox", { name: "Full-time" }));
    await user.click(screen.getByRole("checkbox", { name: "Remote" }));
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ job_types: ["part_time", "full_time"], work_modes: ["remote"] }));
    await user.click(screen.getByRole("checkbox", { name: /Part-time/ }));
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ job_types: ["full_time"] }));
  });

  it("sets a minimum pay per hour and says how high the bar is for jobs without pay", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<Harness onChange={onChange} />);
    expect(screen.queryByText(/jobSearch\.noPayBar/)).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("jobSearch.minPay"), "20");
    await user.selectOptions(screen.getByLabelText("jobSearch.minPayPeriod"), "hour");
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ min_pay: { amount: 20, period: "hour" } }));
    expect(screen.getByText(/jobSearch\.noPayBar:7\.5/)).toBeInTheDocument();
  });

  it("adds things to leave out, once each", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<Harness onChange={onChange} />);
    const words = screen.getByPlaceholderText("jobSearch.exclude_title_wordsPlaceholder");
    await user.type(words, "Senior{Enter}");
    await user.type(words, "senior{Enter}");
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ exclude_title_words: ["Senior"] }));
    await user.click(screen.getByRole("checkbox", { name: "jobSearch.skipAgencies" }));
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ skip_staffing_agencies: true }));
  });

  it("chooses Quick or Detailed and writes notes for the AI", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<Harness onChange={onChange} />);
    expect(screen.getByRole("radio", { name: /jobSearch\.modeQuick/ })).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("radio", { name: /jobSearch\.modeDetailed/ }));
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ mode: "detailed" }));
    await user.type(screen.getByLabelText("jobSearch.notes"), "No nights");
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ notes: "No nights" }));
  });
});
