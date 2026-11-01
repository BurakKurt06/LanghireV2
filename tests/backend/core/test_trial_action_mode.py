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


@pytest.mark.parametrize('label', ['Next','Save and Continue','Sign in','Register','Submit profile','Submit resume','Submit code'])
def test_normal_trial_allows_intermediate_native_forms(label):
    guard=submit_guard.SubmitGuard(mode='trial',live_steps=True)
    decision=guard.decide('click',submit_guard.Control(text=label,tag='button',type='submit',in_form=True))
    assert decision.allowed and not decision.submit


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
