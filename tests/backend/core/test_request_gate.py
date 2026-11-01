"""Exact authorities, credential approval, upload boundaries and broker semantics."""
import asyncio
import base64
from types import SimpleNamespace

import httpx
import pytest

from core import request_gate


def gate(mode="auto", trusted=lambda url: False):
    # This suite retains explicit coverage of the stricter protocol diagnostic.
    # Production defaults are exercised separately in test_trial_action_mode.
    return request_gate.RequestGate(SimpleNamespace(), mode, {"url": "https://jobs.example/1"}, trusted_site=trusted,
        strict_trial_protocols=True)


@pytest.mark.parametrize("url,expected", [
    ("https://EXAMPLE.com:443/path", "https://example.com"),
    ("https://example.com:9443/path", "https://example.com:9443"),
    ("https://www.example.com", "https://www.example.com"),
    ("https://example.com.", "https://example.com"),
    ("https://faß.de", "https://xn--fa-hia.de"),
    ("https://[::1]:9443/", "https://[::1]:9443"),
    ("https://example.com:0/", ""), ("https://example.com:99999/", ""),
    ("https://user:pass@example.com/", ""), ("file:///tmp/a", ""),
    ("javascript:alert(1)", ""), ("https://example.com%2eevil/", ""),
])
def test_authorities_are_exact_and_match_the_http_client(url, expected):
    assert request_gate.origin(url) == expected


@pytest.mark.asyncio
async def test_one_approval_cannot_authorize_the_next_question():
    g = gate()
    ask = {"kind": "credentials", "origin": "https://jobs.example", "url": "https://jobs.example/login"}
    first = asyncio.create_task(g._ask(ask))
    await asyncio.sleep(0)
    ident = next(r["id"] for r in request_gate.pending() if r["job_url"] == g.job["url"])
    assert request_gate.decide([ident], True)
    assert not request_gate.decide([ident], True)
    assert await first
    second = asyncio.create_task(g._ask(ask))
    await asyncio.sleep(0)
    assert not request_gate.decide([ident], True)
    ident2 = next(r["id"] for r in request_gate.pending() if r["job_url"] == g.job["url"])
    assert ident2 != ident
    assert not request_gate.decide([ident2, "bad-id"], True)
    assert request_gate.decide([ident2], False)
    assert not await second


@pytest.mark.asyncio
async def test_expired_or_cancelled_approvals_are_removed(monkeypatch):
    g = gate()
    monkeypatch.setattr(request_gate, "WAIT_SECONDS", .01)
    assert not await g._ask({"kind": "credentials"})
    assert not request_gate.pending()
    assert not request_gate.decide(["a" * 32], True)


@pytest.mark.asyncio
async def test_trial_never_asks_for_or_sends_account_details():
    g=gate('trial')
    assert not await g.approve_credentials('https://boards.greenhouse.io/acme/jobs/1')
    assert request_gate.pending()==[] and not g.credentials_approved('https://boards.greenhouse.io/acme/jobs/1')


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["trial", "auto"])
async def test_the_employers_own_site_gets_account_details_without_a_question(mode):
    g = gate(mode, trusted=lambda url: "acme" in url)
    assert (await g.approve_credentials("https://acme.wd5.myworkdayjobs.com/en-US/careers/login")) is True
    assert request_gate.pending() == []


@pytest.mark.asyncio
async def test_full_auto_never_waits_and_gives_an_unrecognised_site_nothing():
    g = gate("auto")
    assert not await g.approve_credentials("https://login.lookalike.example/")
    assert request_gate.pending() == [] and not g.credentials_approved("https://login.lookalike.example/")


@pytest.mark.parametrize("mode", ["training", "nonsense"])
def test_training_is_a_trial(mode):
    assert gate(mode).mode == "trial"


def test_broker_never_follows_redirects_or_uses_environment_proxies(monkeypatch):
    seen = []
    def handler(req):
        seen.append(req)
        return httpx.Response(302, headers={"Location": "https://other.example/send"})
    g = gate()
    g.healthy = lambda: True
    g.transport_factory = lambda: httpx.MockTransport(handler)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    code, _, _ = g._fetch({"url": "https://jobs.example/1", "method": "POST", "hasPostData": True, "postData": "candidate"})
    assert code == 302 and len(seen) == 1 and seen[0].content == b"candidate"


def test_connections_are_never_shared_between_host_names():
    """The pinned transport connects by address: a pool shared between names could reuse a connection
    whose certificate was checked for another name on the same address."""
    made = []
    g = gate()
    g.healthy = lambda: True
    g.transport_factory = lambda: made.append(1) or httpx.MockTransport(lambda req: httpx.Response(200, content=b"x"))
    for url in ("https://a.example/1", "https://a.example/2", "https://b.example/1"):
        g._fetch({"url": url, "method": "GET"})
    assert len(made) == 2 and set(g._transports) == {"a.example", "b.example"}


def test_a_closed_browser_cannot_send_a_request():
    g = gate()
    with pytest.raises(RuntimeError, match="closed"):
        g._fetch({"url": "https://jobs.example/1", "method": "POST"})


@pytest.mark.parametrize("req", [
    {"hasPostData": True},
    {"hasPostData": True, "postData": "missing-file", "headers": {"Content-Type": "multipart/form-data; boundary=x"}},
])
def test_unreadable_bodies_are_not_silently_truncated(req):
    g = gate()
    g.healthy = lambda: True
    with pytest.raises(ValueError, match="body is unavailable|could not be read completely"):
        g._fetch({"url": "https://jobs.example/1", "method": "POST", **req})


@pytest.mark.asyncio
async def test_page_filenames_cannot_read_files_outside_the_explicit_upload_list(tmp_path):
    g = gate()
    outside = tmp_path / "private.txt"
    outside.write_text("DO NOT SEND", encoding="utf-8")
    prefix = b'Content-Disposition: form-data; name="resume"; filename="private.txt"\r\n\r\n'
    event = {"request": {"postDataEntries": [{"bytes": base64.b64encode(prefix).decode()}, {}]}}
    with pytest.raises(ValueError, match="missing or ambiguous"):
        await g._complete_files(event, None)


@pytest.mark.asyncio
async def test_ambiguous_upload_names_are_refused_and_bytes_are_fixed_when_read(tmp_path):
    g = gate()
    a, b = tmp_path / "a" / "resume.txt", tmp_path / "b" / "resume.txt"
    for p, text in ((a, "FIRST"), (b, "SECOND")):
        p.parent.mkdir()
        p.write_text(text, encoding="utf-8")
    prefix = b'Content-Disposition: form-data; name="resume"; filename="resume.txt"\r\n\r\n'
    def event():
        return {"request": {"postDataEntries": [{"bytes": base64.b64encode(prefix).decode()}, {}]}}
    g.allowed_files = [str(a), str(b)]
    with pytest.raises(ValueError, match="ambiguous"):
        await g._complete_files(event(), None)
    g.allowed_files = [str(a)]
    request = event()
    manifest = await g._complete_files(request, None)
    a.write_text("CHANGED LATER", encoding="utf-8")
    assert base64.b64decode(request["request"]["postDataEntries"][1]["bytes"]) == b"FIRST"
    assert manifest[0]["bytes"] == 5


@pytest.mark.asyncio
async def test_old_password_is_reused_only_after_a_disclosed_approval(data_dir):
    from core import secret_store
    g = gate('trial')
    original = secret_store.site_password("acme.example")
    url = "https://acme.example/apply"
    assert not await g.approve_credentials(url)
    assert not request_gate.pending()
    assert not secret_store.site_password(g.credential_key(url), create=False)
    assert g.legacy_sources == {}
    assert original == secret_store.site_password("acme.example", create=False)
    assert original not in (data_dir / "site_credentials.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_tenant_matched_by_name_never_gets_the_old_shared_password(data_dir):
    """Recognised by the employer's name only (not the job's own site): new account, own password."""
    from core import secret_store
    secret_store.site_password("acme.wd5.myworkdayjobs.com/acme")
    g = gate("auto", trusted=lambda url: True)
    g.job = {"url": "https://www.linkedin.com/jobs/view/1"}
    assert await g.approve_credentials("https://acme.wd5.myworkdayjobs.com/acme/login")
    assert g.legacy_sources == {}


class FakeCDP:
    """Records what the gate does with paused requests (no browser)."""
    def __init__(self, fail_fulfill=False):
        self.calls, self.fail_fulfill = [], fail_fulfill
        fetch = SimpleNamespace(fulfillRequest=self._fulfill, failRequest=self._fail)
        self.send = SimpleNamespace(Fetch=fetch)

    async def _fulfill(self, params, session_id=None):
        if self.fail_fulfill:
            raise RuntimeError("Invalid InterceptionId.")   # the page dropped it (navigated away)
        self.calls.append(("fulfill", params["requestId"], params["responseCode"]))

    async def _fail(self, params, session_id=None):
        self.calls.append(("fail", params["requestId"], params["errorReason"]))


def paused(ident, url, method="GET", body=""):
    return {"requestId": ident, "request": {"url": url, "method": method, "headers": {},
                                            "hasPostData": bool(body), "postData": body}}


def running_gate(mode, handler, fail_fulfill=False):
    g = gate(mode)
    g.client, g.healthy = FakeCDP(fail_fulfill), (lambda: True)
    g.transport_factory = lambda: httpx.MockTransport(handler)
    return g


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["trial", "auto"])
async def test_page_loads_and_step_saves_go_through_without_a_question(mode):
    """Moving between pages is automatic in every mode; the final submit is stopped at the action."""
    sent = []
    g = running_gate(mode, lambda req: sent.append((req.method, req.url.path)) or httpx.Response(200, content=b"ok"))
    g.job['url'] = 'https://jobs.example/apply'
    await g._request({**paused("1", "https://jobs.example/apply"), 'resourceType':'Document'}, "s")
    await g._request(paused("2", "https://cdn.example/app.js"), "s")
    await g._request(paused("3", "https://jobs.example/step/2", "POST", "answers"), "s")
    assert sent == ([("GET", "/apply"), ("GET", "/app.js"), ("POST", "/step/2")] if mode == 'auto' else [('GET','/apply'),('GET','/app.js')])
    assert [c[0] for c in g.client.calls] == (['fulfill']*3 if mode == 'auto' else ['fulfill','fulfill','fail'])
    assert request_gate.pending() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["https://mail.google.com/mail/u/0/", "https://gmail.googleapis.com/gmail/v1/users/me",
                                 "https://www.googleapis.com/gmail/v1/users/me/messages", "file:///C:/secret.txt"])
async def test_gmail_and_non_web_addresses_never_load(url):
    sent = []
    g = running_gate("auto", lambda req: sent.append(req) or httpx.Response(200))
    await g._request(paused("1", url), "s")
    assert sent == [] and g.client.calls == [("fail", "1", "BlockedByClient")]


@pytest.mark.asyncio
async def test_one_failing_resource_fails_alone_and_the_page_goes_on():
    """An ad server that times out, a local address, a huge video: only that request fails."""
    def handler(req):
        if req.url.host == "ads.example":
            raise httpx.ConnectTimeout("timed out")
        return httpx.Response(200, content=b"ok")
    g = running_gate("auto", handler)
    await g._request(paused("1", "https://ads.example/pixel.gif"), "s")
    await g._request(paused("2", "https://jobs.example/apply"), "s")
    assert g.client.calls == [("fail", "1", "Failed"), ("fulfill", "2", 200)]
    assert not g.failed


@pytest.mark.asyncio
async def test_a_request_the_page_already_dropped_is_not_a_failure():
    g = running_gate("auto", lambda req: httpx.Response(200, content=b"ok"), fail_fulfill=True)
    await g._request(paused("1", "https://jobs.example/apply"), "s")
    assert not g.failed and g.client.calls == [("fail", "1", "Failed")]


def test_both_agent_purposes_create_their_own_protected_proxy(tmp_path, monkeypatch):
    from core import shared_config, egress_proxy
    monkeypatch.setattr(shared_config, "selected_browser", lambda settings: (None, tmp_path / "browser"))
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {})
    for purpose in ("application", "collection"):
        kwargs = shared_config.build_browser_kwargs(purpose=purpose)
        request_gate.RequestGate(SimpleNamespace(), "trial", {"url": "https://jobs.example"}, owner=purpose + ":1")
        assert "--disable-extensions" in kwargs["args"]
        assert "--host-resolver-rules=MAP * ~NOTFOUND" in kwargs["args"]
        assert not egress_proxy._servers[("owner", purpose + ":1")].permitted()


@pytest.mark.asyncio
async def test_unclosable_worker_remains_paused_and_network_blocked_without_killing_page():
    from unittest.mock import AsyncMock
    g=gate('trial')
    enable,blocked=AsyncMock(),AsyncMock()
    close=AsyncMock(side_effect=RuntimeError({'code':-32602,'message':"Specified target doesn't support closing"}))
    resume=AsyncMock()
    g.client=SimpleNamespace(send=SimpleNamespace(Network=SimpleNamespace(enable=enable,setBlockedURLs=blocked),
        Target=SimpleNamespace(closeTarget=close),Runtime=SimpleNamespace(runIfWaitingForDebugger=resume)))
    g._fatal=AsyncMock()
    await g._attached({'targetInfo':{'type':'worker','targetId':'worker-local'},'sessionId':'worker-session'})
    blocked.assert_awaited_once_with(params={'urls':['*']},session_id='worker-session')
    assert g.held_workers=={'worker-session'}
    resume.assert_not_awaited();g._fatal.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_network_guard_failure_still_closes_browser():
    from unittest.mock import AsyncMock
    g=gate('trial');g._fatal=AsyncMock()
    g.client=SimpleNamespace(send=SimpleNamespace(Network=SimpleNamespace(enable=AsyncMock(side_effect=RuntimeError('CDP disconnected')))))
    await g._attached({'targetInfo':{'type':'worker','targetId':'local'},'sessionId':'s'})
    g._fatal.assert_awaited_once()
