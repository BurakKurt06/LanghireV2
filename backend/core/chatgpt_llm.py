"""browser-use chat model that runs on a ChatGPT plan (see chatgpt_auth)."""

import asyncio
import json
import uuid

import httpx

from core import chatgpt_auth

# The Codex backend always streams; these events carry the text and the end state.
_TEXT_DELTA = "response.output_text.delta"
_DONE = "response.completed"
_FAILED = ("response.failed", "response.incomplete", "error")


class ChatChatGPT:
    """Responses-API requests to the Codex backend, billed to the signed-in ChatGPT plan."""

    _verified_api_keys = False

    def __init__(self, model: str = "gpt-6-luna", reasoning_effort: str = "", timeout: float = 180):
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        # One id per model object (one per application): sent as the prompt
        # cache key and session header, the way the Codex CLI does. Without
        # both, the backend served nothing from cache; with them, a repeated
        # 9K-token prefix came back ~98% cached. Every step of a run repeats
        # the same system prompt and instructions (~11K tokens).
        self.session_id = str(uuid.uuid4())

    @property
    def provider(self) -> str:
        return "chatgpt"

    @property
    def name(self) -> str:
        return self.model

    @property
    def model_name(self) -> str:
        return self.model

    def build_request(self, messages, output_format=None) -> dict:
        from browser_use.llm.openai.responses_serializer import ResponsesAPIMessageSerializer
        from browser_use.llm.messages import SystemMessage

        # System prompts go in `instructions`; the backend requires it.
        instructions = "\n\n".join(
            m.text for m in messages if isinstance(m, SystemMessage)
        ) or "You are a helpful assistant."
        body = {
            "model": self.model,
            "instructions": instructions,
            "input": ResponsesAPIMessageSerializer.serialize_messages(
                [m for m in messages if not isinstance(m, SystemMessage)]
            ),
            "store": False,
            "stream": True,
            "include": [],
            "prompt_cache_key": self.session_id,
        }
        if self.reasoning_effort:
            body["reasoning"] = {"effort": self.reasoning_effort}
        if output_format is not None:
            from browser_use.llm.schema import SchemaOptimizer
            body["text"] = {"format": {
                "type": "json_schema",
                "name": "agent_output",
                "strict": True,
                "schema": SchemaOptimizer.create_optimized_json_schema(output_format),
            }}
        return body

    async def ainvoke(self, messages, output_format=None, **kwargs):
        from browser_use.llm.exceptions import ModelProviderError
        from browser_use.llm.views import ChatInvokeCompletion

        try:
            access, account = await asyncio.to_thread(chatgpt_auth.credentials)
        except chatgpt_auth.ChatGPTAuthError as e:
            raise ModelProviderError(message=str(e), status_code=401, model=self.name) from e

        text, usage = await self._stream(self.build_request(messages, output_format), access, account)

        if output_format is None:
            return ChatInvokeCompletion(completion=text, usage=usage)
        try:
            parsed = output_format.model_validate_json(text)
        except Exception as e:
            raise ModelProviderError(
                message=f"Failed to parse structured output from model response: {e}",
                status_code=500, model=self.name,
            ) from e
        return ChatInvokeCompletion(completion=parsed, usage=usage)

    async def _stream(self, body: dict, access: str, account: str):
        from browser_use.llm.exceptions import ModelProviderError, ModelRateLimitError

        headers = {**chatgpt_auth.request_headers(access, account), "Accept": "text/event-stream",
                   "session_id": self.session_id, "conversation_id": self.session_id}
        parts, usage, completed = [], None, False
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=20)) as client:
                async with client.stream("POST", f"{chatgpt_auth.CODEX_BASE_URL}/responses",
                                         json=body, headers=headers) as response:
                    if response.status_code != 200:
                        detail = _error_message(await response.aread(), response.status_code)
                        if response.status_code == 429:
                            raise ModelRateLimitError(message=detail, model=self.name)
                        raise ModelProviderError(message=detail, status_code=response.status_code, model=self.name)
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            event = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        kind = event.get("type")
                        if kind == _TEXT_DELTA:
                            parts.append(event.get("delta", ""))
                        elif kind == _DONE:
                            completed = True
                            final = event.get("response") or {}
                            usage = _usage(final.get("usage"))
                            if not parts:  # some replies arrive only in the final event
                                parts.append(_output_text(final))
                        elif kind in _FAILED:
                            raise ModelProviderError(message=_event_error(event), status_code=502, model=self.name)
        except (ModelProviderError, ModelRateLimitError):
            raise
        except httpx.TimeoutException as e:
            raise ModelProviderError(message=f"ChatGPT request timed out: {e}", status_code=504, model=self.name) from e
        except httpx.HTTPError as e:
            raise ModelProviderError(message=f"Couldn't reach ChatGPT: {e}", model=self.name) from e
        if not completed:
            # The stream ended without its final event (a dropped connection): what arrived is
            # a cut-off reply, never to be used as the whole answer.
            raise ModelProviderError(message="ChatGPT's reply was cut off before it finished", status_code=502,
                                     model=self.name)
        return "".join(parts), usage


def _error_message(raw: bytes, status: int) -> str:
    """The backend's error text, with the reset time for plan usage limits."""
    try:
        err = json.loads(raw).get("error") or json.loads(raw).get("detail") or {}
    except (ValueError, AttributeError):
        err = {}
    if isinstance(err, str):
        err = {"message": err}
    message = err.get("message") or raw.decode("utf-8", "replace")[:300] or f"HTTP {status}"
    if status == 429 or err.get("type") == "usage_limit_reached":
        resets = err.get("resets_in_seconds")
        when = f" It resets in about {max(1, round(resets / 60))} minutes." if isinstance(resets, (int, float)) else ""
        return f"Your ChatGPT plan's usage limit is reached.{when} ({message})"
    if status in (401, 403):
        return f"ChatGPT rejected the sign-in ({message}). {chatgpt_auth.SIGN_IN_AGAIN}"
    return message


def _event_error(event: dict) -> str:
    response = event.get("response") or {}
    err = event.get("error") or response.get("error") or {}
    if isinstance(err, dict) and err.get("message"):
        return err["message"]
    reason = (response.get("incomplete_details") or {}).get("reason")
    return f"ChatGPT stopped the reply early ({reason})" if reason else "ChatGPT returned an error"


def _output_text(response: dict) -> str:
    return "".join(
        c.get("text", "")
        for item in response.get("output") or [] if item.get("type") == "message"
        for c in item.get("content") or [] if c.get("type") == "output_text"
    )


def _usage(raw: dict | None):
    from browser_use.llm.views import ChatInvokeUsage
    if not raw:
        return None
    return ChatInvokeUsage(
        prompt_tokens=raw.get("input_tokens", 0),
        prompt_cached_tokens=(raw.get("input_tokens_details") or {}).get("cached_tokens"),
        prompt_cache_creation_tokens=None,
        prompt_image_tokens=None,
        completion_tokens=raw.get("output_tokens", 0),
        total_tokens=raw.get("total_tokens", 0),
    )
