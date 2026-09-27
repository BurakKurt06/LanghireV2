"""Sign in with ChatGPT, so a ChatGPT Plus/Pro plan pays for LangHire's AI calls.

Uses the same OAuth sign-in and the same backend as OpenAI's Codex CLI
(https://github.com/openai/codex, codex-rs/login): usage counts against the
plan's Codex limits instead of per-token API billing. This backend isn't a
published API, so it can change without notice.

Tokens are kept in chatgpt_auth.json in the app data folder, separate from
the Codex CLI's own login.
"""

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from core.config import get_data_dir

ISSUER = "https://auth.openai.com"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # the Codex CLI's public OAuth client
SCOPE = "openid profile email offline_access"
CALLBACK_PORTS = (1455, 1457)  # the redirect ports registered for that client
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
ORIGINATOR = "codex_cli_rs"
# The models endpoint hides models newer than the client; any recent version works.
CODEX_CLIENT_VERSION = "0.999.0"
REFRESH_MARGIN_SECONDS = 300
LOGIN_TIMEOUT_SECONDS = 600

SIGN_IN_AGAIN = "Sign in to ChatGPT again in Settings → LLM."


class ChatGPTAuthError(RuntimeError):
    """Not signed in, or the sign-in expired or was revoked."""

    status_code = 401


def _auth_file():
    return get_data_dir() / "chatgpt_auth.json"


def _jwt_claims(token: str) -> dict:
    """The payload of a JWT, unverified (the tokens come straight from OpenAI over TLS)."""
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, AttributeError):
        return {}


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _account(tokens: dict) -> dict:
    """Email, plan and ChatGPT account id from the ID token (access token as a fallback)."""
    id_claims = _jwt_claims(tokens.get("id_token", ""))
    access_claims = _jwt_claims(tokens.get("access_token", ""))
    auth = id_claims.get("https://api.openai.com/auth") or access_claims.get("https://api.openai.com/auth") or {}
    profile = access_claims.get("https://api.openai.com/profile") or {}
    return {
        "email": id_claims.get("email") or profile.get("email") or "",
        "plan": auth.get("chatgpt_plan_type") or "",
        "account_id": auth.get("chatgpt_account_id") or "",
    }


def _save(tokens: dict, previous: dict | None = None) -> dict:
    """Store a token response, keeping values a refresh response leaves out."""
    data = {**(previous or {}), **{k: v for k, v in tokens.items() if v}}
    exp = _jwt_claims(data.get("access_token", "")).get("exp")
    if not exp and tokens.get("expires_in"):
        exp = time.time() + float(tokens["expires_in"])
    data["expires_at"] = float(exp or time.time() + 3600)
    data.update({k: v for k, v in _account(data).items() if v})
    path = _auth_file()
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def load() -> dict | None:
    """The stored sign-in, or None."""
    try:
        data = json.loads(_auth_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("refresh_token") else None


# ── Sign-in flow ────────────────────────────────────────────────────────────

class _Login:
    """One sign-in attempt: a one-shot local server waiting for OpenAI's redirect."""

    def __init__(self):
        self.state = secrets.token_urlsafe(32)
        self.verifier, challenge = _pkce()
        self.error = ""
        self.done = False
        self.server = self._bind()
        self.redirect_uri = f"http://127.0.0.1:{self.server.server_address[1]}/auth/callback"
        self.url = f"{ISSUER}/oauth/authorize?" + urllib.parse.urlencode({
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": self.redirect_uri,
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": self.state,
            "originator": ORIGINATOR,
        })
        self.started = time.monotonic()

    def _bind(self) -> HTTPServer:
        login = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/auth/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                ok, message = login.handle_callback(urllib.parse.parse_qs(parsed.query))
                body = _result_page(ok, message).encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        last = None
        for port in CALLBACK_PORTS:
            try:
                return HTTPServer(("127.0.0.1", port), Handler)
            except OSError as e:
                last = e
        raise RuntimeError(
            "Couldn't start the sign-in listener: ports 1455 and 1457 are in use. "
            "Close the Codex CLI or app if it's signing in, then try again."
        ) from last

    def handle_callback(self, query: dict) -> tuple[bool, str]:
        if (query.get("state") or [""])[0] != self.state:
            return False, "This sign-in link is out of date. Start the sign-in again from LangHire."
        if query.get("error"):
            self.error = (query.get("error_description") or query["error"])[0]
            self.done = True
            return False, self.error
        code = (query.get("code") or [""])[0]
        try:
            response = httpx.post(f"{ISSUER}/oauth/token", data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": self.verifier,
            }, timeout=30)
            response.raise_for_status()
            _save(response.json())
        except Exception as e:  # network or rejected code: report it on the page and in the app
            self.error = f"Sign-in failed while getting tokens: {e}"
            self.done = True
            return False, self.error
        self.done = True
        return True, "You're signed in. You can close this tab and go back to LangHire."

    def serve(self):
        self.server.timeout = 1
        while not self.done and time.monotonic() - self.started < LOGIN_TIMEOUT_SECONDS:
            self.server.handle_request()
        if not self.done:
            self.error = "Sign-in timed out. Start it again from LangHire."
            self.done = True
        self.server.server_close()

    def cancel(self):
        self.done = True


def _result_page(ok: bool, message: str) -> str:
    import html
    title = "Signed in to ChatGPT" if ok else "ChatGPT sign-in failed"
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title></head>"
        "<body style='font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
        f"<h2>{title}</h2><p>{html.escape(message)}</p></body></html>"
    )


_login_lock = threading.Lock()
_login: _Login | None = None


def start_login(open_browser: bool = True) -> str:
    """Start a sign-in and return the URL to open (opened in the default browser too)."""
    global _login
    with _login_lock:
        if _login and not _login.done:
            _login.cancel()
            time.sleep(1.1)  # let its server loop notice and release the port
        _login = _Login()
        threading.Thread(target=_login.serve, daemon=True).start()
        url = _login.url
    if open_browser:
        webbrowser.open(url)
    return url


def status() -> dict:
    data = load()
    login = _login
    return {
        "signed_in": data is not None,
        "email": (data or {}).get("email", ""),
        "plan": (data or {}).get("plan", ""),
        "pending": bool(login and not login.done),
        "error": login.error if login else "",
        "url": login.url if login and not login.done else "",
    }


def logout() -> None:
    global _login
    with _login_lock:
        if _login:
            _login.cancel()
            _login = None
    data = load()
    try:
        _auth_file().unlink()
    except FileNotFoundError:
        pass
    if data:
        try:  # best effort; the local copy is already gone
            httpx.post(f"{ISSUER}/oauth/revoke", json={
                "client_id": CLIENT_ID, "token": data["refresh_token"], "token_type_hint": "refresh_token",
            }, timeout=10)
        except Exception:
            pass


# ── Using the sign-in ───────────────────────────────────────────────────────

_refresh_lock = threading.Lock()


def credentials() -> tuple[str, str]:
    """(access token, ChatGPT account id), refreshed first when close to expiry."""
    with _refresh_lock:
        data = load()
        if data is None:
            raise ChatGPTAuthError("Not signed in to ChatGPT. " + SIGN_IN_AGAIN)
        if data.get("expires_at", 0) - time.time() < REFRESH_MARGIN_SECONDS:
            data = _refresh(data)
        return data["access_token"], data.get("account_id", "")


def _refresh(data: dict) -> dict:
    try:
        response = httpx.post(f"{ISSUER}/oauth/token", json={
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": data["refresh_token"],
        }, timeout=30)
    except httpx.HTTPError as e:
        raise ChatGPTAuthError(f"Couldn't reach OpenAI to renew the ChatGPT sign-in: {e}") from e
    if response.status_code in (400, 401, 403):
        raise ChatGPTAuthError("The ChatGPT sign-in expired or was revoked. " + SIGN_IN_AGAIN)
    response.raise_for_status()
    return _save(response.json(), previous=data)


def request_headers(access_token: str, account_id: str) -> dict:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "originator": ORIGINATOR,
        "OpenAI-Beta": "responses=experimental",
    }
    if account_id:
        headers["chatgpt-account-id"] = account_id
    return headers


def list_models() -> list[dict]:
    """Models the signed-in plan can use: [{id, name, efforts, default_effort}]."""
    access, account = credentials()
    response = httpx.get(
        f"{CODEX_BASE_URL}/models",
        params={"client_version": CODEX_CLIENT_VERSION},
        headers=request_headers(access, account),
        timeout=20,
    )
    response.raise_for_status()
    models = []
    for m in response.json().get("models", []):
        if m.get("visibility", "list") != "list":
            continue
        if "image" not in m.get("input_modalities", ["text", "image"]):
            continue  # the agent sends screenshots
        models.append({
            "id": m["slug"],
            "name": m.get("display_name") or m["slug"],
            "efforts": [e["effort"] for e in m.get("supported_reasoning_levels", []) if e.get("effort")],
            "default_effort": m.get("default_reasoning_level") or "",
        })
    return models
