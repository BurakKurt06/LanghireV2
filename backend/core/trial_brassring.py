"""BrassRing's shared SaveAndNextPage dispatcher, bound to served page state.

Its public apply.js explicitly sends Submit=(pagetype==3). Only non-review
pages with Submit=false can use this contract. The next page/session/tenant
come from broker-served hidden metadata, not live DOM or submitted JSON.
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import parse_qsl, urlsplit

from .trial_contracts import object_pairs, origin


FIELDS = frozenset('AIPID Attachments BRUID ClientID DaysInDraft DraftMode Education EducationSettings '
    'Elements Experience ExperienceField GQSessionId GoingBack IsGeneralJobSubmission JobTitle Knockedout '
    'LocaleId MissingElements PageId PageReload Prefix PrevPageId PreviousPageType ReqId RequiredFields '
    'ResumeCoverLetters SectionId SiteId Skills SrcQs Submit TQId TQRenderingId WBMode Wotc '
    'WotcCompleted WotcElink eSignature listOfRequiredFieldswithoutQB'.split())
PINNED = {'ClientID': 'partnerid', 'SiteId': 'siteid', 'LocaleId': 'localeid',
    'PrevPageId': 'pageid', 'PageId': 'nextpageid', 'SectionId': 'nextsectionid',
    'ReqId': 'reqid', 'TQId': 'tqid', 'TQRenderingId': 'tqrenderingid',
    'AIPID': 'aipid', 'GQSessionId': 'gqsessionid'}


def selectors(value, depth=0):
    if depth > 30:
        return True
    if isinstance(value, dict):
        return any(re.sub('[^a-z]', '', k.lower()) in {
            'submit', 'issubmitted', 'submissiondate', 'action', 'operation', 'method',
            'finalize', 'finalise', 'customaction', 'httpmethod', 'batch', 'requests'}
            or selectors(v, depth + 1) for k, v in value.items())
    return isinstance(value, list) and any(selectors(v, depth + 1) for v in value)


class BrassRingContracts:
    def __init__(self, owner):
        self.owner = owner
        self.sites, self.pages, self.tokens = {}, {}, {}
        self.auth, self.jobs = {}, {}

    def bound(self, url):
        return origin(url) in self.sites

    def observe(self, req, headers, body, document=False):
        from .trial_vendors import Metadata
        url = req['url']; p = urlsplit(url); base = origin(url)
        if p.hostname not in ('sjobs.brassring.com', 'krb-sjobs.brassring.com', 'jobs.brassring.com') or not base:
            return
        query = object_pairs((k.lower(), v) for k, v in parse_qsl(p.query, keep_blank_values=True, max_num_fields=200))
        if document and self.owner.trusted_site(url):
            tenant = (query.get('partnerid', ''), query.get('siteid', ''))
            if all(re.fullmatch('[0-9]{1,12}', v) for v in tenant):
                self.sites.setdefault(base, tenant)
        if base not in self.sites or len(body) > 5_000_000:
            return
        mime = next((h['value'].split(';', 1)[0].lower() for h in headers if h['name'].lower() == 'content-type'), '')
        if mime not in ('text/html', 'application/xhtml+xml'):
            return
        # Fragment GETs load GQ pages; this same endpoint returns the next HTML
        # fragment after a validated save. No arbitrary JSON grants page state.
        if not (req.get('method') == 'GET' or req.get('method') == 'POST' and p.path.lower() == '/gqweb/saveandnextpage'):
            return
        page = Metadata(body.decode('utf-8', 'replace'))
        if 'partnerid' in query or 'siteid' in query:
            if (query.get('partnerid'), query.get('siteid')) != self.sites[base]:
                return
        else:
            partner, site = page.inputs.get('partnerId', []), page.inputs.get('siteId', [])
            if len(partner) != 1 or len(site) != 1 or (partner[0], site[0]) != self.sites[base]:
                return
        if req.get('method') == 'GET':
            job = query.get('reqid', '')
            if re.fullmatch('[0-9]{1,12}', job):
                self.jobs.setdefault(base, set()).add(job)
            preload = page.inputs.get('preLoadJSON', [])
            cookie = page.inputs.get('CookieValue', [])
            if len(preload) == len(cookie) == 1 and 0 < len(cookie[0]) < 4096:
                decoded = json.loads(preload[0], object_pairs_hook=object_pairs)
                settings = decoded.get('ClientSettings') if isinstance(decoded, dict) else None
                if isinstance(settings, dict) and all(type(settings.get(k)) in (str, int)
                        for k in ('LoginType', 'DefLocaleId', 'DefLanguageId')):
                    self.auth[base] = (cookie[0], {k: str(settings[k]) for k in (
                        'LoginType', 'DefLocaleId', 'DefLanguageId')}, time.monotonic() + 900)
                    if re.fullmatch('[0-9]{1,12}', str(decoded.get('JobId', ''))):
                        self.jobs.setdefault(base, set()).add(str(decoded['JobId']))
        tokens = page.inputs.get('__RequestVerificationToken', [])
        if len(tokens) == 1 and 0 < len(tokens[0]) < 4096:
            self.tokens[base] = tokens[0]
        values = page.inputs.get('pagemetadata', [])
        if len(values) != 1:
            return
        data = json.loads(values[0], object_pairs_hook=object_pairs)
        if (not isinstance(data, dict) or any(k not in data for k in PINNED.values())
                or (str(data['partnerid']), str(data['siteid'])) != self.sites[base]
                or type(data.get('pagetype')) is not int or data['pagetype'] not in (1, 2, 3)):
            return
        for key in PINNED.values():
            if type(data[key]) not in (str, int) or len(str(data[key])) > 256:
                return
        if len(self.pages) < 256:
            self.pages[base, str(data['pageid'])] = data

    def allows(self, req, body, query):
        p = urlsplit(req['url']); base = origin(req['url'])
        if p.path.lower() == '/tgnewui/search/ajax/checklogindetails':
            return self.login(req, body, query)
        if (not self.bound(req['url']) or req.get('method') != 'POST'
                or p.path.lower() != '/gqweb/saveandnextpage' or len(body) > 5_000_000):
            return False
        if set(query) != {'partnerid', 'siteid', 'localeid'} or (query['partnerid'], query['siteid']) != self.sites[base]:
            return False
        headers = {k.lower(): v for k, v in req.get('headers', {}).items()}
        if (headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json'
                or not self.tokens.get(base) or headers.get('rft') != self.tokens[base]):
            return False
        wrapper = json.loads(body, object_pairs_hook=object_pairs)
        if not isinstance(wrapper, dict) or set(wrapper) != {'serializedRequest'} or not isinstance(wrapper['serializedRequest'], str):
            return False
        data = json.loads(wrapper['serializedRequest'], object_pairs_hook=object_pairs)
        if (not isinstance(data, dict) or set(data) - FIELDS or data.get('Submit') is not False
                or type(data.get('DraftMode', False)) is not bool or data.get('PreviousPageType') not in (1, 2)
                or type(data.get('PreviousPageType')) is not int
                or data.get('IsGeneralJobSubmission', False) not in (False, 'false')
                or type(data.get('IsGeneralJobSubmission', False)) not in (bool, str)
                or selectors({k: v for k, v in data.items() if k != 'Submit'})):
            return False
        page = self.pages.get((base, str(data.get('PrevPageId'))))
        return bool(page and page['pagetype'] == data['PreviousPageType']
            and query['localeid'] == str(page['localeid'])
            and all(type(data.get(k)) in (str, int) and str(data[k]) == str(page[v]) for k, v in PINNED.items()))

    def login(self, req, body, query):
        """The published candidate client checks credentials here, then makes
        a separate GET to GQ. No social, batch or application dispatcher.
        """
        base = origin(req['url']); state = self.auth.get(base)
        headers = {k.lower(): v for k, v in req.get('headers', {}).items()}
        if (not state or state[2] <= time.monotonic() or req.get('method') != 'POST' or query
                or headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json'
                or len(body) > 100_000):
            return False
        fields = {'PartnerId', 'SiteId', 'LoginType', 'Email', 'Password', 'CaptchaInputText',
            'BrowserInfo', 'IP', 'Locale', 'LanguageSelected', 'EncryptedSessionId',
            'CalledFrom', 'ResponsiveCandidateZone', 'IsCreateAccountWorkFlow', 'JobSiteIDs', 'JobInfo'}
        data = json.loads(body, object_pairs_hook=object_pairs)
        if (not isinstance(data, dict) or set(data) - fields or not {'Email', 'Password'} <= set(data)
                or data.get('EncryptedSessionId') != state[0]
                or (str(data.get('PartnerId')), str(data.get('SiteId'))) != self.sites.get(base)
                or any(str(data.get(k)) != state[1][v] for k, v in (
                    ('LoginType', 'LoginType'), ('Locale', 'DefLocaleId'), ('LanguageSelected', 'DefLanguageId')))
                or data.get('IsCreateAccountWorkFlow', False) is not False
                or data.get('CalledFrom') not in ('apply', 'responsive', 'candidateZone', 'signIn', 'searchResults')
                or data.get('IP') != '1.1.1.1' or type(data.get('ResponsiveCandidateZone', False)) is not bool
                or any(not isinstance(data.get(k, ''), str) or len(data.get(k, '')) > 8192
                    for k in ('Email', 'Password', 'CaptchaInputText', 'BrowserInfo'))):
            return False
        if 'rft' in headers and headers['rft'] != self.tokens.get(base):
            return False
        if 'JobSiteIDs' in data:
            values = data['JobSiteIDs'] if isinstance(data['JobSiteIDs'], list) else [data['JobSiteIDs']]
            if not values or len(values) > 20 or any(not isinstance(v, str) or not re.fullmatch(
                    '[0-9]{1,12}_' + re.escape(self.sites[base][1]), v) or v.split('_')[0] not in self.jobs.get(base, set()) for v in values):
                return False
        if 'JobInfo' in data:
            value = data['JobInfo']
            if not isinstance(value, str) or not re.fullmatch(r'(?:%%[0-9]{1,12}\|[0-9]{1,12}\|[0-9]{1,12})+%%', value):
                return False
            if any(v.split('|')[0] not in self.jobs.get(base, set()) for v in value.strip('%').split('%%')):
                return False
        return True
