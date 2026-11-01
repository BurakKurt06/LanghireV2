"""Candidate upload protocols and attempts to turn them into an application."""
import json
from urllib.parse import urlencode

import pytest

from core.trial_policy import TrialPolicy
from tests.backend.core.test_trial_contracts import form, req


CASES = {
    'personio': ('https://acme.jobs.personio.de', '/job/123/apply', '<input name="name">',
        '/api/v1/documents', '/api/v1/jobs/123/application'),
    'teamtailor': ('https://career.teamtailor.com', '/jobs/123-role/applications/new',
        '<script src="https://assets-aws.teamtailor-cdn.com/assets/careersite-current.js"></script>',
        '/uploads/presigned_data', '/applications'),
    'breezy': ('https://jobs.breezy.hr', '/p/abc-role/apply',
        '<script id="portal-data" type="application/json">' + json.dumps({
            'pageId': 'apply', 'data': {'company': {'friendly_id': 'jobs'}}}) + '</script>'
        '<script src="https://assets-cdn.breezy.hr/breezy-portal/react/assets/portal-current.js"></script>',
        '/api/portal/jobs/upload', '/api/portal/jobs/apply/abc'),
    'jobvite': ('https://jobs.jobvite.com', '/acme/job/123/apply',
        '<script>angular.module("preloadedData", []).constant("preloadedData", {companyEId: "local-company"});</script>',
        '/careers/resource/resume', '/careers/apply'),
}


def bind(policy, url, page):
    policy.contracts.observe({'url': url, 'method': 'GET'},
        [{'name': 'Content-Type', 'value': 'text/html'}], page.encode(), True)


def payload(vendor, filename='resume.txt', contents=b'LOCAL RESUME'):
    if vendor == 'teamtailor':
        return {'filename': filename}, 'application/json'
    fields = {'companyEId': 'local-company', 'extractInfo': 'true'} if vendor == 'jobvite' else {}
    return form(fields, filename, contents), 'multipart/form-data; boundary=local'


@pytest.mark.parametrize('vendor', CASES)
def test_observed_candidate_preparation_and_final_are_separate(tmp_path, vendor):
    base, start, html, upload, final = CASES[vendor]
    file = tmp_path / 'resume.txt'; file.write_bytes(b'LOCAL RESUME')
    p = TrialPolicy(lambda u: u.startswith(base + '/'), lambda: [str(file)])
    data, mime = payload(vendor)
    assert not p.check(req(base + upload, data, mime=mime)).allowed
    bind(p, base + start, html)
    assert p.check(req(base + upload, data, mime=mime)).allowed
    assert not p.check(req(base + final, {'name': 'PRIVATE FINAL'})).allowed
    assert not p.check(req(base + upload + '?action=submit', data, mime=mime)).allowed
    assert not p.check(req(base + upload, data, method='PUT', mime=mime)).allowed
    assert not p.check(req(base + upload + '/..' + final, data, mime=mime)).allowed


@pytest.mark.parametrize('vendor', CASES)
@pytest.mark.parametrize('attack', ['other-file', 'extra-field', 'other-tenant', 'duplicate-field', 'other-origin', 'override'])
def test_candidate_uploads_do_not_grant_ambiguous_or_other_operations(tmp_path, vendor, attack):
    base, start, html, upload, _ = CASES[vendor]
    file = tmp_path / 'resume.txt'; file.write_bytes(b'LOCAL RESUME')
    p = TrialPolicy(lambda u: u.startswith(base + '/'), lambda: [str(file)])
    bind(p, base + start, html)
    data, mime = payload(vendor)
    url = base + upload
    if attack == 'other-file':
        data, mime = payload(vendor, 'not-authorised.txt', b'SECRET')
    elif attack == 'extra-field':
        data = {'filename': 'resume.txt', 'submit': True} if vendor == 'teamtailor' else form(
            ({'companyEId': 'local-company', 'extractInfo': 'true'} if vendor == 'jobvite' else {}) | {'submit': 'true'})
    elif attack == 'other-tenant':
        if vendor == 'breezy': url = base + '/api/portal/other/upload'
        elif vendor == 'jobvite': data = form({'companyEId': 'other', 'extractInfo': 'true'})
        elif vendor == 'personio': url = 'https://other.jobs.personio.de' + upload
        else: url = 'https://other.teamtailor.com' + upload
    elif attack == 'duplicate-field':
        if vendor == 'teamtailor': data = '{"filename":"resume.txt","filename":"other"}'
        else:
            raw = b'--local\r\nContent-Disposition: form-data; name="file"; filename="resume.txt"\r\n\r\nLOCAL RESUME\r\n'
            data = raw + data
    elif attack == 'other-origin': url = 'https://attacker.example' + upload
    request = req(url, data, mime=mime)
    if attack == 'override': request['headers']['X-HTTP-Method-Override'] = 'POST'
    assert not p.check(request).allowed


def test_teamtailor_signed_key_substitution_is_exact_and_single_use(tmp_path):
    file = tmp_path / 'My Resume.PDF'; file.write_bytes(b'LOCAL RESUME')
    base, start, html, upload, _ = CASES['teamtailor']
    p = TrialPolicy(lambda u: u.startswith(base + '/'), lambda: [str(file)])
    bind(p, base + start, html)
    assert p.check({'url': base + upload, 'method': 'POST', 'headers': {}}).allowed
    sent = req(base + upload, {'filename': file.name})
    storage = 'https://files.s3.eu-west-1.amazonaws.com/'
    grant = {'url': storage, 'fields': {'key': 'resume/unique/${filename}', 'policy': 'signed', 'x-amz-signature': 'signed'}}
    headers = [{'name': 'Content-Type', 'value': 'application/json'}]
    p.contracts.observe(sent, headers, json.dumps(grant).encode())
    fields = grant['fields'] | {'key': 'resume/unique/my_resume.pdf'}
    body = form(fields, file.name)
    good = req(storage, body, mime='multipart/form-data; boundary=local')
    assert p.check(good).allowed
    for change in ({'key': 'resume/other/my_resume.pdf'}, {'submit': 'true'}, {'policy': 'other'}):
        assert not p.check(req(storage, form(fields | change, file.name), mime='multipart/form-data; boundary=local')).allowed
    assert p.contracts.upload(good, body, consume=True)
    p.contracts.observe(sent, headers, json.dumps(grant).encode())
    assert not p.check(good).allowed


def test_breezy_bootstrap_cannot_rebind_the_current_company(tmp_path):
    base, start, html, upload, _ = CASES['breezy']
    file = tmp_path / 'resume.txt'; file.write_bytes(b'LOCAL RESUME')
    p = TrialPolicy(lambda u: u.startswith(base + '/'), lambda: [str(file)])
    bind(p, base + start, html)
    bind(p, base + start, html.replace('"jobs"', '"other"'))
    data, mime = payload('breezy')
    assert p.check(req(base + upload, data, mime=mime)).allowed
    assert not p.check(req(base + '/api/portal/other/upload', data, mime=mime)).allowed


@pytest.mark.parametrize('attack', ['', 'unobserved-url', 'other-tenant', 'extra-query', 'expired', 'unobserved-upload'])
def test_breezy_get_parser_can_only_fetch_the_observed_uploaded_file(tmp_path, attack):
    base, start, page, upload, _ = CASES['breezy']
    file = tmp_path / 'resume.txt'; file.write_bytes(b'LOCAL RESUME')
    p = TrialPolicy(lambda u: u.startswith(base + '/'), lambda: [str(file)])
    bind(p, base + start, page)
    data, mime = payload('breezy')
    value = 'https://files.s3.amazonaws.com/local/resume.txt'
    if attack != 'unobserved-upload':
        p.contracts.observe(req(base + upload, data, mime=mime),
            [{'name': 'Content-Type', 'value': 'application/json'}], json.dumps({'url': value}).encode())
    query = {'friendly_id': 'jobs', 'url': value}
    if attack == 'unobserved-url': query['url'] = 'http://127.0.0.1/private'
    elif attack == 'other-tenant': query['friendly_id'] = 'other'
    elif attack == 'extra-query': query['submit'] = 'true'
    elif attack == 'expired': p.contracts.vendors.parse_urls[base, 'jobs', value] = 0
    request = {'url': base + '/resume/parse?' + urlencode(query), 'method': 'GET', 'headers': {}}
    assert p.check(request).allowed == (not attack)


@pytest.mark.parametrize('path', ['/%72esume/parse', '/%2572esume/parse', '/Resume/Parse',
    '/resume/parse/', '/resume/parse;extra', '/resume;/parse', '/foo/../resume/parse', '//resume/parse'])
def test_parser_route_normalization_cannot_bypass_uploaded_url_binding(path):
    base, start, page, _, _ = CASES['breezy']
    p = TrialPolicy(lambda u: u.startswith(base + '/'))
    bind(p, base + start, page)
    assert not p.check({'url': base + path + '?' + urlencode({'friendly_id': 'jobs', 'url': 'http://127.0.0.1/private'}),
        'method': 'GET', 'headers': {}}).allowed


def test_jazzhr_actual_form_sdk_is_a_read_but_its_stage_one_is_not():
    p = TrialPolicy(lambda u: True)
    url = 'https://acme.applytojob.com/js/apply/submit-resume.js?master'
    assert p.check({'url': url, 'method': 'GET', 'headers': {}}).allowed
    for request in (req(url, {}), {'url': url + '&action=submit', 'method': 'GET', 'headers': {}},
            {'url': url.replace('applytojob.com', 'attacker.example'), 'method': 'GET', 'headers': {}},
            req('https://acme.applytojob.com/apply/job/local-code/stage-one', {'email': 'fake@example.test'})):
        assert not p.check(request).allowed
