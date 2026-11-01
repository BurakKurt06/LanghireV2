"""Candidate auth protocols, with local state and fake credentials only."""
import json
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from core import request_gate, job_page
from core.trial_policy import TrialPolicy
from core.trial_identity import AUTH0, CandidateIdentity, Forms
from tests.backend.core.test_trial_contracts import req
from tests.backend.core.test_request_gate_live import wire_site  # noqa: F401

JOB = 'https://careers-acme.icims.com/jobs/42/local-role/job'
HTML = [{'name':'Content-Type','value':'text/html'}]
CALLBACK = 'https://careers-acme.icims.com/jobs/login?'+urlencode({
    'redirect':'job','css_submitMode':'1','universalLogin':'1','jobid':'42','hashed':'123'})


def authorize(**replace):
    return AUTH0+'/authorize?'+urlencode(dict(redirect_uri=CALLBACK,
        client_id='local_client_identifier_123', audience='https://api.icims.com/v1/',
        scope='openid profile email',organization='org_acme',tenantId='hs-42',
        response_type='code',state='original_client_state_1234',nonce='local-nonce')|replace)


def login_form(state='local-state', route='/u/login/password'):
    return ('<form method="POST"><input type="hidden" name="state" value="'+state+'">'
        '<input name="username"><input name="password" type="password">'
        '<input type="hidden" name="js-available" value="false">'
        '<button name="action" value="default">Continue</button></form>')


def prepare(policy, *, start=None, login=None, page=None):
    identity = policy.identity if isinstance(policy, TrialPolicy) else policy
    start = start or authorize()
    login = login or AUTH0+'/u/login/password?state=local-state'
    identity.observe(req(JOB,method='GET'),200,HTML,
        ("<script>window.top.location.href = '"+start.replace('/',r'\/')+"';</script>").encode(),True)
    identity.observe(req(start,method='GET'),302,[{'name':'Location','value':login}],b'',True)
    identity.observe(req(login,method='GET'),200,HTML,(page or login_form()).encode(),True)
    return login


def login_request(url, **replace):
    return req(url,urlencode(dict(state='local-state',username='fake@example.test',password='Local-Only1',
        **{'js-available':'true','action':'default'})|replace),mime='application/x-www-form-urlencoded')


@pytest.fixture
def policy():
    return TrialPolicy(lambda u:request_gate.origin(u)==request_gate.origin(JOB),job_url=JOB)


def test_authorization_redirect_and_served_form_are_both_required(policy):
    login=prepare(policy)
    assert policy.identity.scope(login)==JOB
    assert policy.check(login_request(login)).allowed
    for url in (login.replace('local-state','another-state'),AUTH0+'/u/login?state=local-state',AUTH0+'/submit'):
        assert not policy.identity.scope(url)
        assert not policy.check(login_request(url)).allowed
    blank=TrialPolicy(lambda u:True,job_url=JOB)
    blank.identity.observe(req(login,method='GET'),200,HTML,login_form().encode(),True)
    assert not blank.check(login_request(login)).allowed


@pytest.mark.parametrize('changes',[
    {'redirect_uri':CALLBACK.replace('careers-acme','careers-other')},
    {'redirect_uri':CALLBACK.replace('jobid=42','jobid=43')},
    {'redirect_uri':CALLBACK.replace('/jobs/login','/applications/finalize')},
    {'redirect_uri':CALLBACK+'&action=submit'},
    {'redirect_uri':CALLBACK+'&jobid=42'},
    {'response_type':'token'}, {'scope':'openid profile email gmail.readonly'},
    {'audience':'https://api.other.test/'}, {'state':'short'}, {'organization':''},
    {'client_id':''}, {'tenantId':''}, {'nonce':''},
])
def test_unbound_or_overprivileged_authorization_never_delegates(policy,changes):
    login=prepare(policy,start=authorize(**changes))
    assert not policy.identity.scope(login)
    assert not policy.check(login_request(login)).allowed


@pytest.mark.parametrize('changes',[
    {'state':'another-state'}, {'action':'submit'}, {'action':'default&submit=true'},
    {'connection':'google-oauth2'}, {'submit':'true'}, {'operation':'finalize'},
    {'redirect_uri':'https://outside.test/'}, {'js-available':'not-a-boolean'},
])
def test_form_operation_and_state_cannot_be_overridden(policy,changes):
    login=prepare(policy)
    assert not policy.check(login_request(login,**changes)).allowed


@pytest.mark.parametrize('extra',['&state=local-state','&action=default','&%73ubmit=true','&password=another',
    '&operation%ZZ=submit','&username=%ED%A0%80'])
def test_duplicate_or_malformed_form_is_not_sent(policy,extra):
    login=prepare(policy);request=login_request(login);request['postData']+=extra
    assert not policy.check(request).allowed


@pytest.mark.parametrize('page',[
    login_form().replace('local-state','another-state'),
    login_form().replace('<form','<base href="https://outside.test/"><form'),
    login_form().replace('name="state"','name="state" name="operation"'),
    login_form().replace('<button','<button formaction="/submit"'),
    login_form().replace('<input name="username">','<input name="connection" value="google-oauth2">'),
    login_form().replace('<form method="POST">','<form action="https://outside.test/collect" method="POST">'),
])
def test_ambiguous_or_social_forms_do_not_create_authority(policy,page):
    login=prepare(policy,page=page)
    assert not policy.identity.scope(login)
    assert not policy.check(login_request(login)).allowed


def test_expired_transaction_does_not_retain_credentials_or_writes(policy,monkeypatch):
    from core import trial_identity
    login=prepare(policy);now=trial_identity.time.monotonic()
    monkeypatch.setattr(trial_identity.time,'monotonic',lambda:now+1000)
    assert not policy.identity.scope(login)
    assert not policy.check(login_request(login)).allowed


def test_successful_login_can_rotate_provider_state_but_a_changed_dispatch_cannot(policy):
    login=prepare(policy)
    next_url=AUTH0+'/u/email-verification?state=next-state'
    headers=[{'name':'Location','value':next_url}]
    policy.identity.observe(login_request(login,action='submit'),302,headers,b'',True)
    assert 'next-state' not in policy.identity.transactions
    policy.identity.observe(login_request(login),302,headers,b'',True)
    page=login_form('next-state').replace('name="password"','name="code"')
    policy.identity.observe(req(next_url,method='GET'),200,HTML,page.encode(),True)
    assert policy.identity.scope(next_url)==JOB
    assert policy.check(req(next_url,urlencode({'state':'next-state','code':'123456',
        'js-available':'true','action':'default'}),mime='application/x-www-form-urlencoded')).allowed


@pytest.mark.parametrize('mode',['trial','auto'])
async def test_provider_approval_is_transaction_scoped_and_uses_employer_vault(mode,data_dir):
    gate=request_gate.RequestGate(SimpleNamespace(),mode,{'url':JOB},trusted_site=lambda u:request_gate.origin(u)==request_gate.origin(JOB))
    login=prepare(gate.trial_policy)
    assert await gate.approve_credentials(login)
    assert gate.credential_scope(login)==JOB
    assert gate.credential_key(gate.credential_scope(login))==gate.credential_key(JOB)
    other=login.replace('local-state','other-state')
    assert not await gate.approve_credentials(other)
    assert not gate.credentials_approved(other)
    assert not gate.credential_sites and not request_gate.pending()


async def test_provider_email_code_keeps_original_employer_sender_and_recipient_scope(monkeypatch,data_dir):
    from cli import apply_jobs
    from tests.backend.cli.test_credentials import Session
    gate=request_gate.RequestGate(SimpleNamespace(),'trial',{'url':JOB},trusted_site=lambda u:request_gate.origin(u)==request_gate.origin(JOB))
    login=prepare(gate.trial_policy)
    calls=[]
    async def find(kind,senders,since,browser,**kwargs):
        calls.append((senders,kwargs))
        return {'code':'123456'}
    monkeypatch.setattr(apply_jobs.inbox,'find_verification',find)
    ref={'request_gate':gate,'secrets':{'account_email':'fake@example.test'},
        'credential_site':{'site':apply_jobs._site(JOB)}}
    tools=apply_jobs.build_tools({'url':JOB,'company':'Acme','title':'Local'},'',True,ref,1)
    tool=tools.registry.registry.actions['get_email_verification'].function
    result=await tool(params=apply_jobs.VerificationRequest(kind='code'),browser_session=Session(login))
    assert not result.error and '123456' in result.extracted_content
    assert len(calls)==1 and 'icims.com' in calls[0][0]
    assert calls[0][1]['recipient']=='fake@example.test'
    assert not calls[0][1]['link_validator']('https://login.icims.com/submit')
    result=await tool(params=apply_jobs.VerificationRequest(kind='code'),browser_session=Session(login.replace('local-state','other')))
    assert result.error and len(calls)==1


def native_case(kind):
    if kind=='icims':
        url=JOB.rsplit('/',1)[0]+'/login'
        dest=url+'?step=email&in_iframe=1&hashed=123'
        page='<form method=post action="'+dest+'"><input name=css_loginName type=email><input type=checkbox name=accept_gdpr value=1></form>'
        data={'css_loginName':'fake@example.test','accept_gdpr':'1'}
        return JOB,url,dest,page,data
    if kind=='taleo':
        job='https://acme.taleo.net/careersection/External/jobdetail.ftl?job=42'
        url='https://acme.taleo.net/careersection/iam/accessmanagement/login.jsf'
        dest=url.replace('login.jsf','iamSecuredLogin.jss')
        data={'javax.faces.ViewState':'local-viewstate','csrftoken':'local-csrf',
            'dialogTemplate-dialogForm-login-name1':'fake@example.test',
            'dialogTemplate-dialogForm-login-password':'Local-Only1','jsfCmdId':'dialogTemplate-dialogForm-login-defaultCmd',
            'vmAction':'','jsfCmdParams':''}
        page='<form method=post action="'+url+'">'+''.join(
            '<input name="'+k+'" '+('type=hidden value="'+v+'"' if k in ('javax.faces.ViewState','csrftoken','vmAction','jsfCmdParams','jsfCmdId') else '')+'>' for k,v in data.items())+'</form>'
        # jsfCmdId is filled by the reviewed cmdSubmit handler.
        page=page.replace('value="dialogTemplate-dialogForm-login-defaultCmd"','value=""')
        return job,url,dest,page,data
    job='https://career5.successfactors.eu/career?company=Acme&career_job_req_id=42'
    url='https://career5.successfactors.eu/career?company=Acme&loginFlowRequired=true'
    dest='https://career5.successfactors.eu/career?_s.crb=local-state'
    data={'company':'Acme','career_ns':'','career_os':'','isInternalUser':'','requestParams':'local-state','username':'fake@example.test','password':'Local-Only1','login_ns':''}
    page='<form method=post action="'+dest+'">'+''.join(
        '<input name="'+k+'" '+('type=hidden value="'+v+'"' if k not in ('username','password') else '')+'>' for k,v in data.items())+'</form>'
    return job,url,dest,page,data


@pytest.mark.parametrize('kind',['icims','taleo','sap'])
def test_observed_native_auth_forms_allow_only_login(kind):
    job,url,dest,page,data=native_case(kind)
    p=TrialPolicy(lambda u:request_gate.origin(u)==request_gate.origin(job),job_url=job)
    p.identity.observe(req(url,method='GET'),200,HTML,page.encode(),True)
    assert p.identity.scope(url)==job
    assert p.check(req(dest,urlencode(data),mime='application/x-www-form-urlencoded')).allowed
    for patch in ({'submit':'true'}, {'action':'finalize'}, {'company':'Other'},{'vmAction':'submit'},{'jsfCmdParams':'finalize'}):
        assert not p.check(req(dest,urlencode(data|patch),mime='application/x-www-form-urlencoded')).allowed
    assert not p.check(req(dest.replace(urlsplit_path(dest),'/applications/finalize'),urlencode(data),mime='application/x-www-form-urlencoded')).allowed


def urlsplit_path(url):
    from urllib.parse import urlsplit
    return urlsplit(url).path


def test_successfactors_company_is_a_credential_tenant():
    assert job_page.tenant('https://career5.successfactors.eu/career?company=Acme')=='acme'
    assert job_page.tenant('https://career5.successfactors.eu/career?company=Other')=='other'
    assert not job_page.tenant('https://career5.successfactors.eu/career?company=Acme&company=Other')


def sap_return_case():
    from pathlib import Path
    page = (Path(__file__).parent/'fixtures/sap-login-return.html').read_text(encoding='utf-8')
    job = 'https://career5.successfactors.eu/career?company=Acme&career_job_req_id=42&career_ns=job_application'
    form = Forms(page, job).forms[0]
    data = [(k, 'Local-Only1' if k=='password' else 'fake@example.test' if k=='username' else v)
            for k, values in form['fields'].items() for v in values]
    return job, form['url'], page, data


@pytest.mark.parametrize('from_board', [False, True])
def test_served_sap_login_preserves_the_application_return_route(from_board):
    job, dest, page, data = sap_return_case()
    p = TrialPolicy(lambda u:request_gate.origin(u)==request_gate.origin(job),
        job_url='https://www.linkedin.com/jobs/view/42' if from_board else job)
    p.identity.observe(req(job,method='GET'),200,HTML,page.encode(),True)
    assert p.identity.scope(job)==job
    assert p.check(req(dest,urlencode(data),mime='application/x-www-form-urlencoded')).allowed
    for key,value in [('career_ns','submit'),('career_os','submit'),('career_os','save'),
            ('login_ns','register'),('company','Other'),('career_company','Other'),('career_job_req_id','43')]:
        changed=[(k,value if k==key else v) for k,v in data]
        assert not p.check(req(dest,urlencode(changed),mime='application/x-www-form-urlencoded')).allowed
    for extra in [[('career_ns','job_application')],[('career_ns','submit')],
            [('career_os','')],[('login_ns','')],[('operation','submit')]]:
        assert not p.check(req(dest,urlencode(data+extra),mime='application/x-www-form-urlencoded')).allowed
    assert not p.check(req(dest,urlencode([(k,v) for k,v in data if k!='career_ns']),
        mime='application/x-www-form-urlencoded')).allowed


@pytest.mark.parametrize('before,after',[
    ('value="job_application"','value="submit"'),
    ('name="career_job_req_id" value="42"','name="career_job_req_id" value="43"'),
    ('name="career_os" value=""','name="career_os" value="submit"'),
    ('name="login_ns" value=""','name="login_ns" value="register"'),
    ('name="career_company" value="Acme"','name="career_company" value="Other"'),
])
def test_sap_return_form_cannot_authorize_another_job_or_dispatch(before,after):
    job,dest,page,data=sap_return_case()
    p=TrialPolicy(lambda u:request_gate.origin(u)==request_gate.origin(job),job_url=job)
    page=page.replace(before,after,1)
    p.identity.observe(req(job,method='GET'),200,HTML,page.encode(),True)
    assert not p.identity.scope(job)
    assert not p.check(req(dest,urlencode(data),mime='application/x-www-form-urlencoded')).allowed


@pytest.mark.parametrize('host',['api2.successfactors.eu','career17preview.sapsf.com','performancemanager10.successfactors.com'])
def test_successfactors_candidate_server_aliases_keep_company_and_dispatch_scope(host):
    job,url,dest,page,data=native_case('sap')
    old='career5.successfactors.eu'
    job,url,dest,page=(v.replace(old,host) for v in (job,url,dest,page))
    p=TrialPolicy(lambda u:request_gate.origin(u)==request_gate.origin(job),job_url=job)
    p.identity.observe(req(url,method='GET'),200,HTML,page.encode(),True)
    assert p.check(req(dest,urlencode(data),mime='application/x-www-form-urlencoded')).allowed
    assert not p.check(req(dest,urlencode(data|{'career_ns':'job_application','career_os':'submit'}),mime='application/x-www-form-urlencoded')).allowed


@pytest.mark.parametrize('kind',['icims','sap'])
def test_board_redirected_native_auth_uses_observed_employer_scope(kind):
    job,url,dest,page,data=native_case(kind)
    p=TrialPolicy(lambda u:request_gate.origin(u)==request_gate.origin(job),job_url='https://www.linkedin.com/jobs/view/42')
    p.identity.observe(req(url,method='GET'),200,HTML,page.encode(),True)
    assert p.identity.scope(url)==url
    assert p.check(req(dest,urlencode(data),mime='application/x-www-form-urlencoded')).allowed
    if kind=='sap':
        other_page=page.replace('Acme','Other')
        other_url=url.replace('Acme','Other')
        p.identity.observe(req(other_url,method='GET'),200,HTML,other_page.encode(),True)
        assert not p.identity.scope(other_url)
        assert not p.check(req(dest,urlencode(data|{'company':'Other'}),mime='application/x-www-form-urlencoded')).allowed


@pytest.mark.parametrize('poison',['boundary','name','extra','wrong-file'])
@pytest.mark.parametrize('mode',['trial','auto'])
async def test_workday_upload_mime_and_file_identity_are_checked_at_real_wire(wire_site,tmp_path,mode,poison):
    import httpx
    from tests.backend.core.test_trial_contracts import form
    from tests.backend.core.test_request_gate import running_gate
    local,seen,_=wire_site
    url='https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/jobapplication/local/resume'
    file=tmp_path/'resume.txt';file.write_bytes(b'LOCAL RESUME')
    def forward(request):
        with httpx.Client(trust_env=False) as c:
            r=c.post(local,content=request.content,headers=dict(request.headers))
        return httpx.Response(r.status_code,content=r.content)
    gate=running_gate(mode,forward)
    gate.allowed_files=[str(file)]
    gate.trusted_site=lambda u:request_gate.origin(u)==request_gate.origin(url)
    gate.trial_policy=TrialPolicy(gate.trusted_site,lambda:gate.allowed_files,url)
    mime='multipart/form-data; boundary=local'
    body=form({},file_field='body')
    if poison=='boundary':mime+='; boundary=other'
    if poison=='name':body=body.replace(b'name="body"',b'name="body"; name="submit"')
    if poison=='extra':body=form({'submit':'true'},file_field='body')
    if poison=='wrong-file':body=body.replace(b'LOCAL RESUME',b'UNAPPROVED FILE')
    await gate._request({'requestId':'upload','request':req(url,body,mime=mime)},'s')
    assert any(r[0]=='POST' for r in seen)==(mode=='auto'),seen
    assert not gate.failed and not request_gate.pending()
