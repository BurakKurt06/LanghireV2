"""Oracle CE draft/login contracts, including reviewed custom-domain bootstrap.

The hosted 2607.26 client saves drafts separately from JobApplications. Its
VerificationTokens endpoint also confirms applications, so merely recognizing
that URL is insufficient: confirmation modes and unbound email tokens are held.
"""
from __future__ import annotations

import base64
import json
import re
from urllib.parse import urlsplit


def _object(pairs):
    result = {}
    for key, value in pairs:
        key = key.lower()
        if key in result:
            raise ValueError("Ambiguous Oracle field")
        result[key] = value
    return result


def _json(body):
    return json.loads(body, object_pairs_hook=_object)


_DRAFT = frozenset(("appdraftid", "draftid", "requisitionnumber", "sitenumber", "content",
    "contentlanguage", "countrycode", "areacode", "phonenumber", "email", "emailaddress", "candidatenumber",
    "proposedcandidatenumber", "draftsource", "lastaccessedon", "objectversionnumber", "preferredchannel",
    "legaldescriptionversionid", "submissionid", "flowid", "stepactionusageid", "applymode"))
_START = frozenset(("sitenumber", "mode", "email", "phone", "phonelegislationcode", "phonechanneltype",
    "requisitionnumber", "lastname", "dateofbirth", "sourcetrackingid", "captchatoken", "control", "resendflag"))
_VERIFY = frozenset(("sitenumber", "urlshortcode", "shortcode", "challengeflag", "persistaccessflag",
    "email", "phone", "phonelegislationcode", "phonechanneltype"))


class OracleContracts:
    def __init__(self, file_matches):
        self.file_matches = file_matches
        self.sites, self.channels, self.tokens, self.access_codes, self.attachments = {}, {}, set(), set(), set()

    def bind(self, page_url, api_url, site_number):
        from .trial_contracts import origin
        api, page = origin(api_url), origin(page_url)
        host = urlsplit(api_url).hostname or ""
        if (api and page and host.endswith(".oraclecloud.com") and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", site_number)
                and urlsplit(api_url).path in ("", "/") and not urlsplit(api_url).query):
            # First bootstrap wins; an unrelated tenant cannot replace it.
            self.sites.setdefault(api, site_number)
            self.sites.setdefault(page, site_number)

    def bound(self, url):
        from .trial_contracts import origin
        return origin(url) in self.sites

    def _resource(self, req):
        path = urlsplit(req["url"]).path
        match = re.fullmatch(r"/hcmRestApi/resources/(?:latest|(?:\d+\.)+\d+)/(recruitingCE[A-Za-z]+)(.*)", path)
        return (match[1], match[2]) if match else ("", "")

    def allows(self, req, body, query):
        from .trial_contracts import origin
        if query or req.get("method") not in ("POST", "PATCH", "DELETE"):
            return False
        api = origin(req["url"])
        host = urlsplit(req["url"]).hostname or ""
        # Native Oracle hosts support the same reviewed protocol without a
        # custom-domain alias. Caller still requires ordinary employer trust.
        resource, suffix = self._resource(req)
        if not resource or (api not in self.sites and not host.endswith(".oraclecloud.com")):
            return False
        headers = {k.lower():v for k,v in req.get("headers", {}).items()}
        mime = headers.get("content-type", "").split(";", 1)[0].lower()
        if mime not in ("application/json", "application/vnd.oracle.adf.resourceitem+json"):
            return False
        data = _json(body) if body else {}
        if not isinstance(data, dict):
            return False
        method = req["method"]
        action_header = headers.get("ora-irc-rest-action")
        if resource == "recruitingCEJobApplicationDrafts":
            if action_header not in (None, "CE_JA_DRAFT"):
                return False
            if suffix in ("",) or re.fullmatch(r"/\d+", suffix):
                if method not in ("POST", "PATCH"):
                    return False
                if "customaction" in data:
                    action = data["customaction"]
                    if action == "ORA-GENERATE-PROPOSED-CAND-NUMBER":
                        return method == "PATCH" and bool(suffix) and set(data) == {"customaction"}
                    if action == "ORA_UPDATE":
                        return set(data) <= {"customaction", "candidatenumber", "emailaddress", "countrycode", "areacode", "phonenumber"}
                    if action == "ORA_UPDATE_COMMUNICATION_CHANNEL":
                        return set(data) <= {"customaction", "phonenumber", "areacode", "countrycode", "email", "draftid", "proposedcandidatenumber", "preferredchannel"}
                    return False
                if (set(data) - _DRAFT or any(isinstance(v, (dict, list)) for v in data.values())
                        or any(data.get(k) not in (None, 0, "") for k in ("submissionid", "flowid", "stepactionusageid"))):
                    return False
                return api not in self.sites or data.get("sitenumber", self.sites[api]) == self.sites[api]
            if re.fullmatch(r"/\d+/child/attachments(?:/\d+)?", suffix):
                if method == "DELETE":
                    return not data and suffix.rsplit("/", 1)[-1] in self.attachments
                if method != "POST" or set(data) - {"action", "id", "type", "category", "attachment", "filecontents"}:
                    return False
                return (data.get("action") == "draft" and data.get("type") == "document"
                    and self.file_matches(data.get("attachment"), base64.b64decode(data.get("filecontents", ""), validate=True)))
        if resource == "recruitingCECandidateResumeParsers" and not suffix and method == "POST":
            if action_header not in (None, "CE_RESUME_PARSE"):
                return False
            allowed = {"profileid", "profiletype", "flowversionid", "filetitle", "filecontents", "languages", "licensesandcertifications",
                       "educations", "attachments", "workexperiences", "workpreferences", "skills"}
            expected = {"languages": "profileitemid", "licensesandcertifications": "profileitemid", "educations": "profileitemid",
                        "attachments": "id", "workexperiences": "profileitemid", "workpreferences": "profileitemid", "skills": "profileitemid"}
            return (not set(data) - allowed and data.get("profileid") == "0011001100" and data.get("profiletype") == "rp"
                and all(isinstance(data.get(k, []), list) and all(isinstance(x, dict) and set(x) == {v} and type(x[v]) is int
                        for x in data.get(k, [])) for k,v in expected.items())
                and self.file_matches(data.get("filetitle"), base64.b64decode(data.get("filecontents", ""), validate=True)))
        if resource == "recruitingCECandidates" and not suffix and method == "POST":
            return (action_header is None and set(data) <= {"candidatenumber", "email", "lastname", "attachments", "action"}
                and data.get("action") == "savedraft" and isinstance(data.get("attachments"), list)
                and all(isinstance(v, dict) and set(v) == {"sourceattachmentid"}
                        and str(v["sourceattachmentid"]) in self.attachments for v in data["attachments"]))
        if resource != "recruitingCEVerificationTokens" or suffix or method != "POST" or api not in self.sites:
            return False
        mode = data.get("mode")
        if mode in ("REFRESH_DEVICE", "REFRESH_ACCESS", "SIGN_OUT"):
            return set(data) == {"mode", "accesscode"} and data["accesscode"] in self.access_codes and action_header is None
        if mode in ("CSS_ACCESS", "JOB_APPLY"):
            channel = "email" if data.get("email") else "phone" if data.get("phone") else ""
            return (not set(data) - _START and data.get("sitenumber") == self.sites[api] and bool(channel)
                and isinstance(data[channel], str) and not any(isinstance(v, (dict, list)) for v in data.values())
                and action_header in (None, mode+":"+channel.upper()))
        if mode is not None or set(data) - _VERIFY or action_header is not None:
            return False  # CONFIRM and JOB_APPLY_CONFIRM also use this endpoint
        if data.get("sitenumber", self.sites[api]) != self.sites[api]:
            return False
        token = data.get("urlshortcode")
        if token:
            return (api, token) in self.tokens
        channel = "email" if data.get("email") else "phone" if data.get("phone") else ""
        return (data.get("challengeflag") is True and bool(data.get("shortcode"))
                and (channel, data.get(channel)) in self.channels.get(api, set()))

    def observe(self, req, headers, body):
        from .trial_contracts import origin
        from .trial_policy import _bytes
        api = origin(req["url"])
        if api not in self.sites or len(body) > 15_000_000 or req.get("method") not in ("POST", "PATCH"):
            return
        try:
            resource, suffix = self._resource(req)
            data = _json(body)
            if not isinstance(data, dict):
                return
            if resource == "recruitingCEVerificationTokens" and not suffix:
                sent = _json(_bytes(req))
                if not self.allows(req, _bytes(req), {}):
                    return
                if sent.get("mode") in ("CSS_ACCESS", "JOB_APPLY"):
                    for channel in ("email", "phone"):
                        if sent.get(channel):
                            self.channels.setdefault(api, set()).add((channel, sent[channel]))
                # Never import tokens which describe a completed submission.
                if data.get("submissionid") or "SubmissionId=" in str(data.get("consumerresponse", "")):
                    return
                if isinstance(data.get("urlshortcode"), str):
                    self.tokens.add((api, data["urlshortcode"]))
                if isinstance(data.get("accesscode"), str):
                    self.access_codes.add(data["accesscode"])
            if resource == "recruitingCEJobApplicationDrafts" and re.fullmatch(r"/\d+/child/attachments", suffix):
                if isinstance(data.get("id"), (str, int)):
                    self.attachments.add(str(data["id"]))
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return
