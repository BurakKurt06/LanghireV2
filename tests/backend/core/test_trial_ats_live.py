"""Real HTTP/browser ATS progression and disguised final writes. Local data only."""
import asyncio
import json
import os

import httpx
import pytest

from cli import apply_jobs
from core import browser_policy, request_gate
from tests.backend.core.test_trial_contracts import QUERIES, ORACLE, API, form
from tests.backend.core.test_request_gate_live import browser_and_gate, wait_for, wire_site  # noqa: F401

pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(
    os.environ.get('LANGHIRE_REAL_BROWSER') != '1', reason='opt-in local browser')]


def transport(local, replies):
    def forward(request):
        # Every allowed operation really opens a local HTTP connection and
        # reaches the listener. Browser-facing API replies emulate the ATS.
        with httpx.Client(trust_env=False) as client:
            response=client.request(request.method,local+request.url.raw_path.decode('ascii').lstrip('/'),
                content=request.content,headers=dict(request.headers))
        mime,body=replies.get(request.url.path,('text/html',response.content))
        return httpx.Response(204 if request.method=='OPTIONS' else response.status_code,
            headers={'content-type':mime,'access-control-allow-origin':'*','access-control-allow-methods':'GET, POST, PATCH, DELETE, OPTIONS',
                     'access-control-allow-headers':'content-type'},content=b'' if request.method=='OPTIONS' else body)
    return lambda:httpx.MockTransport(forward)


async def script(browser, code):
    return await browser_policy.evaluate(browser,'() => {'+code+'; return true;}')


async def post(browser,path,data):
    await script(browser,'fetch('+json.dumps(path)+',{method:"POST",headers:{"Content-Type":"application/json"},body:'+json.dumps(json.dumps(data))+'}).catch(()=>{})')


@pytest.mark.parametrize('vendor',['greenhouse','workable'])
@pytest.mark.parametrize('mode',['trial','auto'])
async def test_signed_ats_resume_upload_reaches_storage_once_and_final_is_held(wire_site,tmp_path,vendor,mode):
    local,seen,site=wire_site
    base='https://job-boards.greenhouse.io' if vendor=='greenhouse' else 'https://apply.workable.com'
    start='/acme/jobs/1' if vendor=='greenhouse' else '/acme/j/LOCAL1/'
    signing='/uncacheable_attributes/presigned_fields' if vendor=='greenhouse' else '/api/v1/jobs/LOCAL1/form/upload/resume'
    signing_url='https://boards.greenhouse.io'+signing+'?fields[]=resume&fields[]=cover_letter' if vendor=='greenhouse' else signing
    storage='https://files.s3.us-east-1.amazonaws.com/upload'
    fields={'key':'local/resume.txt','policy':'signed','x-amz-signature':'local'}
    if vendor=='greenhouse':
        signed={'policy':'signed','x-amz-signature':'local'}
        grant={'url':storage,'resume':{'fields':signed,'key':'stash/resumes/{timestamp}-{unique_id}-fixed'}}
        fields=signed|{'key':'stash/resumes/1792440000000-abc123-fixed','utf8':'✓','authenticity_token':'1234','Content-Type':'application/octet-stream'}
    else:grant={'url':storage,'fields':fields}
    site['html']='<input type=file id=file><button id=next>Weiter</button>'
    replies={signing:('application/json',json.dumps(grant).encode()),'/upload':('text/plain',b'uploaded')}
    browser,gate=await browser_and_gate(mode,base+start,transport_factory=transport(local,replies),trusted_site=lambda u:u.startswith(base+'/acme/'))
    document=tmp_path/'resume.txt';document.write_bytes(b'LOCAL TEST RESUME');gate.allowed_files=[str(document)]
    try:
        await browser.navigate_to(base+start)
        await script(browser,'fetch('+json.dumps(signing_url)+').then(r=>r.json()).then(g=>window.uploadGrant=g)')
        assert await wait_for(lambda:bool(gate.trial_policy.contracts.grants) if mode=='trial' else any(r[1].split('?')[0]==signing for r in seen),gate)
        cdp=await browser.get_or_create_cdp_session();doc=await cdp.cdp_client.send.DOM.getDocument(session_id=cdp.session_id)
        node=await cdp.cdp_client.send.DOM.querySelector(params={'nodeId':doc['root']['nodeId'],'selector':'#file'},session_id=cdp.session_id)
        await cdp.cdp_client.send.DOM.setFileInputFiles(params={'nodeId':node['nodeId'],'files':[str(document)]},session_id=cdp.session_id)
        command='const f=new FormData();for(const [k,v] of Object.entries('+json.dumps(fields)+'))f.append(k,v);f.append("file",document.querySelector("#file").files[0]);fetch('+json.dumps(storage)+',{method:"POST",body:f}).catch(()=>{})'
        await script(browser,command)
        assert await wait_for(lambda:any(r[0]=='POST' and r[1]=='/upload' for r in seen),gate)
        await script(browser,command)
        await post(browser,'/api/v1/jobs/LOCAL1/apply' if vendor=='workable' else '/acme/jobs/1',{'candidate':'PRIVATE FINAL'})
        await asyncio.sleep(.4);await gate.wait_idle()
        uploads=[r for r in seen if r[0]=='POST' and r[1]=='/upload']
        assert len(uploads)==(1 if mode=='trial' else 2),seen
        assert all(b'LOCAL TEST RESUME' in r[2] for r in uploads)
        assert any(r[0]=='POST' and b'PRIVATE FINAL' in r[2] for r in seen)==(mode=='auto'),seen
        assert not request_gate.pending() and not gate.failed
    finally:await gate.close()


@pytest.mark.parametrize('mode',['trial','auto'])
async def test_lever_parser_uses_hidden_account_and_complete_file_but_never_final(wire_site,tmp_path,mode):
    local,seen,site=wire_site;base='https://jobs.lever.co';url=base+'/acme/role/apply'
    site['html']='<input type=hidden name=accountId value=local-account><input type=file id=file>'
    browser,gate=await browser_and_gate(mode,url,transport_factory=transport(local,{'/parseResume':('application/json',b'{"name":"Local Test"}')}),trusted_site=lambda u:u==url)
    file=tmp_path/'resume.txt';file.write_bytes(b'LOCAL RESUME');gate.allowed_files=[str(file)]
    try:
        await browser.navigate_to(url)
        cdp=await browser.get_or_create_cdp_session();doc=await cdp.cdp_client.send.DOM.getDocument(session_id=cdp.session_id)
        node=await cdp.cdp_client.send.DOM.querySelector(params={'nodeId':doc['root']['nodeId'],'selector':'#file'},session_id=cdp.session_id)
        await cdp.cdp_client.send.DOM.setFileInputFiles(params={'nodeId':node['nodeId'],'files':[str(file)]},session_id=cdp.session_id)
        await script(browser,'const f=new FormData();f.append("resume",document.querySelector("#file").files[0]);f.append("accountId","local-account");fetch("/parseResume",{method:"POST",body:f}).catch(()=>{})')
        assert await wait_for(lambda:any(r[0]=='POST' and r[1]=='/parseResume' for r in seen),gate)
        await post(browser,'/acme/role/apply',{'candidate':'PRIVATE FINAL'})
        await asyncio.sleep(.3);await gate.wait_idle()
        assert any(r[0]=='POST' and b'LOCAL RESUME' in r[2] for r in seen)
        assert any(r[0]=='POST' and b'PRIVATE FINAL' in r[2] for r in seen)==(mode=='auto')
        assert not gate.failed and not request_gate.pending()
    finally:await gate.close()


@pytest.mark.parametrize('mode',['trial','auto'])
async def test_ashby_lookup_saves_form_answers_but_spoofed_graphql_final_never_reaches_wire(wire_site,mode):
    local,seen,site=wire_site;base='https://jobs.ashbyhq.com';url=base+'/acme/role/application';path='/api/non-user-graphql'
    site['html']='<input id=name><button>Continuer</button>'
    reply={'data':{'jobPosting':{'applicationForm':{'__typename':'FormRender','id':'local-form'}}}}
    browser,gate=await browser_and_gate(mode,url,transport_factory=transport(local,{path:('application/json',json.dumps(reply).encode())}),trusted_site=lambda u:u.startswith(base+'/acme/'))
    try:
        await browser.navigate_to(url)
        async def operation(op,variables,query=None):
            await post(browser,path+'?op='+op,{'operationName':op,'variables':variables,'query':query or QUERIES[op]})
        await operation('ApiJobPosting',{'organizationHostedJobsPageName':'acme','jobPostingId':'role'})
        assert await wait_for(lambda:'local-form' in gate.trial_policy.contracts.form_ids if mode=='trial' else any(r[0]=='POST' for r in seen),gate)
        await operation('ApiSetFormValue',{'organizationHostedJobsPageName':'acme','formRenderIdentifier':'local-form','path':'name','value':'Local Test'})
        assert await wait_for(lambda:any(r[0]=='POST' and b'Local Test' in r[2] for r in seen),gate)
        await operation('ApiSetFormValue',{'organizationHostedJobsPageName':'acme','formRenderIdentifier':'local-form','path':'name','value':'PRIVATE FINAL'},'mutation ApiSetFormValue { submitSingleApplicationFormAction }')
        await asyncio.sleep(.3);await gate.wait_idle()
        assert any(r[0]=='POST' and b'PRIVATE FINAL' in r[2] for r in seen)==(mode=='auto'),seen
        assert bool(gate.trial_blocked_operation)==(mode=='trial') and not gate.failed and not request_gate.pending()
    finally:await gate.close()


@pytest.mark.parametrize('mode',['trial','auto'])
async def test_oracle_custom_domain_login_draft_and_final_use_different_permissions(wire_site,mode):
    local,seen,site=wire_site;url='https://careers.example/hcmUI/CandidateExperience/en/sites/local'
    site['html']='<script src="https://static.oracle.com/cdn/fa/oj-hcm-ce/2607/main.js"></script><div data-apibaseurl="'+ORACLE+':443" data-sitenumber="CX_1"></div>'
    path=API+'recruitingCEVerificationTokens'
    replies={path:('application/json',b'{"URLShortCode":"local-token","AccessCode":"local-access"}'),'/cdn/fa/oj-hcm-ce/2607/main.js':('application/javascript',b'')}
    browser,gate=await browser_and_gate(mode,url,transport_factory=transport(local,replies),trusted_site=lambda u:request_gate.origin(u)=='https://careers.example')
    try:
        await browser.navigate_to(url)
        await post(browser,ORACLE+path,{'SiteNumber':'CX_1','Mode':'JOB_APPLY','Email':'local@example.test','RequisitionNumber':'1'})
        assert await wait_for(lambda:any(r[0]=='POST' and b'JOB_APPLY' in r[2] for r in seen),gate)
        if mode=='trial':assert await wait_for(lambda:(ORACLE,'local-token') in gate.trial_policy.contracts.oracle.tokens,gate)
        await post(browser,ORACLE+path,{'SiteNumber':'CX_1','URLShortCode':'local-token','ShortCode':'123456'})
        await post(browser,ORACLE+API+'recruitingCEJobApplicationDrafts',{'SiteNumber':'CX_1','RequisitionNumber':'1','Content':'bG9jYWw='})
        assert await wait_for(lambda:any(r[0]=='POST' and 'JobApplicationDrafts' in r[1] for r in seen),gate)
        await post(browser,ORACLE+path,{'Mode':'JOB_APPLY_CONFIRM','URLShortCode':'PRIVATE FINAL'})
        await post(browser,ORACLE+API+'recruitingCEJobApplications',{'Action':'CREATE','Name':'PRIVATE FINAL'})
        await asyncio.sleep(.3);await gate.wait_idle()
        assert any(r[0]=='POST' and b'ShortCode' in r[2] for r in seen)
        assert any(r[0]=='POST' and b'PRIVATE FINAL' in r[2] for r in seen)==(mode=='auto'),seen
        assert not gate.failed and not request_gate.pending()
    finally:await gate.close()


@pytest.mark.parametrize('trigger',['evaluate','localized-click','unreadable-click','keyboard'])
async def test_canonical_actions_advance_live_trial_and_transport_holds_disguised_final(wire_site,monkeypatch,trigger):
    local,seen,site=wire_site;base='https://acme.wd5.myworkdayjobs.com';url=base+'/en-US/External/application'
    step='/wday/cxs/acme/jobapplication/local/educations'
    site['html']='<form onsubmit="event.preventDefault();fetch(\''+step+'\',{method:\'POST\',body:JSON.stringify({school:\'local\'})})"><input id=name><button>Weiter</button></form>'
    browser,gate=await browser_and_gate('trial',url,transport_factory=transport(local,{}),trusted_site=lambda u:request_gate.origin(u)==base)
    monkeypatch.setattr(apply_jobs,'update_job',lambda *a,**k:pytest.fail('Trial must not record submission'))
    apply_jobs.set_run_mode('trial');ref={'request_gate':gate};tools=apply_jobs.build_tools({'url':url,'company':'Local','title':'Intern'},'',True,ref,1);apply_jobs.set_run_mode('auto')
    try:
        await browser.navigate_to(url)
        if trigger=='evaluate':
            result=await tools.registry.execute_action('evaluate',{'code':'(() => {document.querySelector("button").click();return true;})()'},browser_session=browser)
        elif trigger=='keyboard':
            await script(browser,'document.querySelector("#name").focus()')
            result=await tools.registry.execute_action('send_keys',{'keys':'Enter'},browser_session=browser)
        else:
            state=await browser.get_browser_state_summary();idx=next(i for i,n in state.dom_state.selector_map.items() if n.tag_name=='button')
            if trigger=='unreadable-click':
                async def broken(*a,**k):raise RuntimeError('frame metadata unavailable')
                monkeypatch.setattr(apply_jobs,'_guard_inputs',broken)
            result=await tools.registry.execute_action('click',{'index':idx},browser_session=browser)
        assert result.error is None,result
        assert await wait_for(lambda:any(r[0]=='POST' and r[1]==step for r in seen),gate)
        await script(browser,'fetch("/rpc",{method:"POST",body:"PRIVATE FINAL"}).catch(()=>{})')
        await asyncio.sleep(.3);await gate.wait_idle()
        assert not any(r[0]=='POST' and b'PRIVATE FINAL' in r[2] for r in seen)
        assert not ref['submit_guard'].attempts and not request_gate.pending() and not apply_jobs.pending_reviews()
    finally:await gate.close()


@pytest.mark.parametrize('mode',['trial','auto'])
async def test_unclosable_worker_cannot_send_but_parent_page_still_advances(wire_site,mode):
    local,seen,site=wire_site;base='https://acme.wd5.myworkdayjobs.com';url=base+'/en-US/External/application'
    step='/wday/cxs/acme/jobapplication/local/educations'
    site['html']='<h1>Application form</h1><input id=name>'
    browser,gate=await browser_and_gate(mode,url,transport_factory=transport(local,{}),trusted_site=lambda u:request_gate.origin(u)==base)
    try:
        await browser.navigate_to(url)
        worker='fetch('+json.dumps(base+'/worker-final')+',{method:"POST",body:"PRIVATE WORKER"}).catch(()=>{});'
        await script(browser,'window.heldWorker=new Worker(URL.createObjectURL(new Blob(['+json.dumps(worker)+'],{type:"text/javascript"})))')
        await asyncio.sleep(.5)
        assert gate.healthy(),getattr(gate,'failure','')
        await post(browser,step,{'school':'local after worker'})
        assert await wait_for(lambda:any(r[0]=='POST' and r[1]==step for r in seen),gate)
        assert not any(r[0]=='POST' and b'PRIVATE WORKER' in r[2] for r in seen),seen
        assert gate.healthy() and not request_gate.pending()
    finally:await gate.close()


@pytest.mark.parametrize('challenge',[
    'https://api.hcaptcha.com/checksiteconfig?sitekey=dummy',
    'https://www.recaptcha.net/recaptcha/enterprise/reload?k=dummy',
    'https://acme.wd5.myworkdayjobs.com/cdn-cgi/challenge-platform/h/b/jsd/oneshot/local/proof',
])
@pytest.mark.parametrize('mode',['trial','auto'])
async def test_challenge_proof_and_next_do_not_grant_final_submission(wire_site,mode,challenge):
    local,seen,site=wire_site;base='https://acme.wd5.myworkdayjobs.com';url=base+'/en-US/External/application'
    site['html']='<h1>Local fake challenge</h1>'
    browser,gate=await browser_and_gate(mode,url,transport_factory=transport(local,{}),trusted_site=lambda u:request_gate.origin(u)==base)
    try:
        await browser.navigate_to(url)
        await script(browser,'fetch('+json.dumps(challenge)+',{method:"POST",body:"dummy-proof"}).catch(()=>{})')
        assert await wait_for(lambda:any(r[0]=='POST' and b'dummy-proof' in r[2] for r in seen),gate)
        assert not gate.trial_blocked_operation
        await post(browser,'/wday/cxs/acme/jobapplication/local/finalize',{'answer':'PRIVATE FINAL'})
        await asyncio.sleep(.3);await gate.wait_idle()
        assert any(r[0]=='POST' and b'PRIVATE FINAL' in r[2] for r in seen)==(mode=='auto'),seen
        assert not request_gate.pending() and gate.healthy()
    finally:await gate.close()


@pytest.mark.parametrize('poison',['boundary','name','mixed'])
@pytest.mark.parametrize('mode',['trial','auto'])
async def test_multipart_parameter_ambiguity_cannot_reach_signed_upload(wire_site,tmp_path,poison,mode):
    local,seen,site=wire_site;base='https://files.s3.us-east-1.amazonaws.com';url=base+'/'
    site['html']='<h1>Local upload test</h1>'
    browser,gate=await browser_and_gate(mode,url,transport_factory=transport(local,{}),trusted_site=lambda u:False)
    file=tmp_path/'resume.txt';file.write_bytes(b'LOCAL RESUME');gate.allowed_files=[str(file)]
    gate.trial_policy.contracts._grant(url,{'key':'local','policy':'signed'})
    mime='multipart/form-data; boundary=local';body=form({'key':'local','policy':'signed'})
    if poison=='boundary':mime+='; boundary=other'
    if poison=='name':body=body.replace(b'name="file"',b'name="file"; name="other"')
    if poison=='mixed':mime='multipart/mixed; boundary=local'
    try:
        await browser.navigate_to(url)
        await script(browser,'fetch("/",{method:"POST",headers:{"Content-Type":'+json.dumps(mime)+'},body:'+json.dumps(body.decode())+'}).catch(()=>{})')
        await asyncio.sleep(.4);await gate.wait_idle()
        assert any(r[0]=='POST' for r in seen)==(mode=='auto'),seen
        assert not request_gate.pending() and gate.healthy()
    finally:await gate.close()
