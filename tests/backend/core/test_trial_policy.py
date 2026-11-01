"""Contracts are transport permissions, never inferred from button labels."""
import base64
import json

import pytest

from core.trial_policy import TrialPolicy

BASE = "https://acme.wd5.myworkdayjobs.com"
SECTION = "/wday/calypso/cxs/jobapplication/acme/jobapplication/local/educations"


def request(path, method="POST", body="{}", mime="application/json", base=BASE):
    return {"url": base+path, "method": method, "headers": {"Content-Type": mime},
            "hasPostData": bool(body), "postData": body}


@pytest.mark.parametrize("path,method,body,mime", [
    ("/en-US/Careers/login", "POST", "username=test&password=dummy", "application/x-www-form-urlencoded"),
    ("/en-US/Careers/register", "POST", "username=test&password=dummy&sessionSecureToken=local", "application/x-www-form-urlencoded"),
    ("/Careers/initiatereset", "POST", "username=test", "application/x-www-form-urlencoded"),
    ("/wday/cxs/acme/jobpostings/REQ1/jobapplications", "POST", "{}", "text/plain"),
    (SECTION, "POST", '{"school":"Local","fieldOfStudy":{"id":"FinTech"}}', "application/json"),
    (SECTION+"/local", "PUT", '{"degree":{"id":"Bachelors"}}', "application/json"),
    (SECTION+"/local", "DELETE", "{}", "application/json"),
    ("/wday/cxs/acme/jobapplication/local/termsandconditions", "PUT", '{"acceptTermsAndAgreements":true}', "application/json"),
    ("/wday/calypso/cxs/jobapplication/acme/jobapplication/local/questionnaireresponse?type=primary", "POST", '{"questionnaireAnswers":[]}', "application/json"),
])
def test_live_workday_contracts(path, method, body, mime):
    assert TrialPolicy(lambda url: True).check(request(path, method, body, mime)).allowed


@pytest.mark.parametrize("path,body,mime", [
    (SECTION, '{"degree":"Bachelors","submit":true}', "application/json"),
    (SECTION, '{"degree":"Bachelors","operationName":"FinalizeApplication"}', "application/json"),
    (SECTION, '{"answers":[{"_method":"POST","action":"submit"}]}', "application/json"),
    (SECTION, '{"school":"local","school":{"submit":true}}', "application/json"),
    (SECTION, '[{"school":"Local"},{"action":"submit"}]', "application/json"),
    (SECTION+"?type=primary&type=submit", '{}', "application/json"),
    (SECTION.replace("/educations", "/unknown"), '{}', "application/json"),
    (SECTION.replace("/acme/", "/other/"), '{}', "application/json"),
    (SECTION.replace("/educations", "/educations/finalize"), '{}', "application/json"),
    ("/en-US/Careers/login", "username=test&password=dummy&submit=true", "application/x-www-form-urlencoded"),
    ("/en-US/Careers/login", "username=test&username=other&password=dummy", "application/x-www-form-urlencoded"),
    ("/en-US/Careers/login", '{"username":"test","password":"dummy"}', "application/json"),
    ("/wday/cxs/acme/jobpostings/REQ1/jobapplications", '{"finalize":true}', "application/json"),
    ("/graphql", '{"query":"mutation { finalizeApplication }"}', "application/json"),
    ("/draft", '{"draft":true,"action":"submit"}', "application/json"),
    (SECTION.replace("educations", "%65ducations"), '{}', "application/json"),
])
def test_ambiguous_and_disguised_writes_never_get_sending_authority(path, body, mime):
    assert not TrialPolicy(lambda url: True).check(request(path, body=body, mime=mime)).allowed


@pytest.mark.parametrize("path", ["/submitted", "/submit-via-get?answer=local", "/final", "/finalize",
    "/wday/cxs/acme/jobapplication/local/finalize", "/%73ubmit", "/%2573ubmit",
    "/rpc?action=submit", "/rpc?submit=true", "/rpc?operationName=SubmitApplication"])
@pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
def test_final_requests_are_refused_regardless_of_trigger_or_method(path, method):
    assert not TrialPolicy(lambda url: True).check(request(path, method, "" if method != "POST" else "{}")).allowed


def test_raw_bytes_are_authoritative_and_bad_bodies_fail_closed():
    policy = TrialPolicy(lambda url: True)
    req = request(SECTION, body='{"school":"local"}')
    req['postDataEntries'] = [{"bytes": base64.b64encode(b'{"action":"submit"}').decode()}]
    assert not policy.check(req).allowed
    req['postDataEntries'] = [{}]
    assert not policy.check(req).allowed
    req.pop('postDataEntries')
    req['postData'] = '\uddc2'
    assert not policy.check(req).allowed


def test_writes_require_exact_trusted_https_tenant_and_no_method_override():
    req = request(SECTION)
    assert not TrialPolicy(lambda url: False).check(req).allowed
    policy = TrialPolicy(lambda url: True)
    for base in ("https://acme.wd5.myworkdayjobs.com.evil.test", "http://acme.wd5.myworkdayjobs.com", BASE+":9443"):
        assert not policy.check(request(SECTION, base=base)).allowed
    req['headers']['X-HTTP-Method-Override'] = 'POST'
    assert not policy.check(req).allowed


def test_oracle_draft_resource_is_separate_from_submission_and_custom_actions():
    policy = TrialPolicy(lambda url: True)
    base = "https://local.fa.us2.oraclecloud.com"
    path = "/hcmRestApi/resources/11.13.18.05/recruitingCEJobApplicationDrafts"
    data = {"RequisitionNumber":"1", "SiteNumber":"CX", "Content":"bG9jYWw="}
    assert policy.check(request(path, body=json.dumps(data), base=base)).allowed
    assert policy.check(request(path+"/123", "PATCH", json.dumps(data), base=base)).allowed
    for suffix, extra in (("", {"CustomAction":"SUBMIT"}), ("", {"SubmissionId":1}), ("/action/submit", {})):
        assert not policy.check(request(path+suffix, body=json.dumps(data | extra), base=base)).allowed
    assert not policy.check(request(path.replace("JobApplicationDrafts", "JobApplications"), body=json.dumps(data), base=base)).allowed

@pytest.mark.parametrize('payload,allowed', [
 ({'appliedFacets':{},'limit':20,'offset':0,'searchText':'Data Analyst'},True),
 ({'appliedFacets':{'country':['US']},'limit':20,'offset':20,'searchText':''},True),
 ({'submit':True,'searchText':'Data Analyst'},False),
 ({'appliedFacets':{'operation':['submit']}},False),
 ({'limit':'20'},False),
 ({'appliedFacets':{'country':'US'}},False),
])
def test_workday_search_post_is_a_separate_read_contract(payload,allowed):
    req=request('/wday/cxs/acme/Careers/jobs',body=json.dumps(payload))
    assert TrialPolicy(lambda url:True).check(req).allowed is allowed
