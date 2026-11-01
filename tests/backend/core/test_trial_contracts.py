"""Reviewed ATS protocols: positive progression plus adversarial alternatives."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from core.trial_policy import TrialPolicy
from core.trial_contracts import multipart
from core.trial_graphql import OPERATIONS, query_digest, dispatch_digest

QUERIES = json.loads((Path(__file__).parent/'fixtures/ashby_trial_queries.json').read_text(encoding='utf-8'))
ASHBY = 'https://jobs.ashbyhq.com'
S3 = 'https://files.s3.us-east-1.amazonaws.com/'
ORACLE = 'https://test.fa.ocs.oraclecloud.com'
API = '/hcmRestApi/resources/latest/'


def req(url, data=None, method='POST', mime='application/json'):
    body = data if isinstance(data, (str, bytes)) else json.dumps(data or {})
    result = {'url':url,'method':method,'headers':{'Content-Type':mime},'hasPostData':bool(body)}
    if isinstance(body, bytes):
        result['postDataEntries'] = [{'bytes':base64.b64encode(body).decode()}]
    else:
        result['postData'] = body
    return result


def form(fields, filename='resume.txt', contents=b'LOCAL RESUME', file_field='file'):
    parts = [f'--local\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k,v in fields.items()]
    if filename is not None:
        parts.append((f'--local\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\nContent-Type: text/plain\r\n\r\n').encode()+contents+b'\r\n')
    return b''.join(parts)+b'--local--\r\n'


@pytest.fixture
def policy(tmp_path):
    file = tmp_path/'resume.txt'
    file.write_bytes(b'LOCAL RESUME')
    p = TrialPolicy(lambda u: u.startswith(ASHBY+'/acme'), lambda:[str(file)], ASHBY+'/acme/role/application')
    p.contracts.form_ids.add('local-form')
    p.contracts.file_handles.add('local-handle')
    return p


def gql(op='ApiSetFormValue', variables=None, query=None):
    values = {'organizationHostedJobsPageName':'acme','formRenderIdentifier':'local-form',
        'path':'_systemfield_name','value':'Local Test'} if variables is None else variables
    return req(ASHBY+'/api/non-user-graphql?op='+op, {'operationName':op,'variables':values,'query':query or QUERIES[op]})


@pytest.mark.parametrize('op',list(OPERATIONS))
def test_exact_reviewed_graphql_documents_keep_their_fingerprints(op):
    assert query_digest(QUERIES[op]) == OPERATIONS[op]['digest']
    assert query_digest('# comment\n'+QUERIES[op]+'\n') == OPERATIONS[op]['digest']
    assert query_digest(QUERIES[op]+' mutation Extra { submitApplication }') != OPERATIONS[op]['digest']
    assert dispatch_digest(QUERIES[op]) == OPERATIONS[op]['dispatch_digest']


def test_graphql_dispatch_survives_ui_read_fields_but_not_root_fragments_or_other_dispatches(policy):
    original=QUERIES['ApiSetFormValue']
    updated=original.replace('{id ', '{id clientReadField ',1)
    # Add only a read field inside the returned FormRender selection.
    if updated==original:
        point=original.index('{',original.index('setFormValue('))+1
        updated=original[:point]+' clientReadField '+original[point:]
    assert dispatch_digest(updated)==dispatch_digest(original)
    assert policy.check(gql(query=updated)).allowed
    for query in (
        original+' mutation Extra { submitSingleApplicationFormAction }',
        original+' { submitSingleApplicationFormAction }',
        'mutation ApiSetFormValue { ...Hidden } fragment Hidden on Mutation { submitSingleApplicationFormAction }',
        original.replace('setFormValue(', 'submitSingleApplicationFormAction(',1),
        original.replace('setFormValue(', 'otherAlias:setFormValue(',1),
        original.replace('mutation ApiSetFormValue', 'mutation OtherName',1),
        original+' fragment Hidden on Mutation { submitSingleApplicationFormAction }',
        original.replace('{id ', '{id unexpected(argument:""" } } mutation Evil { submitApplication } """) ',1),
        original[:len(original)//2],
    ):
        assert dispatch_digest(query)!=dispatch_digest(original)
        assert not policy.check(gql(query=query)).allowed


def test_ashby_bound_form_save_and_read_have_distinct_authorities(policy):
    assert policy.check(gql()).allowed
    assert policy.check(gql('ApiJobPosting',{'organizationHostedJobsPageName':'ACME','jobPostingId':'role'})).allowed
    policy.contracts.observe(req(ASHBY+'/api/non-user-graphql?op=ApiJobPosting',method='POST'),
        [{'name':'Content-Type','value':'application/json'}],b'{"data":{"applicationForm":{"__typename":"FormRender","id":"from-server"}}}')
    assert 'from-server' in policy.contracts.form_ids
    assert policy.check(gql(variables={'organizationHostedJobsPageName':'acme','formRenderIdentifier':'from-server','path':'email','value':'local@example.test'})).allowed


@pytest.mark.parametrize('change', ['other-org','other-form','extra-variable','spoof-name','extra-query', 'alias', 'batch', 'duplicate', 'wrong-mime', 'raw-bytes', 'missing-handle'])
def test_ashby_mixed_alias_forged_and_cross_tenant_operations_are_refused(policy,change):
    r=gql();d=json.loads(r['postData'])
    if change=='other-org': d['variables']['organizationHostedJobsPageName']='other'
    elif change=='other-form': d['variables']['formRenderIdentifier']='other'
    elif change=='extra-variable': d['variables']['actionIdentifier']='final'
    elif change=='spoof-name': d['query']='mutation ApiSetFormValue { submitSingleApplicationFormAction }'
    elif change=='extra-query': d['query']+=' mutation X { submitApplicationFormAction }'
    elif change=='alias': d['query']=d['query'].replace('setFormValue','submitSingleApplicationFormAction')
    elif change=='batch': d=[d,d]
    elif change=='duplicate': r['postData']=json.dumps(d)[:-1]+',"query":"mutation { submitApplication }"}';assert not policy.check(r).allowed;return
    elif change=='wrong-mime': r['headers']['Content-Type']='application/x-www-form-urlencoded'
    elif change=='raw-bytes': r['postDataEntries']=[{'bytes':base64.b64encode(b'{"operationName":"ApiSubmitApplicationFormAction"}').decode()}]
    elif change=='missing-handle': r=gql('ApiSetFormValueToFile',{'organizationHostedJobsPageName':'acme','formRenderIdentifier':'local-form','path':'resume','fileHandle':'other'}); assert not policy.check(r).allowed;return
    r['postData']=json.dumps(d)
    assert not policy.check(r).allowed


def observe_grant(policy, url=S3, fields=None, path=None):
    base='https://job-boards.greenhouse.io'
    policy.contracts.bind(base+'/acme/jobs/1')
    r={'url':base+(path or '/acme/jobs/1/uncacheable_attributes/presigned_fields?fields[]=resume&fields[]=cover_letter'),'method':'GET'}
    policy.contracts.observe(r,[{'name':'Content-Type','value':'application/json'}],
        json.dumps({'url':url,'resume':{'fields':fields or {'key':'local/resume.txt','policy':'signed','x-amz-signature':'test'}}}).encode())
    return fields or {'key':'local/resume.txt','policy':'signed','x-amz-signature':'test'}


def upload_request(fields,**kwargs):
    return req(S3,form(fields,**kwargs),mime='multipart/form-data; boundary=local')


def test_upload_capability_is_exact_atomic_and_cannot_be_reissued(policy):
    fields=observe_grant(policy)
    r=upload_request(fields)
    assert policy.check(r).allowed
    body=base64.b64decode(r['postDataEntries'][0]['bytes'])
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda _:policy.contracts.upload(r,body,consume=True),range(8)))
    assert sum(results)==1
    observe_grant(policy)
    assert not policy.check(r).allowed  # repeated response cannot reset spent state


@pytest.mark.parametrize('change',['no-grant','foreign-source','foreign-tenant','wrong-url','wrong-key','extra-field','wrong-file','wrong-name','no-file','two-files','encoding','duplicate','boundary-parameter','name-parameter','mixed-type'])
def test_upload_grant_cannot_be_generalized(policy,change):
    fields={'key':'local/resume.txt','policy':'signed','x-amz-signature':'test'}
    if change!='no-grant':
        observe_grant(policy,path='/other/jobs/1/uncacheable_attributes/presigned_fields' if change=='foreign-tenant' else '/acme/not-an-upload-grant' if change=='foreign-source' else None)
    r=upload_request(fields)
    if change=='wrong-url':r['url']=S3+'other'
    if change=='wrong-key':r=upload_request(fields|{'key':'other-account'})
    if change=='extra-field':r=upload_request(fields|{'action':'submit'})
    if change=='wrong-file':r=upload_request(fields,contents=b'PRIVATE FROM OTHER FILE')
    if change=='wrong-name':r=upload_request(fields,filename='other.txt')
    if change=='no-file':r=upload_request(fields,filename=None)
    if change=='boundary-parameter':r['headers']['Content-Type']+='; boundary=other'
    if change=='mixed-type':r['headers']['Content-Type']='multipart/mixed; boundary=local'
    if change=='name-parameter':
        b=base64.b64decode(r['postDataEntries'][0]['bytes']).replace(b'name="file"',b'name="file"; name="other"')
        r['postDataEntries']=[{'bytes':base64.b64encode(b).decode()}]
    if change in ('two-files','encoding','duplicate'):
        b=base64.b64decode(r['postDataEntries'][0]['bytes'])
        if change=='two-files':b=b.replace(b'--local--\r\n',form({},filename='other.txt'))
        elif change=='encoding':b=b.replace(b'Content-Type: text/plain',b'Content-Transfer-Encoding: base64\r\nContent-Type: text/plain')
        else:b=b.replace(b'--local--\r\n',b'--local\r\nContent-Disposition: form-data; name="key"\r\n\r\nother\r\n--local--\r\n')
        r['postDataEntries']=[{'bytes':base64.b64encode(b).decode()}]
    assert not policy.check(r).allowed


def test_upload_metadata_cannot_grant_arbitrary_destinations_and_expiry_holds(policy):
    for url in ('http://files.s3.amazonaws.com/','https://files.s3.amazonaws.com.evil.test/','https://127.0.0.1/','https://evil.test/','https://files.s3.amazonaws.com:9443/'):
        observe_grant(policy,url=url)
    assert not policy.contracts.grants
    fields=observe_grant(policy)
    for grant in policy.contracts.grants.values():grant['expires']=0
    assert not policy.check(upload_request(fields)).allowed


def test_lever_resume_parser_requires_observed_account_and_exact_file(policy):
    base='https://jobs.lever.co';url=base+'/acme/role/apply'
    policy.trusted_site=lambda u:u==url
    policy.contracts.trusted_site=policy.trusted_site
    r=req(base+'/parseResume',form({'accountId':'bound'},file_field='resume'),mime='multipart/form-data; boundary=local')
    assert not policy.check(r).allowed
    policy.contracts.observe({'url':url,'method':'GET'},[{'name':'Content-Type','value':'text/html'}],b'<input type=hidden name=accountId value=bound>',True)
    assert policy.check(r).allowed
    r['postDataEntries']=[{'bytes':base64.b64encode(form({'accountId':'other'},file_field='resume')).decode()}]
    assert not policy.check(r).allowed


def test_current_greenhouse_root_signer_and_generated_key_are_restricted_to_exact_template(policy):
    policy.contracts.bind('https://job-boards.greenhouse.io/acme/jobs/1')
    path='https://boards.greenhouse.io/uncacheable_attributes/presigned_fields?fields[]=resume&fields[]=cover_letter'
    assert policy.check({'url':path,'method':'GET'}).allowed
    reply={'url':S3.rstrip('/'),'resume':{'fields':{'policy':'signed'},'key':'stash/resumes/{timestamp}-{unique_id}-fixed'}}
    policy.contracts.observe({'url':path,'method':'GET'},[{'name':'Content-Type','value':'application/json'}],json.dumps(reply).encode())
    values={'policy':'signed','utf8':'✓','authenticity_token':'1234','Content-Type':'application/octet-stream','key':'stash/resumes/1792440000000-abc123-fixed'}
    assert policy.check(upload_request(values)).allowed
    for key in ('elsewhere/1792440000000-abc123-fixed','stash/resumes/1792440000000-abc123-other','stash/resumes/NaN-abc123-fixed'):
        assert not policy.check(upload_request(values|{'key':key})).allowed


def test_shared_workday_host_pins_employer_tenant_before_neutral_api(policy):
    base='https://wd5.myworkdaysite.com';start=base+'/en-US/recruiting/acme/External/job/REQ1'
    policy=TrialPolicy(lambda u:u==start,job_url=start)
    assert policy.check(req(base+'/en-US/recruiting/acme/External/login','username=local&password=dummy',mime='application/x-www-form-urlencoded')).allowed
    assert policy.check(req(base+'/wday/cxs/acme/jobpostings/REQ1/jobapplications','{}')).allowed
    policy.contracts.bind(base+'/en-US/recruiting/other/External/job/REQ2')
    assert not policy.check(req(base+'/wday/cxs/other/jobpostings/REQ2/jobapplications','{}')).allowed
    assert not policy.check(req(base+'/wday/cxs/acme/jobapplication/1/finalize','{}')).allowed


def test_workable_actual_public_read_filters_and_terminal_lookalikes(policy):
    base='https://apply.workable.com';policy.contracts.bind(base+'/acme/j/1/')
    data={'query':'','department':[],'location':[],'workplace':[],'worktype':[]}
    assert policy.check(req(base+'/api/v3/accounts/acme/jobs',data)).allowed
    for changed in (data|{'submit':True},data|{'worktype':[{'action':'submit'}]},data|{'limit':True}):
        assert not policy.check(req(base+'/api/v3/accounts/acme/jobs',changed)).allowed
    assert policy.check({'url':'https://dm-assets.navyfederal.org/is/content/nfcu/about_us_final','method':'GET'}).allowed
    assert not policy.check({'url':'https://careers.example/final','method':'GET'}).allowed


@pytest.mark.parametrize('url',[
    'https://api.hcaptcha.com/checksiteconfig?v=local&host=careers.example&sitekey=dummy',
    'https://api2.hcaptcha.com/getcaptcha/local-sitekey',
    'https://hcaptcha.com/checkcaptcha/local-sitekey/local-challenge',
    'https://www.recaptcha.net/recaptcha/enterprise/reload?k=dummy',
    'https://www.google.com/recaptcha/api2/userverify?k=dummy',
    'https://jobs.ashbyhq.com/cdn-cgi/challenge-platform/h/b/jsd/oneshot/local/proof',
    'https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/flow/ov1/local-proof',
])
def test_challenge_proof_does_not_grant_any_application_endpoint(policy,url):
    assert policy.check(req(url,'dummy-proof',mime='text/plain')).allowed
    assert not policy.check(req(url.replace('https://','https://evil.test/'),'dummy-proof',mime='text/plain')).allowed
    assert not policy.check(req(url.replace('/checksiteconfig','/applications').replace('/getcaptcha/local-sitekey','/applications').replace('/checkcaptcha/local-sitekey/local-challenge','/applications').replace('/reload','/applications').replace('/userverify','/applications').replace('/oneshot/','/applications/').replace('/flow/ov1/','/applications/'),'dummy-proof',mime='text/plain')).allowed


def oracle_policy(policy):
    policy.contracts.oracle.bind('https://careers.example/apply',ORACLE+':443','CX_1')
    return policy


@pytest.mark.parametrize('mime',['application/json','application/vnd.oracle.adf.resourceitem+json;charset=utf-8'])
@pytest.mark.parametrize('version',['latest','11.13.18.05'])
def test_oracle_real_version_mime_and_separate_draft_actions(policy,mime,version):
    oracle_policy(policy)
    base=ORACLE+'/hcmRestApi/resources/'+version+'/recruitingCEJobApplicationDrafts'
    for suffix,method,data in [('', 'POST',{'SiteNumber':'CX_1','RequisitionNumber':'1','Content':'bG9jYWw=','ApplyMode':'STANDARD_APPLY','SubmissionId':None}),
            ('/123','PATCH',{'CustomAction':'ORA-GENERATE-PROPOSED-CAND-NUMBER'}),
            ('','POST',{'CustomAction':'ORA_UPDATE','CandidateNumber':'1','EmailAddress':'local@example.test'})]:
        assert policy.check(req(base+suffix,data,method,mime)).allowed


@pytest.mark.parametrize('data',[{'CustomAction':'SUBMIT'},{'Action':'CREATE'},{'SiteNumber':'OTHER'},{'SubmissionId':'123'},{'Content':{},'Submit':True}])
def test_oracle_draft_controls_cannot_finalize(policy,data):
    oracle_policy(policy)
    assert not policy.check(req(ORACLE+API+'recruitingCEJobApplicationDrafts',data)).allowed


def test_oracle_only_opened_login_challenge_can_be_verified(policy):
    oracle_policy(policy)
    url=ORACLE+API+'recruitingCEVerificationTokens'
    start=req(url,{'SiteNumber':'CX_1','Mode':'JOB_APPLY','RequisitionNumber':'1','Email':'local@example.test','CaptchaToken':'dummy'})
    assert policy.check(start).allowed
    verify=req(url,{'SiteNumber':'CX_1','URLShortCode':'local-login-token','ShortCode':'123456'})
    assert not policy.check(verify).allowed
    policy.contracts.observe(start,[{'name':'Content-Type','value':'application/json'}],b'{"URLShortCode":"local-login-token","AccessCode":"local-access"}')
    assert policy.check(verify).allowed
    assert policy.check(req(url,{'Mode':'REFRESH_ACCESS','AccessCode':'local-access'})).allowed
    for data in ({'Mode':'CONFIRM','SiteNumber':'CX_1'},{'Mode':'JOB_APPLY_CONFIRM','Email':'local@example.test'},
        {'URLShortCode':'other-token'},{'ShortCode':'123456','ChallengeFlag':True,'Email':'other@example.test'},
        {'Mode':'JOB_APPLY','SiteNumber':'CX_1','Email':'local@example.test','SubmissionId':'123'}):
        assert not policy.check(req(url,data)).allowed
    assert not policy.check(req(ORACLE+API+'recruitingCEJobApplications',{'Action':'CREATE'})).allowed


def test_oracle_custom_bootstrap_requires_trusted_document_and_vendor_api(policy):
    url='https://careers.example/apply';headers=[{'name':'Content-Type','value':'text/html'}]
    html=b'<script src="https://static.oracle.com/cdn/fa/oj-hcm-ce/2607/js/main.js"></script><div data-apibaseurl="'+ORACLE.encode()+b':443" data-sitenumber="CX_1">'
    policy.contracts.observe({'url':url,'method':'GET'},headers,html,True)
    assert not policy.contracts.oracle.bound(ORACLE)
    policy.contracts.trusted_site=lambda u:u==url
    policy.contracts.observe({'url':url,'method':'GET'},headers,html,True)
    assert policy.contracts.oracle.bound(ORACLE)
    for endpoint in ('https://evil.test','http://test.fa.ocs.oraclecloud.com','https://test.fa.ocs.oraclecloud.com.evil.test'):
        policy.contracts.oracle.bind(url,endpoint,'CX_1')
        assert not policy.contracts.oracle.bound(endpoint)


def test_oracle_resume_parse_and_draft_upload_only_use_authorized_bytes(policy):
    oracle_policy(policy)
    content=base64.b64encode(b'LOCAL RESUME').decode()
    parser={'ProfileId':'0011001100','ProfileType':'rp','FlowVersionId':'1','FileTitle':'resume.txt','FileContents':content,'attachments':[{'Id':4}]}
    assert policy.check(req(ORACLE+API+'recruitingCECandidateResumeParsers',parser)).allowed
    url=ORACLE+API+'recruitingCEJobApplicationDrafts/1/child/attachments'
    upload={'Action':'draft','Type':'document','Category':'RESUME','Attachment':'resume.txt','FileContents':content}
    assert policy.check(req(url,upload)).allowed
    assert not policy.check(req(url,upload|{'Action':'submit'})).allowed
    assert not policy.check(req(url,upload|{'FileContents':base64.b64encode(b'other').decode()})).allowed
    assert not policy.check(req(url,json.dumps(upload)[:-1]+',"action":"submit"}')).allowed
