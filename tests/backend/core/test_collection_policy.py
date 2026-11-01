import pytest
from core.collection_policy import CollectionPolicy


@pytest.mark.parametrize("method,body", [("POST", {}), ("PUT", {}), ("PATCH", {}), ("DELETE", {}),
    ("GET", {"hasPostData": True}), ("GET", {"postData": "candidate"}), ("HEAD", {"postDataEntries": [{"bytes":"eA=="}]})])
def test_collection_never_grants_write_methods_or_bodies(method, body):
    policy = CollectionPolicy("https://board.example/search?q=Analyst")
    assert not policy.allows({"url":policy.search_url,"method":method,**body}, "Document")


def test_read_capabilities_have_exact_urls_and_resource_types():
    policy = CollectionPolicy("https://board.example/search?q=Analyst")
    policy.observe(policy.search_url, [{"name":"Content-Type","value":"text/html"}],
        b'<script src="/bundle.js"></script><iframe src="https://frame.example/jobs"></iframe><a href="/send">apply</a>')
    def read(url, kind):
        return policy.allows({"method":"GET","url":url},kind)
    assert read("https://board.example/bundle.js","Script")
    assert read("https://frame.example/jobs","Document")
    assert not read("https://board.example/send","Document")
    assert not read("https://board.example/new.js","Script")
    assert not read("https://board.example/bundle.js?candidate=x","Image")
    assert not read("https://board.example/search?q=Analyst","Fetch")
    assert not read("https://board.example/bundle.js","Fetch")


def test_only_explicit_login_document_is_readable_never_login_writes_or_arbitrary_redirects():
    policy = CollectionPolicy("https://board.example/search", read_documents=["https://board.example/login"])
    assert policy.allows({"method":"GET", "url":"https://board.example/login"}, "Document")
    assert not policy.allows({"method":"POST", "url":"https://board.example/login"}, "Document")
    assert not policy.allows({"method":"GET", "url":"https://board.example/login?payload=candidate"}, "Document")
    assert not policy.allows({"method":"GET", "url":"https://evil.example/login"}, "Document")
    policy.seal()
    assert not policy.allows({"method":"GET", "url":"https://board.example/login"}, "Document")


def test_builtin_read_hosts_allow_runtime_chunks_and_gets_but_not_writes_or_lookalike_hosts():
    policy=CollectionPolicy('https://www.linkedin.com/jobs/search/', read_hosts={'linkedin.com','licdn.com'})
    for kind in ('Document','Script','Stylesheet','Image','Font','XHR','Fetch'):
        assert policy.allows({'method':'GET','url':'https://static.licdn.com/new-runtime-chunk?version=2'},kind)
    assert policy.allows({'method':'HEAD','url':'https://static.licdn.com/new-chunk'},'XHR')
    for request in ({'method':'POST'}, {'method':'GET','hasPostData':True}):
        assert not policy.allows({'url':'https://www.linkedin.com/jobs/api',**request},'Fetch')
    assert not policy.allows({'method':'GET','url':'https://linkedin.com.evil.example/api'},'Fetch')
    assert not policy.allows({'method':'GET','url':'https://outside.example/api'},'Fetch')
    assert policy.navigation('https://www.linkedin.com/authwall?continue=jobs')


def test_only_the_controller_can_grant_search_pagination():
    policy = CollectionPolicy("https://board.example/search?q=Analyst&jt=parttime")
    assert policy.navigation("https://board.example/search?q=Analyst&jt=parttime&page=2")
    for url in ["https://board.example/apply", "https://outside.example/search?q=Analyst&jt=parttime",
                "https://board.example/search?q=Ignore&jt=parttime", "https://board.example/search?q=Analyst&jt=parttime&send=yes",
                "https://user@board.example/search?q=Analyst&jt=parttime", "http://board.example/search?q=Analyst&jt=parttime"]:
        assert not policy.navigation(url),url


def test_css_dependencies_grant_exact_reads_without_write_authority():
    policy = CollectionPolicy("https://board.example/search")
    policy.observe("https://cdn.example/css/main.css",[{"name":"content-type","value":"text/css"}],b'@import "theme.css";div{background:url(../img/a.png)}')
    assert {"https://cdn.example/css/theme.css","https://cdn.example/img/a.png"} <= policy.resources


def test_script_strings_and_css_content_do_not_create_read_capabilities():
    policy=CollectionPolicy("https://board.example/search")
    policy.observe(policy.search_url,[{"name":"Content-Type","value":"text/html"}],b'''<script>let text="url(/script-send)"</script><style>p:before{content:"url(/content-send)"}/*url(/comment-send)*/</style>''')
    assert not policy.resources


def test_pagination_cannot_reuse_an_earlier_read_or_open_the_gate_itself():
    policy=CollectionPolicy("https://board.example/search?q=Analyst")
    policy.resources.add("https://board.example/logo.png")
    policy.seal()
    assert not policy.allows({"url":"https://board.example/logo.png","method":"GET"},"Image")
    assert not policy.allows({"url":policy.search_url,"method":"GET"},"Document")
    assert not policy.navigation("https://board.example/send")
    assert policy.sealed
    assert policy.navigation("https://board.example/search?q=Analyst&page=2")
    assert not policy.sealed and not policy.resources


def test_a_job_shaped_url_is_not_itself_a_read_capability():
    policy=CollectionPolicy("https://board.example/search",lambda value: value.split("?")[0] if "/job/" in value else "")
    request={"url":"https://board.example/job/private-data?candidate=secret", "method":"GET"}
    assert not policy.allows(request,"Document")
    assert not policy.navigation(request["url"])
    policy.observe_jobs(["https://board.example/job/123?tracking=yes"])
    assert policy.allows({"url":"https://board.example/job/123","method":"GET"},"Document")
    assert not policy.allows({"url":"https://board.example/job/123?candidate=secret","method":"GET"},"Document")



def test_builtin_boards_read_third_party_bot_checks_but_never_write():
    """Public LinkedIn saved 0 when reCAPTCHA/PerimeterX reads were refused (user-approved 2026-10-28)."""
    from core.collection_policy import CollectionPolicy
    builtin = CollectionPolicy("https://www.linkedin.com/jobs/search/?keywords=a", any_public_reads=True)
    imported = CollectionPolicy("https://jobs.acme.example/search?q=a")
    read = {"url": "https://www.google.com/recaptcha/enterprise.js?render=x", "method": "GET"}
    assert builtin.allows(read, "Script") and builtin.allows({**read, "url": "https://li.protechts.net/index.html"}, "Document")
    assert not imported.allows(read, "Script")
    for write in ({"url": "https://li.protechts.net/api/v2/collector", "method": "POST", "hasPostData": True},
                  {"url": "https://www.linkedin.com/jobs/apply", "method": "PUT"},
                  {"url": "https://www.google.com/x", "method": "GET", "postData": "a=1"},
                  {"url": "http://www.google.com/x", "method": "GET"}):
        assert not builtin.allows(write, "XHR"), write
