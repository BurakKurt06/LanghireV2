// ── Candidate Profile ──────────────────────────────────────────────────────
export interface Address {
  street: string;
  city: string;
  state: string;
  zip: string;
  country: string;
}

export interface Education {
  degree: string;
  major?: string; // absent in profiles saved before the field existed
  major_alternative?: string; // picked when a form's list doesn't offer the major
  school: string;
  start_date?: string; // absent in profiles saved before the field existed
  graduation: string;
}

/** Answers to the screening questions that sink applications when guessed. */
export interface ScreeningAnswers {
  start_date: string;
  gpa: string;
  linkedin_url: string;
  portfolio_url: string;
  gender: string;
  race_ethnicity: string;
  veteran_status: string;
  disability_status: string;
}

export interface SalaryExpectation {
  min: number;
  max: number;
  currency: string;
  period: "annual" | "monthly" | "hourly";
}

export interface CandidateProfile {
  name: string;
  email: string;
  phone: string;
  phone_country_code: string;
  country: string;
  address: Address;
  work_authorization: string;
  visa_sponsorship_needed: boolean;
  willing_to_relocate: boolean;
  preferred_work_mode: string;
  years_of_experience: number;
  education: Education;
  current_role: string;
  target_job_titles: string[];
  target_locations: string[];
  languages: string[];
  skills: string[];
  salary_expectation: SalaryExpectation;
  notice_period: string;
  nationality: string;
  date_of_birth: string;
  cover_letter: string;
  date_format: string;
  notes: string;
  screening?: ScreeningAnswers;
  /** Where to apply: jobs outside the radius around the ZIP are skipped (remote always counts). */
  search_area?: SearchArea;
}

export interface SearchArea {
  /** Empty: the address ZIP. */
  center_zip: string;
  radius: "25" | "50" | "100" | "any";
}

// ── LLM Settings ──────────────────────────────────────────────────────────
export type LLMProvider = "chatgpt" | "openai" | "anthropic" | "gemini" | "bedrock" | "ollama" | "openrouter" | "openai_compatible";

export interface LLMSettings {
  provider: LLMProvider;
  /** A ChatGPT Plus/Pro plan via "Sign in with ChatGPT" (the sign-in itself is stored by the backend). */
  chatgpt?: {
    model: string;
    /** "" = the model's default. */
    reasoning_effort?: string;
  };
  openai?: {
    api_key: string;
    model: string;
  };
  anthropic?: {
    api_key: string;
    model: string;
  };
  gemini?: {
    api_key: string;
    model: string;
  };
  bedrock?: {
    auth_mode: string;
    profile_name: string;
    access_key: string;
    secret_key: string;
    region: string;
    model: string;
  };
  ollama?: {
    base_url: string;
    model: string;
  };
  openrouter?: {
    api_key: string;
    model: string;
  };
  openai_compatible?: {
    base_url: string;
    api_key: string;
    model: string;
  };
}

export interface ChatGPTStatus {
  signed_in: boolean;
  email: string;
  plan: string;
  /** A sign-in is waiting for the browser. */
  pending: boolean;
  error: string;
  /** The sign-in page, while pending. */
  url: string;
}

export interface ChatGPTModel {
  id: string;
  name: string;
  efforts: string[];
  default_effort: string;
}

// ── App Settings ──────────────────────────────────────────────────────────
export interface AppSettings {
  resume_path: string;
  blocked_domains: string[];
  sensitive_data: {
    email: string;
    password: string;
  };
  max_failures: number;
  data_dir: string;
  telemetry_enabled: boolean;
  theme?: "light" | "dark" | "system";
  /** Job board collection (LinkedIn, Indeed, ...). Default true. */
  job_collection_enabled?: boolean;
  /** Anti-detection patches on the automated browser. Default false. */
  stealth_enabled?: boolean;
  /** Randomised pause between applications. Default false. */
  human_delay_enabled?: boolean;
  human_delay_min?: number;
  human_delay_max?: number;
  /** 2Captcha solving. Default false; the key never reaches the LLM. */
  captcha_solver_enabled?: boolean;
  captcha_api_key?: string;
  /** Browser the agent drives: "" = automatic, a detected id, or "custom". */
  browser?: string;
  /** Executable used when browser is "custom". */
  browser_path?: string;
  /** Extra files the agent may upload (transcript, ...). */
  other_documents?: OtherDocument[];
  /** Turns off optional AI work (fit check, agent job naming, per-step learnings, prep sheets). */
  performance_mode?: boolean;
  /** AI fit check before applying. Default true. */
  fit_check_enabled?: boolean;
  /** Write cover letters into cover-letter boxes. Default false. */
  cover_letter_boxes?: boolean;
}

export interface OtherDocument {
  label: string;
  path: string;
}

/** A Chromium-based browser found on this machine (GET /browsers). */
export interface InstalledBrowser {
  id: string;
  name: string;
  path: string;
  is_default: boolean;
}

// ── Job ───────────────────────────────────────────────────────────────────
export interface Job {
  url: string;
  title: string;
  company: string;
  location: string;
  easy_apply: boolean | null;
  status: JobStatus;
  source?: string;
  search_title?: string;
  collected_at?: string;
  applied_at?: string;
  error?: string;
  description?: string;
  tailored_resume_path?: string;
  /** The user's note for the agent, used on the next attempt. */
  user_note?: string;
  /** When a training run last filled this job in (without submitting). */
  practised_at?: string;
  /** From the Inbox page's Gmail check: what the employer's latest email said. */
  inbox_status?: string;
  inbox_subject?: string;
}

// ── Resume Tailoring ─────────────────────────────────────────────────────
export interface TailorOptions {
  skills: boolean;
  title: boolean;
  overview: boolean;
  experience: boolean;
}

export interface TailorResult {
  url: string;
  status: "done" | "error";
  path?: string;
  content?: string;
  message?: string;
}

// ── Plugins ──────────────────────────────────────────────────────────────
export interface PluginFilter {
  key: string;
  label: string;
  type: "select" | "text";
  options: { value: string; label: string }[];
  url_param: string;
  default: string;
}

export interface PluginConfig {
  name: string;
  display_name: string;
  version: string;
  author: string;
  description: string;
  countries: string[];
  website: string;
  requires_login: boolean;
  login_url: string;
  is_builtin: boolean;
  enabled: boolean;
  filters: PluginFilter[];
}

// ── Country Config ───────────────────────────────────────────────────────
export interface CountryConfig {
  name: string;
  flag: string;
  date_format: string;
  currency: string;
  salary_period: "annual" | "monthly";
  address_labels: { state: string; zip: string };
  work_auth_options: string[];
  show_notice_period: boolean;
  show_nationality: boolean;
  show_cover_letter: boolean;
  show_photo: boolean;
  show_date_of_birth: boolean;
  phone_prefix: string;
  default_sources: string[];
}

export type CountryCode = string;

// ── Memory ────────────────────────────────────────────────────────────────
export interface Memory {
  id: number;
  website_domain: string;
  ats_platform: string | null;
  category: string;
  content: string;
  success: boolean;
  confidence: number;
  job_url: string;
  created_at: string;
  updated_at: string;
  access_count: number;
}

export interface MemoryStats {
  total_memories: number;
  unique_domains: number;
  by_category: Record<string, number>;
}

// ── Metrics / Dashboard ───────────────────────────────────────────────────
export interface OverallStats {
  total_runs: number;
  successes: number;
  failures: number;
  success_rate: number;
  avg_duration: number;
  avg_steps: number;
  total_cost: number;
  first_run: string;
  last_run: string;
}

export interface RunMetric {
  id: number;
  job_url: string;
  job_title: string;
  company: string;
  website_domain: string;
  ats_platform: string | null;
  success: boolean;
  error_message: string | null;
  started_at: string;
  finished_at: string;
  duration_seconds: number;
  step_count: number;
  memories_injected: number;
  memories_extracted: number;
  cost_usd: number | null;
  created_at: string;
}

// ── API Response ──────────────────────────────────────────────────────────
export interface ApiResponse<T> {
  success: boolean;
  data?: T;
  error?: string;
}

export interface HealthChecks {
  database: boolean;
  chromium: boolean;
  chromium_installing: boolean;
  llm_configured: boolean;
  worker_running: boolean;
}

export interface HealthResponse {
  status: "ok" | "degraded";
  version: string;
  frozen: boolean;
  checks: HealthChecks;
  build_id?: string;
}

// ── Status Responses ─────────────────────────────────────────────────────
export interface CollectionStatus {
  running: boolean;
  title: string | null;
  log: string[];
  collected: number;
  max_jobs: number;
  error: string | null;
  finished_at: string | null;
}

export type InboxCategory = "applied" | "assessment" | "interview" | "offer" | "rejected";

export interface InboxEmail {
  id: string;
  date: string;
  sender: string;
  sender_email: string;
  subject: string;
  snippet: string;
  text?: string;
  category: InboxCategory;
  company: string;
  role: string;
  gmail_url: string;
  job_url: string | null;
  prep?: string;
}

export interface InboxStatus {
  checking: boolean;
  requested: boolean;
  status: string;
  error: string;
  schedule: "off" | "daily" | "weekly";
  last_checked: string | null;
  emails: InboxEmail[];
  busy: boolean;
  performance_mode: boolean;
}

export interface CurrentJob {
  worker: number;
  url: string;
  title: string;
  company: string;
  step: number;
  max_steps: number;
  started_at: string;
}

export interface ApplyStatus {
  running: boolean;
  /** Paused from the app so the user can take over the browser. */
  paused?: boolean;
  /** Applications filled in and waiting for the user's approval to submit. */
  reviews?: PendingReview[];
  /** Trial-run applications stuck and waiting for the user's help. */
  help?: HelpRequest[];
  /** The job each worker is on right now. */
  current?: CurrentJob[];
  /** "auto" | "trial" | "training" while running. */
  run_mode?: string | null;
  mode: string | null;
  workers: number;
  log: string[];
  error: string | null;
  finished_at: string | null;
}

// closed: the posting was taken down. needs_you: a step only the user can do (a "confirm you're human" box).
// skipped: the fit check found a hard requirement the user doesn't meet (they can still apply by hand).
export type JobStatus = "pending" | "in_progress" | "applied" | "failed" | "blocked" | "closed" | "needs_you" | "skipped";

export interface JobStats {
  total: number;
  pending: number;
  applied: number;
  failed: number;
  blocked: number;
  in_progress: number;
  closed?: number;
  needs_you?: number;
  skipped?: number;
}

export interface DashboardResponse {
  jobs: JobStats;
  memory: MemoryStats;
  metrics?: Record<string, unknown>;
}

export interface DomainInfo {
  website_domain: string;
  ats_platform: string | null;
  count: number;
  avg_confidence: number;
  success_count: number;
  failure_count: number;
}

export interface SetupStatus {
  onboarding_completed: boolean;
  has_profile: boolean;
  has_llm: boolean;
  has_resume: boolean;
  has_jobs: boolean;
}

// ── Q&A Repository ───────────────────────────────────────────────────────
export interface HelpRequest {
  url: string;
  title: string;
  company: string;
  problem: string;
}

export interface PendingReview {
  url: string;
  title: string;
  company: string;
  summary: string;
}

export interface QAEntry {
  id: number;
  question: string;
  normalized: string;
  answer: string;
  question_type: string;
  source_domain: string;
  /** "user" when you gave the answer; "agent" when the AI recorded it; "guess" when it had to guess. */
  source?: string;
  /** The choices of a dropdown/radio question, one per line. */
  options?: string;
  times_seen: number;
  merged_into_id: number | null;
  created_at: string;
  updated_at: string;
}

export interface QAStats {
  total: number;
  answered: number;
  unanswered: number;
  /** Answers the agent had to guess, for the user to check. */
  guessed?: number;
}

// ── Run Logs ─────────────────────────────────────────────────────────────
export interface RunLog {
  id: number;
  run_id: string;
  job_url: string | null;
  timestamp: string;
  level: string;
  message: string;
  created_at: string;
}

export interface RunWithLogs extends RunMetric {
  run_id: string | null;
  log_count: number;
}
