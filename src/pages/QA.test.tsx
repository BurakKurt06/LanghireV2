import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import QA from "./QA";
import {
  getQAList,
  getQAStats,
  updateQA,
  deleteQA,
  mergeQA,
  autoSquashQA,
  smartSquashQA,
  getDefaultAnswers,
  setDefaultAnswer,
} from "../lib/api";
import { trackEvent } from "../lib/analytics";
import type { QAEntry } from "../lib/types";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (k: string, o?: Record<string, unknown>) =>
      o ? `${k}:${JSON.stringify(o)}` : k,
    i18n: { changeLanguage: vi.fn() },
  }),
}));

vi.mock("../lib/analytics", () => ({ trackEvent: vi.fn() }));

vi.mock("../lib/api", async (importOriginal) => ({
  apiErrorMessage: (await importOriginal<typeof import("../lib/api")>()).apiErrorMessage,
  getQAList: vi.fn(),
  getQAStats: vi.fn(),
  updateQA: vi.fn(),
  deleteQA: vi.fn(),
  mergeQA: vi.fn(),
  autoSquashQA: vi.fn(),
  smartSquashQA: vi.fn(),
  getDefaultAnswers: vi.fn(),
  setDefaultAnswer: vi.fn(),
}));

const mockGetQAList = vi.mocked(getQAList);
const mockGetQAStats = vi.mocked(getQAStats);
const mockUpdateQA = vi.mocked(updateQA);
const mockDeleteQA = vi.mocked(deleteQA);
const mockMergeQA = vi.mocked(mergeQA);
const mockAutoSquashQA = vi.mocked(autoSquashQA);
const mockSmartSquashQA = vi.mocked(smartSquashQA);
const mockTrackEvent = vi.mocked(trackEvent);

const DEFAULTS = [
  { id: "worked_here", question: "Have you worked for this company before?", answer: "No", default: "No", changed: false },
  { id: "heard_about", question: "How did you hear about this job?", answer: "A friend",
    default: "The company's own careers website", changed: true },
];

function makeEntry(over: Partial<QAEntry> = {}): QAEntry {
  return {
    id: 1,
    question: "Why do you want this job?",
    normalized: "why do you want this job",
    answer: "Because I love it.",
    question_type: "freeform",
    source_domain: "greenhouse.io",
    times_seen: 2,
    merged_into_id: null,
    created_at: "2026-01-01",
    updated_at: "2026-01-02",
    ...over,
  };
}

const entries: QAEntry[] = [
  makeEntry({ id: 1, question: "Why do you want this job?" }),
  makeEntry({ id: 2, question: "What is your expected salary?", answer: "", times_seen: 1 }),
];

describe("QA page", () => {
  beforeEach(() => {
    mockGetQAList.mockResolvedValue(entries as never);
    mockGetQAStats.mockResolvedValue({ total: 2, answered: 1, unanswered: 1 } as never);
    mockUpdateQA.mockResolvedValue({ success: true } as never);
    mockDeleteQA.mockResolvedValue({ success: true } as never);
    mockMergeQA.mockResolvedValue({ success: true } as never);
    mockAutoSquashQA.mockResolvedValue({ success: true, merged: 3 } as never);
    mockSmartSquashQA.mockResolvedValue({ success: true, merged: 1 } as never);
    vi.mocked(getDefaultAnswers).mockResolvedValue({ answers: DEFAULTS } as never);
  });

  it("lists the default answers in one place, folded until opened", async () => {
    const user = userEvent.setup();
    render(<QA />);
    const section = await screen.findByRole("region", { name: "Default answers" });
    expect(within(section).getByText(/2 answers, 1 changed by you/)).toBeInTheDocument();
    expect(within(section).queryByLabelText("Have you worked for this company before?")).not.toBeInTheDocument();
    await user.click(within(section).getByRole("button", { name: /Default answers/ }));
    expect(within(section).getByLabelText("Have you worked for this company before?")).toHaveValue("No");
    expect(within(section).getByLabelText("How did you hear about this job?")).toHaveValue("A friend");
    expect(within(section).getAllByText("Built-in answer")).toHaveLength(1);
    expect(within(section).getAllByText("Changed by you")).toHaveLength(1);
  });

  it("changes a default answer and can put the built-in one back", async () => {
    vi.mocked(setDefaultAnswer)
      .mockResolvedValueOnce({ success: true, answers: [{ ...DEFAULTS[0], answer: "Yes, as an intern", changed: true }, DEFAULTS[1]] } as never)
      .mockResolvedValueOnce({ success: true, answers: [{ ...DEFAULTS[0], answer: "Yes, as an intern", changed: true },
        { ...DEFAULTS[1], answer: DEFAULTS[1].default, changed: false }] } as never);
    const user = userEvent.setup();
    render(<QA />);
    const section = await screen.findByRole("region", { name: "Default answers" });
    await user.click(within(section).getByRole("button", { name: /Default answers/ }));

    const worked = within(section).getByLabelText("Have you worked for this company before?");
    await user.clear(worked);
    await user.type(worked, "Yes, as an intern");
    await user.click(within(section).getByRole("button", { name: "Save" }));
    expect(vi.mocked(setDefaultAnswer)).toHaveBeenCalledWith("worked_here", "Yes, as an intern");
    await waitFor(() => expect(within(section).getAllByText("Changed by you")).toHaveLength(2));   // both are now the user's

    await user.click(within(section).getByRole("button", { name: 'Reset "How did you hear about this job?" to the built-in answer' }));
    expect(vi.mocked(setDefaultAnswer)).toHaveBeenLastCalledWith("heard_about", "");
    await waitFor(() =>
      expect(within(section).getByLabelText("How did you hear about this job?")).toHaveValue("The company's own careers website"));
    expect(within(section).getAllByText("Changed by you")).toHaveLength(1);
    expect(within(section).getByLabelText("Have you worked for this company before?")).toHaveValue("Yes, as an intern");
  });

  it("an answer filled in from the default answers is labelled as one", async () => {
    mockGetQAList.mockResolvedValue([makeEntry({ id: 9, question: "Are you related to anyone here?", answer: "No", source: "default" })] as never);
    render(<QA />);
    expect(await screen.findByText("card.origin.default")).toBeInTheDocument();
  });

  it("marks who answered, and says how many questions need an answer", async () => {
    mockGetQAList.mockResolvedValue([
      makeEntry({ id: 1, question: "Q user?", source: "user" }),
      makeEntry({ id: 2, question: "Q ai?", source: "agent" }),
      makeEntry({ id: 3, question: "Q open?", answer: "" }),
      makeEntry({ id: 4, question: "Q guess?", source: "guess" }),
      makeEntry({ id: 5, question: "Q paused?", source: "observed" }),
    ] as never);
    mockGetQAStats.mockResolvedValue({ total: 4, answered: 3, unanswered: 1, guessed: 1 } as never);
    render(<QA />);
    expect(await screen.findByText("card.origin.yours")).toBeInTheDocument();
    expect(screen.getByText("card.origin.fromAI")).toBeInTheDocument();
    expect(screen.getByText("card.origin.needsAnswer")).toBeInTheDocument();
    expect(screen.getByText("card.origin.guess")).toBeInTheDocument();
    expect(screen.getByText("card.origin.observed")).toBeInTheDocument();
    expect(await screen.findByRole("status")).toHaveTextContent('toCheckBanner:{"count":2}');
  });

  it("a guess says what it was worked out from; the user's own answer and other answers don't", async () => {
    mockGetQAList.mockResolvedValue([
      makeEntry({ id: 4, question: "Graduation date", answer: "05/15/2028", source: "guess", basis: "profile: May 2028" }),
      makeEntry({ id: 5, question: "Plain guess?", answer: "No", source: "guess" }),
      makeEntry({ id: 6, question: "Mine?", answer: "Yes", source: "user", basis: "left over" }),
    ] as never);
    render(<QA />);
    expect(await screen.findByText("guessed from profile: May 2028")).toBeInTheDocument();
    expect(screen.getAllByText(/^guessed from /)).toHaveLength(1);
  });

  it("an option chip fills in the answer", async () => {
    mockGetQAList.mockResolvedValue([
      makeEntry({ id: 7, question: "Clearance?", answer: "", options: "Yes\nNo" }),
    ] as never);
    const user = userEvent.setup();
    render(<QA />);
    await user.click(await screen.findByRole("button", { name: "No" }));
    expect(screen.getByDisplayValue("No")).toBeInTheDocument();
    await waitFor(() => expect(mockUpdateQA).toHaveBeenCalledWith(7, "No"), { timeout: 2000 });
  });

  it("shows the loading spinner before the list resolves", () => {
    mockGetQAList.mockReturnValue(new Promise(() => {}) as never);
    render(<QA />);
    expect(screen.queryByText("title")).not.toBeInTheDocument();
  });

  it("renders the question list after load", async () => {
    render(<QA />);
    expect(await screen.findByText("Why do you want this job?")).toBeInTheDocument();
    expect(screen.getByText("What is your expected salary?")).toBeInTheDocument();
    // times_seen badge for the first entry.
    expect(screen.getByText("2x")).toBeInTheDocument();
  });

  it("filters to unanswered, re-fetching with the unanswered flag", async () => {
    const user = userEvent.setup();
    render(<QA />);
    await screen.findByText("Why do you want this job?");

    await user.click(screen.getByRole("button", { name: /filter\.toCheck/ }));

    await waitFor(() =>
      expect(mockGetQAList).toHaveBeenCalledWith(
        expect.objectContaining({ unanswered: true }),
      ),
    );
  });

  it("searching re-fetches the list with the search term", async () => {
    const user = userEvent.setup();
    render(<QA />);
    await screen.findByText("Why do you want this job?");

    await user.type(screen.getByPlaceholderText("searchPlaceholder"), "salary");

    await waitFor(() =>
      expect(mockGetQAList).toHaveBeenCalledWith(
        expect.objectContaining({ search: "salary" }),
      ),
    );
  });

  it("editing an answer debounce-saves via updateQA", async () => {
    const user = userEvent.setup();
    render(<QA />);
    await screen.findByText("Why do you want this job?");

    const textareas = screen.getAllByPlaceholderText("card.answerPlaceholder");
    await user.type(textareas[0], "!");

    await waitFor(
      () => expect(mockUpdateQA).toHaveBeenCalledWith(1, expect.stringContaining("Because I love it.!")),
      { timeout: 2000 },
    );
    expect(mockTrackEvent).toHaveBeenCalledWith("qa_answer_updated");
  });

  it("correcting a guess makes it your answer and refreshes the counts", async () => {
    mockGetQAList.mockResolvedValue([makeEntry({ id: 4, question: "Q guess?", source: "guess", answer: "Yes" })] as never);
    const user = userEvent.setup();
    render(<QA />);
    expect(await screen.findByText("card.origin.guess")).toBeInTheDocument();
    const statsCalls = mockGetQAStats.mock.calls.length;
    await user.type(screen.getByPlaceholderText("card.answerPlaceholder"), "!");
    expect(await screen.findByText("card.origin.yours", {}, { timeout: 2000 })).toBeInTheDocument();
    expect(mockGetQAStats.mock.calls.length).toBeGreaterThan(statsCalls);
  });

  it("a failed save is shown instead of silently dropped", async () => {
    mockUpdateQA.mockRejectedValueOnce(new Error("API Error 500: down"));
    const user = userEvent.setup();
    render(<QA />);
    await screen.findByText("Why do you want this job?");
    await user.type(screen.getAllByPlaceholderText("card.answerPlaceholder")[0], "!");
    expect(await screen.findByText("toast.saveFailed", {}, { timeout: 2000 })).toBeInTheDocument();
  });

  it("deleting a question calls deleteQA and refetches", async () => {
    const user = userEvent.setup();
    render(<QA />);
    await screen.findByText("Why do you want this job?");

    const firstCard = screen.getByText("Why do you want this job?").closest(".card") as HTMLElement;
    await user.click(within(firstCard).getByTitle("card.deleteTitle"));

    await waitFor(() => expect(mockDeleteQA).toHaveBeenCalledWith(1));
    // fetchData runs again after delete (≥ the initial load).
    expect(mockGetQAList.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("auto-squash calls the API and shows a result toast", async () => {
    const user = userEvent.setup();
    render(<QA />);
    await screen.findByText("Why do you want this job?");

    await user.click(screen.getByRole("button", { name: /actions\.autoSquash/ }));

    await waitFor(() => expect(mockAutoSquashQA).toHaveBeenCalled());
    expect(await screen.findByText(/toast\.squashResult/)).toBeInTheDocument();
  });

  it("smart-squash calls the API and tracks the event", async () => {
    const user = userEvent.setup();
    render(<QA />);
    await screen.findByText("Why do you want this job?");

    await user.click(screen.getByRole("button", { name: /actions\.smartSquash/ }));

    await waitFor(() => expect(mockSmartSquashQA).toHaveBeenCalled());
    expect(mockTrackEvent).toHaveBeenCalledWith("qa_smart_squash", { merged: 1 });
  });

  it("merge flow: starting merge shows a banner; picking a target calls mergeQA", async () => {
    const user = userEvent.setup();
    render(<QA />);
    await screen.findByText("Why do you want this job?");

    const firstCard = screen.getByText("Why do you want this job?").closest(".card") as HTMLElement;
    await user.click(within(firstCard).getByTitle("card.mergeTitle"));

    // Merge banner appears.
    expect(await screen.findByText("mergeMode.label")).toBeInTheDocument();

    // Clicking the OTHER card (a merge target) triggers mergeQA(source=1, target=2).
    const secondCard = screen.getByText("What is your expected salary?").closest(".card") as HTMLElement;
    await user.click(secondCard);

    await waitFor(() => expect(mockMergeQA).toHaveBeenCalledWith(1, 2));
  });

  it("a refused merge shows the backend's reason, not a success", async () => {
    mockMergeQA.mockRejectedValueOnce(new Error(
      'API Error 409: {"error":{"code":"invalid_merge","message":"The two questions have different answers; make them the same first."}}'));
    const user = userEvent.setup();
    render(<QA />);
    await screen.findByText("Why do you want this job?");
    const firstCard = screen.getByText("Why do you want this job?").closest(".card") as HTMLElement;
    await user.click(within(firstCard).getByTitle("card.mergeTitle"));
    await user.click(screen.getByText("What is your expected salary?").closest(".card") as HTMLElement);
    expect(await screen.findByText(/different answers; make them the same first/)).toBeInTheDocument();
    expect(screen.queryByText("toast.merged")).not.toBeInTheDocument();
  });

  it("renders an empty state when there are no questions", async () => {
    mockGetQAList.mockResolvedValue([] as never);
    mockGetQAStats.mockResolvedValue({ total: 0, answered: 0, unanswered: 0 } as never);
    render(<QA />);
    expect(await screen.findByText("emptyState.noQuestions")).toBeInTheDocument();
  });

  it("shows an error banner when the load fails", async () => {
    mockGetQAList.mockRejectedValue(new Error("API Error 500: boom"));
    render(<QA />);
    expect(await screen.findByText("API Error 500: boom")).toBeInTheDocument();
  });
});
