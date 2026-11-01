import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import LLMSettingsForm from "./LLMSettingsForm";
import {
  getLLMSettings,
  saveLLMSettings,
  testLLMConnection,
  fetchOllamaModels,
  fetchProviderModels,
  startChatGPTLogin,
  getChatGPTStatus,
  chatGPTLogout,
  getChatGPTModels,
} from "../../lib/api";
import { trackEvent } from "../../lib/analytics";

// i18n: identity translator so assertions can use raw keys.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

vi.mock("../../lib/analytics", () => ({
  trackEvent: vi.fn(),
}));

vi.mock("../../lib/api", () => ({
  getLLMSettings: vi.fn(),
  saveLLMSettings: vi.fn(),
  testLLMConnection: vi.fn(),
  fetchOllamaModels: vi.fn(),
  fetchProviderModels: vi.fn(),
  startChatGPTLogin: vi.fn(),
  getChatGPTStatus: vi.fn(),
  chatGPTLogout: vi.fn(),
  getChatGPTModels: vi.fn(),
}));

const mockGetLLMSettings = vi.mocked(getLLMSettings);
const mockSaveLLMSettings = vi.mocked(saveLLMSettings);
const mockTestLLMConnection = vi.mocked(testLLMConnection);
const mockFetchOllamaModels = vi.mocked(fetchOllamaModels);
const mockFetchProviderModels = vi.mocked(fetchProviderModels);
const mockTrackEvent = vi.mocked(trackEvent);
const mockStartChatGPTLogin = vi.mocked(startChatGPTLogin);
const mockGetChatGPTStatus = vi.mocked(getChatGPTStatus);
const mockChatGPTLogout = vi.mocked(chatGPTLogout);
const mockGetChatGPTModels = vi.mocked(getChatGPTModels);

const signedOut = { signed_in: false, email: "", plan: "", pending: false, error: "", url: "" };
const signedIn = { ...signedOut, signed_in: true, email: "ada@example.com", plan: "plus" };

const baseSettings = {
  provider: "openrouter",
  openai: { api_key: "", model: "gpt-4o" },
  anthropic: { api_key: "", model: "claude-sonnet-4-5" },
  gemini: { api_key: "", model: "gemini-2.5-pro" },
  bedrock: {
    access_key: "",
    secret_key: "",
    region: "us-west-2",
    model: "us.anthropic.claude-sonnet-4-6",
    auth_mode: "profile",
    profile_name: "default",
  },
  ollama: { base_url: "http://localhost:11434", model: "" },
  openrouter: { api_key: "", model: "qwen/qwen3.6-plus" },
  openai_compatible: { base_url: "", api_key: "", model: "" },
};

// Model lists come from the backend (the page never calls providers itself): an
// empty OpenRouter list → the curated fallback renders. A direct fetch fails the test.
function stubFetch(data: unknown = { data: [] }) {
  mockFetchProviderModels.mockResolvedValue({ success: true, data });
  vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("the page must not call providers directly"); }));
}

describe("LLMSettingsForm", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    mockGetLLMSettings.mockResolvedValue(structuredClone(baseSettings) as never);
    mockSaveLLMSettings.mockResolvedValue({ success: true } as never);
    mockTestLLMConnection.mockResolvedValue({ success: true, message: "ok" } as never);
    mockFetchOllamaModels.mockResolvedValue({ success: true, models: [] } as never);
    mockGetChatGPTStatus.mockResolvedValue(signedOut);
    mockGetChatGPTModels.mockResolvedValue({ success: true, models: [] });
    stubFetch();
  });

  it("renders a loading spinner before settings load", async () => {
    render(<LLMSettingsForm />);
    // The provider radios are not present while loading.
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    await waitFor(() => expect(mockGetLLMSettings).toHaveBeenCalled());
    await screen.findByText("selectProvider");
  });

  it("renders all provider radios and starts with openrouter selected", async () => {
    render(<LLMSettingsForm />);
    await screen.findByText("selectProvider");

    const radios = screen.getAllByRole("radio");
    expect(radios).toHaveLength(8);
    const openrouter = screen.getByRole("radio", { name: /providers\.openrouter/ });
    expect(openrouter).toBeChecked();
    // OpenRouter config field (api key) is present.
    expect(screen.getByPlaceholderText("sk-or-v1-...")).toBeInTheDocument();
  });

  it("switches provider via radio, renders that provider's config, and immediate-saves", async () => {
    const user = userEvent.setup();
    render(<LLMSettingsForm />);
    await screen.findByText("selectProvider");

    await user.click(screen.getByRole("radio", { name: /providers\.openai\b/ }));

    // OpenAI key placeholder appears.
    expect(await screen.findByPlaceholderText("sk-...")).toBeInTheDocument();
    await waitFor(() =>
      expect(mockSaveLLMSettings).toHaveBeenCalledWith(
        expect.objectContaining({ provider: "openai" }),
      ),
    );
    expect(mockTrackEvent).toHaveBeenCalledWith("llm_provider_configured", {
      provider: "openai",
    });
  });

  it("debounce-saves api key edits (autosave path)", async () => {
    const user = userEvent.setup();
    render(<LLMSettingsForm />);
    await screen.findByText("selectProvider");

    const keyInput = screen.getByPlaceholderText("sk-or-v1-...");
    await user.type(keyInput, "sk-or-v1-abc");

    await waitFor(
      () =>
        expect(mockSaveLLMSettings).toHaveBeenCalledWith(
          expect.objectContaining({
            openrouter: expect.objectContaining({ api_key: "sk-or-v1-abc" }),
          }),
        ),
      { timeout: 2000 },
    );
  });

  it("Test Connection success shows the returned message", async () => {
    mockTestLLMConnection.mockResolvedValue({
      success: true,
      message: "Connected!",
    } as never);
    const user = userEvent.setup();
    render(<LLMSettingsForm />);
    await screen.findByText("selectProvider");

    await user.click(screen.getByRole("button", { name: /status\.testConnection/ }));

    expect(await screen.findByText("Connected!")).toBeInTheDocument();
    expect(mockTestLLMConnection).toHaveBeenCalled();
  });

  it("Test Connection failure shows an error message", async () => {
    mockTestLLMConnection.mockResolvedValue({
      success: false,
      message: "Bad key",
    } as never);
    const user = userEvent.setup();
    render(<LLMSettingsForm />);
    await screen.findByText("selectProvider");

    await user.click(screen.getByRole("button", { name: /status\.testConnection/ }));

    expect(await screen.findByText("Bad key")).toBeInTheDocument();
  });

  it("Test Connection shows the backend's friendly invalid-key message (#48)", async () => {
    mockTestLLMConnection.mockResolvedValue({
      success: false,
      message: "Invalid API key. Please check your key and try again.",
    } as never);
    const user = userEvent.setup();
    render(<LLMSettingsForm />);
    await screen.findByText("selectProvider");

    await user.click(screen.getByRole("button", { name: /status\.testConnection/ }));
    expect(
      await screen.findByText("Invalid API key. Please check your key and try again."),
    ).toBeInTheDocument();
  });

  it("Test Connection rejection surfaces the thrown error message", async () => {
    mockTestLLMConnection.mockRejectedValue(new Error("timeout"));
    const user = userEvent.setup();
    render(<LLMSettingsForm />);
    await screen.findByText("selectProvider");

    await user.click(screen.getByRole("button", { name: /status\.testConnection/ }));
    expect(await screen.findByText("timeout")).toBeInTheDocument();
  });

  describe("OpenRouter custom model", () => {
    it("renders the curated fallback list plus the custom option", async () => {
      render(<LLMSettingsForm />);
      await screen.findByText("selectProvider");

      const select = screen.getByDisplayValue("qwen/qwen3.6-plus");
      const options = within(select).getAllByRole("option");
      // Fallback models + the custom option.
      expect(options.some((o) => o.getAttribute("value") === "__custom__")).toBe(true);
      expect(options.some((o) => o.getAttribute("value") === "openai/gpt-4o")).toBe(true);
    });

    it("selecting the custom option reveals a text input and saves a typed model", async () => {
      const user = userEvent.setup();
      render(<LLMSettingsForm />);
      await screen.findByText("selectProvider");

      const select = screen.getByDisplayValue("qwen/qwen3.6-plus");
      await user.selectOptions(select, "__custom__");

      // Custom text input appears (placeholder is the example custom tag).
      const customInput = await screen.findByPlaceholderText("openai/gpt-oss-120b:free");
      expect(customInput).toBeInTheDocument();
      // Input is bound to the current model value, so clear before typing.
      await user.clear(customInput);
      await user.type(customInput, "openai/gpt-oss-120b:free");

      await waitFor(() =>
        expect(mockSaveLLMSettings).toHaveBeenCalledWith(
          expect.objectContaining({
            openrouter: expect.objectContaining({
              model: "openai/gpt-oss-120b:free",
            }),
          }),
        ),
      );
    });

    it("loading a saved model that is NOT in the known list starts in custom mode", async () => {
      const custom = structuredClone(baseSettings);
      custom.openrouter.model = "some/unknown-model:free";
      mockGetLLMSettings.mockResolvedValue(custom as never);

      render(<LLMSettingsForm />);
      await screen.findByText("selectProvider");

      // The custom input is visible and pre-filled with the unknown model.
      const customInput = await screen.findByPlaceholderText("openai/gpt-oss-120b:free");
      expect(customInput).toHaveValue("some/unknown-model:free");
      // Select reflects the custom sentinel value.
      const select = screen.getByDisplayValue("openrouter.customOption");
      expect(select).toBeInTheDocument();
    });

    it("uses the live vision-model list when the OpenRouter API returns models", async () => {
      stubFetch({
        data: [
          {
            id: "vendor/vision-a",
            name: "Vision A",
            context_length: 32000,
            architecture: { input_modalities: ["text", "image"] },
            pricing: { prompt: "0.000001", completion: "0.000002" },
          },
          {
            id: "vendor/text-only",
            name: "Text Only",
            context_length: 8000,
            architecture: { input_modalities: ["text"] },
            pricing: { prompt: "0", completion: "0" },
          },
        ],
      });

      render(<LLMSettingsForm />);
      await screen.findByText("selectProvider");

      // Vision model should render; text-only filtered out.
      await waitFor(() => {
        expect(screen.getByText(/Vision A/)).toBeInTheDocument();
      });
      expect(screen.queryByText(/Text Only/)).not.toBeInTheDocument();
    });
  });

  describe("provider config fields", () => {
    it("bedrock: toggling auth mode to keys reveals access/secret key inputs", async () => {
      const user = userEvent.setup();
      render(<LLMSettingsForm />);
      await screen.findByText("selectProvider");

      await user.click(screen.getByRole("radio", { name: /providers\.bedrock/ }));
      // Profile mode renders first (profile name input present).
      await screen.findByText("labels.profileName");

      // Switch to access/secret key auth.
      await user.click(screen.getByRole("button", { name: "labels.accessKeySecretKey" }));
      expect(await screen.findByPlaceholderText("AKIA...")).toBeInTheDocument();
    });

    it("ollama: shows a model dropdown when models are discovered", async () => {
      mockFetchOllamaModels.mockResolvedValue({
        success: true,
        models: ["llama3.1", "mistral"],
      } as never);
      const user = userEvent.setup();
      render(<LLMSettingsForm />);
      await screen.findByText("selectProvider");

      await user.click(screen.getByRole("radio", { name: /providers\.ollama/ }));

      // The debounced ollama fetch (500ms) populates a select with options.
      await waitFor(
        () => expect(screen.getByRole("option", { name: "llama3.1" })).toBeInTheDocument(),
        { timeout: 2000 },
      );
    });

    it("gemini: renders API key + model fields and saves on selection", async () => {
      const user = userEvent.setup();
      render(<LLMSettingsForm />);
      await screen.findByText("selectProvider");

      await user.click(screen.getByRole("radio", { name: /providers\.gemini/ }));

      // API key password field appears.
      expect(await screen.findByPlaceholderText("AIza...")).toBeInTheDocument();
      // Model select offers the Gemini models.
      expect(screen.getByRole("option", { name: "gemini-3-flash-preview" })).toBeInTheDocument();
      expect(screen.getByRole("option", { name: "gemini-2.5-pro" })).toBeInTheDocument();
      expect(screen.getByRole("option", { name: "gemini-2.5-flash" })).toBeInTheDocument();
      // Switching provider immediate-saves with provider=gemini.
      await waitFor(() =>
        expect(mockSaveLLMSettings).toHaveBeenCalledWith(
          expect.objectContaining({ provider: "gemini" }),
        ),
      );
    });

    it("gemini: editing the API key debounce-saves", async () => {
      const user = userEvent.setup();
      render(<LLMSettingsForm />);
      await screen.findByText("selectProvider");

      await user.click(screen.getByRole("radio", { name: /providers\.gemini/ }));
      const keyInput = await screen.findByPlaceholderText("AIza...");
      await user.type(keyInput, "AIzaABC");

      await waitFor(
        () =>
          expect(mockSaveLLMSettings).toHaveBeenCalledWith(
            expect.objectContaining({
              gemini: expect.objectContaining({ api_key: "AIzaABC" }),
            }),
          ),
        { timeout: 2000 },
      );
    });

    it("openai_compatible: renders base URL and model inputs", async () => {
      const user = userEvent.setup();
      render(<LLMSettingsForm />);
      await screen.findByText("selectProvider");

      await user.click(
        screen.getByRole("radio", { name: /providers\.openai_compatible/ }),
      );
      expect(
        await screen.findByPlaceholderText("https://api.together.xyz/v1"),
      ).toBeInTheDocument();
      expect(
        screen.getByPlaceholderText("meta-llama/Llama-3-70b-chat-hf"),
      ).toBeInTheDocument();
    });

    it("gemini: a saved key loads the live model list through the backend", async () => {
      stubFetch({
        models: [
          { name: "models/gemini-9-flash", supportedGenerationMethods: ["generateContent"] },
          { name: "models/gemini-9-flash-tts", supportedGenerationMethods: ["generateContent"] },
          { name: "models/text-embedding-9", supportedGenerationMethods: ["embedContent"] },
        ],
      });
      mockGetLLMSettings.mockResolvedValue({
        ...structuredClone(baseSettings),
        provider: "gemini",
        gemini: { api_key: "AIza-live", model: "gemini-9-flash" },
      } as never);

      render(<LLMSettingsForm />);

      expect(await screen.findByRole("option", { name: "gemini-9-flash" })).toBeInTheDocument();
      // TTS and embedding models are filtered out.
      expect(screen.queryByRole("option", { name: "gemini-9-flash-tts" })).not.toBeInTheDocument();
      expect(screen.queryByRole("option", { name: "text-embedding-9" })).not.toBeInTheDocument();
      expect(mockFetchProviderModels).toHaveBeenCalledWith("gemini", "AIza-live", "");
      expect(fetch).not.toHaveBeenCalled();
    });

    it("anthropic: the custom option reveals a text input for any model ID", async () => {
      const user = userEvent.setup();
      render(<LLMSettingsForm />);
      await screen.findByText("selectProvider");

      await user.click(screen.getByRole("radio", { name: /providers\.anthropic/ }));
      const select = await screen.findByDisplayValue("claude-sonnet-4-5");
      await user.selectOptions(select, "__custom__");
      const input = await waitFor(() => {
        const el = screen.getAllByDisplayValue("claude-sonnet-4-5").find((e) => e.tagName === "INPUT");
        if (!el) throw new Error("custom input not shown");
        return el;
      });
      await user.clear(input);
      await user.type(input, "claude-next");
      await waitFor(() =>
        expect(mockSaveLLMSettings).toHaveBeenCalledWith(
          expect.objectContaining({ anthropic: expect.objectContaining({ model: "claude-next" }) }),
        ),
      );
    });
  });

  describe("ChatGPT plan", () => {
    const planModels = [
      { id: "gpt-6-luna", name: "GPT-6 Luna", efforts: ["low", "medium", "high", "max"], default_effort: "medium" },
      { id: "gpt-6-sol", name: "GPT-6 Sol", efforts: ["low", "medium"], default_effort: "medium" },
    ];

    it("signed out: sign-in starts the browser flow and shows the waiting state", async () => {
      const user = userEvent.setup();
      mockStartChatGPTLogin.mockResolvedValue({ url: "https://auth.openai.com/oauth/authorize?x=1", error: "" });
      render(<LLMSettingsForm />);
      await screen.findByText("selectProvider");

      await user.click(screen.getByRole("radio", { name: /providers\.chatgpt/ }));
      expect(await screen.findByText("chatgpt.notSignedIn")).toBeInTheDocument();
      expect(mockSaveLLMSettings).toHaveBeenCalledWith(expect.objectContaining({ provider: "chatgpt" }));

      mockGetChatGPTStatus.mockResolvedValue({ ...signedOut, pending: true, url: "https://auth.openai.com/oauth/authorize?x=1" });
      await user.click(screen.getByRole("button", { name: /chatgpt\.signIn/ }));
      expect(mockStartChatGPTLogin).toHaveBeenCalled();
      expect(await screen.findByText("chatgpt.waiting")).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /chatgpt\.openAgain/ })).toHaveAttribute("href", "https://auth.openai.com/oauth/authorize?x=1");
    });

    it("shows a sign-in error, such as busy callback ports", async () => {
      const user = userEvent.setup();
      mockStartChatGPTLogin.mockResolvedValue({ url: "", error: "ports 1455 and 1457 are in use" });
      mockGetLLMSettings.mockResolvedValue({ ...structuredClone(baseSettings), provider: "chatgpt" } as never);
      render(<LLMSettingsForm />);
      await user.click(await screen.findByRole("button", { name: /chatgpt\.signIn/ }));
      expect(await screen.findByRole("alert")).toHaveTextContent("in use");
    });

    it("signed in: shows the account, loads the plan's models, and saves model and effort", async () => {
      const user = userEvent.setup();
      mockGetChatGPTStatus.mockResolvedValue(signedIn);
      mockGetChatGPTModels.mockResolvedValue({ success: true, models: planModels });
      mockChatGPTLogout.mockResolvedValue(signedOut);
      mockGetLLMSettings.mockResolvedValue({
        ...structuredClone(baseSettings), provider: "chatgpt", chatgpt: { model: "gpt-6-luna", reasoning_effort: "max" },
      } as never);
      render(<LLMSettingsForm />);

      expect(await screen.findByText("chatgpt.signedInAs")).toBeInTheDocument();
      await waitFor(() => expect(screen.getByRole("option", { name: "max" })).toBeInTheDocument());
      expect(screen.getByLabelText("chatgpt.reasoningEffort")).toHaveValue("max");

      // Sol doesn't offer "max", so switching resets the effort to the model default.
      await user.selectOptions(screen.getByLabelText("labels.model"), "gpt-6-sol");
      await waitFor(() =>
        expect(mockSaveLLMSettings).toHaveBeenLastCalledWith(
          expect.objectContaining({ chatgpt: { model: "gpt-6-sol", reasoning_effort: "" } }),
        ),
      );

      await user.selectOptions(screen.getByLabelText("chatgpt.reasoningEffort"), "low");
      await waitFor(() =>
        expect(mockSaveLLMSettings).toHaveBeenLastCalledWith(
          expect.objectContaining({ chatgpt: { model: "gpt-6-sol", reasoning_effort: "low" } }),
        ),
      );

      await user.click(screen.getByRole("button", { name: /chatgpt\.signOut/ }));
      expect(mockChatGPTLogout).toHaveBeenCalled();
      expect(await screen.findByText("chatgpt.notSignedIn")).toBeInTheDocument();
    });
  });

  describe("saved settings safety", () => {
    it("a failed load shows an error and Retry, never a form that could save defaults", async () => {
      const user = userEvent.setup();
      mockGetLLMSettings.mockRejectedValueOnce(new Error("backend down"));
      render(<LLMSettingsForm />);
      expect(await screen.findByText("status.loadFailed")).toBeInTheDocument();
      expect(screen.queryByText("selectProvider")).not.toBeInTheDocument();
      expect(mockSaveLLMSettings).not.toHaveBeenCalled();
      await user.click(screen.getByRole("button", { name: "status.retry" }));
      expect(await screen.findByText("selectProvider")).toBeInTheDocument();
    });

    it("a model pick right after typing a key saves both, and no older copy lands after it", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
        mockGetLLMSettings.mockResolvedValue({ ...structuredClone(baseSettings), provider: "gemini" } as never);
        render(<LLMSettingsForm />);
        const key = await screen.findByPlaceholderText("AIza...");
        await user.type(key, "AIza-new");                    // debounced autosave pending
        const model = screen.getByDisplayValue("gemini-2.5-pro") as HTMLSelectElement;
        const other = Array.from(model.options).map((o) => o.value).find((v) => v && v !== "gemini-2.5-pro")!;
        await user.selectOptions(model, other);               // immediate save
        await vi.advanceTimersByTimeAsync(2000);              // the old debounce would fire here
        const saves = mockSaveLLMSettings.mock.calls.map(([s]) => s as typeof baseSettings);
        const last = saves[saves.length - 1];
        expect(last.gemini).toEqual({ api_key: "AIza-new", model: other });
        expect(saves.every((s, i) => i < saves.length - 1 || s.gemini.model === other)).toBe(true);
      } finally {
        vi.useRealTimers();
      }
    });

    it("a failed save is shown", async () => {
      const user = userEvent.setup();
      mockSaveLLMSettings.mockRejectedValue(new Error("backend down"));
      render(<LLMSettingsForm />);
      await screen.findByText("selectProvider");
      await user.click(screen.getByRole("radio", { name: /gemini/i }));
      expect(await screen.findByText("status.saveFailed")).toBeInTheDocument();
    });
  });
});

describe("LLMSettingsForm labels", () => {
  beforeEach(() => {
    mockGetLLMSettings.mockResolvedValue({ ...structuredClone(baseSettings), provider: "openai" } as never);
    mockSaveLLMSettings.mockResolvedValue({ success: true } as never);
    mockGetChatGPTStatus.mockResolvedValue(signedOut);
    stubFetch();
  });

  it("the key and model fields are named by their labels", async () => {
    render(<LLMSettingsForm />);
    expect(await screen.findByLabelText("labels.apiKey")).toHaveAttribute("placeholder", "sk-...");
    expect(screen.getByLabelText(/labels\.model/)).toBeInTheDocument();
  });
});
