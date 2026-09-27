"""Tests for the ChatGPT-plan provider: sign-in storage, token refresh, and the Codex requests.

Nothing here talks to OpenAI; httpx is replaced with fakes.
"""
import asyncio
import base64
import json
import time
import urllib.parse

import httpx
import pytest
from pydantic import BaseModel

from core import chatgpt_auth, chatgpt_llm
from core.llm_factory import create_llm, _friendly_llm_error


def _jwt(claims: dict) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


def _tokens(exp_in=3600, refresh="r1"):
    return {
        "id_token": _jwt({
            "email": "ada@example.com",
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-1", "chatgpt_plan_type": "plus"},
        }),
        "access_token": _jwt({"exp": time.time() + exp_in}),
        "refresh_token": refresh,
    }


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)


# ── Sign-in storage ─────────────────────────────────────────────────────────

class TestStoredSignIn:
    def test_save_extracts_account_details(self, data_dir):
        chatgpt_auth._save(_tokens())
        s = chatgpt_auth.status()
        assert s["signed_in"] is True
        assert s["email"] == "ada@example.com"
        assert s["plan"] == "plus"
        assert chatgpt_auth.load()["account_id"] == "acct-1"

    def test_not_signed_in(self, data_dir):
        assert chatgpt_auth.load() is None
        assert chatgpt_auth.status()["signed_in"] is False
        with pytest.raises(chatgpt_auth.ChatGPTAuthError):
            chatgpt_auth.credentials()

    def test_corrupt_file_counts_as_signed_out(self, data_dir):
        chatgpt_auth._auth_file().write_text("{not json", encoding="utf-8")
        assert chatgpt_auth.load() is None

    def test_logout_removes_the_sign_in(self, data_dir, monkeypatch):
        monkeypatch.setattr(chatgpt_auth.httpx, "post", lambda *a, **k: FakeResponse())
        chatgpt_auth._save(_tokens())
        chatgpt_auth.logout()
        assert chatgpt_auth.load() is None


class TestCredentials:
    def test_fresh_token_is_used_as_is(self, data_dir, monkeypatch):
        chatgpt_auth._save(_tokens())
        monkeypatch.setattr(chatgpt_auth.httpx, "post", lambda *a, **k: pytest.fail("no refresh expected"))
        access, account = chatgpt_auth.credentials()
        assert account == "acct-1"
        assert access == chatgpt_auth.load()["access_token"]

    def test_expiring_token_is_refreshed_and_saved(self, data_dir, monkeypatch):
        chatgpt_auth._save(_tokens(exp_in=60))
        new_access = _jwt({"exp": time.time() + 3600})
        calls = []

        def fake_post(url, json=None, **kwargs):
            calls.append((url, json))
            return FakeResponse(body={"access_token": new_access, "refresh_token": "r2"})

        monkeypatch.setattr(chatgpt_auth.httpx, "post", fake_post)
        access, account = chatgpt_auth.credentials()
        assert access == new_access and account == "acct-1"
        assert calls[0][0] == "https://auth.openai.com/oauth/token"
        assert calls[0][1] == {"client_id": chatgpt_auth.CLIENT_ID, "grant_type": "refresh_token", "refresh_token": "r1"}
        stored = chatgpt_auth.load()
        assert stored["refresh_token"] == "r2"          # rotated token kept
        assert stored["email"] == "ada@example.com"     # id_token details survive a refresh without one

    def test_revoked_sign_in_says_to_sign_in_again(self, data_dir, monkeypatch):
        chatgpt_auth._save(_tokens(exp_in=60))
        monkeypatch.setattr(chatgpt_auth.httpx, "post", lambda *a, **k: FakeResponse(status=401))
        with pytest.raises(chatgpt_auth.ChatGPTAuthError, match="Sign in to ChatGPT again"):
            chatgpt_auth.credentials()


class TestLoginFlow:
    @pytest.fixture
    def login(self, data_dir, monkeypatch):
        monkeypatch.setattr(chatgpt_auth, "CALLBACK_PORTS", (0,))  # any free port
        attempt = chatgpt_auth._Login()
        yield attempt
        attempt.server.server_close()

    def test_authorize_url_matches_the_codex_client(self, login):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(login.url).query)
        assert login.url.startswith("https://auth.openai.com/oauth/authorize?")
        assert query["client_id"] == [chatgpt_auth.CLIENT_ID]
        assert query["code_challenge_method"] == ["S256"]
        assert query["redirect_uri"][0].startswith("http://127.0.0.1:")
        assert query["redirect_uri"][0].endswith("/auth/callback")
        assert "offline_access" in query["scope"][0]

    def test_wrong_state_is_rejected_without_a_token_request(self, login, monkeypatch):
        monkeypatch.setattr(chatgpt_auth.httpx, "post", lambda *a, **k: pytest.fail("no exchange expected"))
        ok, _ = login.handle_callback({"state": ["forged"], "code": ["c"]})
        assert not ok and not login.done

    def test_good_callback_exchanges_the_code(self, login, monkeypatch):
        sent = {}

        def fake_post(url, data=None, **kwargs):
            sent.update(data)
            return FakeResponse(body=_tokens())

        monkeypatch.setattr(chatgpt_auth.httpx, "post", fake_post)
        ok, _ = login.handle_callback({"state": [login.state], "code": ["the-code"]})
        assert ok and login.done
        assert sent["code"] == "the-code" and sent["code_verifier"] == login.verifier
        assert sent["redirect_uri"] == login.redirect_uri
        assert chatgpt_auth.status()["signed_in"]

    def test_denied_sign_in_reports_the_error(self, login):
        ok, _ = login.handle_callback({"state": [login.state], "error": ["access_denied"]})
        assert not ok and login.done and login.error == "access_denied"


def test_list_models_keeps_listed_vision_models(data_dir, monkeypatch):
    chatgpt_auth._save(_tokens())
    body = {"models": [
        {"slug": "gpt-6-luna", "display_name": "GPT-6 Luna", "visibility": "list",
         "supported_reasoning_levels": [{"effort": "low"}, {"effort": "max"}], "default_reasoning_level": "medium"},
        {"slug": "hidden", "display_name": "Hidden", "visibility": "hide", "supported_reasoning_levels": []},
        {"slug": "text-only", "display_name": "T", "visibility": "list", "input_modalities": ["text"], "supported_reasoning_levels": []},
    ]}
    seen = {}

    def fake_get(url, params=None, headers=None, **kwargs):
        seen.update(headers)
        return FakeResponse(body=body)

    monkeypatch.setattr(chatgpt_auth.httpx, "get", fake_get)
    models = chatgpt_auth.list_models()
    assert models == [{"id": "gpt-6-luna", "name": "GPT-6 Luna", "efforts": ["low", "max"], "default_effort": "medium"}]
    assert seen["chatgpt-account-id"] == "acct-1"
    assert seen["Authorization"].startswith("Bearer ")


# ── The chat model ──────────────────────────────────────────────────────────

class AgentOutput(BaseModel):
    action: str
    count: int


def _sse(*events) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


@pytest.fixture
def codex(data_dir, monkeypatch):
    """Route the model's requests to a fake Codex backend; returns the captured requests."""
    chatgpt_auth._save(_tokens())
    captured = {"requests": [], "reply": None}
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request):
        captured["requests"].append(request)
        status, content = captured["reply"]
        return httpx.Response(status, content=content)

    monkeypatch.setattr(chatgpt_llm.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    return captured


def _messages():
    from browser_use.llm.messages import SystemMessage, UserMessage
    return [SystemMessage(content="You fill forms."), UserMessage(content="Next step?")]


class TestChatModel:
    def test_create_llm_builds_it_from_settings(self):
        llm = create_llm({"provider": "chatgpt", "chatgpt": {"model": "gpt-6-sol", "reasoning_effort": "high"}})
        assert isinstance(llm, chatgpt_llm.ChatChatGPT)
        assert (llm.model, llm.reasoning_effort, llm.provider) == ("gpt-6-sol", "high", "chatgpt")

    def test_default_model(self):
        assert create_llm({"provider": "chatgpt"}).model == "gpt-6-luna"

    def test_request_shape(self):
        body = chatgpt_llm.ChatChatGPT(reasoning_effort="max").build_request(_messages(), AgentOutput)
        assert body["instructions"] == "You fill forms."
        assert body["input"] == [{"role": "user", "content": "Next step?"}]
        assert body["store"] is False and body["stream"] is True
        assert body["reasoning"] == {"effort": "max"}
        assert body["text"]["format"]["type"] == "json_schema"
        assert set(body["text"]["format"]["schema"]["required"]) == {"action", "count"}

    def test_default_effort_sends_no_reasoning_field(self):
        assert "reasoning" not in chatgpt_llm.ChatChatGPT().build_request(_messages())

    def test_structured_reply_is_parsed(self, codex):
        codex["reply"] = (200, _sse(
            {"type": "response.output_text.delta", "delta": '{"action": "click", '},
            {"type": "response.output_text.delta", "delta": '"count": 2}'},
            {"type": "response.completed", "response": {"usage": {"input_tokens": 9000, "output_tokens": 300, "total_tokens": 9300,
                                                                  "input_tokens_details": {"cached_tokens": 6000}}}},
        ))
        result = asyncio.run(chatgpt_llm.ChatChatGPT().ainvoke(_messages(), AgentOutput))
        assert result.completion == AgentOutput(action="click", count=2)
        assert (result.usage.prompt_tokens, result.usage.prompt_cached_tokens, result.usage.completion_tokens) == (9000, 6000, 300)
        request = codex["requests"][0]
        assert str(request.url) == "https://chatgpt.com/backend-api/codex/responses"
        assert request.headers["chatgpt-account-id"] == "acct-1"
        assert request.headers["originator"] == "codex_cli_rs"

    def test_text_only_in_the_final_event(self, codex):
        codex["reply"] = (200, _sse({"type": "response.completed", "response": {"output": [
            {"type": "message", "content": [{"type": "output_text", "text": "hello"}]}]}}))
        assert asyncio.run(chatgpt_llm.ChatChatGPT().ainvoke(_messages())).completion == "hello"

    def test_usage_limit_is_a_rate_limit_with_reset_time(self, codex):
        from browser_use.llm.exceptions import ModelRateLimitError
        codex["reply"] = (429, json.dumps({"error": {"type": "usage_limit_reached", "message": "limit", "resets_in_seconds": 1800}}).encode())
        with pytest.raises(ModelRateLimitError, match="resets in about 30 minutes"):
            asyncio.run(chatgpt_llm.ChatChatGPT().ainvoke(_messages()))

    def test_failed_response_event_raises(self, codex):
        from browser_use.llm.exceptions import ModelProviderError
        codex["reply"] = (200, _sse({"type": "response.failed", "response": {"error": {"message": "model overloaded"}}}))
        with pytest.raises(ModelProviderError, match="model overloaded"):
            asyncio.run(chatgpt_llm.ChatChatGPT().ainvoke(_messages()))

    def test_signed_out_is_a_clear_error_without_a_request(self, data_dir):
        from browser_use.llm.exceptions import ModelProviderError
        with pytest.raises(ModelProviderError, match="Not signed in to ChatGPT") as e:
            asyncio.run(chatgpt_llm.ChatChatGPT().ainvoke(_messages()))
        assert e.value.status_code == 401
        # The Test button shows it as-is instead of "Invalid API key".
        assert _friendly_llm_error(e.value.message, 401) == e.value.message
