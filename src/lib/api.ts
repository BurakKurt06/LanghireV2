/**
 * API client for communicating with the Python backend sidecar.
 * The backend runs as a local FastAPI server on a dynamic port.
 */
import { invoke } from "@tauri-apps/api/core";
import type {
  InboxStatus,
  CandidateProfile,
  LLMSettings,
  AppSettings,
  Job,
  JobStats,
  MemoryStats,
  Memory,
  HealthResponse,
  RunMetric,
  CollectionStatus,
  ApplyStatus,
  DashboardResponse,
  DomainInfo,
  RunLog,
  RunWithLogs,
  QAEntry,
  QAStats,
  ChatGPTStatus,
  ChatGPTModel,
  FilteredJob,
  JobSearchPrefs,
  SavedSearch,
} from "./types";

const DEFAULT_PORT = 8743;

let backendPort = DEFAULT_PORT;
let tokenPromise: Promise<string> | null = null;

export function setBackendPort(port: number) {
  backendPort = port;
}

function getBaseUrl(): string {
  return `http://127.0.0.1:${backendPort}`;
}

async function getToken(): Promise<string> {
  if (!tokenPromise) {
    tokenPromise = invoke<string>("get_api_token").catch((e) => {
      tokenPromise = null;
      throw new Error(`Failed to read API token: ${e}`);
    });
  }
  return tokenPromise;
}

/** Request options, plus how long to wait before giving up (default DEFAULT_TIMEOUT_MS). */
type RequestOptions = RequestInit & { timeoutMs?: number };

const DEFAULT_TIMEOUT_MS = 60_000;
/** For calls that wait on the AI model (tailoring, cover letters, ...). */
const AI_TIMEOUT_MS = 10 * 60_000;

// GETs already on their way, by path: a poll that fires again before the last answer
// arrived shares it, so polls never pile up and an older answer can't land after a newer one.
const inFlight = new Map<string, Promise<unknown>>();

function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  if (method !== "GET" || options.body !== undefined) return send<T>(path, options);
  const pending = inFlight.get(path);
  if (pending) return pending as Promise<T>;
  const promise = send<T>(path, options).finally(() => inFlight.delete(path));
  inFlight.set(path, promise);
  return promise;
}

async function send<T>(
  path: string,
  options: RequestOptions = {},
  _retried = false
): Promise<T> {
  const url = `${getBaseUrl()}${path}`;
  const token = await getToken();
  const { timeoutMs = DEFAULT_TIMEOUT_MS, ...init } = options;
  // A backend that stopped answering used to leave the page waiting forever.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetch(url, {
      ...init,
      signal: init.signal ?? controller.signal,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
        ...init.headers,
      },
    });
  } catch (e) {
    if (controller.signal.aborted) {
      throw new Error(`The app's backend didn't answer within ${Math.round(timeoutMs / 1000)} seconds (${path}).`);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }

  if (res.status === 401 && !_retried) {
    tokenPromise = null;
    await new Promise(r => setTimeout(r, 1000));
    return send<T>(path, options, true);
  }

  if (!res.ok) {
    const text = await res.text().catch(() => "Unknown error");
    throw new Error(`API Error ${res.status}: ${text}`);
  }

  const text = await res.text();
  if (!text) return {} as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error(`Invalid JSON response from ${path}`);
  }
}

/** The backend's own explanation in a failed request's error ({"error": {"message"}}), or `fallback`. */
export function apiErrorMessage(e: unknown, fallback: string): string {
  const text = e instanceof Error ? e.message : "";
  const body = text.match(/^API Error \d+: ([\s\S]*)$/)?.[1];
  if (body) {
    try {
      const message = JSON.parse(body)?.error?.message;
      if (typeof message === "string" && message) return message;
    } catch { /* not JSON */ }
  }
  return fallback;
}

// ── Health ────────────────────────────────────────────────────────────────
export async function checkHealth() {
  return request<HealthResponse>("/health");
}

export async function getChromiumStatus() {
  const res = await fetch(`${getBaseUrl()}/chromium/status`);
  return res.json() as Promise<{ state: string; message: string }>;
}

// ── Profile ───────────────────────────────────────────────────────────────
export async function getProfile() {
  return request<CandidateProfile>("/profile");
}

export async function saveProfile(profile: CandidateProfile) {
  return request<{ success: boolean }>("/profile", {
    method: "PUT",
    body: JSON.stringify(profile),
  });
}

// ── LLM Settings ──────────────────────────────────────────────────────────
export async function getLLMSettings() {
  return request<LLMSettings>("/settings/llm");
}

export async function saveLLMSettings(settings: LLMSettings) {
  return request<{ success: boolean }>("/settings/llm", {
    method: "PUT",
    body: JSON.stringify(settings),
  });
}

export async function testLLMConnection(settings: LLMSettings) {
  return request<{ success: boolean; message: string }>("/llm/test", {
    method: "POST", timeoutMs: AI_TIMEOUT_MS,
    body: JSON.stringify(settings),
  });
}

export async function fetchOllamaModels(baseUrl: string) {
  return request<{ success: boolean; models: string[]; message?: string }>("/llm/ollama-models", {
    method: "POST",
    body: JSON.stringify({ base_url: baseUrl }),
  });
}

// ── ChatGPT plan sign-in ──────────────────────────────────────────────────
/** A provider's model list, fetched by the backend (the app's content policy blocks
 *  the page from AI providers). A masked key means the saved one. Returns the provider's JSON. */
export async function fetchProviderModels(provider: string, apiKey = "", baseUrl = "") {
  return request<{ success: boolean; data: unknown; message?: string }>("/llm/models", {
    method: "POST",
    body: JSON.stringify({ provider, api_key: apiKey, base_url: baseUrl }),
  });
}

/** Employer sites the app made an account password for. */
export async function getEmployerPasswordSites() {
  return request<{ sites: string[] }>("/credentials/sites");
}

export async function revealEmployerPassword(site: string) {
  return request<{ site: string; password: string }>("/credentials/reveal", {
    method: "POST",
    body: JSON.stringify({ site }),
  });
}

export async function startChatGPTLogin() {
  return request<{ url: string; error: string }>("/chatgpt/login", { method: "POST" });
}

export async function getChatGPTStatus() {
  return request<ChatGPTStatus>("/chatgpt/status");
}

export async function chatGPTLogout() {
  return request<ChatGPTStatus>("/chatgpt/logout", { method: "POST" });
}

export async function getChatGPTModels() {
  return request<{ success: boolean; models: ChatGPTModel[]; message?: string }>("/chatgpt/models");
}

// ── App Settings ──────────────────────────────────────────────────────────
export async function getSettings() {
  return request<AppSettings>("/settings");
}

export async function getBrowsers() {
  return request<{ browsers: import("./types").InstalledBrowser[] }>("/browsers");
}

export async function saveSettings(settings: Partial<AppSettings>) {
  return request<{ success: boolean }>("/settings", {
    method: "PUT",
    body: JSON.stringify(settings),
  });
}

// ── Jobs ──────────────────────────────────────────────────────────────────
export async function getJobs(params?: { status?: string; search?: string; limit?: number; fields?: string[] }) {
  const qs = new URLSearchParams();
  if (params?.status) qs.set("status", params.status);
  if (params?.search) qs.set("search", params.search);
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.fields?.length) qs.set("fields", params.fields.join(","));
  const query = qs.toString();
  return request<Job[]>(`/jobs${query ? `?${query}` : ""}`);
}

export async function getJobStats() {
  return request<JobStats>("/jobs/stats");
}

/** Fetch the jobs export as CSV text (authenticated). Caller triggers the download. */
export async function exportJobsCsv(): Promise<string> {
  const token = await getToken();
  const res = await fetch(`${getBaseUrl()}/jobs/export`, {
    headers: { Authorization: `Bearer ${token}` },
    signal: AbortSignal.timeout(DEFAULT_TIMEOUT_MS),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "Unknown error");
    throw new Error(`API Error ${res.status}: ${text}`);
  }
  return res.text();
}

/** Write the CSV export to a path the user picked in the Save dialog. */
export async function saveJobsCsv(path: string) {
  return request<{ success: boolean; path: string; rows: number }>("/jobs/export/save", {
    method: "POST",
    body: JSON.stringify({ path }),
  });
}

export async function startJobCollection(
  title?: string,
  maxJobs?: number,
  source?: string,
  filters?: Record<string, string>,
  sources?: string[],
  search?: JobSearchPrefs,
) {
  const body: Record<string, unknown> = {};
  if (title) body.title = title;
  if (maxJobs) body.max_jobs = maxJobs;
  if (source) body.source = source;
  if (sources && sources.length > 0) body.sources = sources;
  if (filters && Object.keys(filters).length > 0) body.filters = filters;
  if (search) body.search = search;
  return request<{ success: boolean; message: string }>("/jobs/collect", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Listings the collector left out (they didn't fit the search), newest first. */
export async function getFilteredJobs(limit = 200) {
  return request<{ success: boolean; total: number; jobs: FilteredJob[] }>(`/jobs/filtered?limit=${limit}`);
}

export async function keepFilteredJob(url: string) {
  return request<{ success: boolean; added: number }>("/jobs/filtered/keep", {
    method: "POST",
    body: JSON.stringify({ url }),
  });
}

export async function clearFilteredJobs() {
  return request<{ success: boolean; cleared: number }>("/jobs/filtered", { method: "DELETE" });
}

export async function getSavedSearches() {
  return request<{ success: boolean; searches: SavedSearch[] }>("/jobs/searches");
}

export async function saveSavedSearches(searches: Omit<SavedSearch, "id">[] | SavedSearch[]) {
  return request<{ success: boolean; searches: SavedSearch[] }>("/jobs/searches", {
    method: "PUT",
    body: JSON.stringify({ searches }),
  });
}

export async function updateJobStatus(url: string, status: string) {
  return request<{ success: boolean }>("/jobs/status", {
    method: "PUT",
    body: JSON.stringify({ url, status }),
  });
}

export async function addJob(url: string, title?: string, company?: string, source?: string) {
  return request<{ success: boolean }>("/jobs/add", {
    method: "POST",
    body: JSON.stringify({ url, title, company, source }),
  });
}

export interface BulkAddResult {
  success: boolean;
  added: number;
  skipped_duplicate: number;
  skipped_invalid: number;
  invalid_samples: string[];
  total_submitted: number;
}

/** Add many job URLs at once, from a pasted block or the contents of a file. */
export async function addJobsBulk(text: string) {
  return request<BulkAddResult>("/jobs/add-bulk", {
    method: "POST",
    body: JSON.stringify({ text }),
  });
}

export async function deleteJobs(urls: string[]) {
  return request<{ success: boolean; deleted: number }>("/jobs", {
    method: "DELETE",
    body: JSON.stringify({ urls }),
  });
}

export async function stopJobCollection() {
  return request<{ success: boolean }>("/jobs/collect/stop", { method: "POST" });
}

export async function getCollectionStatus() {
  return request<CollectionStatus>("/jobs/collect/status");
}

// ── Memory ────────────────────────────────────────────────────────────────
export async function getMemoryStats() {
  return request<MemoryStats>("/memory/stats");
}

export async function getMemoryDomains() {
  return request<DomainInfo[]>("/memory/domains");
}

export async function getMemoriesForDomain(domain: string) {
  return request<Memory[]>(
    `/memory/domain/${encodeURIComponent(domain)}`
  );
}

export async function searchMemories(query: string) {
  return request<Memory[]>(`/memory/search?q=${encodeURIComponent(query)}`);
}

export async function decayMemories(days = 30) {
  return request<{ success: boolean; affected: number }>("/memory/decay", {
    method: "POST",
    body: JSON.stringify({ days }),
  });
}

export async function cleanupMemories() {
  return request<{ success: boolean; deleted: number }>("/memory/cleanup", {
    method: "POST",
  });
}

export async function exportMemories() {
  return request<Memory[]>("/memory/export");
}

// ── Apply ─────────────────────────────────────────────────────────────────
export async function startApplying(params: { workers?: number; mode?: string; limit?: number; job_url?: string; job_urls?: string[]; review?: boolean; acknowledge_unknown?: boolean; run_mode?: "auto" | "trial" }) {
  return request<{ success: boolean; message: string }>("/apply/start", {
    method: "POST",
    body: JSON.stringify(params),
  });
}

export async function stopApplying() {
  return request<{ success: boolean }>("/apply/stop", { method: "POST" });
}

export async function pauseApplying() {
  return request<{ success: boolean; paused: boolean }>("/apply/pause", { method: "POST" });
}

export async function resumeApplying() {
  return request<{ success: boolean; paused: boolean }>("/apply/resume", { method: "POST" });
}

export async function answerHelp(url: string, message: string, giveUp: boolean) {
  return request<{ success: boolean }>("/apply/help", {
    method: "POST",
    body: JSON.stringify({ url, message, give_up: giveUp }),
  });
}

export async function saveJobNote(url: string, note: string) {
  return request<{ success: boolean }>("/jobs/note", { method: "PUT", body: JSON.stringify({ url, note }) });
}

export async function reviewApplication(url: string, approve: boolean) {
  return request<{ success: boolean }>("/apply/review", { method: "POST", body: JSON.stringify({ url, approve }) });
}

export async function decideBrowserRequests(ids: string[], approve: boolean) {
  return request<{ success: boolean }>("/apply/requests/decide", {
    method: "POST", body: JSON.stringify({ ids, approve }),
  });
}

// ── Inbox (Gmail check) ───────────────────────────────────────────────────
export async function getInbox() {
  return request<InboxStatus>("/inbox");
}

export async function checkInbox() {
  return request<{ success: boolean }>("/inbox/check", { method: "POST" });
}

export async function setInboxSchedule(schedule: InboxStatus["schedule"]) {
  return request<{ success: boolean }>("/inbox/schedule", { method: "PUT", body: JSON.stringify({ schedule }) });
}

export async function makePrepSheet(id: string) {
  return request<{ success: boolean; prep: string }>("/inbox/prep", { method: "POST", timeoutMs: AI_TIMEOUT_MS, body: JSON.stringify({ id }) });
}

export async function skipJob(url: string) {
  return request<{ success: boolean }>("/apply/skip", { method: "POST", body: JSON.stringify({ url }) });
}

export async function getApplyStatus() {
  return request<ApplyStatus>("/apply/status");
}

// ── Dashboard / Metrics ───────────────────────────────────────────────────
export async function getDashboardData() {
  return request<DashboardResponse>("/dashboard");
}

export async function getMetricRuns(limit = 50) {
  return request<RunMetric[]>(`/metrics/runs?limit=${limit}`);
}

// ── Logs ─────────────────────────────────────────────────────────────────
export async function getRunLogs(runId: string, limit = 500) {
  return request<RunLog[]>(`/logs/runs/${encodeURIComponent(runId)}?limit=${limit}`);
}

export async function getRecentLogs(limit = 100) {
  return request<RunLog[]>(`/logs/recent?limit=${limit}`);
}

export async function getRunsWithLogs(limit = 50) {
  return request<RunWithLogs[]>(`/logs/runs?limit=${limit}`);
}

// ── Auth / Login Sessions ─────────────────────────────────────────────────
export async function getAuthStatus() {
  return request<{
    linkedin: { logged_in: boolean };
    gmail: { logged_in: boolean };
    login_window_open?: boolean;
  }>("/auth/status");
}

/** A bundled board's identifier or Gmail; the backend chooses the sign-in URL. */
export async function launchLogin(service: string) {
  return request<{ success: boolean; message: string }>(`/auth/login/${encodeURIComponent(service)}`, {
    method: "POST",
  });
}

// ── Setup / Onboarding ────────────────────────────────────────────────────
export interface SetupStatus {
  profile: boolean;
  llm: boolean;
  resume: boolean;
  chromium: boolean;
  linkedin: boolean;
  gmail: boolean;
  onboarding_completed: boolean;
  all_required_done: boolean;
}

export async function getSetupStatus() {
  return request<SetupStatus>("/setup/status");
}

export async function completeOnboarding() {
  return request<{ success: boolean }>("/setup/complete-onboarding", {
    method: "POST",
  });
}

export async function parseResumeToProfile() {
  return request<{
    success: boolean;
    message: string;
    profile?: Record<string, unknown>;
    fields_filled?: number;
  }>("/profile/parse-resume", { method: "POST", timeoutMs: AI_TIMEOUT_MS });
}

// ── Q&A Repository ────────────────────────────────────────────────────────
export async function getQAList(params?: { search?: string; unanswered?: boolean }) {
  const qs = new URLSearchParams();
  if (params?.search) qs.set("search", params.search);
  if (params?.unanswered) qs.set("unanswered", "true");
  const query = qs.toString();
  return request<QAEntry[]>(`/qa${query ? `?${query}` : ""}`);
}

export async function getQAStats() {
  return request<QAStats>("/qa/stats");
}

export async function updateQA(id: number, answer: string) {
  return request<{ success: boolean }>(`/qa/${id}`, {
    method: "PUT",
    body: JSON.stringify({ answer }),
  });
}

export async function deleteQA(id: number) {
  return request<{ success: boolean }>(`/qa/${id}`, { method: "DELETE" });
}

export async function mergeQA(sourceId: number, targetId: number) {
  return request<{ success: boolean }>(`/qa/${sourceId}/merge/${targetId}`, { method: "POST" });
}

export async function autoSquashQA() {
  return request<{ success: boolean; merged: number }>("/qa/auto-squash", { method: "POST" });
}

export async function smartSquashQA() {
  return request<{ success: boolean; merged: number }>("/qa/smart-squash", { method: "POST", timeoutMs: AI_TIMEOUT_MS });
}

// ── Resume Tailoring ─────────────────────────────────────────────────────
export async function tailorResumes(jobUrls: string[], options: import("./types").TailorOptions) {
  return request<{ success: boolean; results: import("./types").TailorResult[] }>("/resume/tailor", {
    method: "POST", timeoutMs: AI_TIMEOUT_MS,
    body: JSON.stringify({ job_urls: jobUrls, options }),
  });
}

export async function refineTailoredResume(jobUrl: string, instruction: string) {
  return request<{ success: boolean; content: string; path: string }>("/resume/tailor/refine", {
    method: "POST", timeoutMs: AI_TIMEOUT_MS,
    body: JSON.stringify({ job_url: jobUrl, instruction }),
  });
}

export async function getTailoredResumeContent(jobUrl: string) {
  return request<{ success: boolean; content: string; path: string | null }>("/resume/tailor/get-by-url", {
    method: "POST",
    body: JSON.stringify({ job_url: jobUrl }),
  });
}

// ── Cover Letter ─────────────────────────────────────────────────────────
/** Keep a cover letter on one job (used for that application only). */
export async function saveJobCoverLetter(url: string, coverLetter: string) {
  return request<{ success: boolean }>("/jobs/cover-letter", {
    method: "PUT",
    body: JSON.stringify({ url, cover_letter: coverLetter }),
  });
}

export async function generateCoverLetter(jobDescription: string, jobTitle: string, company: string) {
  return request<{ success: boolean; cover_letter: string }>("/cover-letter/generate", {
    method: "POST", timeoutMs: AI_TIMEOUT_MS,
    body: JSON.stringify({ job_description: jobDescription, job_title: jobTitle, company: company }),
  });
}

// ── Countries ────────────────────────────────────────────────────────────
export async function getCountries() {
  return request<{ success: boolean; countries: Record<string, import("./types").CountryConfig>; notice_period_options: string[] }>("/countries");
}

export async function getCountryConfig(code: string) {
  return request<{ success: boolean; config: import("./types").CountryConfig }>(`/countries/${code}`);
}

// ── Plugins ──────────────────────────────────────────────────────────────
export async function getPlugins(country?: string) {
  const params = country ? `?country=${country}` : "";
  return request<{ success: boolean; plugins: import("./types").PluginConfig[] }>(`/plugins${params}`);
}

export async function importPlugin(filePath: string) {
  return request<{ success: boolean; plugin: { name: string; display_name: string } }>("/plugins/import", {
    method: "POST",
    body: JSON.stringify({ file_path: filePath }),
  });
}

export async function togglePlugin(name: string, enabled: boolean) {
  return request<{ success: boolean }>(`/plugins/${name}/toggle`, {
    method: "PUT",
    body: JSON.stringify({ enabled }),
  });
}

export async function removePlugin(name: string) {
  return request<{ success: boolean }>(`/plugins/${name}`, { method: "DELETE" });
}

export async function reloadPlugins() {
  return request<{ success: boolean; count: number }>("/plugins/reload", { method: "POST" });
}

