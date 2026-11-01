"""Opt-in: the submit guard against a real browser and a real form (no AI, no internet).

Run with:  $env:LANGHIRE_REAL_BROWSER = "1"; uv run pytest tests/backend/cli/test_submit_guard_live.py
A throwaway profile, a local page and a local server; the default run skips it.
"""
import asyncio
import functools
import http.server
import os
import tempfile
import threading
from contextlib import asynccontextmanager

import pytest

import conftest

from cli import apply_jobs

pytestmark = [
    pytest.mark.real_browser,
    pytest.mark.skipif(os.environ.get("LANGHIRE_REAL_BROWSER") != "1", reason="opt-in: launches a real browser"),
]

FORM = """<!doctype html><title>Apply</title>
<form method="post" action="/submitted"><label>Email <input name="email" value="ada@example.com"></label>
<button type="submit">Submit application</button></form>"""


@pytest.fixture
def site():
    posts = []
    folder = tempfile.mkdtemp()
    with open(os.path.join(folder, "apply.html"), "w") as f:
        f.write(FORM)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_POST(self):
            posts.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<title>Thanks</title>Thank you for applying")

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=folder))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/apply.html", posts
    finally:
        server.shutdown()
        server.server_close()


async def _click_submit(mode, url):
    from browser_use import BrowserSession
    browser = BrowserSession(executable_path=conftest.live_browser(),
                             user_data_dir=tempfile.mkdtemp(), headless=True, permissions=[])
    try:
        await browser.start()
        await browser.navigate_to(url)
        await asyncio.sleep(1)
        state = await browser.get_browser_state_summary()
        index = next(i for i, node in state.dom_state.selector_map.items()
                     if "submit application" in (node.get_all_children_text() or "").lower())
        apply_jobs.set_run_mode(mode)
        try:
            tools = apply_jobs.build_tools({"url": url, "title": "Intern", "company": "Acme"}, "", mode == "trial", {}, 1)
        finally:
            apply_jobs.set_run_mode("auto")
        result = await tools.registry.execute_action("click", {"index": index}, browser_session=browser)
        await asyncio.sleep(1.5)
        return result
    finally:
        await browser.kill()


def test_a_trial_never_sends_the_form_without_approval(site, monkeypatch):
    url, posts = site
    monkeypatch.setattr(apply_jobs, "update_job", lambda *a, **k: None)
    result = asyncio.run(_click_submit("training", url))   # the old name: a trial run
    assert "Trial run" in (result.error or "") and posts == []


def test_auto_sends_it(site, monkeypatch):
    url, posts = site
    monkeypatch.setattr(apply_jobs, "update_job", lambda *a, **k: True)   # the attempt is recorded
    result = asyncio.run(_click_submit("auto", url))
    assert result.error is None and posts == ["/submitted"]


def test_auto_doesnt_send_when_the_attempt_cant_be_recorded(site, monkeypatch):
    url, posts = site

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(apply_jobs, "update_job", broken)
    result = asyncio.run(_click_submit("auto", url))
    assert "couldn't record" in (result.error or "") and posts == []


# Codex's reproductions: a real browser, a local form, the button as the page builds it.
@pytest.mark.parametrize("mode, label, button_type, keys", [
    ("training", "Apply Now", "submit", None),
    ("trial", "Apply Now", "submit", None),                         # no approval
    ("training", "Next", "", None),                                 # no type: the form's default submit
    ("training", "Submit application verification", "submit", None),
    ("training", "Submit application", "submit", "Space"),          # Space on the focused button
    ("training", "Sign in", "submit", None),
    ("trial", "Search", "submit", None),
    ("training", "Register", "submit", None),
    ("training", "Search", "poisoned", None),
    ("training", "Submit application", "submit", "ok "),            # text ending in a space, on the button
    ("trial", "Continue", "", "a b"),
])
def test_no_form_is_sent_without_permission(mode, label, button_type, keys, monkeypatch):
    posts = []
    kind = f'type="{button_type}"' if button_type else ""
    if button_type == "poisoned":
        kind = 'type="submit"'
    page = (f'<!doctype html><form method="post" action="/submitted"><input name="email" value="ada@example.com">'
            f'<button id="send" {kind}>{label}</button></form>')
    if button_type == "poisoned":
        page += "<script>Object.defineProperty(HTMLButtonElement.prototype, 'type', {get: () => 'button'});</script>"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(page.encode())

        def do_POST(self):
            posts.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Thank you for applying")

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/apply"
    monkeypatch.setattr(apply_jobs, "update_job", lambda *a, **k: True)

    async def go():
        from browser_use import BrowserSession
        browser = BrowserSession(executable_path=conftest.live_browser(),
                                 user_data_dir=tempfile.mkdtemp(), headless=True, permissions=[])
        try:
            await browser.start()
            await browser.navigate_to(url)
            await asyncio.sleep(0.5)
            state = await browser.get_browser_state_summary()
            index = next(i for i, node in state.dom_state.selector_map.items()
                         if label.lower() in (node.get_all_children_text() or "").lower())
            apply_jobs.set_run_mode(mode)
            try:
                tools = apply_jobs.build_tools({"url": url, "title": "Intern", "company": "Test"}, "", mode == "trial", {}, 1)
            finally:
                apply_jobs.set_run_mode("auto")
            if keys is None:
                result = await tools.registry.execute_action("click", {"index": index}, browser_session=browser)
            else:
                current = await browser.get_current_page()
                await current.evaluate("() => { document.getElementById('send').focus(); return 1; }")
                result = await tools.registry.execute_action("send_keys", {"keys": keys}, browser_session=browser)
            await asyncio.sleep(0.7)
            return result
        finally:
            await browser.kill()

    try:
        result = asyncio.run(go())
    finally:
        server.shutdown()
        server.server_close()
    assert result.error and posts == []


@pytest.fixture
def policy_site():
    posts = []
    form = ('<form method="post" action="/submitted"><input name="email" value="ada@example.com">'
            '<input type="hidden" name="job" value="one">'
            '<textarea id="letter" name="letter">' + 'A' * 100 + '</textarea>'
            '<button id="send" type="submit">Submit application</button></form>')
    pages = {"/": form, "/frame": form, "/parent": '<iframe id="frame" src="/frame"></iframe>',
             "/clobbered": form.replace('<form method="post" action="/submitted">',
                                        '<form method="post" action="/submitted"><input type="hidden" name="elements">'),
             "/false-confirmation": 'Application not submitted<script>'
                 'Object.defineProperty(HTMLElement.prototype, "innerText", '
                 '{get: () => "Thank you for applying"});</script>'}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(("<!doctype html><html><body>" + pages.get(self.path, "") + "</body></html>").encode())

        def do_POST(self):
            posts.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Thank you for applying")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", posts
    finally:
        server.shutdown()
        server.server_close()


@asynccontextmanager
async def policy_browser(url, mode, tmp_path, monkeypatch):
    from browser_use import BrowserSession
    monkeypatch.setattr(apply_jobs, "update_job", lambda *args, **kwargs: True)
    browser = BrowserSession(executable_path=conftest.live_browser(),
                             user_data_dir=str(tmp_path / "browser"), headless=True,
                             permissions=[], enable_default_extensions=False)
    try:
        await browser.start()
        await browser.navigate_to(url)
        await asyncio.sleep(0.3)
        apply_jobs.set_run_mode(mode)
        ref = {}
        try:
            tools = apply_jobs.build_tools({"url": url, "company": "Test", "title": "Intern"}, "", mode == "trial", ref, 1)
        finally:
            apply_jobs.set_run_mode("auto")
        yield browser, tools, ref
    finally:
        await browser.kill()


EMBED = ("(src) => new Promise(done => { const f = document.createElement('iframe'); f.id = 'frame';"
         " f.onload = () => done(1); f.src = src; document.body.replaceChildren(f); })")
SWAP = "(src) => new Promise(done => { const f = document.getElementById('frame'); f.onload = () => done(1); f.src = src; })"


async def _load_frame(page, script, src):
    """EMBED or SWAP the frame, then wait for its load event."""
    assert await asyncio.wait_for(page.evaluate(script, src), 30) == "1"


async def _frame_form(browser, timeout=20.0):
    """browser-use's element map once the framed form is in it. A frame from another site runs in its
    own process and joins the page state some time after it loads: a fixed 1.5 s wait left the form
    out on Windows CI (11-01), and a swapped frame could still show its previous page."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        nodes = (await browser.get_browser_state_summary()).dom_state.selector_map
        names = {n.attributes.get("id") or n.attributes.get("name") for n in nodes.values()}
        if {"send", "email"} <= names:
            return nodes
        assert loop.time() < deadline, f"the framed form never reached the page state: {sorted(filter(None, names))}"
        await asyncio.sleep(0.5)


@pytest.mark.parametrize("mode", ["training", "trial"])
def test_enter_in_an_iframe_is_checked(policy_site, tmp_path, monkeypatch, mode):
    base, posts = policy_site

    async def go():
        async with policy_browser(base + "/parent", mode, tmp_path, monkeypatch) as (browser, tools, ref):
            page = await browser.get_current_page()
            await page.evaluate("() => { document.getElementById('frame').contentDocument.getElementById('send').focus(); return 1; }")
            result = await tools.registry.execute_action("send_keys", {"keys": "Enter"}, browser_session=browser)
            await asyncio.sleep(0.5)
            assert result.error and not posts

    asyncio.run(go())


@pytest.mark.parametrize("path,change", [
    ("/", "document.getElementById('letter').value = 'A'.repeat(100) + 'changed'"),
    ("/", "document.querySelector('input[type=hidden]').value = 'two'"),
    ("/", "document.querySelector('form').action = '/elsewhere'"),
    ("/", "document.getElementById('send').setAttribute('formaction', '/elsewhere')"),
    ("/clobbered", "document.querySelector('input[name=job]').value = 'two'"),
])
def test_approval_covers_full_answers_and_form_destinations(policy_site, tmp_path, monkeypatch, path, change):
    from core import form_check, submit_guard
    base, posts = policy_site

    async def go():
        async with policy_browser(base + path, "trial", tmp_path, monkeypatch) as (browser, tools, ref):
            state = await form_check.scan(browser)
            assert "approval_state" in state
            ref["submit_guard"].approve(await browser.get_current_page_url(),
                                        submit_guard.fingerprint(state["fields"], state["approval_state"]))
            summary = await browser.get_browser_state_summary()
            index = next(i for i, node in summary.dom_state.selector_map.items() if node.attributes.get("id") == "send")
            await (await browser.get_current_page()).evaluate("() => { " + change + "; return 1; }")
            result = await tools.registry.execute_action("click", {"index": index}, browser_session=browser)
            await asyncio.sleep(0.5)
            assert result.error and "submission is disabled" in result.error and not posts

    asyncio.run(go())


def test_a_trial_fills_a_cross_origin_frame_but_cant_approve_its_submit(policy_site, tmp_path, monkeypatch):
    """An application form embedded from another site (a Greenhouse embed): the app can't read that
    frame into the review, so its submit can't be approved, but filling the form must still work."""
    from core import form_check, submit_guard
    base, posts = policy_site
    frame = base.replace("127.0.0.1", "localhost") + "/frame"   # another site: an out-of-process frame

    async def go():
        async with policy_browser(base + "/", "trial", tmp_path, monkeypatch) as (browser, tools, ref):
            await _load_frame(await browser.get_current_page(), EMBED, frame)
            state = await form_check.scan(browser)
            ref["submit_guard"].approve(await browser.get_current_page_url(),
                                        submit_guard.fingerprint(state["fields"], state["approval_state"]))
            nodes = (await _frame_form(browser)).items()
            email = next(i for i, node in nodes if node.attributes.get("name") == "email")
            send = next(i for i, node in nodes if node.attributes.get("id") == "send")
            typed = await tools.registry.execute_action("input", {"index": email, "text": "grace@example.com"},
                                                        browser_session=browser)
            assert typed.error is None
            letter = next(i for i, node in nodes if node.attributes.get("id") == "letter")
            assert (await tools.registry.execute_action("click", {"index": letter}, browser_session=browser)).error is None
            clicked = await tools.registry.execute_action("click", {"index": send}, browser_session=browser)
            await asyncio.sleep(0.5)
            assert clicked.error and "submission is disabled" in clicked.error and not posts

    asyncio.run(go())


def test_page_prototypes_cannot_fake_a_confirmation(policy_site, tmp_path, monkeypatch):
    base, _ = policy_site

    async def go():
        async with policy_browser(base + "/false-confirmation", "auto", tmp_path, monkeypatch) as (browser, _, __):
            assert not await apply_jobs.page_confirms_submission(browser)

    asyncio.run(go())


@pytest.mark.parametrize("mode", ["trial", "training"])
def test_coordinates_cannot_bypass_an_unreadable_frame(policy_site, tmp_path, monkeypatch, mode):
    import json
    from core import browser_policy, form_check, submit_guard
    base, posts = policy_site
    frame = base.replace("127.0.0.1", "localhost") + "/frame"

    async def go():
        async with policy_browser(base + "/", mode, tmp_path, monkeypatch) as (browser, tools, ref):
            page = await browser.get_current_page()
            await _load_frame(page, EMBED, frame)
            state = await form_check.scan(browser)
            # Even an approval of the readable parent must not authorize the embedded submit.
            ref["submit_guard"].approve(await browser.get_current_page_url(),
                                        submit_guard.fingerprint(state["fields"], state["approval_state"]))
            node = next(node for node in (await _frame_form(browser)).values() if node.attributes.get("id") == "send")
            offset = json.loads(await page.evaluate("() => { const f = document.getElementById('frame'); const r = f.getBoundingClientRect(); return JSON.stringify({x:r.left+f.clientLeft,y:r.top+f.clientTop}); }"))
            point = await browser_policy.call_on_node(browser, node,
                "function() { const r = this.getBoundingClientRect(); return {x:r.left+r.width/2,y:r.top+r.height/2}; }")
            tools.set_coordinate_clicking(True)
            result = await tools.registry.execute_action("click", {
                "coordinate_x": round(offset["x"] + point["x"]),
                "coordinate_y": round(offset["y"] + point["y"])}, browser_session=browser)
            await asyncio.sleep(0.5)
            assert result.error and not posts, f"guard_error={result.error!r}, form_posts={posts!r}"

    asyncio.run(go())


@pytest.mark.parametrize("mode", ["trial", "training", "auto"])   # auto: the control, where it does send
@pytest.mark.parametrize("method", ["index_click", "enter_on_button", "space_on_button", "enter_in_field",
                                    "newline_typed", "frame_swapped_after_approval"])
def test_no_input_method_sends_an_embedded_form_on_a_parent_approval(policy_site, tmp_path, monkeypatch, mode, method):
    """A form embedded from another site, with the parent page approved (a stale or unrelated approval):
    no click, key or typed line break may send it."""
    from core import browser_policy, form_check, submit_guard
    base, posts = policy_site
    other_site = base.replace("127.0.0.1", "localhost") + "/frame"
    first = base + "/frame" if method == "frame_swapped_after_approval" else other_site   # same-origin: readable

    async def go():
        async with policy_browser(base + "/", mode, tmp_path, monkeypatch) as (browser, tools, ref):
            page = await browser.get_current_page()
            await _load_frame(page, EMBED, first)
            state = await form_check.scan(browser)
            ref["submit_guard"].approve(await browser.get_current_page_url(),
                                        submit_guard.fingerprint(state["fields"], state["approval_state"]))
            if method == "frame_swapped_after_approval":
                # Approved while the frame was readable; then the same form comes from another site.
                await _load_frame(page, SWAP, other_site)
            nodes = await _frame_form(browser)
            send = next(i for i, n in nodes.items() if n.attributes.get("id") == "send")
            email = next(i for i, n in nodes.items() if n.attributes.get("name") == "email")
            run = tools.registry.execute_action
            if method in ("index_click", "frame_swapped_after_approval"):
                result = await run("click", {"index": send}, browser_session=browser)
            elif method == "newline_typed":
                result = await run("input", {"index": email, "text": "grace@example.com\n"}, browser_session=browser)
            else:
                target = nodes[email if method == "enter_in_field" else send]
                await browser_policy.call_on_node(browser, target, "function() { this.focus(); return 1; }")
                keys = "Space" if method == "space_on_button" else "Enter"
                result = await run("send_keys", {"keys": keys}, browser_session=browser)
            await asyncio.sleep(0.7)
            if mode == "auto":
                assert posts == ["/submitted"], f"the control didn't send the form: {result.error!r}"
            else:
                assert result.error and not posts, f"guard_error={result.error!r}, form_posts={posts!r}"
                assert result.error.startswith(("Training run", "Trial run")), result.error   # the rule, not a crash

    asyncio.run(go())


CLOSED_FORM = """(html) => {
  customElements.define('x-apply', class extends HTMLElement {
    constructor() { super(); this.attachShadow({mode: 'closed'}).innerHTML = html; }
  });
  document.body.innerHTML = '<x-apply></x-apply>';
  return 1;
}"""


@pytest.mark.parametrize("mode", ["training", "trial", "auto"])   # auto: the control, where it does send
@pytest.mark.parametrize("method", ["index_click", "enter_in_field", "space_after_tab", "coordinate_click"])
def test_a_form_in_a_closed_shadow_root_is_checked(policy_site, tmp_path, monkeypatch, mode, method):
    """Page scripts (and so the guard's own reads) can't see into a closed shadow root: focus and a
    point on the page land on its host element, which looks like nothing that submits."""
    import json
    from core import browser_policy
    base, posts = policy_site
    form = ('<form method="post" action="/submitted"><input name="email" value="ada@example.com">'
            '<button id="send">Continue</button></form>')

    async def go():
        async with policy_browser(base + "/", mode, tmp_path, monkeypatch) as (browser, tools, ref):
            await (await browser.get_current_page()).evaluate(CLOSED_FORM, form)
            await asyncio.sleep(0.5)
            if mode == "trial":
                # Approve the page as the review sees it: the closed form isn't in that review.
                from core import form_check, submit_guard
                state = await form_check.scan(browser)
                ref["submit_guard"].approve(await browser.get_current_page_url(),
                                            submit_guard.fingerprint(state["fields"], state["approval_state"]))
            nodes = (await browser.get_browser_state_summary()).dom_state.selector_map
            send = next(i for i, n in nodes.items() if n.attributes.get("id") == "send")
            email = next(i for i, n in nodes.items() if n.attributes.get("name") == "email")
            run = tools.registry.execute_action
            if method == "index_click":
                result = await run("click", {"index": send}, browser_session=browser)
            elif method == "coordinate_click":
                point = await browser_policy.call_on_node(browser, nodes[send],
                    "function() { const r = this.getBoundingClientRect(); return JSON.stringify({x: r.left + r.width / 2, y: r.top + r.height / 2}); }")
                point = json.loads(point)
                tools.set_coordinate_clicking(True)
                result = await run("click", {"coordinate_x": round(point["x"]), "coordinate_y": round(point["y"])},
                                   browser_session=browser)
            else:
                await browser_policy.call_on_node(browser, nodes[email], "function() { this.focus(); return 1; }")
                if method == "space_after_tab":
                    await run("send_keys", {"keys": "Tab"}, browser_session=browser)
                result = await run("send_keys", {"keys": "Space" if method == "space_after_tab" else "Enter"},
                                   browser_session=browser)
            await asyncio.sleep(0.7)
            if mode == "auto":
                assert posts == ["/submitted"], f"the control didn't send the form: {result.error!r}"
            else:
                assert result.error and not posts, f"guard_error={result.error!r}, form_posts={posts!r}"
                assert result.error.startswith(("Training run", "Trial run")), result.error   # the rule, not a crash
                if mode == "trial" and method in ("index_click", "coordinate_click"):
                    assert "submission is disabled" in result.error   # approved page, but not this hidden form

    asyncio.run(go())


@pytest.mark.parametrize("mode", ["training", "auto"])   # auto: the control, where it does send
def test_a_scaled_coordinate_click_is_checked_where_it_lands(policy_site, tmp_path, monkeypatch, mode):
    """With a resized screenshot (Claude Sonnet models), browser-use scales the model's coordinates before
    clicking. The guard used to check the unscaled point (here an empty spot) and let the submit through."""
    import json
    base, posts = policy_site
    page_html = ('<form method="post" action="/submitted"><button id="send" style="position:absolute;'
                 'left:500px;top:400px;width:160px;height:40px">Continue</button></form>')

    async def go():
        async with policy_browser(base + "/", mode, tmp_path, monkeypatch) as (browser, tools, ref):
            page = await browser.get_current_page()
            await page.evaluate("(html) => { document.body.innerHTML = html; return 1; }", page_html)
            size = json.loads(await page.evaluate("() => JSON.stringify([innerWidth, innerHeight])"))
            browser._original_viewport_size = tuple(size)
            browser.llm_screenshot_size = (size[0] // 2, size[1] // 2)   # the model sees a half-size screenshot
            tools.set_coordinate_clicking(True)
            result = await tools.registry.execute_action("click", {"coordinate_x": 290, "coordinate_y": 210},
                                                         browser_session=browser)
            await asyncio.sleep(0.7)
            if mode == "auto":
                assert posts == ["/submitted"], f"the control didn't send the form: {result.error!r}"
            else:
                assert result.error and not posts, f"guard_error={result.error!r}, form_posts={posts!r}"
                assert result.error.startswith(("Training run", "Trial run")), result.error   # the rule, not a crash

    asyncio.run(go())


@pytest.mark.parametrize("mode", ["training", "trial", "auto"])   # auto: the control, where it does send
def test_typing_into_a_submit_button_is_checked(policy_site, tmp_path, monkeypatch, mode):
    """browser-use's input focuses the element and types key by key: a space typed "into" a button presses it."""
    base, posts = policy_site

    async def go():
        async with policy_browser(base + "/", mode, tmp_path, monkeypatch) as (browser, tools, ref):
            nodes = (await browser.get_browser_state_summary()).dom_state.selector_map
            send = next(i for i, n in nodes.items() if n.attributes.get("id") == "send")
            result = await tools.registry.execute_action("input", {"index": send, "text": "a b"}, browser_session=browser)
            await asyncio.sleep(0.7)
            if mode == "auto":
                assert posts == ["/submitted"], f"the control didn't send the form: {result.error!r}"
            else:
                assert result.error and not posts, f"guard_error={result.error!r}, form_posts={posts!r}"
                assert result.error.startswith(("Training run", "Trial run")), result.error   # the rule, not a crash

    asyncio.run(go())


@pytest.mark.parametrize("mode", ["training", "trial", "auto"])   # auto: the control, where it does send
@pytest.mark.parametrize("menu", [
    '<div id="menu" role="listbox" tabindex="0"><div role="option">Yes</div>'
    '<button role="option" type="submit">Submit application</button></div>',
    '<div id="menu" class="ui dropdown" tabindex="0" onclick="1"><div class="item">Yes</div>'
    '<button class="item" type="submit">Submit application</button></div>',
], ids=["aria", "semantic-ui"])
def test_choosing_a_dropdown_item_cant_press_a_submit_button(policy_site, tmp_path, monkeypatch, mode, menu):
    """browser-use's select_dropdown clicks any item under a "ui"/"dropdown" element (Semantic UI forms
    have class="ui form") whose text matches, including a submit button."""
    base, posts = policy_site
    page_html = '<form method="post" action="/submitted"><input name="email" value="ada@example.com">' + menu + '</form>'

    async def go():
        async with policy_browser(base + "/", mode, tmp_path, monkeypatch) as (browser, tools, ref):
            await (await browser.get_current_page()).evaluate("(html) => { document.body.innerHTML = html; return 1; }",
                                                              page_html)
            await asyncio.sleep(0.3)
            nodes = (await browser.get_browser_state_summary()).dom_state.selector_map
            index = next(i for i, n in nodes.items() if n.attributes.get("id") == "menu")
            ordinary = await tools.registry.execute_action("select_dropdown", {"index": index, "text": "Yes"},
                                                           browser_session=browser)
            assert ordinary.error is None and not posts   # choosing an ordinary item still works, in every mode
            result = await tools.registry.execute_action("select_dropdown", {"index": index, "text": "Submit application"},
                                                         browser_session=browser)
            await asyncio.sleep(0.7)
            if mode == "auto":
                assert posts == ["/submitted"], f"the control didn't send the form: {result.error!r} {result.extracted_content!r}"
                assert ref["submit_guard"].attempts == 1   # recorded first; the ordinary choice wasn't counted
            else:
                assert result.error and not posts, f"guard_error={result.error!r}, form_posts={posts!r}"
                assert result.error.startswith(("Training run", "Trial run")), result.error   # the rule, not a crash
                assert result.error.startswith(("Training run", "Trial run")), result.error   # the guard's own rule

    asyncio.run(go())


def test_ordinary_actions_still_work_in_training(policy_site, tmp_path, monkeypatch):
    """The new checks (the browser's hit test, closed shadow roots, dropdown items, spaces) must not
    get in the way of normal filling: none of these may be refused or crash."""
    import json
    from core import browser_policy
    base, posts = policy_site
    page_html = ('<label id="lbl" style="position:absolute;left:40px;top:300px">Name</label>'
                 '<input id="name" style="position:absolute;left:40px;top:330px">'
                 '<input id="search" role="combobox" style="position:absolute;left:40px;top:380px">'
                 '<select id="pick"><option>No</option><option>Yes</option></select>')

    async def go():
        async with policy_browser(base + "/", "training", tmp_path, monkeypatch) as (browser, tools, ref):
            page = await browser.get_current_page()
            await page.evaluate("(html) => { document.body.innerHTML = html; return 1; }", page_html)
            await asyncio.sleep(0.3)
            nodes = (await browser.get_browser_state_summary()).dom_state.selector_map
            by_id = {n.attributes.get("id"): i for i, n in nodes.items()}
            run = tools.registry.execute_action
            tools.set_coordinate_clicking(True)
            point = json.loads(await page.evaluate(
                "() => { const r = document.getElementById('lbl').getBoundingClientRect(); return JSON.stringify([r.left + 5, r.top + 5]); }"))
            results = [await run("click", {"coordinate_x": round(point[0]), "coordinate_y": round(point[1])}, browser_session=browser),
                       await run("input", {"index": by_id["name"], "text": "Ada Lovelace"}, browser_session=browser),
                       await run("send_keys", {"keys": "Tab"}, browser_session=browser),
                       await run("select_dropdown", {"index": by_id["pick"], "text": "Yes"}, browser_session=browser)]
            await browser_policy.call_on_node(browser, nodes[by_id["search"]], "function() { this.focus(); return 1; }")
            results.append(await run("send_keys", {"keys": "Enter"}, browser_session=browser))   # picks in a combobox
            assert [r.error for r in results] == [None] * 5 and not posts
            assert await page.evaluate("() => document.getElementById('name').value") == "Ada Lovelace"
            assert ref["submit_guard"].attempts == 0

    asyncio.run(go())


@pytest.mark.parametrize("mode", ["training", "trial", "auto"])   # auto: the control, where it does send
@pytest.mark.parametrize("label", ['<label id="lbl" for="send">Continue</label>',
                                   '<label id="lbl">Continue <button id="send" type="submit">Submit</button></label>'],
                         ids=["for", "wrapping"])
def test_clicking_a_label_of_a_submit_button_is_checked(policy_site, tmp_path, monkeypatch, mode, label):
    """Clicking a <label> presses the button it labels. (browser-use doesn't index plain labels, so the
    agent reaches them by coordinates; NODE_JS resolves an indexed one the same way.)"""
    import json
    base, posts = policy_site
    button = '' if "wrapping" in label or 'for=' not in label else '<button id="send" type="submit">Submit</button>'
    page_html = f'<form method="post" action="/submitted"><input name="email" value="ada@example.com">{button}</form>'
    if 'for=' in label:
        page_html += label
    else:
        page_html = page_html.replace('</form>', label + '</form>')

    async def go():
        async with policy_browser(base + "/", mode, tmp_path, monkeypatch) as (browser, tools, ref):
            page = await browser.get_current_page()
            await page.evaluate("(html) => { document.body.innerHTML = html; return 1; }", page_html)
            await asyncio.sleep(0.3)
            point = json.loads(await page.evaluate(
                "() => { const r = document.getElementById('lbl').getBoundingClientRect(); return JSON.stringify([r.left + 3, r.top + r.height / 2]); }"))
            tools.set_coordinate_clicking(True)
            result = await tools.registry.execute_action(
                "click", {"coordinate_x": round(point[0]), "coordinate_y": round(point[1])}, browser_session=browser)
            await asyncio.sleep(0.7)
            if mode == "auto":
                assert posts == ["/submitted"], f"the control didn't send the form: {result.error!r}"
            else:
                assert result.error and not posts, f"guard_error={result.error!r}, form_posts={posts!r}"
                assert result.error.startswith(("Training run", "Trial run")), result.error

    asyncio.run(go())
