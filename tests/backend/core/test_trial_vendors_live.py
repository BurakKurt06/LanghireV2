"""Actual Chromium + local HTTP positive controls for candidate preparation."""
import asyncio
import json
import os
from urllib.parse import urlencode

import pytest

from core import browser_policy, request_gate
from tests.backend.core.test_request_gate_live import browser_and_gate, wait_for, wire_site  # noqa: F401
from tests.backend.core.test_trial_ats_live import post, script, transport
from tests.backend.core.test_trial_vendors import CASES
from tests.backend.core.test_trial_brassring import BASE, START, SAVE, PAGE, DATA, LOGIN, AUTH, markup, auth_markup


pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(
    os.environ.get('LANGHIRE_REAL_BROWSER') != '1', reason='opt-in local browser')]


@pytest.mark.parametrize('mode', ['trial', 'auto'])
async def test_jazzhr_form_script_loads_but_first_stage_application_send_does_not(wire_site, mode):
    local, seen, site = wire_site
    base = 'https://acme.applytojob.com'; path = '/js/apply/submit-resume.js'
    site['html'] = '<script src="' + path + '?master"></script><input id=name>'
    browser, gate = await browser_and_gate(mode, base + '/apply/local-code/role',
        transport_factory=transport(local, {path: ('application/javascript', b'document.documentElement.dataset.formSdk="loaded";')}),
        trusted_site=lambda u: request_gate.origin(u) == base)
    try:
        await browser.navigate_to(base + '/apply/local-code/role')
        assert await wait_for(lambda: any(r[0] == 'GET' and r[1] == path + '?master' for r in seen), gate)
        for _ in range(100):
            if await browser_policy.evaluate(browser, '() => document.documentElement.dataset.formSdk === "loaded"'):
                break
            await asyncio.sleep(.05)
        else:
            pytest.fail('The application SDK did not execute')
        await script(browser, 'document.querySelector("#name").value="LOCAL NAME"')
        await post(browser, '/apply/job/local-code/stage-one', {'email': 'PRIVATE FINAL'})
        assert await wait_for(lambda: bool(gate.trial_blocked_operation) if mode == 'trial'
            else any(r[0] == 'POST' for r in seen), gate)
        await gate.wait_idle()
        assert any(r[0] == 'POST' and b'PRIVATE FINAL' in r[2] for r in seen) == (mode == 'auto'), seen
        assert gate.healthy() and not request_gate.pending()
    finally:
        await gate.close()


@pytest.mark.parametrize('vendor', CASES)
@pytest.mark.parametrize('mode', ['trial', 'auto'])
async def test_candidate_preparation_upload_next_and_script_final_on_actual_wire(wire_site, tmp_path, vendor, mode):
    local, seen, site = wire_site
    base, start, metadata, upload, final = CASES[vendor]
    storage = 'https://files.s3.eu-west-1.amazonaws.com/upload'
    signed = {'url': storage, 'fields': {'key': 'local/${filename}', 'policy': 'signed', 'x-amz-signature': 'signed'}}
    site['html'] = metadata + '<input type=file id=file><input id=name><button id=next type=button onclick="document.querySelector(\'#name\').value=\'NEXT PAGE\'">Weiter</button>'
    parsed_file = 'https://files.s3.amazonaws.com/local/resume.txt'
    replies = {upload: ('application/json', json.dumps(signed if vendor == 'teamtailor' else {'url': parsed_file, 'uuid': 'local-file'}).encode())}
    # Scripts referenced in metadata are emulated locally; no public network.
    replies['/breezy-portal/react/assets/portal-current.js'] = ('application/javascript', b'')
    replies['/assets/careersite-current.js'] = ('application/javascript', b'')
    browser, gate = await browser_and_gate(mode, base + start, transport_factory=transport(local, replies),
        trusted_site=lambda u: request_gate.origin(u) == base)
    file = tmp_path / 'resume.txt'; file.write_bytes(b'LOCAL RESUME'); gate.allowed_files = [str(file)]
    try:
        await browser.navigate_to(base + start)
        if mode == 'trial':
            assert await wait_for(lambda: gate.trial_policy.contracts.vendors.bound(base + start), gate)
        cdp = await browser.get_or_create_cdp_session()
        doc = await cdp.cdp_client.send.DOM.getDocument(session_id=cdp.session_id)
        node = await cdp.cdp_client.send.DOM.querySelector(params={'nodeId': doc['root']['nodeId'], 'selector': '#file'}, session_id=cdp.session_id)
        await cdp.cdp_client.send.DOM.setFileInputFiles(params={'nodeId': node['nodeId'], 'files': [str(file)]}, session_id=cdp.session_id)
        if vendor == 'teamtailor':
            await post(browser, upload, {'filename': file.name})
            assert await wait_for(lambda: bool(gate.trial_policy.contracts.grants) if mode == 'trial'
                else any(r[0] == 'POST' and r[1] == upload for r in seen), gate)
            destination = storage
            fields = signed['fields'] | {'key': 'local/resume.txt'}
        else:
            destination = upload
            fields = {'companyEId': 'local-company', 'extractInfo': 'true'} if vendor == 'jobvite' else {}
        code = 'const f=new FormData();for(const [k,v] of Object.entries(' + json.dumps(fields) + ')) f.append(k,v);f.append("file",document.querySelector("#file").files[0]);fetch(' + json.dumps(destination) + ',{method:"POST",body:f}).catch(()=>{})'
        await script(browser, code)
        target = '/upload' if vendor == 'teamtailor' else upload
        assert await wait_for(lambda: any(r[0] == 'POST' and r[1] == target and b'LOCAL RESUME' in r[2] for r in seen), gate)
        if vendor == 'breezy':
            good_parse = '/resume/parse?' + urlencode({'friendly_id': 'jobs', 'url': parsed_file})
            if mode == 'trial':
                assert await wait_for(lambda: bool(gate.trial_policy.contracts.vendors.parse_urls), gate)
            await script(browser, 'fetch(' + json.dumps(good_parse) + ').catch(()=>{})')
            assert await wait_for(lambda: any(r[0] == 'GET' and r[1] == good_parse for r in seen), gate)
            for path in ('/resume/parse', '/%72esume/parse'):
                bad_parse = path + '?' + urlencode({'friendly_id': 'jobs', 'url': 'http://127.0.0.1/PRIVATE FINAL'})
                before = gate.blocked
                await script(browser, 'fetch(' + json.dumps(bad_parse) + ').catch(()=>{})')
                assert await wait_for(lambda: gate.blocked > before or any(r[1] == bad_parse for r in seen), gate)
                await gate.wait_idle()
                assert any(r[1] == bad_parse for r in seen) == (mode == 'auto'), seen
        await script(browser, 'document.querySelector("#next").click()')
        assert (await cdp.cdp_client.send.Runtime.evaluate(params={'expression': 'document.querySelector("#name").value', 'returnByValue': True}, session_id=cdp.session_id))['result']['value'] == 'NEXT PAGE'
        # Disguised final via the actual vendor endpoint, bypassing button
        # labels and the DOM submit wrapper entirely.
        await post(browser, final, {'candidate': 'PRIVATE FINAL'})
        # Smuggle an extra submit selector through the valid upload route.
        bad = code.replace('f.append("file",', 'f.append("submit","PRIVATE FINAL");f.append("file",')
        await script(browser, bad)
        if mode == 'auto':
            assert await wait_for(lambda: sum(r[0] == 'POST' and b'PRIVATE FINAL' in r[2] for r in seen) == 2, gate)
        else:
            assert await wait_for(lambda: bool(gate.trial_blocked_operation), gate)
        await gate.wait_idle()
        assert sum(r[0] == 'POST' and b'PRIVATE FINAL' in r[2] for r in seen) == (2 if mode == 'auto' else 0), seen
        assert not request_gate.pending() and not gate.failed
    finally:
        await gate.close()


@pytest.mark.parametrize('mode', ['trial', 'auto'])
@pytest.mark.parametrize('omit_flags', [False, True])
async def test_brassring_actual_next_reaches_review_but_shared_endpoint_cannot_send(wire_site, mode, omit_flags):
    local, seen, site = wire_site
    site['html'] = markup() + auth_markup() + '<input id=name>'
    following = markup(PAGE | {'pageid': 11, 'nextpageid': 12, 'pagetype': 3}).encode()
    replies = {'/GQWeb/SaveAndNextPage': ('text/html', following)}
    browser, gate = await browser_and_gate(mode, BASE + START, transport_factory=transport(local, replies),
        trusted_site=lambda u: request_gate.origin(u) == BASE)
    async def send(data):
        await script(browser, 'fetch(' + json.dumps(SAVE) + ',{method:"POST",headers:{"Content-Type":"application/json","RFT":"local-rft"},body:' + json.dumps(json.dumps({'serializedRequest': json.dumps(data)})) + '}).catch(()=>{})')
    try:
        await browser.navigate_to(BASE + START)
        auth = {k: v for k, v in AUTH.items() if not omit_flags or k not in ('IsCreateAccountWorkFlow', 'ResponsiveCandidateZone')}
        await post(browser, LOGIN, auth)
        assert await wait_for(lambda: any(r[0] == 'POST' and b'local-password' in r[2] for r in seen), gate)
        data = dict(DATA)
        if omit_flags:
            data.pop('DraftMode')
            data['IsGeneralJobSubmission'] = 'false'
        await send(data)
        assert await wait_for(lambda: any(r[0] == 'POST' and b'LOCAL NAME' in r[2] for r in seen), gate)
        if mode == 'trial':
            assert await wait_for(lambda: (BASE, '11') in gate.trial_policy.contracts.brassring.pages, gate)
        for change in ({'Submit': True}, {'Submit': False, 'PageId': 999}, {'Submit': False, 'PreviousPageType': 2}):
            await send(DATA | {'PrevPageId': 11, 'PageId': 12, 'PreviousPageType': 3,
                'Elements': [{'Name': 'firstName', 'Value': 'PRIVATE FINAL'}]} | change)
        if mode == 'auto':
            assert await wait_for(lambda: sum(r[0] == 'POST' and b'PRIVATE FINAL' in r[2] for r in seen) == 3, gate)
        else:
            assert await wait_for(lambda: bool(gate.trial_blocked_operation), gate)
        await gate.wait_idle()
        assert sum(r[0] == 'POST' and b'PRIVATE FINAL' in r[2] for r in seen) == (3 if mode == 'auto' else 0), seen
        assert not gate.failed and not request_gate.pending()
    finally:
        await gate.close()
