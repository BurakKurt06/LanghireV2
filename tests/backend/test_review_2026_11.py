"""Codex full review 2026-11 (R03, R04, R07-R12, R16-R18): its reproductions, kept as regression tests.

Synthetic data only: temporary data folders, made-up PDFs and a fake AI; nothing is sent anywhere.
R01/R02/R05/R06/R13 live next to the code they cover (test_trial_action_mode*, test_apply_jobs,
test_main_api, Settings.test, Dashboard.recovery.test).
"""
import asyncio
import copy
import threading
import time
from types import SimpleNamespace

import fitz
import pytest

from tests.backend.cli.test_apply_jobs import run, FakeHistory  # noqa: F401  (run is a fixture)


@pytest.fixture(autouse=True)
def isolated_run_mode(monkeypatch):
    from cli import apply_jobs
    monkeypatch.setattr(apply_jobs, '_run_options', {'mode': 'auto'})


# ── Stop (R03, R04, R17) ────────────────────────────────────────────────────

async def test_stop_during_worker_stagger_lets_the_first_worker_clean_up(monkeypatch):
    """R04: Stop while worker 2 waits its turn. Worker 1 is cancelled on its own and finishes its
    cleanup (its browser's event bus still running) before the final sweep, not together with it."""
    import main
    import core.shared_config as shared
    from cli import apply_jobs
    from models import ApplyRequest

    started, staggering, cleanup_finished = threading.Event(), threading.Event(), threading.Event()
    jobs = {f"https://jobs.example.test/{i}": {
        "url": f"https://jobs.example.test/{i}", "title": "Synthetic role", "company": f"Example {i}", "status": "pending"
    } for i in range(2)}
    monkeypatch.setattr(main, "_start_problem", lambda **kw: "")
    monkeypatch.setattr(main, "_load_jobs", lambda: copy.deepcopy(jobs))
    monkeypatch.setattr(main, "_kill_browser_processes", lambda: None)
    monkeypatch.setattr(main, "_wait_for_browsers_to_close", lambda _: None)
    monkeypatch.setattr(main, "_get_metrics_store", lambda: None)
    monkeypatch.setattr(main, "load_llm_settings", lambda: {})
    monkeypatch.setattr(main, "_browser_user", lambda: "")
    monkeypatch.setattr(shared, "load_json", lambda path, default: copy.deepcopy(jobs) if path == shared.JOBS_FILE else {})
    monkeypatch.setattr(shared, "prepare_worker_profiles", lambda _: None)
    monkeypatch.setattr(shared, "prepare_mail_profile", lambda: None)

    async def stagger():
        staggering.set()
        await asyncio.Future()

    async def worker(*args, **kwargs):
        async def bus():
            await asyncio.Future()
        bus_task = asyncio.create_task(bus())
        started.set()
        try:
            await asyncio.Future()
        finally:
            # Like browser.kill(): it needs the event bus to still be running to finish.
            await asyncio.sleep(.15)
            if not bus_task.cancelled():
                cleanup_finished.set()

    monkeypatch.setattr(shared, "worker_stagger", stagger)
    monkeypatch.setattr(apply_jobs, "worker", worker)
    monkeypatch.setattr(main, "_apply_status", {"running": False, "log": []})
    result = await main.start_applying(ApplyRequest(workers=2, mode="all"))
    assert result["success"]
    assert await asyncio.to_thread(started.wait, 5)
    assert await asyncio.to_thread(staggering.wait, 5)
    await asyncio.to_thread(main._force_stop, main._apply_status)
    await asyncio.to_thread(main._apply_thread.join, 5)
    assert not main._apply_thread.is_alive()
    assert cleanup_finished.is_set(), "Stop cancelled worker 1 together with its cleanup"


async def test_stop_does_not_block_the_server(monkeypatch):
    """R17: the Stop endpoint runs the (blocking) stop off the server's event loop."""
    import main
    monkeypatch.setattr(main, "_set_apply_paused", lambda _: None)
    monkeypatch.setattr(main, "_force_stop", lambda _: time.sleep(.20))
    ran = []

    async def health_poll():
        await asyncio.sleep(.01)
        ran.append(time.monotonic())
    poll = asyncio.create_task(health_poll())
    await asyncio.sleep(0)
    start = time.monotonic()
    await main.stop_applying()
    await poll
    assert ran[0] - start < .10, "the Stop endpoint blocked other requests"


def test_stopped_run_waits_for_its_profile_copy_thread(monkeypatch):
    """R03: a run isn't over (and doesn't free the browser) while its profile-copy thread still runs."""
    import main
    monkeypatch.setattr(main, '_get_metrics_store', lambda: None)
    monkeypatch.setattr(main, '_kill_browser_processes', lambda: None)
    monkeypatch.setattr(main, '_wait_for_browsers_to_close', lambda _: None)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def profile_copy():
        entered.set()
        release.wait(5)
        finished.set()

    async def a_run():
        await asyncio.to_thread(profile_copy)
    status = {'running': False, 'log': []}
    thread = main._run_async_in_thread(a_run, status)
    try:
        assert entered.wait(3)
        stopper = threading.Thread(target=main._force_stop, args=(status,))
        stopper.start()
        thread.join(1)
        assert status['running'] or finished.is_set(), 'Run reported finished while its profile copy still ran'
    finally:
        release.set()
        assert finished.wait(3)
        thread.join(5)
        stopper.join(10)
    assert not thread.is_alive() and not status['running']


# ── The user's edits (R07) ──────────────────────────────────────────────────

def test_a_cleared_field_counts_as_the_users_edit():
    from core.form_snapshot import changed_fields, describe_changes
    changes = changed_fields({'portfolio': ('Portfolio URL', 'https://example.test/old')},
                             {'portfolio': ('Portfolio URL', '   ')})
    assert changes == [('Portfolio URL', 'https://example.test/old', '')]
    assert describe_changes(changes) == '- "Portfolio URL": leave it empty (the user cleared it)'
    # Whitespace-only differences and fields that stay empty aren't edits.
    assert changed_fields({'a': ('A', 'x')}, {'a': ('A', 'x ')}) == []
    assert changed_fields({'a': ('A', '')}, {'a': ('A', ' ')}) == []


def test_a_cleared_field_is_pinned_empty_and_not_saved_as_an_answer(monkeypatch):
    from cli import apply_jobs
    from tests.backend.cli.test_answer_quality import _FakeAgent
    saved = []
    store = SimpleNamespace(qa_user_answers=lambda: {}, qa_set_answer=lambda *a, **kw: saved.append(a))
    monkeypatch.setattr(apply_jobs, 'get_memory_store', lambda: store)
    agent = _FakeAgent()
    apply_jobs.keep_user_edits(agent, [('What is your portfolio URL?', 'https://example.test/old', '')])
    assert '"What is your portfolio URL?": leave it empty (the user cleared it)' in agent.system_text()
    assert saved == []


# ── Saved passwords (R08) ───────────────────────────────────────────────────

def test_a_damaged_password_file_is_never_replaced(data_dir):
    from core import secret_store
    vault = data_dir / 'site_credentials.json'
    damaged = '{"https://careers.example.test": {"password": "incomplete'
    vault.write_text(damaged, encoding='utf-8')
    with pytest.raises(secret_store.VaultUnreadable):
        secret_store.site_password('https://another.example.test')
    assert secret_store.site_password('https://another.example.test', create=False) == ''
    assert vault.read_text(encoding='utf-8') == damaged


def test_a_missing_password_file_still_starts_a_new_one(data_dir):
    from core import secret_store
    password = secret_store.site_password('https://careers.example.test')
    assert password and (data_dir / 'site_credentials.json').exists()
    assert secret_store.site_password('https://careers.example.test') == password


# ── Tailoring (R09, R10, R11, R18) ──────────────────────────────────────────

@pytest.fixture
def tailoring(data_dir, tmp_path, monkeypatch):
    from resume import tailor
    from core import config
    source = tmp_path / 'synthetic.pdf'

    def write_source(skill='Original Python SQL'):
        with fitz.open() as doc:
            page = doc.new_page()
            page.insert_text((40, 50), 'SKILLS')
            page.insert_text((40, 90), skill)
            doc.save(source)
    write_source()
    monkeypatch.setattr(tailor, 'load_settings', lambda: {'resume_path': str(source)})
    monkeypatch.setattr(tailor, 'load_llm_settings', lambda: {'provider': 'gemini'})
    monkeypatch.setattr(tailor, 'create_llm', lambda _: object())
    monkeypatch.setattr(config, 'load_profile', lambda: {'name': 'Synthetic Person', 'current_role': 'Data Analyst'})
    prompts = []

    async def invoke(llm, settings, messages):
        prompts.append(messages[0].content)
        return SimpleNamespace(completion='{"0": "Python SQL", "1": "Data Analyst | Python, SQL"}')
    monkeypatch.setattr(tailor, 'ainvoke_with_fallback', invoke)
    return tailor, source, prompts, write_source


async def test_refine_keeps_the_instruction_with_a_long_posting(tailoring, monkeypatch):
    """R10: the instruction used to be appended to the description and cut off with it."""
    import main
    from core import shared_config
    _, _, prompts, _ = tailoring
    url = 'https://jobs.example.test/refine'
    monkeypatch.setattr(shared_config, 'read_jobs', lambda: {url: {'description': 'Synthetic role. ' * 220}})
    monkeypatch.setattr(shared_config, 'update_job', lambda *a, **kw: None)
    result = await main.refine_tailored_resume({'job_url': url, 'instruction': 'Keep SQL first; do not remove Python.'})
    assert result['success']
    assert len(prompts) == 1 and 'Keep SQL first; do not remove Python.' in prompts[0]


async def test_the_job_title_option_is_tailored(tailoring):
    """R18: Job title alone used to fail with "No tailorable sections"."""
    tailor, _, prompts, _ = tailoring
    result = await tailor.tailor_resume('https://jobs.example.test/title', 'Synthetic role', {'title': True})
    assert len(prompts) == 1
    assert "never claim a job title" in prompts[0] and 'current role: Data Analyst' in prompts[0]
    assert result['sections_tailored'] == 1 and '## Headline\n\nPython SQL' in result['content']
    with fitz.open(result['path']) as doc:
        assert 'Python SQL' in ''.join(page.get_text() for page in doc)


async def test_a_resume_replaced_during_tailoring_is_not_certified(tailoring, monkeypatch):
    """R09: the fingerprint is of the PDF that was read, not of whatever is there after the AI wait."""
    tailor, source, _, write_source = tailoring

    async def generate(*args, **kwargs):
        write_source('New Rust skills')
        return {0: 'Old Python'}
    monkeypatch.setattr(tailor, '_generate_tailored_text', generate)
    url = 'https://jobs.example.test/source-race'
    await tailor.tailor_resume(url, 'Synthetic role', {'skills': True})
    path, _reason = tailor.usable_tailored_resume(url, str(source))
    assert path is None


def test_the_pdf_keeps_non_latin_names(data_dir):
    """R11: base-14 Helvetica printed these as dots."""
    from resume import tailor
    output = data_dir / 'synthetic-unicode.pdf'
    tailor._generate_fresh_pdf([], {}, 'Synthetic Łukasz 王', '', '', output)
    with fitz.open(output) as doc:
        text = ''.join(page.get_text() for page in doc)
    assert 'ŁUKASZ 王' in text


# ── Q&A merge (R12) ─────────────────────────────────────────────────────────

def test_merging_into_an_unanswered_question_keeps_the_answer(tmp_path):
    from memory.store import MemoryStore
    store = MemoryStore(tmp_path / 'synthetic.db')
    try:
        source = store.qa_add('Synthetic question one', 'Synthetic answer')
        target = store.qa_add('Synthetic question two', '')
        assert store.qa_merge(source['id'], target['id'])
        rows = store.qa_list()
        assert len(rows) == 1 and rows[0]['answer'] == 'Synthetic answer'
    finally:
        store.close()


def test_merging_still_refuses_different_answers(tmp_path):
    from memory.store import MemoryStore
    store = MemoryStore(tmp_path / 'synthetic.db')
    try:
        a = store.qa_add('Synthetic question one', 'Yes')
        b = store.qa_add('Synthetic question two', 'No')
        assert not store.qa_merge(a['id'], b['id'])
        assert {r['answer'] for r in store.qa_list()} == {'Yes', 'No'}
    finally:
        store.close()


# ── Metrics (R16) ───────────────────────────────────────────────────────────

def test_an_unconfirmed_application_is_not_a_success_metric(run, monkeypatch):  # noqa: F811
    from cli import apply_jobs
    rows = []
    monkeypatch.setattr(apply_jobs, 'MetricsStore', lambda: SimpleNamespace(record_run=lambda **kw: rows.append(kw)))
    run.browser.page_text = 'Please review the form.'
    status, _, _ = run(history=FakeHistory(success=True, steps=3))
    assert status == 'needs_you'
    assert rows[0]['success'] is False
