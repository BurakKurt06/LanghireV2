"""The application browser's network: every request is paused here and fetched by the backend.

The browser itself can't connect anywhere (its proxy refuses everything). The backend
fetches each request from a checked public address (never this computer or the local
network, never Gmail) and hands the response back to the page. Full Auto allows page
loads and step saves and records recognized final submits. Normal Trial uses the
same transport, with the agent's final-submit action blocked by SubmitGuard.
Site scripts and intermediate steps can send data or submit. The stricter ATS
contracts remain available explicitly for protocol diagnostics. Collection retains
its separate read policy. Credentials stay scoped to recognized employer sites.

Workers and WebSockets are deliberately unsupported in application browsers.
New targets are paused before execution on a separate CDP connection. Failure
closes this browser's proxy circuit; it never falls back to unguarded browsing.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
import uuid
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import egress_proxy, job_page, secret_store
from .collection_policy import is_write
from .trial_policy import TrialPolicy

_pending: dict[str, dict] = {}
_lock = threading.RLock()
MAX_PENDING = 64
MAX_INFLIGHT_BYTES = 40_000_000
MAX_INFLIGHT_REQUESTS = 256  # a page load can ask for many resources at once
WAIT_SECONDS = 1500
_console = False
_console_lock = asyncio.Lock()


def use_console_reviews():
    global _console
    if not sys.stdin.isatty():
        raise RuntimeError("Reviewed browser runs need the desktop app or an interactive terminal")
    _console = True


def origin(url: str) -> str:
    try:
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
            return ""
        host = httpx.URL(url).raw_host.decode("ascii").lower().rstrip(".")
        if any(c.isspace() for c in host) or "%" in host:
            return ""
        if p.port == 0:
            return ""
        port = p.port or (443 if p.scheme == "https" else 80)
        host = f"[{host}]" if ":" in host else host
        return f"{p.scheme}://{host}" + (f":{port}" if port != (443 if p.scheme == "https" else 80) else "")
    except (ValueError, UnicodeError, httpx.InvalidURL):
        return ""


def pending() -> list[dict]:
    with _lock:
        for ident, request in list(_pending.items()):
            if not request["loop"].is_running():
                _pending.pop(ident, None)
        return [dict(r["public"]) for r in _pending.values() if not r["decided"] and not r["future"].done()]


def decide(ids: list[str], approved: bool) -> bool:
    """Only the displayed immutable request IDs; never a wildcard/future grant."""
    with _lock:
        if not ids or len(ids) > MAX_PENDING or len(set(ids)) != len(ids):
            return False
        if any(i not in _pending or _pending[i]["decided"] or _pending[i]["future"].done()
               or not _pending[i]["loop"].is_running() for i in ids):
            return False
        for i in ids:
            r = _pending[i]
            r["decided"] = True
            def deliver(r=r):
                if not r["future"].done():
                    r["future"].set_result(approved)
            r["loop"].call_soon_threadsafe(deliver)
    return True


class RequestGate:
    def __init__(self, browser, mode, job, *, owner="", redact=lambda s: s, transport_factory=None, trusted_site=None,
                 document_hosts=None, collection_policy=None, strict_trial_protocols=False):
        self.browser, self.job = browser, job
        self.mode = "auto" if mode == "auto" else "trial"  # "training" was merged into trial
        self.owner, self.redact = owner, redact
        self.document_hosts = frozenset(document_hosts) if document_hosts is not None else None
        self.collection_policy = collection_policy
        # Normal Trial is action-based, as requested. This internal opt-in keeps
        # the stricter protocol diagnostics available; the app does not set it.
        self.strict_trial_protocols = bool(strict_trial_protocols)
        # Collection deliberately keeps its separate compatibility policy.
        self.trial_blocked_operation = ""
        self.transport_factory = transport_factory or job_page._PinnedTransport
        # The employer's own application site by the app's rules: the job's own site, or its
        # applicant-system tenant (cli/apply_jobs.employer_site).
        self.trusted_site = trusted_site or (lambda url: False)
        self.trial_policy = TrialPolicy(lambda url: self.trusted_site(url), lambda: self.allowed_files, job.get("url", ""))
        self.client = None
        self.ready = False
        self.failed = False
        self.closed = False
        self.sessions = set()
        self.targets = set()
        self.held_workers = set()
        self.tasks = set()
        self.ids = set()
        self.credential_sites = set()
        self.legacy_sources = {}
        self.sent = 0
        self.blocked = 0
        self.agent = None
        self.allowed_files = []
        self.active_requests = 0
        self.active_bytes = 0
        self._http_slots = asyncio.Semaphore(8)  # page loads fetch many resources at once
        self._transports = {}
        self._site_lock = asyncio.Lock()
        self._configure_lock = asyncio.Lock()
        if owner:
            # The browser never connects out, including non-HTTP channels.
            # Approved HTTP is fetched by the backend, not continueRequest.
            egress_proxy.protect(owner, lambda: False)

    @property
    def live_trial(self):
        return self.mode == "trial" and self.collection_policy is None

    @property
    def strict_trial(self):
        return self.live_trial and self.strict_trial_protocols

    def healthy(self):
        task = getattr(self.client, "_message_handler_task", None)
        return self.ready and not self.failed and not self.closed and task is not None and not task.done()

    async def _ask(self, public):
        if self.closed or self.failed:
            return False
        if _console:
            async with _console_lock:
                print("\nWebsite approval:", self.job.get("title", ""), public.get("description", ""))
                print(public.get("method", ""), public.get("url", ""))
                if public.get("body"):
                    print(public["body"])
                for file in public.get("files", []):
                    print("File:", file["name"], file["bytes"], "bytes", file["sha256"])
                expected = public["origin"] if public.get("kind") in ("automatic_site", "credentials") else "allow"
                try:
                    reply = await asyncio.wait_for(asyncio.to_thread(input, f"Type {expected!r} to allow; anything else blocks: "), WAIT_SECONDS)
                    return reply.strip() == expected
                except (EOFError, asyncio.TimeoutError):
                    return False
        loop = asyncio.get_running_loop()
        ident = uuid.uuid4().hex
        public = {"id": ident, "job_url": self.job["url"], "title": self.job.get("title", ""),
                  "company": self.job.get("company", ""), "mode": self.mode, **public}
        future = loop.create_future()
        with _lock:
            if len(self.ids) >= MAX_PENDING or len(_pending) >= MAX_PENDING * 4:
                return False
            _pending[ident] = {"public": public, "future": future, "loop": loop, "decided": False}
            self.ids.add(ident)
        try:
            return await asyncio.wait_for(future, WAIT_SECONDS) is True
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return False
        finally:
            with _lock:
                _pending.pop(ident, None)
                self.ids.discard(ident)

    async def approve_credentials(self, url):
        if self.collection_policy is not None:
            return False  # collection cannot sign in, disclose credentials or open an approval prompt
        delegated = self.trial_policy.identity.scope(url)
        if delegated:
            # Transaction-scoped provider pages never enter the origin-wide
            # approval cache. Another Auth0 tenant/state gets no authority.
            return True
        key = (origin(url), job_page.tenant(url))
        if not key[0].startswith("https://") or urlsplit(url).port not in (None, 443):
            return False
        async with self._site_lock:
            if key in self.credential_sites:
                return True
            host = urlsplit(url).hostname.lower().rstrip(".").removeprefix("www.")
            legacy = host + ("/" + key[1] if key[1] and key[1] not in host else "")
            destination = self.credential_key(url)
            legacy = (legacy if not secret_store.site_password(destination, create=False)
                      and secret_store.site_password(legacy, create=False) else "")
            if self.trusted_site(url):
                # The employer's own site: no question. The old shared password only goes back to
                # the job's own origin and tenant (a tenant matched by name could be a lookalike).
                job_url = self.job.get("url", "")
                if key != (origin(job_url), job_page.tenant(job_url)):
                    legacy = ""
                ok = True
            else:
                ok = False  # every unattended mode refuses unknown credential destinations without a prompt
            if ok:
                self.credential_sites.add(key)
                if legacy:
                    self.legacy_sources[destination] = legacy
            return ok

    @staticmethod
    def credential_key(url):
        tenant = job_page.tenant(url)
        return origin(url) + ("/" + tenant if tenant else "")

    def credential_scope(self, url):
        return self.trial_policy.identity.scope(url) or url

    def credentials_approved(self, url):
        return bool(self.trial_policy.identity.scope(url)) or (origin(url), job_page.tenant(url)) in self.credential_sites

    async def wait_idle(self):
        while (self.ids or self.active_requests) and not self.closed and not self.failed:
            await asyncio.sleep(.1)
        if not self.healthy():
            raise RuntimeError("The browser request gate stopped; application automation was refused")

    async def wait_forwarded_idle(self):
        """Drain requests that can reach transport after collection seals its policy.

        A site can endlessly retry a refused XHR, keeping CDP request IDs busy.
        Those refusals carry no data to transport and must not stall the reader.
        Callers must seal first, so queued requests cannot gain new read authority.
        """
        if self.collection_policy is None or not self.collection_policy.sealed:
            raise RuntimeError("Collection reads must be sealed before draining transport")
        while self.active_requests and not self.closed and not self.failed:
            await asyncio.sleep(.1)
        if not self.healthy():
            raise RuntimeError("The browser request gate stopped; collection was refused")

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def start(self):
        from cdp_use import CDPClient
        self.client = CDPClient(self.browser.cdp_url)
        self.browser.cdp_client._cdp_request_timeout_s = WAIT_SECONDS + 60
        for event in ("NavigateToUrlEvent", "ClickElementEvent", "ClickCoordinateEvent", "TypeTextEvent"):
            os.environ["TIMEOUT_" + event] = str(WAIT_SECONDS + 60)
        await self.client.start()
        # Callbacks must return immediately: cdp-use reads command replies on
        # the same task which dispatches events.
        self.client.register.Target.attachedToTarget(lambda e, s: self._spawn(self._attached(e)))
        self.client.register.Fetch.requestPaused(lambda e, s: self._spawn(self._request(e, s)))
        await self.client.send.Target.setAutoAttach(params={"autoAttach": True, "flatten": True,
                                                            "waitForDebuggerOnStart": True})
        targets = (await self.client.send.Target.getTargets())["targetInfos"]
        for target in targets:
            if target["type"] == "page":
                if target.get("url") not in ("", "about:blank", "chrome://newtab/"):
                    raise RuntimeError("Application browser must start with a blank page")
                # Auto-attach includes existing pages. Attaching again creates
                # two Fetch interceptors for the same request.
                for _ in range(100):
                    if target["targetId"] in self.targets:
                        break
                    if self.failed:
                        raise RuntimeError("Request interception could not be installed")
                    await asyncio.sleep(.05)
                else:
                    raise RuntimeError("The browser did not attach its initial page")
        if self.collection_policy is not None:
            cookies = await self.client.send.Storage.getCookies()
            self.collection_policy.snapshot_cookies(cookies["cookies"])
        self.ready = True
        if not self.healthy():
            raise RuntimeError("The browser request gate did not start")

    async def _attached(self, event):
        info, sid = event["targetInfo"], event["sessionId"]
        try:
            if info["type"] in ("worker", "shared_worker", "service_worker"):
                # Chromium exposes Network but not Fetch on worker sessions,
                # and closeTarget is unsupported for some dedicated workers.
                # Keep them paused with all network blocked; do not kill the
                # usable page merely because the optional worker can't close.
                # The independent deny-all proxy is still in force as well.
                await self.client.send.Network.enable(session_id=sid)
                await self.client.send.Network.setBlockedURLs(params={"urls": ["*"]}, session_id=sid)
                try:
                    await self.client.send.Target.closeTarget(params={"targetId": info["targetId"]})
                except RuntimeError as error:
                    if "Specified target doesn't support closing" not in str(error):
                        raise
                    self.held_workers.add(sid)
                return  # never resume or detach an unbrokered worker
            if info["type"] not in ("page", "iframe"):
                await self.client.send.Target.closeTarget(params={"targetId": info["targetId"]})
                return
            if info["targetId"] in self.targets:
                await self.client.send.Runtime.runIfWaitingForDebugger(session_id=sid)
                return
            await self._configure(sid)
            self.targets.add(info["targetId"])
            await self.client.send.Runtime.runIfWaitingForDebugger(session_id=sid)
        except Exception:
            await self._fatal()

    async def _configure(self, sid):
        async with self._configure_lock:
            await self._configure_locked(sid)

    async def _configure_locked(self, sid):
        if sid in self.sessions:
            return
        self.sessions.add(sid)
        try:
            await self.client.send.Network.enable(session_id=sid)
            await self.client.send.Network.setBypassServiceWorker(params={"bypass": True}, session_id=sid)
            await self.client.send.Network.setCacheDisabled(params={"cacheDisabled": True}, session_id=sid)
            await self.client.send.Network.setBlockedURLs(params={"urls": ["ws://*", "wss://*"]}, session_id=sid)
            await self.client.send.Fetch.enable(
                params={"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}, session_id=sid)
            await self.client.send.Target.setAutoAttach(
                params={"autoAttach": True, "flatten": True, "waitForDebuggerOnStart": True}, session_id=sid)
        except Exception:
            self.failed = True
            raise

    async def _fatal(self):
        self.failed = True
        if self.agent is not None:
            self.agent.stop()
        # Leave interception installed until the browser exits. Detaching a
        # debugger first would release paused requests.
        try:
            await self.browser.kill()
        except Exception:
            pass

    async def _request(self, event, sid):
        ident = event["requestId"]
        reserved = 0
        counted = False
        try:
            req = event["request"]
            if self.collection_policy is not None and not self.collection_policy.allows(req, event.get("resourceType")):
                self.blocked += 1
                # Nothing is sent either way. A refused script write gets an empty 403, not a network
                # error: LinkedIn re-sends a POST that fails at the network level at once (34,000
                # refusals in one 54 s collection, 2026-10-31). Refused reads and pages fail as before.
                answer = (403, [], b"") if is_write(req) and event.get("resourceType") != "Document" else None
                await self._settle(ident, sid, answer)
                return
            def size():
                entries = req.get("postDataEntries") or []
                if entries and all(isinstance(e.get("bytes"), str) for e in entries):
                    # Binary uploads have an authoritative byte representation;
                    # CDP's optional text rendering can contain surrogates.
                    return sum(len(e["bytes"]) for e in entries)
                return len((req.get("postData") or "").encode()) + sum(len(e.get("bytes", "")) for e in req.get("postDataEntries") or [])
            try:
                reserved = size()
            except (UnicodeError, ValueError, TypeError):
                # CDP can contain an unpaired surrogate from a page script.
                # Refuse that request unchanged; never corrupt form answers or
                # take down every other request in the application browser.
                self.blocked += 1
                await self._settle(ident, sid, None)
                return
            if self.active_requests >= MAX_INFLIGHT_REQUESTS or self.active_bytes + reserved > MAX_INFLIGHT_BYTES:
                self.blocked += 1
                await self.client.send.Fetch.failRequest(params={"requestId": ident, "errorReason": "BlockedByClient"}, session_id=sid)
                return
            self.active_requests += 1
            self.active_bytes += reserved
            counted = True
            try:
                await self._complete_files(event, sid)
            except ValueError:
                # An upload the app can't fill in completely is never sent truncated: this request fails.
                self.blocked += 1
                await self._settle(ident, sid, None)
                return
            self.active_bytes -= reserved
            reserved = 0
            try:
                reserved = size()
            except (UnicodeError, ValueError, TypeError):
                self.blocked += 1
                await self._settle(ident, sid, None)
                return
            self.active_bytes += reserved
            if self.active_bytes > MAX_INFLIGHT_BYTES:
                self.blocked += 1
                await self._settle(ident, sid, None)
                return
            dest = origin(req["url"])
            host = urlsplit(dest).hostname or ""
            if self.document_hosts is not None and event.get("resourceType") == "Document" and (
                host not in self.document_hosts or not dest.startswith("https://") or urlsplit(dest).port not in (None, 443)
            ):
                self.blocked += 1
                await self._settle(ident, sid, None)
                return
            mail = any(host == name or host.endswith("." + name) for name in (
                "mail.google.com", "gmail.com", "inbox.google.com", "gmail.googleapis.com", "content-gmail.googleapis.com"))
            mail = mail or (host == "www.googleapis.com" and "/gmail/" in req["url"].lower())
            # These protections apply equally to Full Auto and normal Trial.
            if not (dest and self.healthy() and not mail):
                self.blocked += 1
                await self.client.send.Fetch.failRequest(params={"requestId": ident, "errorReason": "BlockedByClient"}, session_id=sid)
                return
            if self.strict_trial:
                verdict = self.trial_policy.check(req)
                if not verdict.allowed:
                    self.blocked += 1
                    # Third-party telemetry should not end a usable form. A
                    # refused employer operation or final request must be visible.
                    if verdict.final or event.get("resourceType") == "Document" or (
                        (self.trusted_site(req["url"]) or self.trial_policy.contracts.bound(req["url"])
                         or self.trial_policy.contracts.oracle.bound(req["url"])) and event.get("resourceType") != "Ping"
                        and not re.search(r"/(?:analytics|telemetry|logging|microscope)(?:/|$)", urlsplit(req["url"]).path)):
                        self.trial_blocked_operation = verdict.reason
                    await self._settle(ident, sid, None)
                    return
            try:
                async with self._http_slots:
                    response = await asyncio.to_thread(self._fetch, req)
                if self.strict_trial and 200 <= response[0] < 300:
                    self.trial_policy.contracts.observe(req, response[1], response[2], event.get("resourceType") == "Document")
                if self.collection_policy is None:
                    # Auto needs the same scoped credential delegation as Trial.
                    # Redirects are observed before the browser follows them.
                    self.trial_policy.identity.observe(req, response[0], response[1], response[2], event.get("resourceType") == "Document")
                if self.collection_policy is not None:
                    self.collection_policy.observe(req["url"], response[1], response[2])
            except Exception:
                # One unreachable, refused or oversized resource (an ad server, a local address, a long
                # video) fails on its own and the page goes on. The browser can't connect by itself.
                self.blocked += 1
                await self._settle(ident, sid, None)
                return
            self.sent += 1
            await self._settle(ident, sid, response)
        except Exception as error:
            # A malformed event or a broken gate must never release a request.
            self.failure = self.redact(str(error))[:300]
            await self._fatal()
        finally:
            if counted:
                self.active_requests -= 1
                self.active_bytes -= reserved

    async def _complete_files(self, event, sid):
        """Chrome omits file bytes in postDataEntries. Fill them from an unambiguous
        matching authorized file; never send a truncated resume or read an arbitrary
        filesystem path supplied by the page.
        """
        entries = event["request"].get("postDataEntries") or []
        if not any("bytes" not in e for e in entries):
            return []
        def read_authorized():
            candidates, total = [], 0
            for path in self.allowed_files:
                file = Path(path)
                if not file.is_file():
                    continue
                with file.open("rb") as source:
                    data = source.read(25_000_001)
                total += len(data)
                if total > 25_000_000:
                    raise ValueError("Authorized uploads exceed 25 MB")
                candidates.append({"name": file.name, "bytes": base64.b64encode(data).decode(), "size": len(data)})
            return candidates
        candidates = await asyncio.to_thread(read_authorized)
        prefix = b""
        manifest = []
        for entry in entries:
            if "bytes" in entry:
                prefix = base64.b64decode(entry["bytes"], validate=True)
                continue
            names = re.findall(rb'filename="([^"]*)"', prefix)
            if not names:
                raise ValueError("Upload field is ambiguous; submit this file manually")
            name = names[-1].decode("utf-8")
            matching = {f["bytes"]: f for f in candidates if f["name"].replace('"', '%22').replace('\r', '%0D').replace('\n', '%0A') == name}
            if len(matching) != 1:
                raise ValueError("Upload file is missing or ambiguous; submit it manually")
            file = next(iter(matching.values()))
            data = base64.b64decode(file["bytes"], validate=True)
            entry["bytes"] = file["bytes"]
            manifest.append({"name": file["name"], "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        return manifest

    def _transport(self, host):
        # One connection pool per host name: a connection opened (and its certificate checked) for one
        # name is never reused for another name that happens to share its address.
        with _lock:
            transport = self._transports.get(host)
            if transport is None:
                transport = self._transports[host] = self.transport_factory()
            return transport

    def _fetch(self, req):
        if not self.healthy():
            raise RuntimeError("The review browser closed before the request was sent")
        if self.strict_trial:
            if not self.trial_policy.check(req).allowed:
                raise ValueError("Trial refused this server operation before transport")
            if self.trial_policy.contracts.upload_destination(req):
                from .trial_policy import _bytes
                if not self.trial_policy.contracts.upload(req, _bytes(req), consume=True):
                    raise ValueError("Trial upload grant was spent or expired")
        headers = {k: v for k, v in req.get("headers", {}).items()
                   if not k.startswith(":") and k.lower() not in {"host", "connection", "proxy-authorization", "content-length"}}
        if self.collection_policy is not None:
            # Scripts cannot smuggle a second payload in a Referer or custom
            # header while reloading an otherwise permitted read URL.
            if self.collection_policy.dynamic_read(req["url"]):
                # The board's GET APIs need its CSRF/session and protocol
                # headers. These stay scoped to configured board/CDN hosts.
                headers = {k:v for k,v in headers.items() if k.lower() not in {"referer", "referrer"}}
            else:
                headers = {k:v for k,v in headers.items() if k.lower() in {
                    "accept", "accept-language", "cookie", "user-agent", "origin", "range",
                    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site"}}
            for key in list(headers):
                if key.lower() == "cookie":
                    headers[key] = self.collection_policy.read_cookie_header(req["url"], headers[key])
        body = None
        if req.get("hasPostData"):
            entries = req.get("postDataEntries")
            if entries and all(isinstance(e.get("bytes"), str) for e in entries):
                body = b"".join(base64.b64decode(e["bytes"], validate=True) for e in entries)
            elif isinstance(req.get("postData"), str):
                if "multipart/form-data" in next((v for k, v in headers.items() if k.lower() == "content-type"), ""):
                    raise ValueError("Upload payload could not be read completely; submit this file manually")
                body = req["postData"].encode()
            else:
                raise ValueError("Request body is unavailable; manual submission is required")
            if len(body) > 25_000_000:
                raise ValueError("Request body exceeds the safe broker limit")
        # Straight to the transport: no environment proxies, no cookie jar of its own (the browser's
        # Cookie header goes as it is), no redirects (each is a fresh browser request) and no TLS exceptions.
        request = httpx.Request(req["method"], req["url"], headers=headers, content=body,
                                extensions={"timeout": httpx.Timeout(25).as_dict()})
        response = self._transport(request.url.host).handle_request(request)
        try:
            deadline = time.monotonic() + 30
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                if self.closed or self.failed or time.monotonic() > deadline:
                    raise TimeoutError("Website request stopped or exceeded its total time limit")
                size += len(chunk)
                if size > 15_000_000:
                    raise ValueError("Website response exceeds the safe broker limit")
                chunks.append(chunk)
            excluded = {b"transfer-encoding", b"content-encoding", b"content-length", b"connection"}
            result_headers = [{"name": k.decode("latin-1"), "value": v.decode("latin-1")}
                              for k, v in response.headers.raw if k.lower() not in excluded]
            return response.status_code, result_headers, b"".join(chunks)
        finally:
            response.close()

    async def _settle(self, ident, sid, response):
        """Hand the backend's response to the page, or fail the request (response None).

        A request the page already dropped (it navigated away) can't be settled any more;
        that isn't a fault of the gate.
        """
        try:
            if response is None:
                await self.client.send.Fetch.failRequest(params={"requestId": ident, "errorReason": "Failed"},
                                                         session_id=sid)
                return
            code, headers, body = response
            await self.client.send.Fetch.fulfillRequest(params={"requestId": ident, "responseCode": code,
                "responseHeaders": headers, "body": base64.b64encode(body).decode()}, session_id=sid)
        except Exception:
            if not self.healthy():
                raise
            if response is not None:
                try:
                    await self.client.send.Fetch.failRequest(params={"requestId": ident, "errorReason": "Failed"},
                                                             session_id=sid)
                except Exception:
                    pass

    async def close(self):
        self.closed = True
        with _lock:
            for ident in list(self.ids):
                r = _pending.get(ident)
                if r and not r["future"].done():
                    r["future"].cancel()
        await self._fatal()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.client:
            await self.client.stop()
        with _lock:
            transports, self._transports = list(self._transports.values()), {}
        for transport in transports:
            transport.close()
