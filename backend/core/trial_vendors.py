"""Candidate preparation APIs, separate from each vendor's application send.

The permissions here come from reviewed client protocols (TESTING.md), never
button text or a caller's claim that an arbitrary request is a draft. Metadata
is accepted only from successful broker responses on the trusted job site.
"""
from __future__ import annotations

import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .trial_contracts import multipart, object_pairs, origin


class Metadata(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.portal, self.scripts, self.inputs = [], [], {}
        self.capture = None
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if len(data) != len(attrs):
            return
        if tag == "script":
            self.capture = [] if data.get("id") == "portal-data" and data.get("type") == "application/json" else None
            if data.get("src"):
                self.scripts.append(data["src"])
        if tag == "input" and data.get("type") == "hidden":
            name = data.get("name", data.get("id", ""))
            self.inputs.setdefault(name, []).append(data.get("value", ""))

    def handle_data(self, value):
        if self.capture is not None:
            self.capture.append(value)

    def handle_endtag(self, tag):
        if tag == "script" and self.capture is not None:
            self.portal.append(json.loads("".join(self.capture), object_pairs_hook=object_pairs))
            self.capture = None


class VendorContracts:
    def __init__(self, owner):
        self.owner = owner
        self.sites = {}
        self.parse_urls = {}

    def bound(self, url):
        return origin(url) in self.sites

    def filenames(self):
        return {Path(f).name for f in self.owner.allowed_files() if Path(f).is_file()}

    def observe(self, req, headers, body, document=False):
        if len(body) > 15_000_000:
            return
        url = req["url"]
        base, path = origin(url), urlsplit(url).path
        mime = next((h["value"].split(";", 1)[0].lower() for h in headers if h["name"].lower() == "content-type"), "")
        if not base:
            return
        if document and mime in ("text/html", "application/xhtml+xml") and self.owner.trusted_site(url):
            source = body.decode("utf-8", "replace")
            page = Metadata(source)
            host = urlsplit(url).hostname or ""
            if (host.endswith(".teamtailor.com") or any(re.fullmatch(
                    r"https?://assets-aws\.teamtailor-cdn\.com/assets/careersite-[A-Za-z0-9.-]+\.js", s) for s in page.scripts)):
                self.sites.setdefault(base, ("teamtailor", ""))
            if host == "jobs.jobvite.com":
                ids = set(re.findall(r"\bcompanyEId\s*:\s*['\"]([A-Za-z0-9_-]{1,100})['\"]", source))
                if len(ids) == 1:
                    self.sites.setdefault(base, ("jobvite", ids.pop()))
            if re.fullmatch(r"[a-z0-9-]+\.jobs\.personio\.de", host):
                self.sites.setdefault(base, ("personio", host.split('.')[0]))
            if len(page.portal) == 1:
                data = page.portal[0]
                nested = data.get("data") if isinstance(data, dict) else None
                company = nested.get("company") if isinstance(nested, dict) else None
                if not isinstance(company, dict):
                    company = {}
                tenant = company.get("friendly_id", "")
                if (isinstance(data, dict) and data.get("pageId") == "apply" and isinstance(tenant, str)
                        and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", tenant)
                        and any(s.startswith("https://assets-cdn.breezy.hr/breezy-portal/") for s in page.scripts)):
                    self.sites.setdefault(base, ("breezy", tenant))
        site = self.sites.get(base)
        if not site:
            return
        kind, tenant = site
        if (kind == "breezy" and path == f"/api/portal/{tenant}/upload"
                and req.get("method") == "POST" and mime == "application/json"):
            from .trial_policy import _bytes
            if self.allows(req, _bytes(req), {}):
                data = json.loads(body, object_pairs_hook=object_pairs)
                value = data.get('url', '') if isinstance(data, dict) else ''
                if not isinstance(value, str):
                    return
                location = urlsplit(value)
                if origin(value) and not location.fragment and re.fullmatch(
                        r"(?:[a-z0-9.-]+\.)?s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com", location.hostname or ''):
                    self.parse_urls = {k: expiry for k, expiry in self.parse_urls.items() if expiry > time.monotonic()}
                    if len(self.parse_urls) < 128:
                        self.parse_urls[base, tenant, value] = time.monotonic() + 600
        # Teamtailor signs an S3 POST before sending the separate /candidates
        # application. Resolve only its documented filename substitution, to
        # an explicitly supplied local file, then reuse atomic one-use grants.
        if (kind == "teamtailor" and path == "/uploads/presigned_data"
                and req.get("method") == "POST" and mime == "application/json"):
            from .trial_policy import _bytes
            if not self.allows(req, _bytes(req), {}):
                return
            data = json.loads(body, object_pairs_hook=object_pairs)
            fields = data.get("fields") if isinstance(data, dict) else None
            if isinstance(fields, dict) and isinstance(fields.get("key"), str):
                for filename in self.filenames():
                    escaped = re.sub(r"_+", "_", re.sub(r"[^a-z0-9_.-]", "_", filename, flags=re.I)).lower()
                    key = fields["key"]
                    if key.count("${filename}") == 1:
                        self.owner._grant(data.get("url", ""), fields | {"key": key.replace("${filename}", escaped)})

    def read_check(self, req, query):
        """The Breezy GET parser fetches a URL server-side: it is not an
        unrestricted read. Only the just-uploaded, broker-returned file is
        eligible; the query cannot nominate another tenant or destination.
        """
        p = urlsplit(req['url']); base = origin(req['url'])
        site = self.sites.get(base)
        decoded = p.path
        for _ in range(20):
            newer = unquote(decoded, errors='strict')
            if newer == decoded:
                break
            decoded = newer
        segments = []
        for part in decoded.replace('\\', '/').split('/'):
            part = part.split(';', 1)[0]
            if part == '..':
                if segments:
                    segments.pop()
            elif part not in ('', '.'):
                segments.append(part)
        normalized = '/' + '/'.join(segments).lower()
        if normalized != '/resume/parse' or not (site and site[0] == 'breezy' or (p.hostname or '').endswith('.breezy.hr')):
            return None
        return bool(p.path == '/resume/parse' and site and site[0] == 'breezy' and req.get('method') == 'GET'
            and set(query) == {'friendly_id', 'url'} and query['friendly_id'] == site[1]
            and self.parse_urls.get((base, site[1], query['url']), 0) > time.monotonic())

    def allows(self, req, body, query):
        site = self.sites.get(origin(req["url"]))
        if not site or req.get("method") != "POST" or query:
            return False
        kind, tenant = site
        path = urlsplit(req["url"]).path
        mime = next((v for k, v in req.get("headers", {}).items() if k.lower() == "content-type"), "")
        if kind == "teamtailor" and path == "/uploads/presigned_data":
            if mime.split(";", 1)[0].strip().lower() != "application/json" and (body or mime):
                return False
            data = json.loads(body or b"{}", object_pairs_hook=object_pairs)
            return bool(self.filenames()) and (data == {} or isinstance(data, dict)
                and set(data) == {"filename"} and data["filename"] in self.filenames())
        if ((kind == "breezy" and path in (f"/api/portal/{tenant}/upload", f"/api/portal/{tenant}/upload/file"))
                or kind == "personio" and path == "/api/v1/documents"):
            fields, files = multipart(mime, body)
            return (not fields and len(files) == 1 and files[0][0] == "file"
                    and self.owner.file_matches(files[0][1], files[0][2]))
        if kind == "jobvite" and path in ("/careers/resource/resume", "/careers/resource/coverletter", "/careers/resource/portfolio"):
            fields, files = multipart(mime, body)
            return (set(fields) <= {"companyEId", "extractInfo"} and fields.get("companyEId") == tenant
                    and fields.get("extractInfo", "false") in ("true", "false")
                    and len(files) == 1 and files[0][0] == "file"
                    and self.owner.file_matches(files[0][1], files[0][2]))
        return False
