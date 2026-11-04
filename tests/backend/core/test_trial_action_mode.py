"""Production Trial follows normal requests; its boundary is the agent action."""
from types import SimpleNamespace
import httpx
import pytest

from core import request_gate, submit_guard
from tests.backend.core.test_request_gate import FakeCDP, paused
from tests.backend.core.test_request_gate_live import wire_site  # noqa: F401


@pytest.mark.parametrize('path', ['/rpc', '/submit', '/career', '/careersection/application.jss'])
async def test_default_trial_forwards_opaque_step_requests_without_approval(path):
    sent=[]
    gate=request_gate.RequestGate(SimpleNamespace(), 'trial', {'url':'https://jobs.example/application'})
    gate.client=FakeCDP();gate.healthy=lambda:True
    gate.transport_factory=lambda:httpx.MockTransport(lambda r:sent.append(r) or httpx.Response(200,content=b'ok'))
    try:
        assert gate.live_trial and not gate.strict_trial
        await gate._request(paused('step','https://jobs.example'+path,'POST','opaque=next&operation=submit'),'s')
        assert len(sent)==1 and sent[0].content==b'opaque=next&operation=submit'
        assert gate.client.calls==[('fulfill','step',200)]
        assert not gate.trial_blocked_operation and not request_gate.pending()
    finally:
        for transport in gate._transports.values():transport.close()


@pytest.mark.parametrize('label', ['Next','Save and Continue','Sign in','Register','Submit profile','Submit code','Weiter'])
def test_normal_trial_allows_intermediate_native_forms(label):
    guard=submit_guard.SubmitGuard(mode='trial',live_steps=True)
    decision=guard.decide('click',submit_guard.Control(text=label,tag='button',type='submit',in_form=True))
    assert decision.allowed and not decision.submit


@pytest.mark.parametrize('label', ['Submit resume', 'Submit your resume', 'Submit CV', 'Submit documents'])
def test_a_submit_label_naming_the_resume_is_final(label):
    """Codex review R01 and its re-check: "Submit resume" sends one-page applications, including forms that
    also upload the file, so Trial never activates it (the label can't tell it from an upload step)."""
    guard=submit_guard.SubmitGuard(mode='trial',live_steps=True)
    decision=guard.decide('click',submit_guard.Control(text=label,tag='button',type='submit',in_form=True))
    assert not decision.allowed and decision.submit


@pytest.mark.parametrize('label', ['Upload resume', 'Upload documents', 'Attach resume'])
def test_upload_buttons_are_still_steps(label):
    guard=submit_guard.SubmitGuard(mode='trial',live_steps=True)
    assert guard.decide('click',submit_guard.Control(text=label,tag='button',type='submit',in_form=True)).allowed


@pytest.mark.parametrize('label', ['Bewerbung absenden','Jetzt bewerben','Envoyer ma candidature','Enviar solicitud',
                                   'Enviar candidatura','Invia candidatura','Sollicitatie versturen','Absenden','Postuler'])
def test_normal_trial_stops_translated_final_controls(label):
    """Codex review R01: international employers' final controls."""
    guard=submit_guard.SubmitGuard(mode='trial',live_steps=True)
    decision=guard.decide('click',submit_guard.Control(text=label,tag='button',type='submit',in_form=True))
    assert not decision.allowed and decision.submit


@pytest.mark.parametrize('label', ['Submit application','Submit','Finish application','Apply'])
@pytest.mark.parametrize('action,text', [('click',''),('send_keys','Enter'),('send_keys','Space'),('input',' ')])
def test_normal_trial_stops_final_control_activation(label,action,text):
    guard=submit_guard.SubmitGuard(mode='trial',live_steps=True)
    control=submit_guard.Control(text=label,tag='button',type='submit',in_form=True)
    decision=guard.decide(action,control,text=text)
    assert not decision.allowed and decision.submit


def test_trial_cannot_bypass_final_control_with_agent_script_or_unreadable_target():
    guard=submit_guard.SubmitGuard(mode='trial',live_steps=True)
    assert not guard.decide('evaluate',None).allowed
    assert not guard.decide('click',None).allowed
    assert not guard.decide('send_keys',submit_guard.Control(tag='input',in_form=True),text='Tab Enter').allowed
    assert not guard.decide('send_keys',submit_guard.Control(tag='input',in_form=True),text='Alt+s').allowed


def test_initial_apply_link_is_still_navigation():
    guard=submit_guard.SubmitGuard(mode='trial',live_steps=True)
    assert guard.decide('click',submit_guard.Control(text='Apply',tag='a',in_form=False)).allowed


@pytest.mark.parametrize('target',['private','mail'])
async def test_default_trial_still_blocks_private_and_gmail_connections(wire_site,target):
    local,seen,_=wire_site
    url=local+'private-submit' if target=='private' else 'https://mail.google.com/mail/u/0/'
    gate=request_gate.RequestGate(SimpleNamespace(),'trial',{'url':'https://jobs.example/apply'})
    gate.client=FakeCDP();gate.healthy=lambda:True
    try:
        await gate._request(paused('blocked',url,'POST','local-test'),'s')
        assert not seen and gate.blocked==1
        assert gate.client.calls[0][0]=='fail' and not request_gate.pending()
    finally:
        for transport in gate._transports.values():transport.close()


@pytest.mark.parametrize('label', ['Submit profile', 'Continue', '➤'])
def test_final_step_context_is_not_an_intermediate_form(label):
    guard = submit_guard.SubmitGuard(mode='trial', live_steps=True)
    decision = guard.decide('click', submit_guard.Control(
        text=label, tag='button', type='submit', in_form=True, final_context=True))
    assert not decision.allowed and decision.submit


@pytest.mark.parametrize('label', ['Save and submit', 'Finish & submit', 'Done', 'Confirm',
                                   'Başvuruyu gönder', 'Wyślij aplikację', '応募を送信'])
def test_additional_final_labels_are_stopped_without_a_heading(label):
    guard = submit_guard.SubmitGuard(mode='trial', live_steps=True)
    decision = guard.decide('click', submit_guard.Control(text=label, tag='button', type='submit', in_form=True))
    assert not decision.allowed and decision.submit


@pytest.mark.parametrize('action,text', [('send_keys', 'Enter'), ('input', 'synthetic\n')])
def test_implicit_submission_checks_the_forms_default_submitter(action, text):
    guard = submit_guard.SubmitGuard(mode='trial', live_steps=True)
    field = submit_guard.Control(tag='input', type='text', in_form=True, submitter_text='Submit application')
    assert guard.decide(action, field, text=text).submit
    # An actual intermediate native Next remains available without another model call or approval.
    field.submitter_text = 'Next'
    assert guard.decide(action, field, text=text).allowed
    field.final_context = True
    assert guard.decide(action, field, text=text).submit


@pytest.mark.parametrize('tag,label', [('a', 'Continue'), ('div', 'Continue'), ('div', '➤')])
def test_final_page_rule_also_checks_styled_application_controls(tag, label):
    guard = submit_guard.SubmitGuard(mode='trial', live_steps=True)
    control = submit_guard.Control(text=label, tag=tag, role='button', type='button', in_form=True, final_context=True)
    assert guard.decide('click', control).submit
    control.final_context = False
    assert guard.decide('click', control).allowed


def test_final_page_context_does_not_block_a_combobox_picker():
    guard = submit_guard.SubmitGuard(mode='trial', live_steps=True)
    control = submit_guard.Control(text='▾', tag='button', type='button', role='combobox', in_form=True, final_context=True)
    assert guard.decide('click', control).allowed


@pytest.mark.parametrize('name,visible', [
    ('Next', 'Submit'), ('Next', 'Apply Now'), ('Next', 'Submit application'),
    ('Submit application', 'Next'), ('Submit profile', 'Submit application'),
])
def test_accessible_names_and_visible_text_both_keep_final_submit_evidence(name, visible):
    guard = submit_guard.SubmitGuard(mode='trial', live_steps=True)
    control = submit_guard.Control(text=name, text_alternative=visible, tag='button', in_form=True)
    decision = guard.decide('click', control)
    assert decision.submit and not decision.allowed


@pytest.mark.parametrize('action,text', [('send_keys', 'Enter'), ('input', 'synthetic\n')])
@pytest.mark.parametrize('name,visible', [
    ('Submit application', ''), ('Next', 'Submit application'), ('Apply Now', ''), ('Next', 'Apply Now'),
])
def test_implicit_submission_uses_accessible_and_visible_default_submitter_names(action, text, name, visible):
    guard = submit_guard.SubmitGuard(mode='trial', live_steps=True)
    control = submit_guard.Control(tag='input', type='text', in_form=True,
        submitter_text=name, submitter_alternative=visible)
    decision = guard.decide(action, control, text=text)
    assert decision.submit and not decision.allowed


@pytest.mark.parametrize('name,visible', [('Next', ''), ('→', ''), ('Submit profile', 'Next')])
def test_reading_an_accessible_name_keeps_intermediate_native_steps_available(name, visible):
    guard = submit_guard.SubmitGuard(mode='trial', live_steps=True)
    control = submit_guard.Control(text=name, text_alternative=visible, tag='button', in_form=True)
    assert guard.decide('click', control).allowed


@pytest.mark.parametrize('name', ['Submit application', 'Apply Now', 'Submit resume'])
def test_plain_text_input_is_not_submission_even_with_a_named_final_submitter(name):
    guard = submit_guard.SubmitGuard(mode='trial', live_steps=True)
    control = submit_guard.Control(text=name, tag='input', type='text', in_form=True, submitter_text=name)
    decision = guard.decide('input', control, text='synthetic text without a line break')
    assert decision.allowed and not decision.submit


@pytest.mark.parametrize('action,text', [('send_keys', 'Enter'), ('send_keys', 'Space'), ('input', 'synthetic text')])
def test_a_focused_next_button_uses_its_own_label_instead_of_another_apply_button(action, text):
    guard = submit_guard.SubmitGuard(mode='trial', live_steps=True)
    control = submit_guard.Control(text='Next', tag='button', in_form=True, submitter_text='Apply Now')
    assert guard.decide(action, control, text=text).allowed


def test_css_final_text_is_not_hidden_by_an_accessible_next_name():
    guard = submit_guard.SubmitGuard(mode='trial', live_steps=True)
    control = submit_guard.Control(text='Next', text_pseudo='Submit application', tag='button', in_form=True)
    assert guard.decide('click', control).submit
    assert not guard.decide('click', control).allowed


@pytest.mark.parametrize('action,text', [('send_keys', 'Enter'), ('input', 'synthetic\n')])
def test_default_submitter_css_text_remains_final_when_its_accessible_name_is_next(action, text):
    guard = submit_guard.SubmitGuard(mode='trial', live_steps=True)
    control = submit_guard.Control(tag='input', type='text', in_form=True,
        submitter_text='Next', submitter_pseudo='Submit application')
    decision = guard.decide(action, control, text=text)
    assert decision.submit and not decision.allowed
