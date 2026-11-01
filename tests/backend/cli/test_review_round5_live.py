"""Additional native-submit checks: every suspected bypass needs a real local POST control."""
import asyncio
import os

import pytest

from tests.backend.cli.test_submit_guard_live import policy_browser, policy_site

pytestmark = [pytest.mark.real_browser,
              pytest.mark.skipif(os.environ.get("LANGHIRE_REAL_BROWSER") != "1", reason="opt-in: launches a real browser")]


@pytest.mark.parametrize("mode", ["training", "trial", "auto"])
@pytest.mark.parametrize("attributes", ['role="combobox"', 'aria-autocomplete="list"', 'contenteditable="true"', 'role="button"'])
def test_native_input_enter_cannot_be_disguised(policy_site, tmp_path, monkeypatch, mode, attributes):
    from core import browser_policy
    base, posts = policy_site
    html = (f'<form method="post" action="/submitted"><input id="field" name="email" value="dummy@example.com" {attributes}>'
            '<button id="send" type="submit">Continue</button></form>')

    async def go():
        async with policy_browser(base + "/", mode, tmp_path, monkeypatch) as (browser, tools, ref):
            await (await browser.get_current_page()).evaluate("(html) => { document.body.innerHTML = html; return 1; }", html)
            nodes = (await browser.get_browser_state_summary()).dom_state.selector_map
            field = next(n for n in nodes.values() if n.attributes.get("id") == "field")
            await browser_policy.call_on_node(browser, field, "function() { this.focus(); return 1; }")
            result = await tools.registry.execute_action("send_keys", {"keys": "Enter"}, browser_session=browser)
            await asyncio.sleep(0.6)
            if mode == "auto":
                assert posts == ["/submitted"], f"positive control did not post: {result.error!r}"
            else:
                assert result.error and not posts, f"guard={result.error!r}, actual_posts={posts!r}"
                assert result.error.startswith(("Training run", "Trial run"))

    asyncio.run(go())


@pytest.mark.parametrize("mode", ["training", "trial", "auto"])
@pytest.mark.parametrize("method", ["index", "coordinate"])
@pytest.mark.parametrize("tag", ["span", "a"])
def test_aria_label_descendant_cannot_hide_its_submit(policy_site, tmp_path, monkeypatch, mode, method, tag):
    import json
    base, posts = policy_site
    html = ('<form method="post" action="/submitted"><button id="send" type="submit">Continue</button></form>'
            f'<label for="send"><{tag} id="alias" role="button" tabindex="0">Choose</{tag}></label>')

    async def go():
        async with policy_browser(base + "/", mode, tmp_path, monkeypatch) as (browser, tools, ref):
            page = await browser.get_current_page()
            await page.evaluate("(html) => { document.body.innerHTML = html; return 1; }", html)
            if method == "index":
                nodes = (await browser.get_browser_state_summary()).dom_state.selector_map
                index = next(i for i, n in nodes.items() if n.attributes.get("id") == "alias")
                params = {"index": index}
            else:
                point = json.loads(await page.evaluate("() => { const r = document.getElementById('alias').getBoundingClientRect(); return JSON.stringify([r.left+r.width/2,r.top+r.height/2]); }"))
                tools.set_coordinate_clicking(True)
                params = {"coordinate_x": round(point[0]), "coordinate_y": round(point[1])}
            result = await tools.registry.execute_action("click", params, browser_session=browser)
            await asyncio.sleep(0.6)
            if mode == "auto":
                assert posts == ["/submitted"], f"positive control did not post: {result.error!r}"
            else:
                assert result.error and not posts, f"guard={result.error!r}, actual_posts={posts!r}"
                assert result.error.startswith(("Training run", "Trial run"))

    asyncio.run(go())
