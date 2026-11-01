"""Live Trial request contracts. A button label or an old approval grants nothing.

Reads remain live; writes require an independently identified, non-terminal ATS
endpoint. Unknown writes fail on their own, without a review queue. This is not a
proof about arbitrary servers (including servers that mutate on an ordinary GET).
Protocol evidence and the supported/unsupported boundaries are in TESTING.md.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import re
from urllib.parse import parse_qsl, unquote, urlsplit

from .trial_contracts import ATSContracts, multipart, origin as contract_origin
from .trial_identity import CandidateIdentity


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    final: bool = False
    reason: str = ""


FINAL = Verdict(False, True, "Trial blocked final submission. Nothing was submitted by this request.")
UNKNOWN = Verdict(False, False, "Trial refused an unrecognised server operation; it could submit the application. No approval is requested.")
ALLOW = Verdict(True)
_TERMINAL = re.compile(r"(?:^|[^a-z])(?:submit(?:ted|application)?|final(?:ize|ise)|completeapplication|sendapplication)(?:$|[^a-z])", re.I)
_CONTROLS = {"action", "customaction", "operation", "operationname", "query", "mutation", "method", "httpmethod", "batch", "requests", "submit", "finalize", "finalise", "issubmitted", "submissiondate"}
_SECTIONS = frozenset((
    "name", "names", "address", "phonenumber", "emailaddress", "source", "previousworker",
    "workexperiences", "educations", "skills", "resumeattachments", "additionalattachments",
    "webaddresses", "socials", "certifications", "languages", "languagedefinition",
    "selfidentify", "uspersonalinfo", "nonuspersonalinfo", "availability", "termsandconditions",
    "questionnaireresponse", "termslockmoment",
))
_AUTH = {"login": {"username", "password", "sessionSecureToken"},
         "register": {"username", "password", "sessionSecureToken"},
         "initiatereset": {"username"},
         "resendactivationinfo": {"username", "sessionSecureToken"}}


def _key(value):
    return re.sub(r"[^a-z]", "", value.lower())


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate request key")
        result[key] = value
    return result


def _controls(value, depth=0):
    if depth > 30:
        return True
    if isinstance(value, dict):
        return any(_key(k) in _CONTROLS or _controls(v, depth+1) for k, v in value.items())
    if isinstance(value, list):
        return any(_controls(v, depth+1) for v in value)
    return False


def _bytes(req):
    entries = req.get("postDataEntries")
    if entries:
        return b"".join(base64.b64decode(e["bytes"], validate=True) for e in entries)
    return (req.get("postData") or "").encode("utf-8")


class TrialPolicy:
    def __init__(self, trusted_site, allowed_files=lambda: (), job_url=""):
        self.trusted_site = trusted_site
        self.contracts = ATSContracts(trusted_site, allowed_files, job_url)
        self.identity = CandidateIdentity(trusted_site, job_url)

    def check(self, req):
        try:
            return self._check(req)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OSError):
            return UNKNOWN

    def _check(self, req):
        url = urlsplit(req["url"])
        path = url.path
        # Check encoded terminal paths too. Write adapters require canonical paths.
        decoded = path
        for _ in range(4):
            newer = unquote(decoded, errors="strict")
            if newer == decoded:
                break
            decoded = newer
        # JazzHR's published form SDK is named submit-resume.js. It is a
        # static read, not its separate /apply/job/{code}/stage-one send.
        # Keep this exact; arbitrary "submit" URLs receive no asset exception.
        jazz_script = (req.get('method') == 'GET' and url.scheme == 'https'
            and url.port in (None, 443) and not url.username and not url.password
            and (url.hostname or '').endswith('.applytojob.com')
            and path == '/js/apply/submit-resume.js' and url.query in ('', 'master'))
        if not jazz_script and (_TERMINAL.search(decoded) or "final" in decoded.lower().strip("/").split("/")):
            return FINAL
        pairs = parse_qsl(url.query, keep_blank_values=True, max_num_fields=200)
        # Greenhouse requests several signed upload fields with repeated
        # fields[] parameters. This documented read has no operation selector.
        gh_fields = (req.get("method") == "GET" and (url.hostname or "").endswith(".greenhouse.io")
            and (re.fullmatch(r"/[^/]+/jobs/\d+/uncacheable_attributes/presigned_fields", path)
                 or (url.hostname == "boards.greenhouse.io" and path == "/uncacheable_attributes/presigned_fields"))
            and all(k == "fields[]" and re.fullmatch(r"[A-Za-z0-9_-]+", v) for k,v in pairs))
        query = dict(pairs) if gh_fields else _object(pairs)
        if any(_key(k) in _CONTROLS for k in query):
            return FINAL if any(_TERMINAL.search(v) for v in query.values()) else UNKNOWN
        headers = {k.lower(): v for k, v in req.get("headers", {}).items()}
        if len(headers) != len(req.get("headers", {})) or any(k in headers for k in ("content-encoding", "transfer-encoding")):
            return UNKNOWN
        if any(k in headers for k in ("x-http-method-override", "x-method-override", "x-http-method", "soapaction")):
            return UNKNOWN
        method = req.get("method")
        has_body = any(req.get(k) for k in ("hasPostData", "postData", "postDataEntries"))
        preparation_read = self.contracts.vendors.read_check(req, query)
        if preparation_read is False:
            return UNKNOWN
        if method in ("GET", "HEAD", "OPTIONS"):
            return ALLOW if not has_body else UNKNOWN
        if (url.scheme != "https" or url.port not in (None, 443) or url.username or url.password
                or path != decoded or "\\" in path or ";" in path
                or any(part in (".", "..") for part in path.split("/"))):
            return UNKNOWN
        host = (url.hostname or "").lower().rstrip(".")
        body = _bytes(req)
        mime = headers.get("content-type", "").split(";", 1)[0].lower().strip()
        if self.contracts.challenge(req, body):
            return ALLOW
        if self.contracts.upload(req, body):
            return ALLOW
        if self.identity.allows(req, body):
            return ALLOW
        if (self.trusted_site(req["url"]) or self.contracts.oracle.bound(req["url"])) and self.contracts.oracle.allows(req, body, query):
            return ALLOW
        if not self.trusted_site(req["url"]) and not self.contracts.bound(req["url"]):
            return UNKNOWN
        if self.contracts.allows(req, body, query):
            return ALLOW
        workday_job_host = re.fullmatch(r"[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com", host)
        workday_shared_host = re.fullmatch(r"wd\d+\.myworkdaysite\.com", host)
        if workday_job_host or workday_shared_host:
            if workday_job_host:
                tenant = host.split(".")[0]
            else:
                tenants = self.contracts.workday_tenants.get(contract_origin(req["url"]), set())
                if len(tenants) != 1:
                    return UNKNOWN
                tenant = next(iter(tenants))
            search = re.fullmatch(rf"/wday/cxs/{re.escape(tenant)}/[A-Za-z0-9_-]+/jobs", path)
            if search and method == "POST" and not query and mime == "application/json":
                # Workday's public client uses POST for this read-only lookup.
                # It is not an application/draft endpoint and carries only filters.
                data = json.loads(body, object_pairs_hook=_object)
                if (isinstance(data, dict) and set(data) <= {"appliedFacets", "limit", "offset", "searchText"}
                        and not _controls(data)
                        and type(data.get("limit", 20)) is int and 0 <= data.get("limit", 20) <= 100
                        and type(data.get("offset", 0)) is int and 0 <= data.get("offset", 0) <= 100_000
                        and isinstance(data.get("searchText", ""), str)
                        and isinstance(data.get("appliedFacets", {}), dict)
                        and all(isinstance(values, list) and all(isinstance(v, str) for v in values)
                                for values in data.get("appliedFacets", {}).values())):
                    return ALLOW
                return UNKNOWN
            auth_prefix = (rf"recruiting/{re.escape(tenant)}/" if workday_shared_host else "")
            auth = re.fullmatch(r"/(?:[a-z]{2}-[A-Z]{2}/)?" + auth_prefix + r"[A-Za-z0-9_-]+/(login|register|initiatereset|resendactivationinfo)", path)
            if auth and method == "POST" and not query and mime == "application/x-www-form-urlencoded":
                data = _object(parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=20))
                return ALLOW if data and set(data) <= _AUTH[auth[1]] and all(isinstance(v, str) for v in data.values()) else UNKNOWN
            prefix = rf"/wday/(?:calypso/cxs/jobapplication|cxs)/{re.escape(tenant)}/"
            # Public client creates a draft here, then finalizes it separately.
            create = re.fullmatch(prefix + r"jobpostings/[A-Za-z0-9_-]+/jobapplications", path)
            if create and method == "POST" and not query and body == b"{}":
                return ALLOW
            section = re.fullmatch(prefix + r"jobapplication/[A-Za-z0-9_-]+/([a-z]+)(?:/[A-Za-z0-9_-]+)?", path)
            if section and section[1] in _SECTIONS and method in ("POST", "PUT", "DELETE"):
                if query and not (section[1] == "questionnaireresponse" and set(query) == {"type"}
                                  and query["type"] in {"primary", "secondary", "supplementary"}):
                    return UNKNOWN
                data = json.loads(body, object_pairs_hook=_object)
                return ALLOW if isinstance(data, dict) and not _controls(data) else UNKNOWN
            # Scan/upload is separate from finalizing. File bytes were already
            # checked against the explicit upload list by RequestGate.
            upload = re.fullmatch(prefix + r"jobapplication/[A-Za-z0-9_-]+/(?:resume|questionnaireanswer/[A-Za-z0-9_-]+/attachments)", path)
            scan = re.fullmatch(rf"/wday/(?:calypso/cxs/common|cxs)/{re.escape(tenant)}/scanfile", path)
            if (upload or scan) and method == "POST" and not query and mime == "multipart/form-data":
                # Use the same strict parser as other hosted upload contracts:
                # header-registry defects are separate from message.defects.
                fields, files = multipart(headers["content-type"], body)
                if (not fields and len(files)==1 and files[0][0]=="body"
                        and self.contracts.file_matches(files[0][1],files[0][2])):
                    return ALLOW
        return UNKNOWN
