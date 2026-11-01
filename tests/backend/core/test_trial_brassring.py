"""Real dispatcher schema: a false Submit flag alone grants no transition."""
import html
import json

import pytest

from core.trial_policy import TrialPolicy
from tests.backend.core.test_trial_contracts import req
from tests.backend.core.test_trial_vendors import bind


BASE = 'https://sjobs.brassring.com'
START = '/TGNewUI/GQ?partnerid=123&siteid=456&reqid=789'
SAVE = '/GQWeb/SaveAndNextPage?partnerid=123&siteid=456&localeid=1033'
PAGE = {'partnerid': 123, 'siteid': 456, 'localeid': 1033, 'pageid': 10,
    'nextpageid': 11, 'nextsectionid': 2, 'reqid': 789, 'tqid': 12,
    'tqrenderingid': 13, 'aipid': 'local-draft', 'gqsessionid': 'local-session', 'pagetype': 2}
DATA = {'ClientID': 123, 'SiteId': 456, 'LocaleId': 1033, 'PrevPageId': 10,
    'PageId': 11, 'SectionId': 2, 'ReqId': 789, 'TQId': 12, 'TQRenderingId': 13,
    'AIPID': 'local-draft', 'GQSessionId': 'local-session', 'PreviousPageType': 2,
    'Submit': False, 'DraftMode': False, 'Elements': [{'Name': 'firstName', 'Value': 'LOCAL NAME'}]}


def markup(page=PAGE):
    return '<input type="hidden" name="__RequestVerificationToken" value="local-rft">' + \
        '<input type="hidden" id="pagemetadata" value="' + html.escape(json.dumps(page), quote=True) + '">'


LOGIN = '/TgNewUI/Search/Ajax/CheckLoginDetails'
AUTH = {'PartnerId': '123', 'SiteId': '456', 'LoginType': '0', 'Email': 'fake@example.test',
    'Password': 'local-password', 'CaptchaInputText': '', 'BrowserInfo': 'Google Chrome(Desktop)',
    'IP': '1.1.1.1', 'Locale': '1033', 'LanguageSelected': '1', 'EncryptedSessionId': 'local-cookie',
    'CalledFrom': 'apply', 'ResponsiveCandidateZone': True, 'IsCreateAccountWorkFlow': False,
    'JobSiteIDs': '789_456'}


def auth_markup():
    preload = {'ClientSettings': {'LoginType': '0', 'DefLocaleId': '1033', 'DefLanguageId': '1'}}
    return '<input type=hidden id=CookieValue value=local-cookie>' + \
        '<input type=hidden id=preLoadJSON value="' + html.escape(json.dumps(preload), quote=True) + '">'


@pytest.mark.parametrize('attack', ['', 'site', 'session', 'locale', 'login-type', 'create', 'flow', 'job',
    'social', 'extra', 'expired', 'csrf', 'no-bootstrap'])
def test_brassring_candidate_login_is_bound_to_served_tenant_and_session(attack):
    p = TrialPolicy(lambda u: u.startswith(BASE + '/'))
    if attack != 'no-bootstrap': bind(p, BASE + START, markup() + auth_markup())
    data = dict(AUTH)
    for name, change in {'site': ('SiteId', '999'), 'session': ('EncryptedSessionId', 'other'),
        'locale': ('Locale', '999'), 'login-type': ('LoginType', 'other'), 'create': ('IsCreateAccountWorkFlow', True),
        'flow': ('CalledFrom', 'submit'), 'job': ('JobSiteIDs', '999_456'),
        'social': ('socialMediaLoginRequest', {}), 'extra': ('Submit', True)}.items():
        if attack == name: data[change[0]] = change[1]
    if attack == 'expired':
        cookie, settings, _ = p.contracts.brassring.auth[BASE]
        p.contracts.brassring.auth[BASE] = cookie, settings, 0
    request = req(BASE + LOGIN, data)
    if attack == 'csrf': request['headers']['RFT'] = 'other'
    assert p.check(request).allowed == (not attack)
    if not attack:
        # Angular omits undefined optional flags when serializing JSON.
        data.pop('IsCreateAccountWorkFlow'); data.pop('ResponsiveCandidateZone')
        assert p.check(req(BASE + LOGIN, data)).allowed


def save(data=DATA, path=SAVE):
    request = req(BASE + path, {'serializedRequest': json.dumps(data)})
    request['headers']['RFT'] = 'local-rft'
    return request


def policy():
    p = TrialPolicy(lambda u: u.startswith(BASE + '/'))
    bind(p, BASE + START, markup())
    return p


def test_brassring_served_nonterminal_pages_and_draft_use_live_dispatcher():
    p = policy()
    assert p.check(save()).allowed
    assert p.check(save(DATA | {'DraftMode': True})).allowed
    following = PAGE | {'pageid': 11, 'nextpageid': 12, 'pagetype': 3}
    p.contracts.observe(save(), [{'name': 'Content-Type', 'value': 'text/html'}], markup(following).encode())
    assert not p.check(save(DATA | {'PrevPageId': 11, 'PageId': 12, 'PreviousPageType': 3, 'Submit': True})).allowed
    assert not p.check(save(DATA | {'PrevPageId': 11, 'PageId': 12, 'PreviousPageType': 2, 'Submit': False})).allowed


@pytest.mark.parametrize('variant', ['omitted-draft', 'general-false-string', 'both'])
def test_brassring_first_next_serialization_matches_the_published_client(variant):
    # apply.js copies DraftMode BEFORE normalizing page.draftmode. Angular
    # omits that undefined property on the first Next; the client also
    # treats the general-application flag as a literal string.
    data = dict(DATA)
    if variant in ('omitted-draft', 'both'): data.pop('DraftMode')
    if variant in ('general-false-string', 'both'): data['IsGeneralJobSubmission'] = 'false'
    p = policy()
    assert p.check(save(data)).allowed
    assert not p.check(save(data | {'Submit': True})).allowed


def test_other_company_response_on_shared_origin_cannot_rotate_login_or_page_authority():
    p = policy()
    bind(p, BASE + START, markup() + auth_markup())
    other = PAGE | {'pageid': 999, 'nextpageid': 1000}
    poisoned = markup(other) + auth_markup().replace('local-cookie', 'other-cookie')
    bind(p, BASE + START.replace('siteid=456', 'siteid=999'), poisoned)
    assert p.contracts.brassring.auth[BASE][0] == 'local-cookie'
    assert (BASE, '999') not in p.contracts.brassring.pages


@pytest.mark.parametrize('attack', ['submit', 'truthy', 'missing-submit', 'review', 'unobserved', 'next-page',
    'next-section', 'session', 'tenant', 'query-tenant', 'locale', 'csrf', 'missing-csrf', 'extra',
    'nested-selector', 'wrapper', 'duplicate-json', 'general-job', 'draft-type', 'wrong-mime', 'no-bootstrap'])
def test_brassring_disguised_final_or_cross_session_operations_stay_blocked(attack):
    p = policy(); data = dict(DATA); request = None
    if attack == 'submit': data['Submit'] = True
    elif attack == 'truthy': data['Submit'] = 'false'
    elif attack == 'missing-submit': data.pop('Submit')
    elif attack == 'review': data['PreviousPageType'] = 3
    elif attack == 'unobserved': data['PrevPageId'] = 999
    elif attack == 'next-page': data['PageId'] = 999
    elif attack == 'next-section': data['SectionId'] = 999
    elif attack == 'session': data['GQSessionId'] = 'other'
    elif attack == 'tenant': data['SiteId'] = 999
    elif attack == 'query-tenant': request = save(path=SAVE.replace('siteid=456', 'siteid=999'))
    elif attack == 'locale': data['LocaleId'] = 999
    elif attack == 'extra': data['Operation'] = 'submit'
    elif attack == 'nested-selector': data['Elements'] = [{'method': 'submit'}]
    elif attack == 'general-job': data['IsGeneralJobSubmission'] = True
    elif attack == 'draft-type': data['DraftMode'] = 'true'
    elif attack == 'no-bootstrap': p = TrialPolicy(lambda u: u.startswith(BASE + '/'))
    request = request or save(data)
    if attack == 'csrf': request['headers']['RFT'] = 'other'
    elif attack == 'missing-csrf': request['headers'].pop('RFT')
    elif attack == 'wrong-mime': request['headers']['Content-Type'] = 'text/plain'
    elif attack == 'wrapper': request = req(BASE + SAVE, {'serializedRequest': json.dumps(data), 'Submit': True})
    elif attack == 'duplicate-json':
        duplicate = json.dumps(data)[:-1] + ',"Submit":true}'
        request = save(); request['postData'] = json.dumps({'serializedRequest': duplicate})
    assert not p.check(request).allowed
