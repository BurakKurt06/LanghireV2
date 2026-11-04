"""Actual local connections and CDP reads for collector design changes."""
import asyncio
import os

import pytest

from cli import collect_jobs
from core import browser_policy, request_gate
from core.collection_policy import CollectionPolicy
from tests.backend.core.test_request_gate_live import browser_and_gate, bridge_to, wire_site  # noqa: F401
from tests.backend.test_main_api import _isolate_state  # noqa: F401

pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(
    os.environ.get("LANGHIRE_REAL_BROWSER") != "1", reason="opt-in local browser")]

@pytest.mark.asyncio
async def test_collection_scripts_cannot_send_writes_or_arbitrary_gets(wire_site):
    local, seen, site = wire_site
    site["html"] = """<img src='/logo.png'><button type=button onclick="
        fetch('/post',{method:'POST',body:'candidate'}).catch(()=>{});
        fetch('/get').catch(()=>{});navigator.sendBeacon('/beacon','candidate');
        new Image().src='/pixel.png';new Image().src='/logo.png';window.open('/popup');
        document.body.dataset.more='yes'">Show more jobs</button>"""
    url = "https://board.example/search"
    browser, gate = await browser_and_gate("trial", url, transport_factory=bridge_to(local))
    gate.collection_policy = CollectionPolicy(url)
    try:
        await browser.navigate_to(url)
        gate.collection_policy.seal()
        await gate.wait_idle()
        result = await browser_policy.evaluate(browser, collect_jobs.NEXT_JS)
        assert result.get("clicked") == "Show more jobs"
        await asyncio.sleep(.7)
        assert await browser_policy.evaluate(browser, "() => document.body.dataset.more") == "yes"
        assert seen == [("GET", "/search"),("GET","/logo.png")], seen
        assert gate.collection_policy.refused >= 4
        assert request_gate.pending() == []
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_a_refused_write_is_answered_so_a_retrying_script_stops(wire_site):
    """LinkedIn's guest pages count every failed request with another POST to
    /jobs-guest/api/ingraphs/counter: refused as a network error, that POST failed and was re-sent
    about 600 times a second (34,000 refusals in one 54 s collection, 2026-10-31). An HTTP refusal
    ends it. Nothing is sent either way."""
    local, seen, site = wire_site
    site["html"] = """<link rel="icon" href="data:,"><script>
        let tries = 0;
        function count() {
          tries += 1; document.body.dataset.tries = tries;
          fetch('/jobs-guest/api/ingraphs/counter', {method: 'POST', body: 'metric'})
            .then(r => { document.body.dataset.status = r.status; })
            .catch(() => { if (tries < 300) count(); });
        }
        addEventListener('load', count);
        </script><body></body>"""
    url = "https://board.example/search"
    browser, gate = await browser_and_gate("trial", url, transport_factory=bridge_to(local))
    gate.collection_policy = CollectionPolicy(url)
    try:
        await browser.navigate_to(url)
        await asyncio.sleep(2)
        tries = await browser_policy.evaluate(browser, "() => document.body.dataset.tries")
        status = await browser_policy.evaluate(browser, "() => document.body.dataset.status || ''")
        assert (tries, status) == ("1", "403")
        assert gate.collection_policy.refused == 1
        assert seen == [("GET", "/search")], seen
        assert request_gate.pending() == []
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_reader_sees_cross_origin_nested_frames_and_closed_roots_but_not_traps(wire_site):
    local, seen, site = wire_site
    style = '<style>body{margin:0}a{display:block;height:30px;width:200px}</style>'
    child = style + '''<div id=closed></div><a href="https://board.example/job/2">Frame listing</a>
      <iframe src="https://deep.example/nested" style="border:0;width:240px;height:100px"></iframe>
      <script>document.querySelector('#closed').attachShadow({mode:'closed'}).innerHTML=
      '<a href="https://board.example/job/3">Closed frame listing</a><a style="display:none" href="https://board.example/job/9">Hidden trap</a>';</script>'''
    site["html"] = style + '''<div id=closed></div>
      <iframe src="https://child.example/child" style="border:0;width:400px;height:250px"></iframe>
      <iframe src="https://child.example/covered" style="position:absolute;left:500px;top:0;border:0;width:200px;height:200px"></iframe>
      <div style="position:absolute;left:500px;top:0;width:200px;height:200px;background:white;z-index:10">Cover</div>
      <iframe src="https://child.example/hidden" style="opacity:0;border:0;width:300px;height:150px"></iframe>
      <script>document.querySelector('#closed').attachShadow({mode:'closed'}).innerHTML='<a href="/job/1">Closed top listing</a>';</script>'''
    site["responses"] = {
        "/child": (200, {}, child.encode()),
        "/nested": (200, {}, (style+'<a href="https://board.example/job/4">Deep listing</a>').encode()),
        "/covered": (200, {}, (style+'<a href="https://board.example/job/8">Covered trap</a>').encode()),
        "/hidden": (200, {}, (style+'<a href="https://board.example/job/7">Transparent trap</a>').encode()),
    }
    url = "https://board.example/search"
    browser, gate = await browser_and_gate("trial", url, transport_factory=bridge_to(local))
    gate.collection_policy = CollectionPolicy(url)
    try:
        await browser.navigate_to(url)
        await asyncio.sleep(1)
        result = await browser_policy.collection_evaluate(browser, collect_jobs.LISTING_JS, [r"/job/(\d+)"], 60)
        diagnostic = {}
        if {c["id"] for c in result["cards"]} != {"1", "2", "3", "4"}:
            _, frames, parents = await browser_policy._collection_frames(browser)
            diagnostic["parents"] = parents
            diagnostic["seen"] = seen
            for fid, frame in frames.items():
                try:
                    diagnostic[fid] = [await browser_policy._frame_offset(fid, frames, parents),
                        await browser_policy._frame_call(frame, collect_jobs.LISTING_JS, [[r"/job/(\d+)"],60])]
                except Exception as exc:
                    diagnostic[fid] = str(exc)
            import json
            print(json.dumps(diagnostic, default=str))
        assert {c["id"] for c in result["cards"]} == {"1", "2", "3", "4"}, (result, diagnostic)
        assert ("GET", "/child") in seen and ("GET", "/nested") in seen
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_declared_resources_work_and_redirects_cannot_create_new_read_capabilities(wire_site):
    local, seen, site = wire_site
    site["html"]='<body><script src="/ok.js"></script><link rel="stylesheet" href="/style.css"><img src="/redirect.png"></body>'
    site["responses"]={
        "/ok.js":(200,{"Content-Type":"application/javascript"},b"document.body.dataset.loaded='yes';fetch('/sent').catch(()=>{});"),
        "/style.css":(200,{"Content-Type":"text/css"},b"body{color:rgb(1,2,3)}"),
        "/redirect.png":(302,{"Location":"/sent"},b""),
    }
    url="https://board.example/search"
    browser,gate=await browser_and_gate("trial",url,transport_factory=bridge_to(local))
    gate.collection_policy=CollectionPolicy(url)
    try:
        await browser.navigate_to(url)
        await asyncio.sleep(.4)
        assert ("GET","/ok.js") in seen and ("GET","/style.css") in seen
        assert ("GET","/sent") not in seen
        assert await browser_policy.evaluate(browser,"() => document.body.dataset.loaded")=="yes"
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transform",["rotate(20deg) scale(.8)","perspective(500px) rotateY(25deg) rotate(10deg) scale(.8)"])
async def test_rotated_frame_is_read_and_cross_frame_opacity_still_hides_traps(wire_site,transform):
    local,seen,site=wire_site
    site["html"]='''<style>body{margin:0}</style>
       <iframe src="https://child.example/rotated" style="position:absolute;left:150px;top:100px;border:5px solid;transform:rotate(20deg) scale(.8);width:300px;height:200px"></iframe>
       <iframe src="https://child.example/faint" style="opacity:.1;position:absolute;left:600px;top:100px;border:0;width:250px;height:200px"></iframe>
       <div style="visibility:hidden"><iframe src="https://child.example/override" style="visibility:visible;position:absolute;left:150px;top:400px;border:0;width:250px;height:150px"></iframe></div>'''
    site["html"]=site["html"].replace("rotate(20deg) scale(.8)",transform)
    site["responses"]={"/rotated":(200,{},b'<a style="display:block;width:180px;height:40px" href="https://board.example/job/1">Rotated listing</a>'),
        "/faint":(200,{},b'<a style="opacity:.1;display:block;width:180px;height:40px" href="https://board.example/job/9">Opacity trap</a>'),
        "/override":(200,{},b'<a style="display:block;width:180px;height:40px" href="https://board.example/job/2">Visible override</a>')}
    url="https://board.example/search"
    browser,gate=await browser_and_gate("trial",url,transport_factory=bridge_to(local))
    gate.collection_policy=CollectionPolicy(url)
    try:
        await browser.navigate_to(url)
        await asyncio.sleep(.4)
        result=await browser_policy.collection_evaluate(browser,collect_jobs.LISTING_JS,[r"/job/(\d+)"],60)
        assert {c["id"] for c in result["cards"]}=={"1","2"},result
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_embedded_scrolling_keeps_all_listings_before_the_outer_page_moves(wire_site):
    local,seen,site=wire_site
    site["html"]='<style>body{margin:0}</style><iframe src="https://child.example/list" style="border:0;width:400px;height:220px"></iframe><div style="height:1200px"></div>'
    site["responses"]={"/list":(200,{},('<style>body{margin:0}a{display:block;height:80px}</style>'+''.join(
        f'<div><a href="https://board.example/job/{n}">Analyst {n}</a></div>' for n in range(1,13))).encode())}
    url="https://board.example/search"
    browser,gate=await browser_and_gate("trial",url,transport_factory=bridge_to(local))
    gate.collection_policy=CollectionPolicy(url)
    try:
        await browser.navigate_to(url)
        await asyncio.sleep(.4)
        found=set()
        for _ in range(12):
            result=await browser_policy.collection_evaluate(browser,collect_jobs.LISTING_JS,[r"/job/(\d+)"],60)
            found.update(c["id"] for c in result["cards"])
            if len(found)==12:
                break
            await browser_policy.collection_evaluate(browser,collect_jobs.SCROLL_JS,scroll=True)
        assert found=={str(n) for n in range(1,13)},found
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_reloading_a_read_cannot_smuggle_text_in_cookies_or_referrer(wire_site):
    local,seen,site=wire_site
    html='''<body><img src="/logo.png"><button type=button onclick="document.cookie='candidate=local-secret';
      history.replaceState({},'', '/search?candidate=local-secret');new Image().src='/logo.png'">Show more jobs</button></body>'''
    site["responses"]={"/search":(200,{"Set-Cookie":"session=server-value; Path=/; HttpOnly"},html.encode()),
                         "/logo.png":(200,{},b"")}
    url="https://board.example/search"
    browser,gate=await browser_and_gate("trial",url,transport_factory=bridge_to(local))
    gate.collection_policy=CollectionPolicy(url)
    try:
        await browser.navigate_to(url)
        await browser_policy.evaluate(browser,collect_jobs.NEXT_JS)
        await asyncio.sleep(.4)
        headers=site["request_headers"]
        assert len(headers)>=2
        assert any("server-value" in v for h in headers for k,v in h.items() if k.lower()=="cookie"),headers
        assert all("local-secret" not in str(h) for h in headers),headers
        assert request_gate.pending()==[]
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_only_observed_canonical_jobs_can_be_opened_not_job_shaped_payload_urls(wire_site):
    local,seen,site=wire_site
    site["html"]='''<body><a href="/job/123?tracking=yes">Actual listing</a><script>
      const f=document.createElement('iframe');f.src='/job/private?candidate=local-secret';document.body.append(f);
      </script></body>'''
    url="https://board.example/search"
    browser,gate=await browser_and_gate("trial",url,transport_factory=bridge_to(local))
    gate.collection_policy=CollectionPolicy(url,lambda value:value.split("?")[0] if "/job/" in value else "")
    try:
        await browser.navigate_to(url)
        await asyncio.sleep(.3)
        assert not any("local-secret" in row[1] for row in seen),seen
        await browser.navigate_to("https://board.example/job/123")
        assert ("GET","/job/123") in seen,seen
        assert request_gate.pending()==[]
    finally:
        await gate.close()


@pytest.mark.parametrize('builtin', [True, False])
async def test_production_collector_loads_runtime_third_party_reads_without_write_authority(wire_site, data_dir, monkeypatch, builtin):
    """Use _open_browser, dynamic chunks/API and actual wire evidence; no public traffic."""
    from dataclasses import replace
    import httpx
    from browser_use import BrowserSession
    from core import job_page, shared_config
    from core.browsers import find_browser
    from sources.registry import PluginRegistry
    from tests.backend.core.test_request_gate_live import wait_for

    local, seen, site = wire_site
    url = 'https://www.linkedin.com/jobs/search/?keywords=Local'
    site['html'] = '''<div id="jobs" hidden></div><script>
      const s=document.createElement('script');s.src='https://bot.example.test/bootstrap.js';document.head.append(s);
      </script>'''
    site['responses'] = {
        '/bootstrap.js': (200, {'Content-Type': 'application/javascript'}, b"{const s=document.createElement('script');s.src='https://chunks.example.test/runtime.js';document.head.append(s);}"),
        '/runtime.js': (200, {'Content-Type': 'application/javascript'}, b"""
          fetch('https://www.linkedin.com/jobs/api').then(r=>r.text()).then(html=>{
            document.querySelector('#jobs').innerHTML=html;document.querySelector('#jobs').hidden=false;
          });
          fetch('https://www.linkedin.com/write',{method:'POST',body:'must-not-send'}).catch(()=>{});
          fetch('https://mail.google.com/mail/u/0/').catch(()=>{});
          fetch('https://127.0.0.1/private').catch(()=>{});
        """),
        '/jobs/api': (200, {}, b'<a style="display:block;width:200px;height:40px" href="https://www.linkedin.com/jobs/view/1234567890/">Local Analyst</a><a hidden href="https://www.linkedin.com/jobs/view/9999999999/">Hidden trap</a>'),
    }
    monkeypatch.setattr(shared_config, 'selected_browser', lambda settings: (find_browser('brave') or find_browser('chrome'), data_dir/'browser'))
    monkeypatch.setattr(shared_config, 'load_runtime_settings', lambda: {})
    def headless(**kwargs):
        kwargs['args'] = [arg for arg in kwargs.get('args', []) if not arg.startswith('--host-resolver-rules=')] + ['--host-resolver-rules=MAP * ~NOTFOUND']
        return BrowserSession(headless=True, **kwargs)
    monkeypatch.setattr(collect_jobs, 'BrowserSession', headless)
    real_gate = request_gate.RequestGate
    class LocalPages(httpx.BaseTransport):
        def __init__(self):
            self.pages, self.other = bridge_to(local)(), job_page._PinnedTransport()
        def handle_request(self, request):
            if request.url.host in {'www.linkedin.com', 'bot.example.test', 'chunks.example.test'}:
                return self.pages.handle_request(request)
            return self.other.handle_request(request)  # real private-address check, never mocked public
        def close(self):
            self.pages.close()
            self.other.close()
    monkeypatch.setattr(request_gate, 'RequestGate', lambda *a, **kw: real_gate(*a, **kw, transport_factory=LocalPages))
    plugin = replace(PluginRegistry().get_by_name('linkedin'), is_builtin=builtin)
    browser, gate = collect_jobs._open_browser(plugin, url, 'Local Analyst')
    try:
        assert gate.collection_policy.any_public_reads == builtin
        await browser.start()
        await gate.start()
        await browser.navigate_to(url)
        if builtin:
            assert await wait_for(lambda: ('GET', '/jobs/api') in seen, gate), seen
        await asyncio.sleep(.5)
        result = await browser_policy.collection_evaluate(browser, collect_jobs.LISTING_JS, [r'/jobs/view/(\d+)'], 60)
        assert {c['id'] for c in result['cards']} == ({'1234567890'} if builtin else set()), (result, seen)
        assert (('GET', '/runtime.js') in seen) == builtin
        assert not any(r[0] != 'GET' or r[1] in ('/private', '/mail/u/0/', '/write') for r in seen), seen
        gate.collection_policy.seal()
        await browser_policy.evaluate(browser, "() => {fetch('/sealed').catch(()=>{});return true;}")
        await asyncio.sleep(.2)
        assert ('GET', '/sealed') not in seen
        assert gate.healthy() and not request_gate.pending()
    finally:
        await gate.close()
