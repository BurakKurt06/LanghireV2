"""Real browsers and real local HTTP; no employer accounts or application writes."""
import asyncio
import json
import os
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from cli import apply_jobs
from core import browser_policy, request_gate, secret_store
from tests.backend.core.test_request_gate_live import browser_and_gate, wait_for, wire_site  # noqa: F401
from tests.backend.core.test_trial_identity import JOB, AUTH0, authorize, login_form, native_case

pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(
    os.environ.get('LANGHIRE_REAL_BROWSER')!='1',reason='opt-in local browser')]


def local_transport(local, replies):
    def forward(request):
        with httpx.Client(trust_env=False) as client:
            response=client.request(request.method,local+request.url.raw_path.decode('ascii').lstrip('/'),
                content=request.content,headers=dict(request.headers))
        code,headers,body=replies.get((request.method,request.url.host,request.url.path),(200,{'Content-Type':'text/html'},response.content))
        return httpx.Response(code,headers=headers,content=body)
    return lambda:httpx.MockTransport(forward)


async def run_js(browser, js):
    return await browser_policy.evaluate(browser,'() => {'+js+';return true;}')


@pytest.mark.parametrize('mode',['trial','auto'])
async def test_icims_auth0_redirect_types_employer_scoped_password_and_blocks_final(wire_site,mode,monkeypatch):
    local,seen,site=wire_site
    start=authorize();login=AUTH0+'/u/login/password?state=local-state'
    employer=urlsplit(JOB).hostname
    replies={
        ('GET',employer,urlsplit(JOB).path):(200,{'Content-Type':'text/html'},
            ("<script>window.top.location.href = '"+start.replace('/',r'\/')+"';</script>").encode()),
        ('GET','login.icims.com','/authorize'):(302,{'Location':login},b''),
        ('GET','login.icims.com','/u/login/password'):(200,{'Content-Type':'text/html'},login_form().encode()),
        ('POST','login.icims.com','/u/login/password'):(200,{'Content-Type':'text/html'},b'<h1>Local sign-in received</h1>'),
    }
    browser,gate=await browser_and_gate(mode,JOB,transport_factory=local_transport(local,replies),
        trusted_site=lambda u:request_gate.origin(u)==request_gate.origin(JOB))
    monkeypatch.setattr(apply_jobs,'update_job',lambda *a,**k:True)
    apply_jobs.set_run_mode(mode)
    ref={'request_gate':gate,'secrets':{'account_email':'fake@example.test'},'secret_masks':{}}
    tools=apply_jobs.build_tools({'url':JOB,'title':'Local','company':'Acme'},'',mode=='trial',ref,1)
    apply_jobs.set_run_mode('auto')
    try:
        await browser.navigate_to(JOB)
        assert await wait_for(lambda:gate.trial_policy.identity.scope(login)==JOB,gate)
        state=await browser.get_browser_state_summary()
        for name,tag in [('username','account_email'),('password','password')]:
            idx=next(i for i,n in state.dom_state.selector_map.items() if n.tag_name=='input' and n.attributes.get('name')==name)
            result=await tools.registry.execute_action('input',{'index':idx,'text':'<secret>'+tag+'</secret>'},browser_session=browser)
            assert not result.error,result
        vault=secret_store.site_password(gate.credential_key(JOB),create=False)
        assert vault and not secret_store.site_password('https://login.icims.com',create=False)
        # Enter uses the primary default submit button, with action=default.
        await run_js(browser,'document.querySelector("button").click()')
        assert await wait_for(lambda:any(r[0]=='POST' and '/u/login/password' in r[1] for r in seen),gate)
        posted=next(r[2] for r in seen if r[0]=='POST' and '/u/login/password' in r[1])
        decoded=parse_qs(posted.decode('utf-8'),strict_parsing=True)
        assert decoded['password']==[vault] and decoded['username']==['fake@example.test']
        assert ref['credential_site']['site']==apply_jobs._site(JOB)
        # Provider authentication never grants an application-write permission.
        await browser.navigate_to(JOB.rsplit('/',1)[0]+'/review')
        await run_js(browser,'fetch("/applications/finalize",{method:"POST",body:"PRIVATE FINAL"}).catch(()=>{})')
        await asyncio.sleep(.4);await gate.wait_idle()
        assert any(r[0]=='POST' and b'PRIVATE FINAL' in r[2] for r in seen)==(mode=='auto'),seen
        assert gate.healthy() and not request_gate.pending() and not apply_jobs.pending_reviews()
    finally:await gate.close()


@pytest.mark.parametrize('mode',['trial','auto'])
@pytest.mark.parametrize('kind',['icims','taleo','sap'])
async def test_native_authentication_reaches_wire_but_changed_dispatch_does_not(wire_site,kind,mode):
    local,seen,site=wire_site
    job,url,dest,page,data=native_case(kind)
    site['html']=page
    browser,gate=await browser_and_gate(mode,job,transport_factory=local_transport(local,{}),
        trusted_site=lambda u:request_gate.origin(u)==request_gate.origin(job))
    try:
        await browser.navigate_to(url)
        assert await wait_for(lambda:gate.trial_policy.identity.scope(url)==job,gate)
        async def send(payload,path=dest):
            await run_js(browser,'fetch('+json.dumps(path)+',{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:'+json.dumps(urlencode(payload))+'}).catch(()=>{})')
        await send(data)
        assert await wait_for(lambda:any(r[0]=='POST' for r in seen),gate)
        before=len([r for r in seen if r[0]=='POST'])
        await send(data|{'operation':'submit','marker':'PRIVATE FINAL'})
        await asyncio.sleep(.4);await gate.wait_idle()
        posts=[r for r in seen if r[0]=='POST']
        assert len(posts)==before+(1 if mode=='auto' else 0),seen
        assert any(b'PRIVATE+FINAL' in r[2] for r in posts)==(mode=='auto')
        assert not request_gate.pending() and gate.healthy()
    finally:await gate.close()


@pytest.mark.parametrize('mode',['trial','auto'])
async def test_sap_native_job_return_login_posts_but_application_commands_stay_blocked(wire_site,mode):
    from tests.backend.core.test_trial_identity import sap_return_case
    local,seen,site=wire_site
    job,dest,source,data=sap_return_case()
    site['html']=source
    browser,gate=await browser_and_gate(mode,job,transport_factory=local_transport(local,{}),
        trusted_site=lambda u:request_gate.origin(u)==request_gate.origin(job))
    try:
        await browser.navigate_to(job)
        assert await wait_for(lambda:gate.trial_policy.identity.scope(job)==job,gate)
        await run_js(browser, '''document.querySelector('[name=username]').value='fake@example.test';
            document.querySelector('[name=password]').value='Local-Only1';document.querySelector('form').submit();''')
        assert await wait_for(lambda:any(r[0]=='POST' for r in seen),gate)
        posted=next(r[2] for r in seen if r[0]=='POST')
        fields=parse_qs(posted.decode(),keep_blank_values=True)
        assert fields['career_ns']==['job_application','job_application']
        assert fields['career_os']==[''] and fields['password']==['Local-Only1']
        for key,value in [('career_os','submit'),('career_ns','submit'),('login_ns','register'),
                ('company','Other'),('career_job_req_id','43')]:
            payload=urlencode([(k,value if k==key else v) for k,v in data])
            await run_js(browser,'fetch('+json.dumps(dest)+',{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:'+json.dumps(payload)+'}).catch(()=>{})')
        await asyncio.sleep(.5)
        await gate.wait_idle()
        assert len([r for r in seen if r[0]=='POST'])==(6 if mode=='auto' else 1),seen
        assert gate.healthy() and not request_gate.pending()
    finally:
        await gate.close()


@pytest.mark.parametrize('poison',['state','duplicate','destination','operation','social'])
@pytest.mark.parametrize('mode',['trial','auto'])
async def test_provider_authentication_cannot_smuggle_another_operation(wire_site,mode,poison):
    local,seen,site=wire_site;start=authorize();login=AUTH0+'/u/login/password?state=local-state'
    site['html']=("<script>window.top.location.href = '"+start.replace('/',r'\/')+"';</script>")
    replies={('GET','login.icims.com','/authorize'):(302,{'Location':login},b''),
        ('GET','login.icims.com','/u/login/password'):(200,{'Content-Type':'text/html'},login_form().encode())}
    browser,gate=await browser_and_gate(mode,JOB,transport_factory=local_transport(local,replies),
        trusted_site=lambda u:request_gate.origin(u)==request_gate.origin(JOB))
    try:
        await browser.navigate_to(JOB)
        assert await wait_for(lambda:gate.trial_policy.identity.scope(login)==JOB,gate)
        data={'state':'local-state','username':'fake@example.test','password':'PRIVATE FINAL','action':'default','js-available':'true'}
        target=login
        if poison=='state':data['state']='another-state'
        if poison=='operation':data['action']='submit'
        if poison=='social':data['connection']='google-oauth2'
        if poison=='destination':target=AUTH0+'/u/login?state=unbound'
        body=urlencode(data)+('&action=default' if poison=='duplicate' else '')
        await run_js(browser,'fetch('+json.dumps(target)+',{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:'+json.dumps(body)+'}).catch(()=>{})')
        await asyncio.sleep(.4);await gate.wait_idle()
        assert any(r[0]=='POST' for r in seen)==(mode=='auto'),seen
        assert not request_gate.pending() and gate.healthy()
    finally:await gate.close()


@pytest.mark.parametrize('mode',['trial','auto'])
async def test_provider_rotated_state_advances_to_verification_code_on_real_wire(wire_site,mode):
    local,seen,site=wire_site;start=authorize();login=AUTH0+'/u/login/password?state=local-state'
    next_url=AUTH0+'/u/email-verification?state=next-state'
    site['html']=("<script>window.top.location.href = '"+start.replace('/',r'\/')+"';</script>")
    verification=login_form('next-state').replace('name="password"','name="code"')
    replies={('GET','login.icims.com','/authorize'):(302,{'Location':login},b''),
        ('GET','login.icims.com','/u/login/password'):(200,{'Content-Type':'text/html'},login_form().encode()),
        ('POST','login.icims.com','/u/login/password'):(302,{'Location':next_url},b''),
        ('GET','login.icims.com','/u/email-verification'):(200,{'Content-Type':'text/html'},verification.encode()),
        ('POST','login.icims.com','/u/email-verification'):(200,{'Content-Type':'text/html'},b'<h1>Local code accepted</h1>')}
    browser,gate=await browser_and_gate(mode,JOB,transport_factory=local_transport(local,replies),
        trusted_site=lambda u:request_gate.origin(u)==request_gate.origin(JOB))
    try:
        await browser.navigate_to(JOB)
        assert await wait_for(lambda:gate.trial_policy.identity.scope(login)==JOB,gate)
        await run_js(browser,'document.querySelector("[name=username]").value="fake@example.test";document.querySelector("[name=password]").value="Local-Only1";document.querySelector("button").click()')
        assert await wait_for(lambda:gate.trial_policy.identity.scope(next_url)==JOB,gate)
        # Local fake code, never Gmail or an actual authentication token.
        await run_js(browser,'document.querySelector("[name=code]").value="123456";document.querySelector("button").click()')
        assert await wait_for(lambda:any(r[0]=='POST' and '/u/email-verification' in r[1] for r in seen),gate)
        posted=next(r[2] for r in seen if r[0]=='POST' and '/u/email-verification' in r[1])
        assert parse_qs(posted.decode())['code']==['123456']
        assert not gate.trial_blocked_operation and not request_gate.pending() and gate.healthy()
    finally:await gate.close()


@pytest.mark.parametrize('mode',['trial','auto'])
async def test_successfactors_api_alias_allows_login_but_holds_application_dispatch(wire_site,mode):
    local,seen,site=wire_site
    job,url,dest,page,data=native_case('sap')
    job,url,dest,page=(s.replace('career5.successfactors.eu','api2.successfactors.eu') for s in (job,url,dest,page))
    site['html']=page
    browser,gate=await browser_and_gate(mode,job,transport_factory=local_transport(local,{}),
        trusted_site=lambda u:request_gate.origin(u)==request_gate.origin(job))
    try:
        await browser.navigate_to(url)
        assert await wait_for(lambda:gate.trial_policy.identity.scope(url)==job,gate)
        for payload in (data,data|{'career_ns':'job_application','career_os':'submit'}):
            await run_js(browser,'fetch('+json.dumps(dest)+',{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:'+json.dumps(urlencode(payload))+'}).catch(()=>{})')
        assert await wait_for(lambda:any(r[0]=='POST' for r in seen),gate)
        await asyncio.sleep(.4);await gate.wait_idle()
        posts=[parse_qs(r[2].decode(),keep_blank_values=True) for r in seen if r[0]=='POST']
        assert any(p.get('career_os')==['submit'] for p in posts)==(mode=='auto'),posts
        assert not request_gate.pending() and gate.healthy()
    finally:await gate.close()
