"""Reviewed candidate authentication, independently of application permissions.

Forms narrow an already reviewed authentication protocol; arbitrary HTML can
never nominate a write endpoint. Auth0 state is bound by the broker's observed
authorization redirect, to one original iCIMS employer. No social-provider or
application submission endpoint is included here.
"""
from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re
import threading
import time
from urllib.parse import parse_qsl, urljoin, urlsplit

from .trial_contracts import origin

TTL = 900
AUTH0 = "https://login.icims.com"
AUTH_ROUTES = frozenset(("/u/login", "/u/login/identifier", "/u/login/password",
    "/u/signup", "/u/signup/identifier", "/u/signup/password", "/u/email-verification",
    "/u/mfa-email-challenge", "/u/mfa-otp-challenge", "/u/reset-password"))
AUTH_FIELDS = frozenset(("state", "username", "password", "email", "code", "action", "captcha",
    "captcha_code", "js-available", "webauthn-available", "is-brave", "webauthn-platform-available"))
FLAGS = frozenset(("js-available", "webauthn-available", "is-brave", "webauthn-platform-available"))
SAP_HOST = re.compile(r"(?:career|performancemanager|api)\d+(?:preview)?\.(?:successfactors\.(?:com|eu)|sapsf\.(?:com|eu))$")
TALEO_AUTH = "/careersection/iam/accessmanagement/"


def pairs(value):
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ValueError("Invalid form encoding")
    return parse_qsl(value, keep_blank_values=True, strict_parsing=False, encoding="utf-8", errors="strict", max_num_fields=256)


def unique(value):
    out = {}
    for key, item in pairs(value):
        if key in out:
            raise ValueError("Duplicate authentication selector")
        out[key] = item
    return out


class Forms(HTMLParser):
    def __init__(self, page, url):
        super().__init__(convert_charrefs=True)
        self.url, self.forms, self.current, self.bad = url, [], None, False
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if len(data) != len(attrs):
            self.bad = True
        if tag == "base":
            self.bad = True  # effective destinations must not differ from the parser
        if tag == "form":
            if self.current is not None:
                self.bad = True
            self.current = {"url": urljoin(self.url, data.get("action") or self.url),
                "method": data.get("method", "get").upper(), "fields": {}, "hidden": {},
                "mime": data.get("enctype", "application/x-www-form-urlencoded").lower()}
            self.forms.append(self.current)
        if tag in ("input", "button", "select", "textarea") and self.current is not None:
            if data.get("form") or data.get("formaction") or data.get("formmethod"):
                self.bad = True
            name = data.get("name")
            if not name or "disabled" in data or data.get("type") == "button":
                return
            self.current["fields"].setdefault(name, []).append(data.get("value", ""))
            if data.get("type", "").lower() == "hidden":
                self.current["hidden"].setdefault(name, []).append(data.get("value", ""))

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


@dataclass
class Grant:
    scope: str
    fields: dict
    hidden: dict
    variable: set
    expires: float
    kind: str


class CandidateIdentity:
    def __init__(self, trusted_site, job_url=""):
        self.trusted_site, self.job_url = trusted_site, job_url
        self.lock = threading.RLock()
        self.starts, self.transactions, self.forms, self.pages = {}, {}, {}, {}
        self.companies = {}

    def _prune(self):
        now = time.monotonic()
        self.starts = {k:v for k,v in self.starts.items() if v[1] > now}
        self.transactions = {k:v for k,v in self.transactions.items() if v[1] > now}
        self.forms = {k:v for k,v in self.forms.items() if v.expires > now}
        self.pages = {k:v for k,v in self.pages.items() if v[1] > now}

    def scope(self, url):
        """Only a served, reviewed login form can delegate credential typing."""
        with self.lock:
            self._prune()
            page = self.pages.get(url)
            return page[0] if page else ""

    def _icims_start(self, url, scope):
        p = urlsplit(url)
        if origin(url) != AUTH0 or p.path != "/authorize" or p.fragment:
            return
        q = unique(p.query)
        callback = urlsplit(q.get("redirect_uri", ""))
        employer = urlsplit(scope)
        if (origin(q.get("redirect_uri", "")) != origin(scope)
                or callback.path not in ("/jobs/login", "/connect/login")
                or q.get("response_type") != "code" or q.get("scope") != "openid profile email"
                or q.get("audience") != "https://api.icims.com/v1/"
                or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", q.get("client_id", ""))
                or not re.fullmatch(r"org_[A-Za-z0-9]+", q.get("organization", ""))
                or not re.fullmatch(r"[A-Za-z0-9-]+", q.get("tenantId", ""))
                or not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", q.get("state", ""))
                or not q.get("nonce")):
            return
        target = unique(callback.query)
        if (target.get("css_submitMode") != "1" or target.get("universalLogin") != "1"
                or target.get("redirect") not in ("job", "welcome")
                or set(target) - {"css_submitMode", "universalLogin", "redirect", "loginOnly", "jobid", "hashed", "in_iframe"}):
            return
        job = re.match(r"/jobs/(\d+)/", employer.path)
        if callback.path == "/jobs/login" and (not job or target.get("jobid") != job[1]):
            return
        self.starts.setdefault(url, (scope, time.monotonic()+TTL))

    def observe(self, req, status, headers, body, document=False):
        with self.lock:
            self._prune()
            if len(self.forms)+len(self.starts)+len(self.transactions) >= 256:
                return
            try:
                self._observe(req, status, headers, body, document)
            except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
                return  # bad metadata never adds authority

    def _observe(self, req, status, headers, body, document):
        url, p = req["url"], urlsplit(req["url"])
        source = self.starts.get(url)
        if 300 <= status < 400 and source is None and url in self.forms:
            from .trial_policy import _bytes
            if self.allows(req, _bytes(req)):
                grant = self.forms[url]
                source = (grant.scope, grant.expires)
        if 300 <= status < 400 and source:
            locations = [h["value"] for h in headers if h["name"].lower() == "location"]
            if len(locations) == 1:
                dest = urljoin(url, locations[0]); d = urlsplit(dest); q = unique(d.query)
                if origin(dest) == AUTH0 and d.path in AUTH_ROUTES and set(q) == {"state"} and q["state"]:
                    self.transactions.setdefault(q["state"], source)
            return
        mime = [h["value"].split(";",1)[0].lower() for h in headers if h["name"].lower() == "content-type"]
        if not (200 <= status < 300 and document and mime == ["text/html"] and len(body) <= 2_000_000):
            return
        page = body.decode("utf-8", "replace")
        icims = bool(re.fullmatch(r"(?:careers|jobs)-[a-z0-9-]+\.icims\.com", p.hostname or ""))
        trusted = bool(origin(url) and self.trusted_site(url))
        if icims and trusted:
            # iCIMS uses a literal JS redirect, rather than an HTTP Location.
            # Read only the fixed literal statement, never evaluate page code.
            for match in re.finditer(r"window\.top\.location\.href\s*=\s*'([^'\r\n]+)'\s*;", page):
                literal = match[1].replace(r"\/", "/")
                if "\\" not in literal:
                    self._icims_start(literal, self.job_url if origin(self.job_url)==origin(url) else url)
        parsed = Forms(page, url)
        if parsed.bad:
            return
        for form in parsed.forms:
            dest = urlsplit(form["url"])
            if (form["method"] != "POST" or form["mime"] != "application/x-www-form-urlencoded"
                    or origin(form["url"]) != origin(url)):
                continue
            fields, hidden = form["fields"], form["hidden"]
            scope, kind, variable = "", "", set()
            if origin(url) == AUTH0 and p.path in AUTH_ROUTES and dest.path == p.path:
                q = unique(p.query)
                tx = self.transactions.get(q.get("state", ""))
                if (set(q) == {"state"} and tx and form["url"] == url
                        and hidden.get("state") == [q["state"]] and set(fields) <= AUTH_FIELDS
                        and all(len(v)==1 for v in fields.values()) and "connection" not in fields):
                    scope, kind = tx[0], "auth0"
                    variable = {"username", "email", "password", "code", "captcha", "captcha_code"} | set(FLAGS)
            elif icims and trusted and re.fullmatch(r"/jobs/\d+/[A-Za-z0-9_-]+/login", dest.path):
                q = unique(dest.query)
                job = re.match(r"/jobs/(\d+)/", p.path)
                original_job = re.match(r"/jobs/(\d+)/", urlsplit(self.job_url).path)
                if (job and dest.path.split('/')[2]==job[1]
                        and set(q) <= {"step", "css_submitMode", "in_iframe", "hashed"}
                        and q.get("step", "email") == "email" and q.get("css_submitMode", "1") == "1"
                        and set(fields) <= {"css_loginName", "accept_gdpr"} and "css_loginName" in fields):
                    scope = self.job_url if origin(url)==origin(self.job_url) and original_job and original_job[1]==job[1] else url
                    kind, variable = "icims-email", {"css_loginName", "accept_gdpr"}
            elif trusted and (p.hostname or "").endswith(".taleo.net") and dest.path == TALEO_AUTH+"login.jsf":
                if ("javax.faces.ViewState" in hidden and "csrftoken" in hidden
                        and "dialogTemplate-dialogForm-login-name1" in fields
                        and "dialogTemplate-dialogForm-login-password" in fields):
                    scope, kind = self.job_url if origin(self.job_url)==origin(url) else url, "taleo-login"
                    variable = {"dialogTemplate-dialogForm-login-name1", "dialogTemplate-dialogForm-login-password",
                        "dialogTemplate-dialogForm:login:cookieenable", "jsfCmdId", "tz"}
            elif trusted and SAP_HOST.fullmatch(p.hostname or "") and dest.path == "/career":
                company = hidden.get("company", [])
                job_company = unique(urlsplit(self.job_url).query).get("company", "") if origin(url)==origin(self.job_url) else ""
                page_company = unique(p.query).get("company", "")
                expected = self.companies.get(origin(url), job_company or page_company)
                # The native login keeps the destination application namespace;
                # the live SAP form renders that same hidden field twice. It
                # is a return route, while career_os dispatches the operation.
                # Accept only the served identical values and this job's ID.
                namespaces = hidden.get("career_ns", [])
                job_id = unique(urlsplit(self.job_url).query).get("career_job_req_id", "") if origin(url)==origin(self.job_url) else ""
                page_job = unique(p.query).get("career_job_req_id", "")
                returning = (1 <= len(namespaces) <= 2 and set(namespaces) == {"job_application"}
                    and re.fullmatch(r"[0-9]{1,20}", job_id or page_job or "")
                    and hidden.get("career_job_req_id") == [job_id or page_job]
                    and (not page_job or not job_id or page_job == job_id))
                if (re.fullmatch(r"[A-Za-z0-9_-]{1,100}", expected) and company == [expected] and (not page_company or page_company == expected)
                        and "username" in fields and "password" in fields
                        and (namespaces == [""] or returning) and hidden.get("career_os") == [""]
                        and hidden.get("login_ns") == [""]
                        and hidden.get("isInternalUser") == [""] and "requestParams" in hidden
                        and hidden.get("career_company", [expected]) == [expected]):
                    scope = self.job_url if job_company == expected else url
                    kind, variable = "sap-login", {"username", "password", "recaptcha_response_field"}
                    self.companies.setdefault(origin(url), expected)
            if not kind:
                continue
            grant = Grant(scope, fields, hidden, variable, time.monotonic()+TTL, kind)
            self.forms[form["url"]] = grant
            if kind == "taleo-login":
                # Oracle's served cmdSubmit calls secureSubmit to this fixed
                # login-only handler, with the same JSF state and CSRF fields.
                self.forms[origin(url)+TALEO_AUTH+"iamSecuredLogin.jss"] = grant
            self.pages[url] = (scope, grant.expires)
            self.pages[form["url"]] = (scope, grant.expires)

    def allows(self, req, body):
        if req.get("method") != "POST" or len(body) > 512_000:
            return False
        mime = [v for k,v in req.get("headers", {}).items() if k.lower()=="content-type"]
        if len(mime)!=1 or mime[0].split(";",1)[0].strip().lower()!="application/x-www-form-urlencoded":
            return False
        with self.lock:
            self._prune()
            grant = self.forms.get(req["url"])
            if not grant:
                return False
            data = {}
            for key, value in pairs(body.decode("utf-8")):
                data.setdefault(key, []).append(value)
            if set(data) - (set(grant.fields)|grant.variable):
                return False
            for key, values in data.items():
                if key in grant.variable:
                    if len(values)!=1:
                        return False
                    if key in FLAGS and values[0] not in ("true", "false"):
                        return False
                elif values != grant.fields[key]:
                    return False
            if any(key not in data for key in grant.hidden):
                return False
            if grant.kind == "auth0" and data.get("action") != ["default"]:
                return False
            if grant.kind == "taleo-login" and data.get("jsfCmdId") not in (
                    ["dialogTemplate-dialogForm-login-defaultCmd"], ["dialogTemplate-dialogForm-login-register"]):
                return False
            return True
