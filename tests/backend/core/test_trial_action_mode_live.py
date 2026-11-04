"""Default Trial, real native ATS-shaped forms and actual local HTTP posts."""
import asyncio
import os
import httpx
import pytest

from cli import apply_jobs
from core import request_gate, browser_policy
from tests.backend.core.test_request_gate_live import browser_and_gate, wait_for, wire_site  # noqa: F401

pytestmark=[pytest.mark.real_browser,pytest.mark.skipif(os.environ.get('LANGHIRE_REAL_BROWSER')!='1',reason='opt-in local browser')]


@pytest.mark.parametrize('mode',['trial','auto'])
@pytest.mark.parametrize('vendor',['icims','sap','taleo'])
async def test_default_live_flow_logs_in_saves_next_and_stops_only_final_control(wire_site,monkeypatch,vendor,mode,embedded=False):
    local,seen,site=wire_site
    base={'icims':'https://careers-acme.icims.com','sap':'https://career5.successfactors.eu','taleo':'https://acme.taleo.net'}[vendor]
    root={'icims':'/jobs/42/role','sap':'/career','taleo':'/careersection/application.jss'}[vendor]
    job=base+root+'?company=Acme&career_job_req_id=42'
    def form(action,label):
        return ('<form method="post" action="'+action+'"><input name="answer" value="local-test">'
            '<input type="hidden" name="dispatcher" value="opaque-'+vendor+'"><button id="action">'+label+'</button></form>')
    site['responses']={root+'?company=Acme&career_job_req_id=42':(200,{},form(root+'/login','Sign in').encode()),
        root+'/profile-page':(200,{},form(root+'/profile','Submit profile').encode()),
        root+'/next-page':(200,{},form(root+'/opaque-command','Next').encode()),
        root+'/review':(200,{},form(root+'/submit',{'icims':'Submit application','sap':'Apply','taleo':'Submit'}[vendor]).encode())}
    shell='https://careers.example.test/shell'
    if embedded:
        site['responses']['/shell']=(200,{},('<iframe width="900" height="700" src="'+job+'"></iframe>').encode())
    redirects={root+'/login':root+'/profile-page',root+'/profile':root+'/next-page',root+'/opaque-command':root+'/review'}
    def forward(req):
        with httpx.Client(trust_env=False) as client:
            reply=client.request(req.method,local+req.url.raw_path.decode().lstrip('/'),content=req.content,headers=dict(req.headers))
        if req.method=='POST' and req.url.path in redirects:
            return httpx.Response(303,headers={'location':base+redirects[req.url.path]},content=b'')
        return httpx.Response(reply.status_code,headers=reply.headers,content=reply.content)
    browser,gate=await browser_and_gate(mode,job,transport_factory=lambda:httpx.MockTransport(forward),
        trusted_site=lambda u:request_gate.origin(u)==base,strict_trial_protocols=False)
    monkeypatch.setattr(apply_jobs,'update_job',lambda *a,**kw:True)
    # A full-form approval scan is not needed to click a readable Next button.
    async def broken_scan(*a,**kw):raise RuntimeError('unrelated form scan unavailable')
    if mode=='trial':monkeypatch.setattr(apply_jobs.form_check,'scan',broken_scan)
    apply_jobs.set_run_mode(mode);ref={'request_gate':gate}
    try:tools=apply_jobs.build_tools({'url':job,'company':'Acme','title':'Local'},'',mode=='trial',ref,1)
    finally:apply_jobs.set_run_mode('auto')
    async def click():
        state=await browser.get_browser_state_summary()
        index=next(i for i,node in state.dom_state.selector_map.items() if node.tag_name=='button')
        return await tools.registry.execute_action('click',{'index':index},browser_session=browser)
    try:
        assert not gate.strict_trial_protocols
        await browser.navigate_to(shell if embedded else job)
        for path in ('/login','/profile','/opaque-command'):
            result=await click()
            assert not result.error,result
            assert await wait_for(lambda:any(r[0]=='POST' and r[1]==root+path for r in seen),gate)
        assert await wait_for(lambda:any(r[0]=='GET' and r[1]==root+'/review' for r in seen),gate)
        result=await click();await asyncio.sleep(.3);await gate.wait_idle()
        assert bool(result.error)==(mode=='trial')
        assert any(r[0]=='POST' and r[1]==root+'/submit' for r in seen)==(mode=='auto'),seen
        # The user's accepted distinction: a site's own request is not stopped
        # just because it could submit. The agent's final control is stopped.
        await browser_policy.evaluate(browser,"() => {fetch('"+root+"/submit',{method:'POST',body:'local site script'}).catch(()=>{});return true;}")
        assert await wait_for(lambda:any(r[0]=='POST' and r[1]==root+'/submit' and r[2]==b'local site script' for r in seen),gate)
        assert gate.healthy() and not gate.trial_blocked_operation
        assert not request_gate.pending() and not apply_jobs.pending_reviews()
    finally:
        await gate.close()


async def test_default_trial_advances_a_cross_origin_embedded_application(wire_site,monkeypatch):
    await test_default_live_flow_logs_in_saves_next_and_stops_only_final_control(
        wire_site,monkeypatch,'icims','trial',embedded=True)


# Codex review R01 (2026-11), from security/review-2026-11/test_trial_labels_live.py: a final review page
# whose control says "Submit resume" or an international "send the application" label.
from tests.backend.core.test_request_gate_live import bridge_to  # noqa: E402


async def _click_the_button(wire_site, monkeypatch, html):
    local, seen, site = wire_site
    site['html'] = html
    job = 'https://careers.example.test/role'
    browser, gate = await browser_and_gate('trial', job, transport_factory=bridge_to(local),
        trusted_site=lambda u: request_gate.origin(u) == 'https://careers.example.test',
        strict_trial_protocols=False)
    monkeypatch.setattr(apply_jobs, 'update_job', lambda *a, **kw: True)
    apply_jobs.set_run_mode('trial')
    try:
        tools = apply_jobs.build_tools({'url': job, 'company': 'Synthetic', 'title': 'Local'}, '', True,
            {'request_gate': gate}, 1)
        await browser.navigate_to(job)
        state = await browser.get_browser_state_summary()
        index = next(i for i, node in state.dom_state.selector_map.items() if node.tag_name == 'button')
        result = await tools.registry.execute_action('click', {'index': index}, browser_session=browser)
        sent = await wait_for(lambda: any(r[0] == 'POST' for r in seen), gate, tries=10)
        return result, sent
    finally:
        apply_jobs.set_run_mode('auto')
        await gate.close()


@pytest.mark.parametrize('label', ['Submit resume', 'Bewerbung absenden', 'Envoyer ma candidature', 'Submit application'])
async def test_trial_does_not_activate_a_final_application_control(wire_site, monkeypatch, label):
    result, sent = await _click_the_button(wire_site, monkeypatch,
        '<h1>Final application review</h1><p>This is the final step. Send your application.</p>'
        '<form method="post" action="/application-received"><input name="candidate" value="synthetic">'
        f'<button id="final">{label}</button></form>')
    assert not sent, f'Trial activated the final {label!r} control and sent the local application'
    assert result.error
    assert result.error.startswith('Trial run:')


async def test_trial_does_not_send_a_final_form_that_also_uploads_a_resume(wire_site, monkeypatch):
    """Codex re-check after R01: a one-page application (name, email, resume file) whose button says
    "Submit resume". Its form has a file input, and it is the final submit all the same."""
    result, sent = await _click_the_button(wire_site, monkeypatch,
        '<h1>Apply</h1><form method="post" action="/application-received" enctype="multipart/form-data">'
        '<input name="name" value="Synthetic Person"><input name="email" value="person@example.test">'
        '<input type="file" name="resume"><button id="final">Submit resume</button></form>')
    assert not sent, 'Trial sent a final application whose form also uploads a resume'
    assert result.error
    assert result.error.startswith('Trial run:')


async def test_trial_still_uploads_a_resume_on_an_upload_step(wire_site, monkeypatch):
    """The other side: an upload step whose button says "Upload resume" goes through in Trial."""
    result, sent = await _click_the_button(wire_site, monkeypatch,
        '<h1>Documents</h1><form method="post" action="/upload-step" enctype="multipart/form-data">'
        '<input type="file" name="resume"><button id="upload">Upload resume</button></form>')
    assert sent and not result.error, result.error
