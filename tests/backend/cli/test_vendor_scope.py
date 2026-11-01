"""The same ATS hostname must not collapse two employer accounts."""
import pytest

from cli.apply_jobs import employer_site, _site
from core.job_page import tenant


@pytest.mark.parametrize('job,same,other,expected', [
    ('https://sjobs.brassring.com/TGNewUI/GQ?partnerid=123&siteid=456',
     'https://sjobs.brassring.com/GQWeb/SaveAndNextPage?siteid=456&partnerid=123&localeid=1033',
     'https://sjobs.brassring.com/TGNewUI/GQ?partnerid=123&siteid=999', '123:456'),
    ('https://jobs.jobvite.com/acme/job/abc', 'https://jobs.jobvite.com/acme/job/abc/apply',
     'https://jobs.jobvite.com/other/job/abc', 'acme'),
    ('https://recruiting.ultipro.com/ACME1003/JobBoard/123/OpportunityDetail?opportunityId=1',
     'https://recruiting.ultipro.com/ACME1003/JobBoard/123/Account/Login',
     'https://recruiting.ultipro.com/OTHER1003/JobBoard/123/Account/Login', 'acme1003'),
])
def test_shared_vendor_credentials_are_employer_specific(job, same, other, expected):
    assert tenant(job) == expected
    assert employer_site(same, job) == _site(job)
    assert not employer_site(other, job)
    assert _site(other) != _site(job)


@pytest.mark.parametrize('query', ['partnerid=123', 'siteid=456', 'partnerid=123&partnerid=999&siteid=456',
    'partnerid=123&PartnerID=999&siteid=456', 'partnerid=123&siteid=456&siteid=999', 'partnerid=123&siteid=bad'])
def test_brassring_ambiguous_company_parameters_name_no_vault_tenant(query):
    page = 'https://sjobs.brassring.com/TGNewUI/GQ?' + query
    assert not tenant(page)
    assert not employer_site(page, 'https://sjobs.brassring.com/TGNewUI/GQ?partnerid=123&siteid=456')
