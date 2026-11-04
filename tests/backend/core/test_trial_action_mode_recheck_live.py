"""Explicit local-only Trial action probes, synthetic candidate, fake HTTPS transport."""
import asyncio
import html
import json
import os

import pytest
from cli import apply_jobs
from core import browser_policy, request_gate
from tests.backend.core.test_request_gate_live import browser_and_gate, bridge_to, wait_for, wire_site

pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(os.environ.get('LANGHIRE_REAL_BROWSER') != '1', reason='opt in')]

CASES = [
    (f'exact-{label}-{file}', label, 'button', 'click', file, 'final')
    for label in ['Submit resume', 'Submit your resume', 'Submit CV', 'Submit documents'] for file in [False, True]
] + [
    (f'label-{label}', label, 'button', 'click', False, 'final')
    for label in ['Submit profile', 'Save and submit', 'Done', 'Confirm', 'Send', 'Finish & submit', 'Continue', 'Submit']
] + [
    ('submit-file', 'Submit', 'button', 'click', True, 'final'),
    ('input-submit', 'Submit application', 'input', 'click', False, 'final'),
    ('aria-only', 'Submit application', 'aria', 'click', False, 'final'),
    ('icon-only', 'Submit application', 'icon', 'click', False, 'final'),
    ('link-final', 'Submit application', 'link', 'click', False, 'final'),
    ('div-final', 'Submit application', 'div', 'click', False, 'final'),
    ('link-continue-final', 'Continue', 'link', 'click', False, 'final'),
    ('div-continue-final', 'Continue', 'div', 'click', False, 'final'),
    ('div-icon-final', '➤', 'div', 'click', False, 'final'),
    ('iframe-final', 'Submit application', 'iframe', 'click', False, 'final'),
    ('shadow-open', 'Submit application', 'shadow-open', 'click', False, 'final'),
    ('shadow-closed', 'Submit application', 'shadow-closed', 'click', False, 'final'),
    ('enter-field', 'Submit application', 'button', 'Enter', False, 'final'),
    ('enter-no-heading', 'Submit application', 'button', 'Enter', False, 'final'),
    ('newline-final', 'Submit application', 'button', 'input-newline', False, 'final'),
    ('newline-no-heading', 'Submit application', 'button', 'input-newline', False, 'final'),
    ('step-counter', 'Continue', 'step-counter', 'click', False, 'final'),
    ('space-final', 'Submit application', 'button', 'Space', False, 'final'),
    ('accesskey', 'Submit application', 'button', 'Alt+s', False, 'final'),
    ('ctrl-enter', 'Submit application', 'shortcut', 'Control+Enter', False, 'site-script'),
    ('script-requestSubmit', 'Next', 'script-request', 'click', False, 'site-script'),
    ('script-click', 'Next', 'script-click', 'click', False, 'site-script'),
    ('turkish', 'Başvuruyu gönder', 'button', 'click', False, 'final'),
    ('polish', 'Wyślij aplikację', 'button', 'click', False, 'final'),
    ('japanese', '応募を送信', 'button', 'click', False, 'final'),
    ('upload', 'Upload resume', 'button', 'click', True, 'intermediate'),
    ('attach', 'Attach resume', 'button', 'click', True, 'intermediate'),
    # Claude's PR #25 review: names absent from innerText/value/aria-label/title.
] + [
    (f'accessible-{kind}-{heading}', 'Submit application', kind, 'click', False, 'final')
    for kind in ('image', 'labelled', 'css') for heading in ('no-heading', 'final-heading')
] + [
    (f'accessible-{kind}-{action}-no-heading', 'Submit application', kind, action, False, 'final')
    for kind in ('image', 'labelled', 'css') for action in ('coordinate', 'Space', 'Enter', 'input-newline')
] + [
    ('native-next-no-heading', 'Next', 'button', 'click', False, 'intermediate-next'),
    ('native-next-final-heading', 'Next', 'button', 'click', False, 'final'),
    ('unnamed-arrow-next-no-heading', '➤', 'icon', 'click', False, 'intermediate-next'),
    ('aria-next-visible-submit-no-heading', 'Next', 'aria-next-visible-submit', 'click', False, 'final'),
    ('aria-submit-visible-next-no-heading', 'Submit application', 'aria-submit-visible-next', 'click', False, 'final'),
    ('labelled-next-visible-apply-no-heading', 'Next', 'labelled-next-visible-apply', 'click', False, 'final'),
    ('css-before-no-heading', 'Submit application', 'css-before', 'click', False, 'final'),
    ('css-split-no-heading', 'Submit application', 'css-split', 'click', False, 'final'),
    ('css-title-next-no-heading', 'Submit application', 'css-title-next', 'click', False, 'final'),
    ('css-aria-next-no-heading', 'Submit application', 'css-aria-next', 'click', False, 'final'),
    ('css-visible-next-no-heading', 'Submit application', 'css-visible-next', 'click', False, 'final'),
    ('css-title-next-enter-no-heading', 'Submit application', 'css-title-next', 'Enter', False, 'final'),
    ('css-hidden-display-next-no-heading', 'Next', 'css-hidden-display', 'click', False, 'intermediate-next'),
    ('css-hidden-visibility-next-no-heading', 'Next', 'css-hidden-visibility', 'click', False, 'intermediate-next'),
    ('labelled-multiple-no-heading', 'Submit application', 'labelled-multiple', 'click', False, 'final'),
    ('labelled-long-id-no-heading', 'Submit application', 'labelled-long-id', 'click', False, 'final'),
    ('image-child-no-heading', 'Submit application', 'image-child', 'click', False, 'final'),
    ('apply-enter-no-heading', 'Apply Now', 'button', 'Enter', False, 'final'),
    ('apply-newline-no-heading', 'Apply Now', 'button', 'input-newline', False, 'final'),
] + [
    (f'accessible-{kind}-next-no-heading', 'Next', kind, 'click', False, 'intermediate-next')
    for kind in ('image', 'labelled', 'css')
] + [
    (f'accessible-{kind}-next-enter-no-heading', 'Next', kind, 'Enter', False, 'intermediate-next')
    for kind in ('image', 'labelled', 'css')
]

@pytest.mark.parametrize('case,label,kind,action,file,classification', CASES, ids=[x[0] for x in CASES])
async def test_trial_action_matrix(wire_site, monkeypatch, case, label, kind, action, file, classification):
    local, seen, site = wire_site
    label = html.escape(label)
    control = f'<button id="final" accesskey="s">{label}</button>'
    if kind == 'input': control = f'<input id="final" type="submit" value="{label}">'
    if kind == 'aria': control = f'<button id="final" aria-label="{label}" style="width:100px;height:40px"><svg width="20" height="20"><path d="M0 0L20 20"/></svg></button>'
    if kind == 'icon': control = '<button id="final" style="width:100px;height:40px">➤</button>'
    if kind == 'image': control = f'<input id="final" type="image" alt="{label}" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" style="width:200px;height:44px">'
    if kind == 'labelled': control = f'<span id="lbl">{label}</span><button id="final" aria-labelledby="lbl" style="width:200px;height:44px"><svg/></button>'
    if kind in ('css', 'css-before', 'css-split'):
        styles = ('#final::before{content:"Submit "}#final::after{content:"application"}' if kind == 'css-split' else
                  '#final::' + ('before' if kind == 'css-before' else 'after') + '{content:' + json.dumps(html.unescape(label)) + '}')
        control = f'<style>{styles}</style><button id="final" style="width:200px;height:44px"></button>'
    if kind in ('css-title-next', 'css-aria-next', 'css-visible-next'):
        attributes = 'title="Next"' if kind == 'css-title-next' else 'aria-label="Next"' if kind == 'css-aria-next' else ''
        visible = 'Next' if kind == 'css-visible-next' else ''
        control = '<style>#final::after{content:"Submit application"}</style>' + f'<button id="final" {attributes} style="width:200px;height:44px">{visible}</button>'
    if kind in ('css-hidden-display', 'css-hidden-visibility'):
        hidden = 'display:none' if kind == 'css-hidden-display' else 'visibility:hidden'
        control = '<style>#final::after{content:"Submit application";' + hidden + '}</style><button id="final" title="Next" style="width:200px;height:44px">Next</button>'
    if kind == 'aria-next-visible-submit': control = '<button id="final" aria-label="Next">Submit</button>'
    if kind == 'aria-submit-visible-next': control = '<button id="final" aria-label="Submit application">Next</button>'
    if kind == 'labelled-next-visible-apply': control = '<span id="lbl">Next</span><button id="final" aria-labelledby="lbl">Apply Now</button>'
    if kind == 'labelled-multiple': control = '<span id="verb">Submit</span><span id="noun">application</span><button id="final" aria-labelledby="verb noun" style="width:200px;height:44px"><svg/></button>'
    if kind == 'labelled-long-id':
        reference = 'synthetic-' + 'a' * 160
        control = f'<span id="{reference}">Submit application</span><button id="final" aria-labelledby="{reference}" style="width:200px;height:44px"><svg/></button>'
    if kind == 'image-child': control = '<button id="final" style="width:200px;height:44px"><img alt="Submit application" src="data:image/gif;base64,R0lGODlhAQABAAAAACw="></button>'
    if kind in ('link', 'div'):
        tag = 'a' if kind == 'link' else 'div'
        control = f'<{tag} id="final" role="button" tabindex="0" style="width:200px;height:40px" onclick="this.closest(\'form\').requestSubmit()">{label}</{tag}>'
    if kind.startswith('script-'):
        script = "document.getElementById('f').requestSubmit()" if kind == 'script-request' else "document.getElementById('hidden-final').click()"
        control = f'<button id="final" type="button" onclick="{script}">Next</button><button id="hidden-final" hidden>Submit application</button>'
    form = '<form id="f" method="post" action="https://careers.example.test/application-received"><input id="answer" name="candidate" value="synthetic">' + ('<input type="file" name="resume">' if file else '') + control + '</form>'
    header = '<h1>Final application review — last step</h1><p>Send your application to complete it.</p>'
    if classification == 'site-script':
        header = '<h1>Application details — step 1 of 3</h1><p>Save these details and move to the next step.</p>'
    if classification == 'intermediate':
        header = '<h1>Documents — step 1 of 3</h1>'
        form = form.replace('/application-received', '/upload-step')
    if case.endswith('no-heading'):
        header = '<h1>Application</h1>'
    if kind == 'step-counter':
        header = '<h1>Application</h1><h2>Step 3 of 3</h2>'
    header = '<meta charset="utf-8">' + header
    site['html'] = header + form
    if kind == 'iframe':
        site['html'] = header + '<iframe width="900" height="700" src="https://frame.example.test/frame"></iframe>'
        site['responses'] = {'/frame': (200, {}, (header + form).encode())}
    if kind.startswith('shadow-'):
        mode = kind.split('-')[1]
        site['html'] = header + '<div id="host"></div><script>document.getElementById("host").attachShadow({mode:' + json.dumps(mode) + '}).innerHTML=' + json.dumps(form) + '</script>'
    if kind == 'shortcut':
        site['html'] += '<script>document.addEventListener("keydown",e=>{if(e.ctrlKey&&e.key==="Enter"){e.preventDefault();document.getElementById("f").requestSubmit()}})</script>'
    job = 'https://careers.example.test/role'
    browser, gate = await browser_and_gate('trial', job, transport_factory=bridge_to(local), trusted_site=lambda u: True, strict_trial_protocols=False)
    monkeypatch.setattr(apply_jobs, 'update_job', lambda *a, **kw: True)
    apply_jobs.set_run_mode('trial')
    try:
        tools = apply_jobs.build_tools({'url': job, 'company': 'Synthetic', 'title': 'Local'}, '', True, {'request_gate': gate}, 1)
        await browser.navigate_to(job)
        # The international labels must be rendered as UTF-8, not a legacy-encoding fallback.
        encoding = await browser_policy.evaluate(browser, '() => document.characterSet')
        assert encoding.upper() == 'UTF-8'
        # Bounded readiness wait, including embedded/closed-shadow browser-use element maps.
        deadline = asyncio.get_running_loop().time() + 20
        while True:
            state = await browser.get_browser_state_summary()
            nodes = state.dom_state.selector_map
            found = [i for i,n in nodes.items() if n.attributes.get('id') == 'final']
            if found: break
            assert asyncio.get_running_loop().time() < deadline, f'final control not in browser element map: {case}'
            await asyncio.sleep(.1)
        if action == 'input-newline':
            field = next(i for i,n in nodes.items() if n.attributes.get('id') == 'answer')
            result = await tools.registry.execute_action('input', {'index': field, 'text': 'synthetic\n'}, browser_session=browser)
        elif action == 'click':
            result = await tools.registry.execute_action('click', {'index': found[0]}, browser_session=browser)
        elif action == 'coordinate':
            point = await browser_policy.evaluate(browser, "() => {const r=document.getElementById('final').getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2];}")
            if isinstance(point, str): point = json.loads(point)
            tools.set_coordinate_clicking(True)
            result = await tools.registry.execute_action('click', {'coordinate_x': round(point[0]), 'coordinate_y': round(point[1])}, browser_session=browser)
        else:
            target = 'answer' if action in ('Enter', 'Control+Enter') else 'final'
            await browser_policy.evaluate(browser, '() => {document.getElementById(' + json.dumps(target) + ').focus();return true;}')
            result = await tools.registry.execute_action('send_keys', {'keys': action}, browser_session=browser)
        sent = await wait_for(lambda: any(r[0]=='POST' for r in seen), gate, tries=20)
        record = {'case':case,'classification':classification,'encoding':encoding,'observed_label':nodes[found[0]].get_all_children_text(),'action_error':str(result.error) if result.error else None,
                  'posts':[{'path':r[1],'body':r[2].decode(errors='replace')} for r in seen if r[0]=='POST']}
        print('RECHECK ' + json.dumps(record, ensure_ascii=False))
        if classification == 'final':
            assert not sent, f'Agent activated final application control: {record}'
            assert result.error, f'Final action was allowed: {record}'
            assert result.error.startswith('Trial run:'), f'Final action failed for a non-Trial reason: {record}'
        elif action != 'Control+Enter':
            assert sent and not result.error, record
        # Ctrl+Enter on a page-owned handler is observed, not misrepresented as a native final click.
    finally:
        apply_jobs.set_run_mode('auto')
        await gate.close()
