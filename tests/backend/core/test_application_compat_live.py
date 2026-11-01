"""Local wire/browser evidence for the screenshot regressions. No paid solver/accounts."""
import asyncio
import os

import pytest

import conftest

from cli import apply_jobs, collect_jobs
from core import browser_policy, captcha, form_check
from core.collection_policy import CollectionPolicy
from sources.registry import PluginRegistry
from tests.backend.core.test_request_gate_live import browser_and_gate, bridge_to, wire_site  # noqa: F401
from tests.backend.test_main_api import _isolate_state  # noqa: F401

pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(
    os.environ.get("LANGHIRE_REAL_BROWSER") != "1", reason="opt-in local browser")]


async def test_workday_query_password_and_upload_are_not_completed_answers(wire_site):
    local, seen, site = wire_site
    site["html"] = '''<form><div data-automation-id="formField-major">
      <label for="major">Field of Study *</label><input id="major" role="combobox" value="FinTech"></div>
      <label for="password">Password *</label><input id="password" type="password" required>
      <label for="file">Transcript *</label><input id="file" type="file" required>
      <label for="email">Email *</label><input id="email" value="ada@example.com" required>
      <label for="degree">Degree *</label><select id="degree"><option value="phd">Doctorate</option><option value="bs">Bachelor of Science</option></select>
      <label>Disabled old question <input required disabled></label></form>'''
    browser, gate = await browser_and_gate("auto", "https://careers.example/form", transport_factory=bridge_to(local))
    try:
        await browser.navigate_to("https://careers.example/form")
        data = await form_check.scan(browser)
        assert {f["label"] for f in form_check.missing_required(data)} == {"Field of Study", "Password", "Transcript"}
        assert form_check.profile_mismatches(data, {'education':{'degree':'B.S.'}}) == [('Degree','Doctorate','B.S.')]
        await browser_policy.evaluate(browser, '''() => {
          document.querySelector('#password').value='local-test-password';
          const pill=document.createElement('span');pill.dataset.automationId='selectedItem';
          pill.textContent='Financial Technology';document.querySelector('[data-automation-id^="formField"]').append(pill);
          const transfer=new DataTransfer();transfer.items.add(new File(['dummy'], 'transcript.txt'));
          document.querySelector('#file').files=transfer.files;return true;
        }''')
        data = await form_check.scan(browser)
        assert form_check.missing_required(data) == []
        assert "local-test-password" not in str(data["fields"])
        assert next(f for f in data["fields"] if f["label"] == "Field of Study")["value"] == "Financial Technology"
        await browser_policy.evaluate(browser, "() => { document.querySelector('#degree').value='bs';return true; }")
        assert form_check.profile_mismatches(await form_check.scan(browser), {'education':{'degree':'B.S.'}}) == []
        assert not any(r[0] == "POST" for r in seen)
    finally:
        await gate.close()


async def test_real_browser_keeps_the_captcha_budget_and_token_can_reach_local_step(wire_site, monkeypatch):
    local, seen, site = wire_site
    site["html"] = '''<form action="/step" method="post"><div class="h-captcha" data-sitekey="dummy-local-key"></div>
      <textarea name="h-captcha-response"></textarea><button type=submit>Continue</button></form>'''
    browser, gate = await browser_and_gate("auto", "https://careers.example/form", transport_factory=bridge_to(local))
    calls = []
    monkeypatch.setattr(captcha, "_solve_sync", lambda *args: calls.append(1) or "local-only-token")
    try:
        await browser.navigate_to("https://careers.example/form")
        settings = {"captcha_solver_enabled": True, "captcha_api_key": "dummy-not-a-real-key"}
        assert await captcha.handle(browser, settings)
        assert await captcha.handle(browser, settings)
        with pytest.raises(captcha.CaptchaUnsolved, match="two solver attempts"):
            await captcha.handle(browser, settings)
        assert calls == [1, 1]
        # A normal Auto action, with production submit-attempt recording.
        monkeypatch.setattr(apply_jobs, "update_job", lambda *args, **kwargs: True)
        apply_jobs.set_run_mode("auto")
        tools = apply_jobs.build_tools({"url":"https://careers.example/form", "company":"Test", "title":"Intern"}, "", False, {}, 1)
        state = await browser.get_browser_state_summary()
        index = next(i for i,n in state.dom_state.selector_map.items() if n.tag_name == "button")
        result = await tools.registry.execute_action("click", {"index": index}, browser_session=browser)
        assert result.error is None
        await asyncio.sleep(.4)
        assert any(r[0] == "POST" and r[1] == "/step" and b"local-only-token" in r[2] for r in seen), seen
    finally:
        await gate.close()


def test_image_data_uri_cannot_make_the_real_sdk_fetch_a_local_address(wire_site, monkeypatch):
    from twocaptcha import TwoCaptcha
    local, seen, _ = wire_site
    # Keep the real normal()/get_method() dispatch, but never contact a provider.
    # Before the repair this produces an actual GET to the local listener.
    monkeypatch.setattr(TwoCaptcha, "solve", lambda *args, **kwargs: {"code":"local-only-answer"})
    challenge = captcha.Challenge(type=captcha.IMAGE_CAPTCHA,
        image_b64="data:image/png;base64," + local + "must-not-connect")
    with pytest.raises(ValueError, match="embedded"):
        captcha._solve_sync("dummy-not-a-real-key", challenge)
    assert seen == []


LINKS = {
    "linkedin": "https://www.linkedin.com/jobs/view/4474996157/",
    "indeed": "https://www.indeed.com/viewjob?jk=ab12cd34567890ef",
    "glassdoor": "https://www.glassdoor.com/job-listing/data-analyst-acme-JV_IC1147401_KO0,12_KE13,17.htm?jl=1009991234567",
    "reed": "https://www.reed.co.uk/jobs/data-analyst/57405261",
    "seek": "https://au.seek.com/job/91234567",
    "stepstone": "https://www.stepstone.de/stellenangebote--Data-Analyst-Berlin-Acme--14534607-inline.html",
    "naukri": "https://www.naukri.com/job-listings-data-analyst-acme-bengaluru-3-to-6-years-191026123456",
    "handshake": "https://app.joinhandshake.com/stu/jobs/12345678",
}


async def test_inert_recovery_keeps_visible_server_listings_and_excludes_css_traps(wire_site):
    local, seen, site = wire_site
    site["html"] = '''<title>Jobs</title><a href="https://www.linkedin.com/jobs/view/4474996157/">Data Analyst</a>
      <a style="display:none" href="https://www.linkedin.com/jobs/view/4474996158/">Hidden trap</a>
      <script>document.body.style.opacity='0';fetch('/forbidden',{method:'POST',body:'candidate'}).catch(()=>{});</script>'''
    plugin = PluginRegistry().get_by_name("linkedin")
    url = plugin.build_search_url(["Data Analyst"], [], 3)
    browser, gate = await browser_and_gate("trial", url, transport_factory=bridge_to(local))
    gate.collection_policy = CollectionPolicy(url, lambda value: collect_jobs.canonical_job_url(plugin, value))
    try:
        await browser.navigate_to(url)
        cards, state = await collect_jobs._read_page(browser, gate, plugin, set())
        assert not cards and state["hidden"] == 2
        cards, _ = await collect_jobs._inert_results(browser, gate, plugin, url, set())
        assert [c["title"] for c in cards] == ["Data Analyst"]
        assert not any(r[0] == "POST" for r in seen), seen
    finally:
        await gate.close()


async def test_retrying_refused_scripts_cannot_stall_collection(wire_site):
    local, seen, site = wire_site
    site["html"] = '''<a href="https://www.linkedin.com/jobs/view/4474996157/">Data Analyst</a>
      <script>function again(){fetch('/forbidden',{method:'POST',body:'candidate'}).catch(()=>setTimeout(again,1))};again()</script>'''
    plugin = PluginRegistry().get_by_name("linkedin")
    url = plugin.build_search_url(["Data Analyst"], [], 3)
    browser, gate = await browser_and_gate("trial", url, transport_factory=bridge_to(local))
    gate.collection_policy = CollectionPolicy(url)
    try:
        await browser.navigate_to(url)
        cards, _ = await asyncio.wait_for(collect_jobs._read_page(browser, gate, plugin, set()), 12)
        assert [c["title"] for c in cards] == ["Data Analyst"]
        assert gate.collection_policy.refused > 0
        assert not any(r[0] == "POST" for r in seen), seen
    finally:
        await gate.close()


@pytest.mark.parametrize("mode", ["auto", "trial"])
async def test_modes_advance_and_save_steps_but_trial_holds_final_submit(wire_site, monkeypatch, mode):
    from core import request_gate
    local, seen, site = wire_site
    site["html"] = '''<title>Application</title><button type=button id=next onclick="
      fetch('/wday/cxs/acme/jobapplication/local/educations',{method:'POST',body:JSON.stringify({school:'local'})}).then(()=>{
        document.body.innerHTML='<form method=post action=/final><input name=answer value=local><button type=submit>Submit application</button></form>';
      })">Next</button>'''
    url = "https://acme.wd5.myworkdayjobs.com/en-US/Careers/application"
    browser, gate = await browser_and_gate(mode, url, transport_factory=bridge_to(local), trusted_site=lambda u: u.startswith("https://acme.wd5.myworkdayjobs.com/"))
    monkeypatch.setattr(apply_jobs, "update_job", lambda *args, **kwargs: True)
    apply_jobs.set_run_mode(mode)
    ref = {}
    tools = apply_jobs.build_tools({"url":url, "company":"Test", "title":"Intern"}, "", mode == "trial", ref, 1)
    apply_jobs.set_run_mode("auto")
    try:
        await browser.navigate_to(url)
        state = await browser.get_browser_state_summary()
        index = next(i for i,n in state.dom_state.selector_map.items() if n.tag_name == "button")
        result = await tools.registry.execute_action("click", {"index":index}, browser_session=browser)
        assert result.error is None
        await gate.wait_idle()
        assert any(r[0] == "POST" and r[1] == "/wday/cxs/acme/jobapplication/local/educations" for r in seen), seen
        state = await browser.get_browser_state_summary()
        index = next(i for i,n in state.dom_state.selector_map.items() if n.tag_name == "button")
        result = await tools.registry.execute_action("click", {"index":index}, browser_session=browser)
        await gate.wait_idle()
        finals = [r for r in seen if r[0] == "POST" and r[1] == "/final"]
        assert len(finals) == (1 if mode == "auto" else 0), (result, seen)
        assert bool(result.error) == (mode == "trial")
        assert request_gate.pending() == []
    finally:
        await gate.close()


async def test_dynamic_board_chunks_and_get_apis_render_without_allowing_writes(wire_site):
    import httpx
    local, seen, site = wire_site
    site["html"] = '''<body style="opacity:0"><main id=results></main><script>
      fetch('https://static.licdn.com/bootstrap',{method:'HEAD'}).then(()=>{
        const script=document.createElement('script');script.src='https://static.licdn.com/runtime-chunk.js';document.head.append(script);
      });
      fetch('/forbidden',{method:'POST',body:'must-not-send'}).catch(()=>{});
      fetch('https://outside.example/unlisted').catch(()=>{});
      </script>'''
    site["responses"] = {
        "/runtime-chunk.js": (200, {}, b'''fetch('https://www.linkedin.com/jobs/api/list').then(r=>r.json()).then(j=>{
          document.querySelector('#results').innerHTML='<a href="https://www.linkedin.com/jobs/view/4474996157/">'+j.title+'</a>';
          document.body.style.opacity='1';});'''),
        "/jobs/api/list": (200, {}, b'{"title":"Data Analyst"}'),
    }
    def transport():
        def forward(request):
            with httpx.Client(trust_env=False) as client:
                response=client.request(request.method, local + request.url.raw_path.decode().lstrip('/'), content=request.content)
            mime = 'application/javascript' if request.url.path.endswith('.js') else 'application/json' if request.url.path.endswith('/list') else 'text/html'
            return httpx.Response(response.status_code, headers={'content-type':mime, 'access-control-allow-origin':'*'}, content=response.content)
        return httpx.MockTransport(forward)
    plugin = PluginRegistry().get_by_name("linkedin")
    url = plugin.build_search_url(["Data Analyst"], [], 3)
    browser, gate = await browser_and_gate("trial", url, transport_factory=transport)
    gate.collection_policy = CollectionPolicy(url, read_hosts={"linkedin.com", "licdn.com"})
    try:
        await browser.navigate_to(url)
        for _ in range(80):
            if await browser_policy.evaluate(browser, "() => document.body.style.opacity === '1'"):
                break
            await asyncio.sleep(.1)
        cards, _ = await collect_jobs._read_page(browser, gate, plugin, set())
        assert [c['title'] for c in cards] == ['Data Analyst']
        assert ("GET", "/runtime-chunk.js") in seen and ("GET", "/jobs/api/list") in seen
        assert not any(r[0] == 'POST' or '/unlisted' in r[1] for r in seen), seen
    finally:
        await gate.close()


async def test_malformed_cdp_body_fails_only_that_request_and_next_post_reaches_server(wire_site):
    import base64
    from tests.backend.core.test_request_gate import running_gate, paused
    local, seen, _ = wire_site
    transport = bridge_to(local)()
    gate = running_gate('auto', lambda request: transport.handle_request(request))
    await gate._request(paused('bad', 'https://careers.example/bad', 'POST', '\uddc2'), 's')
    assert not gate.failed and gate.blocked == 1 and seen == []
    await gate._request(paused('good', 'https://careers.example/good', 'POST', 'local-answer'), 's')
    assert not gate.failed and seen == [('POST','/good',b'local-answer')]
    binary = b'\xff\x00\xc2'
    event=paused('binary', 'https://careers.example/binary', 'POST', '\uddc2')
    event['request']['postDataEntries']=[{'bytes':base64.b64encode(binary).decode()}]
    await gate._request(event,'s')
    assert not gate.failed and seen[-1] == ('POST','/binary',binary)
    assert gate.active_requests == gate.active_bytes == 0


async def test_detailed_interninsider_keeps_observed_employer_links_and_job_notes_scoping(wire_site, data_dir, _isolate_state, monkeypatch):
    import json
    from types import SimpleNamespace
    from browser_use import BrowserSession
    from core import request_gate, shared_config
    local, seen, site = wire_site
    job_url = 'https://boards.greenhouse.io/localtest/jobs/1234567'
    site['html'] = f'<article><a href="{job_url}">BI Analyst</a><p>Local Employer</p></article>'
    monkeypatch.setattr(shared_config, 'selected_browser', lambda settings: (
        conftest.live_browser(), data_dir / 'browser'))
    monkeypatch.setattr(shared_config, 'load_runtime_settings', lambda: {})
    monkeypatch.setattr(collect_jobs, 'BrowserSession', lambda **kwargs: BrowserSession(headless=True, **kwargs))
    monkeypatch.setattr(collect_jobs, 'refresh_credentials', lambda: None)
    monkeypatch.setattr(shared_config, 'get_llm', lambda: object())
    monkeypatch.setattr(shared_config, 'get_fallback_llm', lambda: None)
    monkeypatch.setattr(collect_jobs, 'LOGS_DIR', data_dir)
    real_gate = request_gate.RequestGate
    monkeypatch.setattr(request_gate, 'RequestGate', lambda *args, **kwargs: real_gate(*args, **kwargs, transport_factory=bridge_to(local)))
    class FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs=kwargs
        def stop(self): pass
        async def run(self, on_step_start):
            browser=self.kwargs['browser_session']
            await browser.navigate_to(self.kwargs['initial_actions'][0]['navigate']['url'])
            await on_step_start(self)
            marker={'url':job_url,'title':'BI Analyst','company':'Local Employer','match':9,'why':'BI reporting fits user notes'}
            await self.kwargs['register_new_step_callback'](None,SimpleNamespace(memory='@@JOB_FOUND: '+json.dumps(marker)),1)
            return SimpleNamespace(history=[])
    monkeypatch.setattr(collect_jobs,'Agent',FakeAgent)
    profile={'job_search':{'mode':'detailed','min_match':5,'notes':'Include BI reporting and analytics roles'}}
    saved=await collect_jobs.collect_for_title('Data Analyst',{},profile,max_jobs=1,plugin=PluginRegistry().get_by_name('interninsider'))
    assert len(saved)==1 and saved[0]['url']==job_url and saved[0]['title']=='BI Analyst'
    assert saved[0]['match']==9 and saved[0]['match_source']=='ai_relevance'
    assert 'user_note' not in shared_config.read_jobs()[job_url]
    assert not any(row[0]=='POST' for row in seen), seen


@pytest.mark.parametrize("name", LINKS)
async def test_each_supported_quick_recipe_still_collects_visible_links_with_guards(wire_site, name):
    local, seen, site = wire_site
    plugin = PluginRegistry().get_by_name(name)
    search = plugin.build_search_url(["Data Analyst"], ["London"], 3)
    link = LINKS[name]
    site["html"] = f'''<title>Jobs</title><article><a href="{link}">Data Analyst</a><p>Local Test Company</p></article>
        <script>fetch('/application', {{method:'POST',body:'must-never-send'}}).catch(()=>{{}})</script>'''
    browser, gate = await browser_and_gate("trial", search, transport_factory=bridge_to(local))
    gate.collection_policy = CollectionPolicy(search, lambda url: collect_jobs.canonical_job_url(plugin, url))
    try:
        await browser.navigate_to(search)
        gate.collection_policy.seal()
        await gate.wait_idle()
        data = await browser_policy.collection_evaluate(browser, collect_jobs.LISTING_JS, [plugin.listing.job_link], 60)
        assert len(data["cards"]) == 1, data
        jobs, skipped = collect_jobs.card_jobs(plugin, data["cards"], {})
        assert len(jobs) == 1 and skipped == 0 and jobs[0]["title"] == "Data Analyst"
        assert collect_jobs.canonical_job_url(plugin, link) == jobs[0]["url"]
        assert not any(r[0] == "POST" for r in seen), seen
    finally:
        await gate.close()
