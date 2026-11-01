"""Actual local HTTP: live login/steps, with final and unknown writes held."""
import asyncio
import json
import os
import pytest
from core import request_gate, browser_policy
from cli import apply_jobs
from tests.backend.core.test_request_gate_live import browser_and_gate, bridge_to, wait_for, wire_site  # noqa: F401

pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(os.environ.get('LANGHIRE_REAL_BROWSER') != '1', reason='opt-in local browser')]
BASE = 'https://acme.wd5.myworkdayjobs.com'
URL = BASE + '/en-US/Careers/application'
STEP = '/wday/cxs/acme/jobapplication/local/educations'
FINAL = '/wday/cxs/acme/jobapplication/local/finalize'
def trusted(url): return request_gate.origin(url) == BASE

@pytest.mark.parametrize('mode', ['trial','auto'])
async def test_native_login_next_cookie_redirect_and_final_button(wire_site, monkeypatch, mode):
    local, seen, site = wire_site
    login = '<form method=post action=/en-US/Careers/login><input name=username value=local@example.test><input name=password type=password value=dummy><button>Sign in</button></form>'
    step = '<form onsubmit="event.preventDefault();fetch(this.action,{method:\'POST\',body:JSON.stringify({school:\'local\'})}).then(()=>location.href=\'/review\')" method=post action="'+STEP+'"><input name=school value=local><button>Next</button></form>'
    final = '<form method=post action="'+FINAL+'"><input name=answer value=local><button>Submit application</button></form>'
    site['html']=final
    site['responses']={'/en-US/Careers/application':(302,{'Location':'/login-page'},b''),'/login-page':(200,{'Set-Cookie':'session=local; HttpOnly; Secure; Path=/; SameSite=Lax'},login.encode()),'/step-page':(200,{},step.encode()),'/review':(200,{},final.encode())}
    browser,gate=await browser_and_gate(mode,URL,transport_factory=bridge_to(local),trusted_site=trusted)
    monkeypatch.setattr(apply_jobs,'update_job',lambda *a,**k:True)
    apply_jobs.set_run_mode(mode)
    ref={'request_gate':gate}
    tools=apply_jobs.build_tools({'url':URL,'company':'Local','title':'Intern'},'',mode=='trial',ref,1)
    apply_jobs.set_run_mode('auto')
    async def click():
        state=await browser.get_browser_state_summary()
        index=next(i for i,n in state.dom_state.selector_map.items() if n.tag_name=='button')
        return await tools.registry.execute_action('click',{'index':index},browser_session=browser)
    try:
        await browser.navigate_to(URL)
        assert await gate.approve_credentials(URL)
        assert (await click()).error is None
        assert await wait_for(lambda:any(r[0]=='POST' and r[1]=='/en-US/Careers/login' for r in seen),gate)
        body=next(r[2] for r in seen if r[0]=='POST')
        assert b'username=local%40example.test' in body and b'password=dummy' in body
        assert any({k.lower():v for k,v in h.items()}.get('cookie')=='session=local' for h in site['request_headers']), site['request_headers']
        await browser.navigate_to(BASE+'/step-page')
        assert (await click()).error is None
        assert await wait_for(lambda:any(r[0]=='POST' and r[1]==STEP for r in seen),gate)
        await browser.navigate_to(BASE+'/review')
        result=await click()
        await gate.wait_idle()
        assert any(r[0]=='POST' and r[1]==FINAL for r in seen)==(mode=='auto'),seen
        assert bool(result.error)==(mode=='trial')
        assert not request_gate.pending() and not apply_jobs.pending_reviews()
    finally: await gate.close()

@pytest.mark.parametrize('mode',['trial','auto'])
@pytest.mark.parametrize('attack',['post','get','beacon','image','popup','iframe','redirect','unknown-rpc','mixed-step'])
async def test_scripts_cannot_turn_next_into_final_or_unknown_writes(wire_site,mode,attack):
    local,seen,site=wire_site
    site['html']='<button type=button>Continue</button>'
    site['responses']={'/redirect':(302,{'Location':FINAL+'?answer=PRIVATE'},b'')}
    browser,gate=await browser_and_gate(mode,URL,transport_factory=bridge_to(local),trusted_site=trusted)
    commands={
      'post':f"fetch('{FINAL}',{{method:'POST',body:'PRIVATE'}}).catch(()=>{{}})",
      'get':f"fetch('{FINAL}?answer=PRIVATE').catch(()=>{{}})",
      'beacon':f"navigator.sendBeacon('{FINAL}','PRIVATE')",
      'image':f"new Image().src='{FINAL}?answer=PRIVATE'",
      'popup':f"window.open('{FINAL}?answer=PRIVATE')",
      'iframe':f"const f=document.createElement('iframe');f.src='{FINAL}?answer=PRIVATE';document.body.append(f)",
      'redirect':"fetch('/redirect').catch(()=>{})",
      'unknown-rpc':"fetch('/rpc',{method:'POST',body:'PRIVATE'}).catch(()=>{})",
      'mixed-step':f"fetch('{STEP}',{{method:'POST',body:JSON.stringify({{school:'local',action:'submit',answer:'PRIVATE'}})}}).catch(()=>{{}})"}
    try:
        await browser.navigate_to(URL)
        # Bypass action labels on purpose: prove the actual transport boundary.
        await browser_policy.evaluate(browser,'() => {'+commands[attack]+';return true;}')
        await asyncio.sleep(.5)
        await gate.wait_idle()
        outgoing=[r for r in seen if r[0]=='POST' or 'PRIVATE' in r[1]]
        assert bool(outgoing)==(mode=='auto'),seen
        assert not request_gate.pending() and not gate.failed
    finally: await gate.close()

async def test_old_approval_cannot_submit_and_trial_finish_never_waits(wire_site,monkeypatch):
    from core import form_check,submit_guard
    local,seen,site=wire_site
    site['html']='<form method=post action="'+FINAL+'"><input name=email value=ada@example.test><button>Submit application</button></form>'
    browser,gate=await browser_and_gate('trial',URL,transport_factory=bridge_to(local),trusted_site=trusted)
    monkeypatch.setattr(apply_jobs,'update_job',lambda *a,**k:pytest.fail('Trial must not record final submit'))
    ref={'request_gate':gate}
    apply_jobs.set_run_mode('trial')
    tools=apply_jobs.build_tools({'url':URL,'company':'Local','title':'Intern'},'',True,ref,1)
    apply_jobs.set_run_mode('auto')
    try:
        await browser.navigate_to(URL)
        form=await form_check.scan(browser)
        ref['submit_guard'].approve(URL,submit_guard.fingerprint(form['fields'],form.get('approval_state')))
        state=await browser.get_browser_state_summary()
        index=next(i for i,n in state.dom_state.selector_map.items() if n.tag_name=='button')
        result=await tools.registry.execute_action('click',{'index':index},browser_session=browser)
        assert result.error and not any(r[0]=='POST' for r in seen)
        done=await asyncio.wait_for(tools.registry.execute_action('request_submit_review',{'summary':'Filled fields'},browser_session=browser),2)
        assert done.is_done and done.success and ref['review_skipped']
        assert not request_gate.pending() and not apply_jobs.pending_reviews()
    finally: await gate.close()

async def test_oracle_draft_reaches_wire_but_final_and_hidden_action_do_not(wire_site):
    local,seen,site=wire_site
    site['html']='<button>Continue</button>'
    base='https://local.fa.us2.oraclecloud.com'
    draft='/hcmRestApi/resources/11.13.18.05/recruitingCEJobApplicationDrafts'
    browser,gate=await browser_and_gate('trial',base+'/application',transport_factory=bridge_to(local),trusted_site=lambda u:request_gate.origin(u)==base)
    try:
        await browser.navigate_to(base+'/application')
        for path,data in ((draft,{'SiteNumber':'CX','RequisitionNumber':'1','Content':'bG9jYWw='}),(draft,{'CustomAction':'SUBMIT','Content':'PRIVATE'}),(draft.replace('JobApplicationDrafts','JobApplications'),{'Email':'PRIVATE'})):
            await browser_policy.evaluate(browser,'() => {fetch('+json.dumps(path)+',{method:"POST",headers:{"Content-Type":"application/json"},body:'+json.dumps(json.dumps(data))+'}).catch(()=>{});return true;}')
        await asyncio.sleep(.5)
        await gate.wait_idle()
        posts=[r for r in seen if r[0]=='POST']
        assert len(posts)==1 and posts[0][1]==draft and b'PRIVATE' not in posts[0][2],seen
        assert not request_gate.pending() and not gate.failed
    finally: await gate.close()

@pytest.mark.parametrize('mode',['trial','auto'])
async def test_workday_resume_upload_keeps_authorized_file_bytes(wire_site,tmp_path,mode):
    local,seen,site=wire_site
    upload='/wday/calypso/cxs/jobapplication/acme/jobapplication/local/resume'
    site['html']='<form action="'+upload+'" method=post enctype=multipart/form-data><input id=file type=file name=body><button>Upload</button></form>'
    document=tmp_path/'resume.txt'
    document.write_bytes(b'LOCAL TEST RESUME 123456')
    browser,gate=await browser_and_gate(mode,URL,transport_factory=bridge_to(local),trusted_site=trusted)
    gate.allowed_files=[str(document)]
    try:
        await browser.navigate_to(URL)
        cdp=await browser.get_or_create_cdp_session()
        doc=await cdp.cdp_client.send.DOM.getDocument(session_id=cdp.session_id)
        node=await cdp.cdp_client.send.DOM.querySelector(params={'nodeId':doc['root']['nodeId'],'selector':'#file'},session_id=cdp.session_id)
        await cdp.cdp_client.send.DOM.setFileInputFiles(params={'nodeId':node['nodeId'],'files':[str(document)]},session_id=cdp.session_id)
        await browser_policy.evaluate(browser,"() => {document.querySelector('form').submit();return true;}")
        assert await wait_for(lambda:any(r[0]=='POST' for r in seen),gate)
        posts=[r for r in seen if r[0]=='POST']
        assert len(posts)==1 and posts[0][1]==upload and b'LOCAL TEST RESUME 123456' in posts[0][2],seen
        assert not gate.failed and not request_gate.pending()
    finally: await gate.close()

async def test_native_next_cannot_disguise_a_final_form_post(wire_site,monkeypatch):
    local,seen,site=wire_site
    site['html']='<form method=post action="'+FINAL+'"><input name=answer value=PRIVATE><button>Next</button></form>'
    browser,gate=await browser_and_gate('trial',URL,transport_factory=bridge_to(local),trusted_site=trusted)
    apply_jobs.set_run_mode('trial')
    ref={'request_gate':gate}
    tools=apply_jobs.build_tools({'url':URL,'company':'Local','title':'Intern'},'',True,ref,1)
    apply_jobs.set_run_mode('auto')
    try:
        await browser.navigate_to(URL)
        state=await browser.get_browser_state_summary()
        index=next(i for i,n in state.dom_state.selector_map.items() if n.tag_name=='button')
        await tools.registry.execute_action('click',{'index':index},browser_session=browser)
        assert await wait_for(lambda:bool(gate.trial_blocked_operation),gate)
        await gate.wait_idle()
        assert not any(r[0]=='POST' for r in seen),seen
        assert ref['submit_guard'].attempts==0 and not request_gate.pending()
    finally: await gate.close()

async def test_trial_workday_read_lookup_post_and_runtime_resources_stay_live(wire_site):
    local,seen,site=wire_site
    path='/wday/cxs/acme/Careers/jobs'
    site['html']='<script>fetch("'+path+'",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({appliedFacets:{},limit:20,offset:0,searchText:"Analyst"})}).then(()=>document.body.dataset.loaded="yes");</script><body></body>'
    browser,gate=await browser_and_gate('trial',URL,transport_factory=bridge_to(local),trusted_site=trusted)
    try:
        await browser.navigate_to(URL)
        assert await wait_for(lambda:any(r[0]=='POST' and r[1]==path for r in seen),gate)
        assert not gate.trial_blocked_operation and not request_gate.pending()
        await browser_policy.evaluate(browser,'() => {fetch("'+path+'",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({submit:true})}).catch(()=>{});return true;}')
        await asyncio.sleep(.3)
        await gate.wait_idle()
        assert len([r for r in seen if r[0]=='POST'])==1 and gate.trial_blocked_operation,seen
    finally: await gate.close()
