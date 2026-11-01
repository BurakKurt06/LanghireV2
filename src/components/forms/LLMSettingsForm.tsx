import { useState, useEffect, useRef, useCallback, useId } from "react";
import { TestTube, CheckCircle, XCircle, Loader2, ChevronDown, ChevronUp, ExternalLink } from "lucide-react";
import {
  getLLMSettings, saveLLMSettings, testLLMConnection, fetchOllamaModels, fetchProviderModels,
  startChatGPTLogin, getChatGPTStatus, chatGPTLogout, getChatGPTModels,
} from "../../lib/api";
import { trackEvent } from "../../lib/analytics";
import type { ChatGPTModel, ChatGPTStatus, LLMProvider, LLMSettings } from "../../lib/types";
import { useTranslation } from "react-i18next";
import PasswordInput from "../ui/PasswordInput";

const PROVIDERS: { id: LLMProvider; nameKey: string; descKey: string }[] = [
  { id: "chatgpt", nameKey: "providers.chatgpt.name", descKey: "providers.chatgpt.description" },
  { id: "openrouter", nameKey: "providers.openrouter.name", descKey: "providers.openrouter.description" },
  { id: "openai", nameKey: "providers.openai.name", descKey: "providers.openai.description" },
  { id: "anthropic", nameKey: "providers.anthropic.name", descKey: "providers.anthropic.description" },
  { id: "gemini", nameKey: "providers.gemini.name", descKey: "providers.gemini.description" },
  { id: "bedrock", nameKey: "providers.bedrock.name", descKey: "providers.bedrock.description" },
  { id: "ollama", nameKey: "providers.ollama.name", descKey: "providers.ollama.description" },
  { id: "openai_compatible", nameKey: "providers.openai_compatible.name", descKey: "providers.openai_compatible.description" },
];

// The *_MODELS lists below are offline fallbacks. Once an API key is entered,
// each provider's live model list replaces them (see useLiveModels).
const OPENAI_MODELS = ["gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.4", "gpt-5-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4o"];
const ANTHROPIC_MODELS = ["claude-sonnet-5", "claude-haiku-4-5", "claude-opus-5", "claude-opus-5-5", "claude-sonnet-4-6", "claude-sonnet-4-5"];
const GEMINI_MODELS = [
  "gemini-3-flash-preview",
  "gemini-3.8-flash",
  "gemini-3.5-flash",
  "gemini-3.1-flash-lite",
  "gemini-3.1-pro-preview",
  "gemini-3-pro-preview",
  "gemini-2.5-flash",
  "gemini-2.5-pro",
];
// Chat-capable models only: drop embedding, speech, image, realtime and dated snapshots.
const GEMINI_EXCLUDE = /embedding|tts|image|audio|live|aqa|learnlm|gemma|transcribe|robotics|computer-use|customtools/i;
const OPENAI_EXCLUDE = /audio|realtime|tts|transcribe|image|search|instruct|embedding|moderation|codex|3\.5|-\d{4}-\d{2}-\d{2}$|-\d{4}$/i;
const OPENROUTER_FALLBACK_MODELS = [
  "qwen/qwen3.6-plus",
  "bytedance-seed/seed-2.0-lite",
  "bytedance-seed/seed-1.6",
  "qwen/qwen3.6-27b",
  "qwen/qwen3-vl-32b-instruct",
  "qwen/qwen2.5-vl-72b-instruct",
  "openai/gpt-4o",
  "openai/gpt-4.1",
  "openai/gpt-4.1-mini",
  "anthropic/claude-sonnet-4",
  "anthropic/claude-haiku-4",
  "google/gemini-2.5-pro-preview",
  "google/gemini-2.5-flash-preview",
  "meta-llama/llama-4-maverick",
  "mistralai/mistral-large-2411",
];
// Until the signed-in plan's own list loads.
const CHATGPT_FALLBACK_MODELS: ChatGPTModel[] = [
  { id: "gpt-6-luna", name: "GPT-6 Luna", efforts: [], default_effort: "" },
  { id: "gpt-6-sol", name: "GPT-6 Sol", efforts: [], default_effort: "" },
];
const CHATGPT_FALLBACK_EFFORTS = ["low", "medium", "high"];
const BEDROCK_MODELS = [
  "us.anthropic.claude-sonnet-4-6",
  "us.anthropic.claude-haiku-4-5-20251001-v1:0",
  "anthropic.claude-3-5-sonnet-20241022-v2:0",
  "anthropic.claude-3-haiku-20240307-v1:0",
];

const defaultSettings: LLMSettings = {
  provider: "openrouter",
  chatgpt: { model: "gpt-6-luna", reasoning_effort: "" },
  openai: { api_key: "", model: "gpt-5.4-mini" },
  anthropic: { api_key: "", model: "claude-sonnet-5" },
  gemini: { api_key: "", model: "gemini-3-flash-preview" },
  bedrock: { access_key: "", secret_key: "", region: "us-west-2", model: "us.anthropic.claude-sonnet-4-6", auth_mode: "profile", profile_name: "default" },
  ollama: { base_url: "http://localhost:11434", model: "" },
  openrouter: { api_key: "", model: "qwen/qwen3.6-plus" },
  openai_compatible: { base_url: "", api_key: "", model: "" },
};

const OR_CUSTOM = "__custom__";

const byNewest = (a: string, b: string) => b.localeCompare(a, undefined, { numeric: true });

// Lists come through the backend: the app's content policy blocks the page from
// AI providers, and a saved key (shown masked) stays in the backend.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
async function providerModels(provider: string, key = "", baseUrl = ""): Promise<any> {
  const res = await fetchProviderModels(provider, key, baseUrl);
  if (!res.success) throw new Error(res.message || "model list unavailable");
  return res.data || {};
}

async function loadOpenAIModels(key: string): Promise<string[]> {
  const data = await providerModels("openai", key);
  return (data.data || [])
    .map((m: { id: string }) => m.id)
    .filter((id: string) => /^(gpt-|o\d|chatgpt-)/.test(id) && !OPENAI_EXCLUDE.test(id))
    .sort(byNewest);
}

async function loadAnthropicModels(key: string): Promise<string[]> {
  const data = await providerModels("anthropic", key);
  // The API already returns newest first.
  return (data.data || []).map((m: { id: string }) => m.id);
}

async function loadGeminiModels(key: string): Promise<string[]> {
  const data = await providerModels("gemini", key);
  return (data.models || [])
    .filter((m: { name: string; supportedGenerationMethods?: string[] }) =>
      m.name.startsWith("models/gemini") &&
      m.supportedGenerationMethods?.includes("generateContent") &&
      !GEMINI_EXCLUDE.test(m.name)
    )
    .map((m: { name: string }) => m.name.replace(/^models\//, ""))
    .sort(byNewest);
}

async function loadCompatibleModels(baseUrl: string, key: string): Promise<string[]> {
  const data = await providerModels("openai_compatible", key, baseUrl);
  return (data.data || []).map((m: { id: string }) => m.id).sort();
}

/** Fetch a provider's model list (debounced) whenever `trigger` changes. */
function useLiveModels(enabled: boolean, trigger: string, load: () => Promise<string[]>) {
  const [models, setModels] = useState<string[]>([]);
  const [fetching, setFetching] = useState(false);
  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load;
  });

  useEffect(() => {
    setModels([]);
    if (!enabled) return;
    let cancelled = false;
    const timer = setTimeout(async () => {
      setFetching(true);
      try {
        const ids = await loadRef.current();
        if (!cancelled && ids.length > 0) setModels(ids);
      } catch { /* offline, bad key or blocked: keep the fallback list */ }
      finally { if (!cancelled) setFetching(false); }
    }, 500);
    return () => { cancelled = true; clearTimeout(timer); setFetching(false); };
  }, [enabled, trigger]);

  return { models, fetching };
}

interface ModelPickerProps {
  value: string;
  options: string[];
  onChange: (model: string) => void;
  customLabel: string;
  placeholder?: string;
  /** For the label above it (htmlFor). */
  id?: string;
}

/** Model dropdown with a "Custom model ID…" escape hatch; a plain input when there is no list. */
function ModelPicker({ value, options, onChange, customLabel, placeholder, id }: ModelPickerProps) {
  const [custom, setCustom] = useState(false);
  const optionsKey = options.join("\n");

  // Start in custom mode when the saved model isn't in the list.
  useEffect(() => {
    if (value && !options.includes(value)) setCustom(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, optionsKey]);

  if (options.length === 0) {
    return <input id={id} value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} className="input-base" />;
  }

  return (
    <>
      <select
        id={id}
        value={custom ? OR_CUSTOM : value}
        onChange={(e) => {
          if (e.target.value === OR_CUSTOM) {
            setCustom(true);
          } else {
            setCustom(false);
            onChange(e.target.value);
          }
        }}
        className="input-base"
      >
        {options.map((m) => <option key={m} value={m}>{m}</option>)}
        <option value={OR_CUSTOM}>{customLabel}</option>
      </select>
      {custom && (
        <input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder ?? options[0]}
          aria-label={customLabel}
          className="input-base font-mono mt-2"
        />
      )}
    </>
  );
}

const PROVIDER_DISPLAY_NAMES: Record<string, string> = {
  chatgpt: "ChatGPT",
  openai: "OpenAI",
  anthropic: "Anthropic",
  gemini: "Google Gemini",
  bedrock: "AWS Bedrock",
  ollama: "Ollama",
  openrouter: "OpenRouter",
  openai_compatible: "OpenAI-Compatible",
};

type ChatGPTSettings = NonNullable<LLMSettings["chatgpt"]>;

/** "Sign in with ChatGPT" plus the plan's model and reasoning effort. */
function ChatGPTSection({ value, onChange }: { value: ChatGPTSettings; onChange: (patch: Partial<ChatGPTSettings>) => void }) {
  const { t } = useTranslation("llm");
  const [status, setStatus] = useState<ChatGPTStatus | null>(null);
  const [models, setModels] = useState<ChatGPTModel[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(() => getChatGPTStatus().then(setStatus).catch(() => {}), []);
  useEffect(() => { refresh(); }, [refresh]);

  // Poll while the sign-in page is open in the browser.
  useEffect(() => {
    if (!status?.pending) return;
    const timer = setInterval(refresh, 2000);
    return () => clearInterval(timer);
  }, [status?.pending, refresh]);

  useEffect(() => {
    if (!status?.signed_in) { setModels([]); return; }
    let cancelled = false;
    getChatGPTModels()
      .then((r) => { if (!cancelled && r.success && r.models.length > 0) setModels(r.models); })
      .catch(() => { /* keep the fallback list */ });
    return () => { cancelled = true; };
  }, [status?.signed_in]);

  const signIn = async () => {
    setBusy(true);
    setError("");
    try {
      const result = await startChatGPTLogin();
      if (result.error) setError(result.error);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const signOut = async () => {
    setBusy(true);
    try { setStatus(await chatGPTLogout()); } catch { /* backend unreachable */ }
    finally { setBusy(false); }
  };

  const options = models.length > 0 ? models : CHATGPT_FALLBACK_MODELS;
  const effortsFor = (id: string) => {
    const offered = options.find((m) => m.id === id)?.efforts ?? [];
    return offered.length > 0 ? offered : CHATGPT_FALLBACK_EFFORTS;
  };
  const efforts = effortsFor(value.model);
  const effort = value.reasoning_effort || "";
  const shownError = error || status?.error || "";

  return (
    <div className="space-y-3">
      <div className="rounded-2xl border border-border p-4 space-y-3">
        {status?.signed_in ? (
          <div className="flex items-center gap-3">
            <CheckCircle className="w-4 h-4 text-green-600 flex-shrink-0" />
            <div className="min-w-0">
              <p className="text-sm font-medium text-foreground truncate">{t("chatgpt.signedInAs", { email: status.email || "ChatGPT" })}</p>
              {status.plan && <p className="text-xs text-muted-foreground capitalize">{t("chatgpt.plan", { plan: status.plan })}</p>}
            </div>
            <button onClick={signOut} disabled={busy} className="btn-secondary ml-auto disabled:opacity-50">{t("chatgpt.signOut")}</button>
          </div>
        ) : (
          <>
            <p className="text-sm text-muted-foreground">{status?.pending ? t("chatgpt.waiting") : t("chatgpt.notSignedIn")}</p>
            <div className="flex items-center gap-3 flex-wrap">
              <button onClick={signIn} disabled={busy} className="btn-primary disabled:opacity-50">
                {(busy || status?.pending) && <Loader2 className="w-4 h-4 animate-spin" />}
                {t("chatgpt.signIn")}
              </button>
              {status?.pending && status.url && (
                <a href={status.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs text-primary font-semibold hover:underline">
                  {t("chatgpt.openAgain")} <ExternalLink className="w-3 h-3" />
                </a>
              )}
            </div>
          </>
        )}
        {shownError && !status?.signed_in && (
          <p role="alert" className="text-xs text-red-600 whitespace-pre-wrap break-words">{shownError}</p>
        )}
      </div>
      <div>
        <label htmlFor="chatgpt-model" className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.model")}</label>
        <select
          id="chatgpt-model"
          value={value.model}
          onChange={(e) => {
            const model = e.target.value;
            // Drop an effort the new model doesn't offer.
            onChange(effort && !effortsFor(model).includes(effort) ? { model, reasoning_effort: "" } : { model });
          }}
          className="input-base"
        >
          {!options.some((m) => m.id === value.model) && <option value={value.model}>{value.model}</option>}
          {options.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
        </select>
      </div>
      <div>
        <label htmlFor="chatgpt-effort" className="block text-sm font-semibold text-foreground mb-1.5">{t("chatgpt.reasoningEffort")}</label>
        <select id="chatgpt-effort" value={effort} onChange={(e) => onChange({ reasoning_effort: e.target.value })} className="input-base">
          <option value="">{t("chatgpt.effortDefault")}</option>
          {effort && !efforts.includes(effort) && <option value={effort}>{effort}</option>}
          {efforts.map((level) => <option key={level} value={level}>{level}</option>)}
        </select>
        <p className="text-xs text-muted-foreground mt-1">{t("chatgpt.effortHint")}</p>
      </div>
      <p className="text-xs text-muted-foreground">{t("chatgpt.howItWorks")}</p>
    </div>
  );
}

interface LLMSettingsFormProps {
  onSaved?: () => void;
  compact?: boolean;
}

export default function LLMSettingsForm({ onSaved, compact }: LLMSettingsFormProps) {
  const uid = useId();  // links each label to its field
  const { t } = useTranslation("llm");
  const [settings, setSettings] = useState<LLMSettings>(defaultSettings);
  const [testStatus, setTestStatus] = useState<"idle" | "testing" | "success" | "error">("idle");
  const [testMessage, setTestMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [loading, setLoading] = useState(true);
  // The saved settings couldn't be read: show that (and Retry) instead of a form
  // whose defaults would be saved over them by the first change.
  const [loadFailed, setLoadFailed] = useState(false);
  const [showGuide, setShowGuide] = useState(false);
  const [ollamaModels, setOllamaModels] = useState<string[]>([]);
  const [ollamaFetching, setOllamaFetching] = useState(false);
  const [openrouterModels, setOpenrouterModels] = useState<{ id: string; name: string; context: number; promptPrice: string; completionPrice: string }[]>([]);
  const [openrouterFetching, setOpenrouterFetching] = useState(false);
  const [openrouterCustom, setOpenrouterCustom] = useState(false);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const latest = useRef<LLMSettings>(defaultSettings);  // what the next save sends
  const ollamaFetchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(() => {
    getLLMSettings()
      .then((data) => {
        latest.current = { ...defaultSettings, ...data };
        setSettings(latest.current);
      })
      .catch(() => setLoadFailed(true))
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => { load(); }, [load]);
  const retry = () => {
    setLoading(true);
    setLoadFailed(false);
    load();
  };

  useEffect(() => {
    return () => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
      if (ollamaFetchTimer.current) clearTimeout(ollamaFetchTimer.current);
    };
  }, []);

  const loadOllamaModels = useCallback((baseUrl: string) => {
    if (ollamaFetchTimer.current) clearTimeout(ollamaFetchTimer.current);
    ollamaFetchTimer.current = setTimeout(async () => {
      if (!baseUrl.trim()) return;
      setOllamaFetching(true);
      try {
        const result = await fetchOllamaModels(baseUrl);
        if (result.success) setOllamaModels(result.models);
      } catch { /* server unreachable */ }
      finally { setOllamaFetching(false); }
    }, 500);
  }, []);

  useEffect(() => {
    if (settings.provider === "ollama" && settings.ollama?.base_url) {
      loadOllamaModels(settings.ollama.base_url);
    }
  }, [settings.provider, settings.ollama?.base_url, loadOllamaModels]);

  useEffect(() => {
    if (settings.provider !== "openrouter" || openrouterModels.length > 0) return;
    setOpenrouterFetching(true);
    providerModels("openrouter")
      .then(data => {
        const formatPrice = (p: string | number | undefined) => {
          if (!p) return "free";
          const n = typeof p === "string" ? parseFloat(p) : p;
          if (n === 0) return "free";
          const perMillion = n * 1_000_000;
          return perMillion < 1 ? `$${perMillion.toFixed(2)}/M` : `$${perMillion.toFixed(1)}/M`;
        };
        const vision = (data.data || [])
          .filter((m: { architecture?: { input_modalities?: string[] } }) =>
            m.architecture?.input_modalities?.includes("image")
          )
          .map((m: { id: string; name: string; context_length?: number; pricing?: { prompt?: string; completion?: string } }) => ({
            id: m.id,
            name: m.name,
            context: m.context_length || 0,
            promptPrice: formatPrice(m.pricing?.prompt),
            completionPrice: formatPrice(m.pricing?.completion),
          }))
          .sort((a: { id: string }, b: { id: string }) => a.id.localeCompare(b.id));
        if (vision.length > 0) setOpenrouterModels(vision);
      })
      .catch(() => {})
      .finally(() => setOpenrouterFetching(false));
  }, [settings.provider, openrouterModels.length]);

  const openaiKey = settings.openai?.api_key?.trim() || "";
  const openaiLive = useLiveModels(settings.provider === "openai" && !!openaiKey, openaiKey, () => loadOpenAIModels(openaiKey));
  const anthropicKey = settings.anthropic?.api_key?.trim() || "";
  const anthropicLive = useLiveModels(settings.provider === "anthropic" && !!anthropicKey, anthropicKey, () => loadAnthropicModels(anthropicKey));
  const geminiKey = settings.gemini?.api_key?.trim() || "";
  const geminiLive = useLiveModels(settings.provider === "gemini" && !!geminiKey, geminiKey, () => loadGeminiModels(geminiKey));
  const compatBase = settings.openai_compatible?.base_url?.trim() || "";
  const compatKey = settings.openai_compatible?.api_key?.trim() || "";
  const compatLive = useLiveModels(
    settings.provider === "openai_compatible" && /^https?:\/\//.test(compatBase),
    `${compatBase}\n${compatKey}`,
    () => loadCompatibleModels(compatBase, compatKey),
  );

  // Start in "custom" mode when the saved model isn't in the known list/fallback.
  useEffect(() => {
    if (settings.provider !== "openrouter") return;
    const model = settings.openrouter?.model;
    if (!model) return;
    const known = openrouterModels.some((m) => m.id === model) || OPENROUTER_FALLBACK_MODELS.includes(model);
    if (!known) setOpenrouterCustom(true);
  }, [settings.provider, settings.openrouter?.model, openrouterModels]);

  // Saves go out one at a time, in order, and each sends the newest settings: a
  // debounced save holding an older copy used to land after a later one (a typed
  // key, then a model pick within the debounce) and put the old model back.
  const saveQueue = useRef<Promise<void>>(Promise.resolve());
  const [saveFailed, setSaveFailed] = useState(false);

  const saveLatest = useCallback((track: boolean) => {
    if (saveTimer.current) { clearTimeout(saveTimer.current); saveTimer.current = null; }
    setSaving(true);
    setSaved(false);
    saveQueue.current = saveQueue.current.then(async () => {
      const toSave = latest.current;
      try {
        await saveLLMSettings(toSave);
        setSaveFailed(false);
        setSaved(true);
        setTimeout(() => setSaved(false), 2000);
        if (track) trackEvent("llm_provider_configured", { provider: toSave.provider });
        onSaved?.();
      } catch {
        setSaveFailed(true);
      } finally {
        setSaving(false);
      }
    });
    return saveQueue.current;
  }, [onSaved]);

  // Autosave with debounce (typing)
  const autoSave = useCallback((newSettings: LLMSettings) => {
    latest.current = newSettings;
    setSaved(false);
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => { saveTimer.current = null; saveLatest(false); }, 800);
  }, [saveLatest]);

  // Immediate save (for dropdown/radio changes); also sends anything still waiting to autosave
  const immediateSave = useCallback(async (newSettings: LLMSettings) => {
    latest.current = newSettings;
    await saveLatest(true);
  }, [saveLatest]);

  const updateProvider = (provider: LLMProvider) => {
    const ns = { ...settings, provider };
    setSettings(ns);
    setTestStatus("idle");
    immediateSave(ns);
  };

  const updateChatGPT = (patch: Partial<ChatGPTSettings>) => {
    const ns = { ...settings, chatgpt: { ...defaultSettings.chatgpt!, ...settings.chatgpt, ...patch } };
    setSettings(ns);
    immediateSave(ns);
  };

  const updateOpenAI = (field: string, value: string) => {
    const ns = { ...settings, openai: { ...settings.openai!, [field]: value } };
    setSettings(ns);
    if (field === "model") immediateSave(ns); else autoSave(ns);
  };

  const updateAnthropic = (field: string, value: string) => {
    const ns = { ...settings, anthropic: { ...settings.anthropic!, [field]: value } };
    setSettings(ns);
    if (field === "model") immediateSave(ns); else autoSave(ns);
  };

  const updateGemini = (field: string, value: string) => {
    const ns = { ...settings, gemini: { ...settings.gemini!, [field]: value } };
    setSettings(ns);
    if (field === "model") immediateSave(ns); else autoSave(ns);
  };

  const updateBedrock = (field: string, value: string) => {
    const ns = { ...settings, bedrock: { ...settings.bedrock!, [field]: value } };
    setSettings(ns);
    if (field === "auth_mode" || field === "model") immediateSave(ns); else autoSave(ns);
  };

  const updateOllama = (field: string, value: string) => {
    const ns = { ...settings, ollama: { ...settings.ollama!, [field]: value } };
    setSettings(ns);
    if (field === "model") immediateSave(ns); else autoSave(ns);
  };

  const updateOpenRouter = (field: string, value: string) => {
    const ns = { ...settings, openrouter: { ...settings.openrouter!, [field]: value } };
    setSettings(ns);
    if (field === "model") immediateSave(ns); else autoSave(ns);
  };

  const updateOpenAICompatible = (field: string, value: string) => {
    const ns = { ...settings, openai_compatible: { ...settings.openai_compatible!, [field]: value } };
    setSettings(ns);
    autoSave(ns);
  };

  const handleTest = async () => {
    setTestStatus("testing");
    setTestMessage("");
    try {
      const result = await testLLMConnection(settings);
      setTestStatus(result.success ? "success" : "error");
      setTestMessage(result.message || (result.success ? t("status.connectionSuccessful") : t("status.connectionFailed")));
    } catch (e) {
      setTestStatus("error");
      setTestMessage(e instanceof Error ? e.message : t("status.connectionFailed"));
    }
  };

  if (loading) return <div className="flex justify-center py-4"><Loader2 className="w-5 h-5 animate-spin text-primary" /></div>;

  if (loadFailed) {
    return (
      <div role="alert" className="error-banner flex items-center justify-between gap-3">
        <span>{t("status.loadFailed")}</span>
        <button type="button" onClick={retry} className="btn-secondary">{t("status.retry")}</button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {saveFailed && (
        <div role="alert" className="error-banner">{t("status.saveFailed")}</div>
      )}
      {/* API Key Guide — OpenAI & Anthropic only */}
      {(settings.provider === "openai" || settings.provider === "anthropic" || settings.provider === "openrouter") && (
        <div className="rounded-2xl border border-border overflow-hidden">
          <button
            onClick={() => setShowGuide(!showGuide)}
            className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-secondary/50 transition-colors"
          >
            <span className="text-[13px] font-semibold text-primary">
              {t("guide.howToGetKey", { provider: PROVIDER_DISPLAY_NAMES[settings.provider] })}
            </span>
            {showGuide ? <ChevronUp className="w-4 h-4 text-muted-foreground" /> : <ChevronDown className="w-4 h-4 text-muted-foreground" />}
          </button>

          {showGuide && (
            <div className="px-4 pb-4 border-t border-border pt-3">
              {settings.provider === "openai" ? (
                <div className="space-y-4">
                  <div className="space-y-2.5">
                    {[
                      { step: 1, text: t("guide.openai.step1"), link: "https://platform.openai.com/signup", linkText: t("guide.openai.step1Link") },
                      { step: 2, text: t("guide.openai.step2"), link: "https://platform.openai.com/settings/organization/billing/overview", linkText: t("guide.openai.step2Link") },
                      { step: 3, text: t("guide.openai.step3"), link: "https://platform.openai.com/api-keys", linkText: t("guide.openai.step3Link") },
                      { step: 4, text: t("guide.openai.step4") },
                      { step: 5, text: t("guide.openai.step5") },
                    ].map(({ step, text, link, linkText }) => (
                      <div key={step} className="flex gap-3">
                        <div className="w-6 h-6 rounded-full bg-secondary flex items-center justify-center flex-shrink-0 mt-0.5">
                          <span className="text-xs font-bold text-foreground">{step}</span>
                        </div>
                        <div className="text-[13px] text-foreground leading-relaxed">
                          {text}
                          {link && (
                            <a href={link} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-primary font-semibold hover:underline ml-1">
                              {linkText} <ExternalLink className="w-3 h-3" />
                            </a>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                  <div className="bg-[#F7F7F7] rounded-xl p-3 text-[12px] text-muted-foreground">
                    <strong className="text-foreground">{t("guide.note")}</strong> {t("guide.openai.note")}
                  </div>
                </div>
              ) : settings.provider === "anthropic" ? (
                <div className="space-y-4">
                  <div className="space-y-2.5">
                    {[
                      { step: 1, text: t("guide.anthropic.step1"), link: "https://console.anthropic.com/signup", linkText: t("guide.anthropic.step1Link") },
                      { step: 2, text: t("guide.anthropic.step2"), link: "https://console.anthropic.com/settings/billing", linkText: t("guide.anthropic.step2Link") },
                      { step: 3, text: t("guide.anthropic.step3"), link: "https://console.anthropic.com/settings/keys", linkText: t("guide.anthropic.step3Link") },
                      { step: 4, text: t("guide.anthropic.step4") },
                      { step: 5, text: t("guide.anthropic.step5") },
                    ].map(({ step, text, link, linkText }) => (
                      <div key={step} className="flex gap-3">
                        <div className="w-6 h-6 rounded-full bg-secondary flex items-center justify-center flex-shrink-0 mt-0.5">
                          <span className="text-xs font-bold text-foreground">{step}</span>
                        </div>
                        <div className="text-[13px] text-foreground leading-relaxed">
                          {text}
                          {link && (
                            <a href={link} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-primary font-semibold hover:underline ml-1">
                              {linkText} <ExternalLink className="w-3 h-3" />
                            </a>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                  <div className="bg-[#F7F7F7] rounded-xl p-3 text-[12px] text-muted-foreground">
                    <strong className="text-foreground">{t("guide.note")}</strong> {t("guide.anthropic.note")}
                  </div>
                </div>
              ) : (
                <div className="space-y-4">
                  <div className="space-y-2.5">
                    {[
                      { step: 1, text: t("guide.openrouter.step1"), link: "https://openrouter.ai", linkText: t("guide.openrouter.step1Link") },
                      { step: 2, text: t("guide.openrouter.step2"), link: "https://openrouter.ai/keys", linkText: t("guide.openrouter.step2Link") },
                      { step: 3, text: t("guide.openrouter.step3") },
                      { step: 4, text: t("guide.openrouter.step4") },
                    ].map(({ step, text, link, linkText }) => (
                      <div key={step} className="flex gap-3">
                        <div className="w-6 h-6 rounded-full bg-secondary flex items-center justify-center flex-shrink-0 mt-0.5">
                          <span className="text-xs font-bold text-foreground">{step}</span>
                        </div>
                        <div className="text-[13px] text-foreground leading-relaxed">
                          {text}
                          {link && (
                            <a href={link} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-primary font-semibold hover:underline ml-1">
                              {linkText} <ExternalLink className="w-3 h-3" />
                            </a>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                  <div className="bg-[#F7F7F7] rounded-xl p-3 text-[12px] text-muted-foreground">
                    <strong className="text-foreground">{t("guide.note")}</strong> {t("guide.openrouter.note")}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Provider Selection */}
      <div className={compact ? "" : "card"}>
        {!compact && <h3 className="text-sm font-bold text-foreground mb-4">{t("selectProvider")}</h3>}
        <div className="space-y-2">
          {PROVIDERS.map(({ id, nameKey, descKey }) => (
            <label key={id} className={`flex items-start gap-3 p-4 rounded-2xl border cursor-pointer transition-all ${settings.provider === id ? "border-foreground bg-secondary" : "border-border hover:border-gray-300"}`}>
              <input type="radio" name="provider" value={id} checked={settings.provider === id} onChange={() => updateProvider(id)} className="mt-1" />
              <div>
                <p className="text-sm font-medium text-foreground">{t(nameKey)}</p>
                <p className="text-xs text-muted-foreground">{t(descKey)}</p>
              </div>
            </label>
          ))}
        </div>
      </div>

      {/* Provider Config */}
      <div className={compact ? "" : "card"}>
        {!compact && <h3 className="text-sm font-bold text-foreground mb-4">{t("configuration", { provider: PROVIDER_DISPLAY_NAMES[settings.provider] })}</h3>}

        {settings.provider === "chatgpt" && (
          <ChatGPTSection value={{ ...defaultSettings.chatgpt!, ...settings.chatgpt }} onChange={updateChatGPT} />
        )}

        {settings.provider === "openai" && (
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f1`}>{t("labels.apiKey")}</label>
              <PasswordInput id={`${uid}-f1`} value={settings.openai?.api_key || ""} onChange={(e) => updateOpenAI("api_key", e.target.value)} placeholder="sk-..." className="input-base font-mono" />
            </div>
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f2`}>
                {t("labels.model")}
                {openaiLive.fetching && <Loader2 className="w-3 h-3 animate-spin inline ml-2" />}
              </label>
              <ModelPicker id={`${uid}-f2`}
                value={settings.openai?.model ?? "gpt-5.4-mini"}
                options={openaiLive.models.length > 0 ? openaiLive.models : OPENAI_MODELS}
                onChange={(m) => updateOpenAI("model", m)}
                customLabel={t("openrouter.customOption")}
              />
            </div>
          </div>
        )}

        {settings.provider === "anthropic" && (
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f3`}>{t("labels.apiKey")}</label>
              <PasswordInput id={`${uid}-f3`} value={settings.anthropic?.api_key || ""} onChange={(e) => updateAnthropic("api_key", e.target.value)} placeholder="sk-ant-..." className="input-base font-mono" />
            </div>
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f4`}>
                {t("labels.model")}
                {anthropicLive.fetching && <Loader2 className="w-3 h-3 animate-spin inline ml-2" />}
              </label>
              <ModelPicker id={`${uid}-f4`}
                value={settings.anthropic?.model ?? "claude-sonnet-5"}
                options={anthropicLive.models.length > 0 ? anthropicLive.models : ANTHROPIC_MODELS}
                onChange={(m) => updateAnthropic("model", m)}
                customLabel={t("openrouter.customOption")}
              />
            </div>
          </div>
        )}

        {settings.provider === "gemini" && (
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f5`}>{t("labels.apiKey")}</label>
              <PasswordInput id={`${uid}-f5`} value={settings.gemini?.api_key || ""} onChange={(e) => updateGemini("api_key", e.target.value)} placeholder="AIza..." className="input-base font-mono" />
            </div>
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f6`}>
                {t("labels.model")}
                {geminiLive.fetching && <Loader2 className="w-3 h-3 animate-spin inline ml-2" />}
              </label>
              <ModelPicker id={`${uid}-f6`}
                value={settings.gemini?.model ?? "gemini-3-flash-preview"}
                options={geminiLive.models.length > 0 ? geminiLive.models : GEMINI_MODELS}
                onChange={(m) => updateGemini("model", m)}
                customLabel={t("openrouter.customOption")}
              />
            </div>
          </div>
        )}

        {settings.provider === "bedrock" && (
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-medium text-foreground mb-2">{t("labels.authentication")}</label>
              <div className="flex gap-2">
                <button onClick={() => updateBedrock("auth_mode", "profile")} className={`filter-tab ${(settings.bedrock?.auth_mode || "profile") === "profile" ? "filter-tab-active" : "filter-tab-inactive"}`}>{t("labels.awsCliProfile")}</button>
                <button onClick={() => updateBedrock("auth_mode", "keys")} className={`filter-tab ${settings.bedrock?.auth_mode === "keys" ? "filter-tab-active" : "filter-tab-inactive"}`}>{t("labels.accessKeySecretKey")}</button>
              </div>
            </div>
            {(settings.bedrock?.auth_mode || "profile") === "profile" ? (
              <div>
                <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f7`}>{t("labels.profileName")}</label>
                <input id={`${uid}-f7`} value={settings.bedrock?.profile_name || "default"} onChange={(e) => updateBedrock("profile_name", e.target.value)} className="input-base" />
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f8`}>{t("labels.accessKey")}</label>
                  <PasswordInput id={`${uid}-f8`} value={settings.bedrock?.access_key || ""} onChange={(e) => updateBedrock("access_key", e.target.value)} placeholder="AKIA..." className="input-base font-mono" />
                </div>
                <div>
                  <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f9`}>{t("labels.secretKey")}</label>
                  <PasswordInput id={`${uid}-f9`} value={settings.bedrock?.secret_key || ""} onChange={(e) => updateBedrock("secret_key", e.target.value)} className="input-base font-mono" />
                </div>
              </div>
            )}
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f10`}>{t("labels.region")}</label>
              <input id={`${uid}-f10`} value={settings.bedrock?.region || "us-west-2"} onChange={(e) => updateBedrock("region", e.target.value)} className="input-base" />
            </div>
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.model")}</label>
              {/* No live list: Bedrock's model listing needs signed AWS requests. */}
              <ModelPicker
                value={settings.bedrock?.model ?? BEDROCK_MODELS[0]}
                options={BEDROCK_MODELS}
                onChange={(m) => updateBedrock("model", m)}
                customLabel={t("openrouter.customOption")}
              />
            </div>
          </div>
        )}

        {settings.provider === "ollama" && (
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f11`}>{t("labels.serverUrl")}</label>
              <input id={`${uid}-f11`} value={settings.ollama?.base_url || "http://localhost:11434"} onChange={(e) => updateOllama("base_url", e.target.value)} placeholder="http://localhost:11434" className="input-base font-mono" />
              <p className="text-xs text-muted-foreground mt-1">{t("ollama.serverUrlHint")}</p>
            </div>
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5">
                {t("labels.model")}
                {ollamaFetching && <Loader2 className="w-3 h-3 animate-spin inline ml-2" />}
              </label>
              {ollamaModels.length > 0 ? (
                <select value={settings.ollama?.model || ""} onChange={(e) => updateOllama("model", e.target.value)} className="input-base">
                  <option value="">{t("labels.selectModel")}</option>
                  {ollamaModels.map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
              ) : (
                <input value={settings.ollama?.model || ""} onChange={(e) => updateOllama("model", e.target.value)} placeholder="e.g. llama3.1" className="input-base" />
              )}
              <p className="text-xs text-muted-foreground mt-1">
                {ollamaModels.length > 0
                  ? t("ollama.modelsFound", { count: ollamaModels.length })
                  : t("ollama.noModelsHint")}
              </p>
            </div>
          </div>
        )}

        {settings.provider === "openrouter" && (
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f12`}>{t("labels.apiKey")}</label>
              <PasswordInput id={`${uid}-f12`} value={settings.openrouter?.api_key || ""} onChange={(e) => updateOpenRouter("api_key", e.target.value)} placeholder="sk-or-v1-..." className="input-base font-mono" />
              <p className="text-xs text-muted-foreground mt-1">
                {t("openrouter.keyHint")}{" "}
                <a href="https://openrouter.ai/keys" target="_blank" rel="noopener noreferrer" className="text-primary font-semibold hover:underline">openrouter.ai/keys</a>
              </p>
            </div>
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f13`}>
                {t("labels.modelVision")}
                {openrouterFetching && <Loader2 className="w-3 h-3 animate-spin inline ml-2" />}
              </label>
              <select id={`${uid}-f13`}
                value={openrouterCustom ? OR_CUSTOM : (settings.openrouter?.model || "openai/gpt-4o")}
                onChange={(e) => {
                  if (e.target.value === OR_CUSTOM) {
                    setOpenrouterCustom(true);
                  } else {
                    setOpenrouterCustom(false);
                    updateOpenRouter("model", e.target.value);
                  }
                }}
                className="input-base"
              >
                {openrouterModels.length > 0
                  ? openrouterModels.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.name} — in: {m.promptPrice} · out: {m.completionPrice} · {m.context >= 1000 ? `${Math.round(m.context / 1000)}K` : m.context} ctx
                    </option>
                  ))
                  : OPENROUTER_FALLBACK_MODELS.map(m => <option key={m} value={m}>{m}</option>)
                }
                <option value={OR_CUSTOM}>{t("openrouter.customOption")}</option>
              </select>
              {openrouterCustom && (
                <div className="mt-2">
                  <input
                    value={settings.openrouter?.model || ""}
                    onChange={(e) => updateOpenRouter("model", e.target.value)}
                    placeholder="openai/gpt-oss-120b:free"
                    className="input-base font-mono"
                  />
                  <p className="text-xs text-muted-foreground mt-1">{t("openrouter.customHint")}</p>
                </div>
              )}
              {!openrouterCustom && openrouterModels.length > 0 && (() => {
                const selected = openrouterModels.find(m => m.id === (settings.openrouter?.model || "openai/gpt-4o"));
                return selected ? (
                  <div className="flex gap-3 mt-2 text-[11px] text-muted-foreground">
                    <span>Input: <strong className="text-foreground">{selected.promptPrice}</strong></span>
                    <span>Output: <strong className="text-foreground">{selected.completionPrice}</strong></span>
                    <span>Context: <strong className="text-foreground">{selected.context >= 1000 ? `${Math.round(selected.context / 1000)}K tokens` : `${selected.context} tokens`}</strong></span>
                  </div>
                ) : null;
              })()}
              <p className="text-xs text-muted-foreground mt-1.5">
                {openrouterModels.length > 0
                  ? t("openrouter.modelsAvailable", { count: openrouterModels.length })
                  : t("openrouter.curatedList")}
                {" "}{t("openrouter.browseAt")}{" "}
                <a href="https://openrouter.ai/models" target="_blank" rel="noopener noreferrer" className="text-primary font-semibold hover:underline">openrouter.ai/models</a>
              </p>
            </div>
          </div>
        )}

        {settings.provider === "openai_compatible" && (
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f14`}>{t("labels.baseUrl")}</label>
              <input id={`${uid}-f14`} value={settings.openai_compatible?.base_url || ""} onChange={(e) => updateOpenAICompatible("base_url", e.target.value)} placeholder="https://api.together.xyz/v1" className="input-base font-mono" />
              <p className="text-xs text-muted-foreground mt-1">{t("openaiCompatible.baseUrlHint")}</p>
            </div>
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f15`}>{t("labels.apiKey")} <span className="text-muted-foreground font-normal">({t("openaiCompatible.optional")})</span></label>
              <PasswordInput id={`${uid}-f15`} value={settings.openai_compatible?.api_key || ""} onChange={(e) => updateOpenAICompatible("api_key", e.target.value)} placeholder="sk-..." className="input-base font-mono" />
              <p className="text-xs text-muted-foreground mt-1">{t("openaiCompatible.apiKeyHint")}</p>
            </div>
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5" htmlFor={`${uid}-f16`}>
                {t("labels.model")}
                {compatLive.fetching && <Loader2 className="w-3 h-3 animate-spin inline ml-2" />}
              </label>
              <ModelPicker id={`${uid}-f16`}
                value={settings.openai_compatible?.model || ""}
                options={compatLive.models}
                onChange={(m) => updateOpenAICompatible("model", m)}
                customLabel={t("openrouter.customOption")}
                placeholder="meta-llama/Llama-3-70b-chat-hf"
              />
              <p className="text-xs text-muted-foreground mt-1">{t("openaiCompatible.modelHint")}</p>
            </div>
          </div>
        )}
      </div>

      {/* Status + Test */}
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2 h-5">
          {saving && <span className="flex items-center gap-1 text-xs text-muted-foreground"><Loader2 className="w-3 h-3 animate-spin" /> {t("status.saving")}</span>}
          {saved && !saving && <span className="flex items-center gap-1 text-xs text-green-600"><CheckCircle className="w-3 h-3" /> {t("status.saved")}</span>}
        </div>
        <button onClick={handleTest} disabled={testStatus === "testing"} className="btn-secondary disabled:opacity-50 ml-auto">
          {testStatus === "testing" ? <Loader2 className="w-4 h-4 animate-spin" /> : <TestTube className="w-4 h-4" />}
          {testStatus === "testing" ? t("status.testing") : t("status.testConnection")}
        </button>
      </div>
      {testStatus === "success" && <div className="flex items-center gap-2 text-green-600"><CheckCircle className="w-4 h-4" /><span className="text-sm">{testMessage}</span></div>}
      {testStatus === "error" && <div className="flex items-center gap-2 text-red-600"><XCircle className="w-4 h-4" /><span className="text-sm">{testMessage}</span></div>}
    </div>
  );
}
