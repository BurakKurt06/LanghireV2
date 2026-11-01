"""Non-terminal hosted ATS operations and broker-observed file upload grants.

No DOM label, model assertion, operationName alone or page-supplied destination
can add a permission. Upload grants come only from specific first-party broker
responses and accept exact signed fields plus an explicitly available file.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from email.parser import BytesParser
from email.policy import default
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .trial_graphql import OPERATIONS, dispatch_digest


def origin(url):
    p = urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
        return ""
    return "https://" + p.hostname.lower().rstrip(".")


def family(host):
    for suffix, name in (("greenhouse.io", "greenhouse"), ("lever.co", "lever"),
        ("ashbyhq.com", "ashby"), ("workable.com", "workable"), ("myworkdaysite.com", "workday"),
        ("myworkdayjobs.com", "workday"), ("icims.com", "icims")):
        if host == suffix or host.endswith("." + suffix):
            return name
    return ""


def object_pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("Duplicate field")
        out[k] = v
    return out


def multipart(mime, body):
    if len(body) > 25_000_000 or "\r" in mime or "\n" in mime:
        raise ValueError("Invalid multipart body")
    message = BytesParser(policy=default).parsebytes(
        ("Content-Type: " + mime + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
    if (message.defects or not message.is_multipart() or message.get_content_type() != "multipart/form-data"
            or message["content-type"].defects
            or any(k != "boundary" for k,_ in message.get_params()[1:])):
        raise ValueError("Unreadable multipart body")
    fields, files, names = {}, [], set()
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if (part.defects or any(getattr(value, "defects", ()) for _,value in part.items())
                or part.is_multipart() or part.get_content_disposition() != "form-data"
                or len(part.get_all("content-disposition", [])) != 1
                or len(part.get_all("content-type", [])) > 1 or part.get("content-transfer-encoding")
                or not isinstance(name, str) or len(name) > 512 or name in names or len(names) >= 64):
            raise ValueError("Ambiguous multipart field")
        names.add(name)
        data = part.get_payload(decode=True)
        if not isinstance(data, bytes):
            raise ValueError("Unreadable multipart field")
        filename = part.get_filename()
        if filename is not None:
            files.append((name, filename, data, part.get_content_type()))
        else:
            if len(data) > 64_000:
                raise ValueError("Oversized multipart field")
            fields[name] = data.decode("utf-8")
    if not names:
        raise ValueError("Empty multipart body")
    return fields, files


class PageMetadata(HTMLParser):
    """Read browser-decoded attributes without executing page code."""
    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.accounts, self.oracle = set(), []
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if len(data) != len(attrs):
            return
        if tag == "input" and data.get("name") == "accountId" and data.get("type") == "hidden":
            self.accounts.add(data.get("value", ""))
        if data.get("data-apibaseurl") and data.get("data-sitenumber"):
            self.oracle.append((data["data-apibaseurl"], data["data-sitenumber"]))


class ATSContracts:
    def __init__(self, trusted_site, allowed_files=lambda: (), job_url=""):
        self.trusted_site, self.allowed_files = trusted_site, allowed_files
        self.sites, self.workday_tenants, self.form_ids = {}, {}, set()
        self.lever_accounts, self.file_handles = {}, set()
        from .trial_oracle import OracleContracts
        self.oracle = OracleContracts(self.file_matches)
        from .trial_vendors import VendorContracts
        self.vendors = VendorContracts(self)
        from .trial_brassring import BrassRingContracts
        self.brassring = BrassRingContracts(self)
        self.grants = {}
        self.lock = threading.RLock()
        if job_url and trusted_site(job_url):
            self.bind(job_url)

    def bind(self, url):
        key = origin(url)
        p = urlsplit(url)
        kind = family(p.hostname or "")
        if not key or not kind:
            return
        parts = p.path.strip("/").split("/")
        tenant = parts[0] if parts else ""
        if kind == "workday":
            m = re.match(r"/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([A-Za-z0-9_-]+)/", p.path)
            tenant = m[1] if m else p.hostname.split(".")[0]
            if key in self.sites and self.sites[key] != (kind, tenant):
                return
            self.workday_tenants.setdefault(key, set()).add(tenant)
        # The job's first tenant on a shared origin stays pinned for this run.
        self.sites.setdefault(key, (kind, tenant))

    def bound(self, url):
        return origin(url) in self.sites or self.vendors.bound(url) or self.brassring.bound(url)

    def file_matches(self, filename, data):
        digest = hashlib.sha256(data).digest()
        for value in self.allowed_files():
            file = Path(value)
            try:
                with file.open("rb") as f:
                    expected = f.read(25_000_001)
                sanitized = re.sub(r'[<>:"/\\|?*\s]', '_', file.name).strip('._')
                sanitized = re.sub('_+', '_', sanitized) or 'untitled'
                if filename in (file.name, sanitized) and len(expected) <= 25_000_000 and hashlib.sha256(expected).digest() == digest:
                    return True
            except OSError:
                continue
        return False

    def challenge(self, req, body):
        """Challenge proofs/configuration have no ATS application-submit API.

        Exact vendor routes only; this does not grant a challenge iframe or
        script permission to call the employer's final/unknown endpoints.
        """
        p = urlsplit(req["url"])
        if req.get("method") != "POST" or len(body) > 1_000_000:
            return False
        mime = next((v for k,v in req.get("headers", {}).items() if k.lower() == "content-type"), "")
        if mime.lower().startswith("multipart/"):
            return False
        host = p.hostname or ""
        if host in ("hcaptcha.com", "api.hcaptcha.com", "api2.hcaptcha.com"):
            return bool(re.fullmatch(r"/(?:checksiteconfig|getcaptcha/[A-Za-z0-9_-]+|checkcaptcha/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+)", p.path))
        if host in ("www.google.com", "www.recaptcha.net", "recaptcha.net"):
            return bool(re.fullmatch(r"/recaptcha/(?:api2|enterprise)/(?:reload|userverify|replaceimage|payload)", p.path))
        cloudflare = (host == "challenges.cloudflare.com" or self.bound(req["url"])
                      or self.oracle.bound(req["url"]) or self.trusted_site(req["url"]))
        return bool(cloudflare and re.fullmatch(
            r"/cdn-cgi/challenge-platform/h/[bg]/(?:jsd/oneshot|flow/ov1)/[A-Za-z0-9_:./-]+", p.path))

    def allows(self, req, body, query):
        if self.vendors.allows(req, body, query):
            return True
        if self.brassring.allows(req, body, query):
            return True
        p = urlsplit(req["url"])
        site = self.sites.get(origin(req["url"]))
        if not site:
            return False
        kind, tenant = site
        mime = next((v for k, v in req.get("headers", {}).items() if k.lower() == "content-type"), "")
        method, path = req.get("method"), p.path
        if kind == "lever" and path == "/parseResume" and method == "POST" and not query:
            fields, files = multipart(mime, body)
            return (set(fields) == {"accountId"} and fields["accountId"] in self.lever_accounts.get(origin(req["url"]), set()) and len(files) == 1
                    and files[0][0] == "resume" and self.file_matches(files[0][1], files[0][2]))
        if kind == "workable" and re.fullmatch(r"/api/v3/accounts/" + re.escape(tenant) + r"/jobs", path) and method == "POST" and not query and mime.split(";",1)[0].strip().lower() == "application/json":
            data = json.loads(body, object_pairs_hook=object_pairs)
            return (isinstance(data, dict) and set(data) <= {"query", "location", "department", "workplace", "worktype", "workplace_type", "employment_type", "limit", "offset"}
                    and type(data.get("limit", 20)) is int and 0 <= data.get("limit", 20) <= 100
                    and type(data.get("offset", 0)) is int and 0 <= data.get("offset", 0) <= 100_000
                    and all(isinstance(v, str) or (isinstance(v, list) and all(isinstance(x, str) for x in v))
                            for k,v in data.items() if k not in ("limit", "offset")))
        if kind == "ashby" and path == "/api/non-user-graphql" and method == "POST" and mime.split(";",1)[0].strip().lower() == "application/json":
            data = json.loads(body, object_pairs_hook=object_pairs)
            if not isinstance(data, dict) or set(data) - {"operationName", "query", "variables"}:
                return False
            op = OPERATIONS.get(data.get("operationName", ""))
            variables = data.get("variables", {})
            if (not op or query != {"op": data["operationName"]} or dispatch_digest(data.get("query")) != op["dispatch_digest"]
                    or not isinstance(variables, dict) or set(variables) - set(op["variables"])):
                return False
            if "organizationHostedJobsPageName" in op["variables"] and str(variables.get("organizationHostedJobsPageName", "")).casefold() != tenant.casefold():
                return False
            if "formRenderIdentifier" in op["variables"] and variables.get("formRenderIdentifier") not in self.form_ids:
                return False
            if "fileHandle" in op["variables"] and variables.get("fileHandle") not in self.file_handles:
                return False
            if "fileHandles" in op["variables"] and (not isinstance(variables.get("fileHandles"), list)
                    or any(h not in self.file_handles for h in variables["fileHandles"])):
                return False
            if data["operationName"] == "ApiCreateFileUploadHandle":
                if variables.get("fileUploadContext") != "NonUserFormEngine":
                    return False
                if type(variables.get("contentLength")) is not int or not any(
                        Path(f).name == variables.get("filename") and Path(f).is_file()
                        and Path(f).stat().st_size == variables["contentLength"] for f in self.allowed_files()):
                    return False
            return True
        return False

    def _grant(self, url, fields, key_template=None):
        p = urlsplit(url)
        if not origin(url) or p.fragment or not re.fullmatch(r"(?:[a-z0-9.-]+\.)?s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com", p.hostname or ""):
            return
        if not isinstance(fields, dict) or not fields or len(fields) > 32 or any(
                not isinstance(k, str) or not isinstance(v, str) or len(k) > 512 or len(v) > 16_384 for k, v in fields.items()):
            return
        url = origin(url) + (p.path or "/") + ("?"+p.query if p.query else "")
        key_pattern = None
        if key_template is not None:
            if not isinstance(key_template, str) or len(key_template) > 1024 or "key" in fields:
                return
            key_pattern = re.escape(key_template).replace(r"\{timestamp\}", r"[0-9]{13}").replace(r"\{unique_id\}", r"[a-z0-9]{1,14}")
            fields = fields | {"utf8":"✓", "authenticity_token":"1234", "Content-Type":"application/octet-stream"}
        with self.lock:
            # Keep consumed grants until expiry: repeated metadata cannot make
            # a spent upload usable again, including concurrent responses.
            self.grants = {k:v for k,v in self.grants.items() if v["expires"] > time.monotonic()}
            key = url + "#" + hashlib.sha256(json.dumps([fields,key_pattern], sort_keys=True).encode()).hexdigest()
            if key not in self.grants and len(self.grants) < 128:
                self.grants[key] = {"url": url, "fields": fields, "key_pattern":key_pattern, "expires": time.monotonic()+600, "used": False}

    def upload_destination(self, req):
        with self.lock:
            return req.get("method") == "POST" and any(v["url"] == req.get("url") for v in self.grants.values())

    def upload(self, req, body, consume=False):
        with self.lock:
            candidates = [v for v in self.grants.values() if v["url"] == req.get("url") and not v["used"]
                          and v["expires"] > time.monotonic()]
            if not candidates or req.get("method") != "POST":
                return False
            mime = next((v for k,v in req.get("headers", {}).items() if k.lower() == "content-type"), "")
            fields, files = multipart(mime, body)
            if len(files) != 1 or files[0][0] != "file" or not self.file_matches(files[0][1], files[0][2]):
                return False
            for grant in candidates:
                compared = dict(fields)
                if grant["key_pattern"] is not None:
                    if not re.fullmatch(grant["key_pattern"], compared.pop("key", "")):
                        continue
                if "Content-Type" not in grant["fields"] and compared.get("Content-Type") == files[0][3]:
                    compared.pop("Content-Type")
                if compared == grant["fields"]:
                    if consume:
                        grant["used"] = True
                    return True
            return False

    def observe(self, req, headers, body, document=False):
        try:
            self.vendors.observe(req, headers, body, document)
            self.brassring.observe(req, headers, body, document)
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError, OSError):
            pass  # malformed metadata grants no permission
        mime = next((h["value"].split(";",1)[0].lower() for h in headers if h["name"].lower() == "content-type"), "")
        url = req["url"]
        if document and mime in ("text/html", "application/xhtml+xml") and self.trusted_site(url):
            metadata = PageMetadata(body.decode("utf-8", "replace"))
            # Custom Oracle careers domains explicitly name their Oracle API
            # origin. This binds only that vendor's reviewed protocol, never
            # arbitrary destinations, credential approval or application sends.
            if b"static.oracle.com/cdn/fa/oj-hcm-ce/" in body:
                for api, site_number in metadata.oracle:
                    self.oracle.bind(url, api, site_number)
            if family(urlsplit(url).hostname or "") == "lever" and metadata.accounts:
                self.lever_accounts.setdefault(origin(url), set()).update(metadata.accounts)
        self.oracle.observe(req, headers, body)
        if document and mime in ("text/html", "application/xhtml+xml") and self.trusted_site(url):
            self.bind(url)
        site = self.sites.get(origin(url))
        # Current Greenhouse signs through a fixed vendor endpoint separate
        # from the tenant page. The returned policy/key bounds each upload.
        greenhouse_signer = (urlsplit(url).hostname == "boards.greenhouse.io"
            and urlsplit(url).path == "/uncacheable_attributes/presigned_fields"
            and any(kind == "greenhouse" for kind,_ in self.sites.values()))
        if greenhouse_signer:
            site = ("greenhouse", "")
        if not site or len(body) > 15_000_000:
            return
        kind, tenant = site
        try:
            data = json.loads(body, object_pairs_hook=object_pairs) if mime == "application/json" else None
            if data is None and kind == "ashby" and document:
                match = re.search(rb"window\.__appData\s*=\s*", body)
                if match:
                    data = json.JSONDecoder(object_pairs_hook=object_pairs).raw_decode(body[match.end():].decode("utf-8"))[0]
            path = urlsplit(url).path
            if kind == "greenhouse" and (greenhouse_signer or re.fullmatch(r"/" + re.escape(tenant) + r"/jobs/\d+/uncacheable_attributes/presigned_fields", path)) and req.get("method") == "GET" and isinstance(data, dict):
                for value in data.values():
                    if isinstance(value, dict) and isinstance(value.get("fields"), dict):
                        self._grant(data.get("url", ""), value["fields"], value.get("key"))
            if kind == "workable" and re.fullmatch(r"/api/v1/jobs/[A-Za-z0-9_-]+/form/upload/[A-Za-z0-9_-]+", path) and req.get("method") == "GET" and isinstance(data, dict):
                self._grant(data.get("url", ""), data.get("fields"))
            if kind == "ashby" and isinstance(data, dict):
                def visit(value):
                    if isinstance(value, dict):
                        if value.get("__typename") == "FormRender" and isinstance(value.get("id"), str):
                            self.form_ids.add(value["id"])
                        for v in value.values():
                            visit(v)
                    elif isinstance(value, list):
                        for v in value:
                            visit(v)
                visit(data)
                if path == "/api/non-user-graphql" and req.get("method") == "POST":
                    from .trial_policy import _bytes
                    sent = json.loads(_bytes(req), object_pairs_hook=object_pairs)
                    if sent.get("operationName") == "ApiCreateFileUploadHandle":
                        handle = data.get("data", {}).get("fileUploadHandle")
                        if isinstance(handle, dict):
                            fields = handle.get("fields")
                            if isinstance(fields, str):
                                fields = json.loads(fields, object_pairs_hook=object_pairs)
                            self._grant(handle.get("url", ""), fields)
                            if isinstance(handle.get("handle"), str):
                                self.file_handles.add(handle["handle"])
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
            return  # malformed metadata never adds authority
