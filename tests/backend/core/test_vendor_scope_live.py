"""Exercise the real input wrapper; Auto controls make real local POSTs."""
import os
from urllib.parse import parse_qs

import pytest

from cli import apply_jobs
from core import request_gate, secret_store
from tests.backend.core.test_request_gate_live import browser_and_gate, wait_for, wire_site  # noqa: F401
from tests.backend.core.test_trial_ats_live import script, transport


pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(
    os.environ.get('LANGHIRE_REAL_BROWSER') != '1', reason='opt-in local browser')]


@pytest.mark.parametrize('job,others', [
    ('https://sjobs.brassring.com/TGNewUI/GQ?partnerid=123&siteid=456', [
        'https://sjobs.brassring.com/TGNewUI/GQ?partnerid=123&siteid=999',
        'https://sjobs.brassring.com/TGNewUI/GQ?partnerid=123&siteid=456&siteid=999']),
    ('https://jobs.jobvite.com/acme/job/abc', ['https://jobs.jobvite.com/other/job/abc', 'https://jobs.jobvite.com/']),
    ('https://recruiting.ultipro.com/ACME1003/JobBoard/123/Account/Login', [
        'https://recruiting.ultipro.com/OTHER1003/JobBoard/123/Account/Login', 'https://recruiting.ultipro.com/']),
])
async def test_shared_vendor_cannot_type_another_employers_password_then_post_it(wire_site, monkeypatch, job, others):
    local, seen, site = wire_site
    site['html'] = '<form method=post><input name=password type=password><button>Sign in</button></form>'
    browser, gate = await browser_and_gate('auto', job, transport_factory=transport(local, {}),
        trusted_site=lambda u: bool(apply_jobs.employer_site(u, job)))
    monkeypatch.setattr(apply_jobs, 'update_job', lambda *a, **k: True)
    ref = {'request_gate': gate, 'secrets': {}, 'secret_masks': {}}
    apply_jobs.set_run_mode('auto')
    tools = apply_jobs.build_tools({'url': job, 'title': 'Local', 'company': 'Acme'}, '', False, ref, 1)
    try:
        await browser.navigate_to(job)
        state = await browser.get_browser_state_summary()
        index = next(i for i, node in state.dom_state.selector_map.items()
            if node.tag_name == 'input' and node.attributes.get('name') == 'password')
        result = await tools.registry.execute_action('input', {'index': index, 'text': '<secret>password</secret>'}, browser_session=browser)
        assert not result.error, result
        password = secret_store.site_password(gate.credential_key(job), create=False)
        assert password
        await script(browser, 'document.querySelector("form").submit()')
        assert await wait_for(lambda: any(r[0] == 'POST' for r in seen), gate)
        assert parse_qs(next(r[2] for r in seen if r[0] == 'POST').decode())['password'] == [password]
        for other in others:
            await browser.navigate_to(other)
            state = await browser.get_browser_state_summary()
            index = next(i for i, node in state.dom_state.selector_map.items()
                if node.tag_name == 'input' and node.attributes.get('name') == 'password')
            result = await tools.registry.execute_action('input', {'index': index, 'text': '<secret>password</secret>'}, browser_session=browser)
            assert result.error, result
            before = sum(r[0] == 'POST' for r in seen)
            await script(browser, 'document.querySelector("form").submit()')
            assert await wait_for(lambda: sum(r[0] == 'POST' for r in seen) > before, gate)
            body = [r[2] for r in seen if r[0] == 'POST'][-1]
            assert parse_qs(body.decode(), keep_blank_values=True).get('password') == ['']
        assert gate.healthy() and not request_gate.pending()
    finally:
        await gate.close()
