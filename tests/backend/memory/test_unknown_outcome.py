"""A run that ends without the agent saying whether it succeeded (stopped, timed out, max steps).

browser-use's AgentHistoryList.is_successful() is None then. Saving that run's learnings
crashed the application with "int() argument must be a ... real number, not 'NoneType'"
(four real jobs ended as "Submission outcome unknown" or failed because of it).
"""
from memory.extractors import store_learnings
from memory.store import MemoryStore


def test_learnings_of_a_run_with_no_verdict_are_saved_as_not_successful(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    learnings = [{"content": "The form has four steps: Personal, Resume, Questions, Review", "category": "form_strategy"}]
    assert store_learnings(store, learnings, "https://jobs.example.com/apply/1", None) == (1, 0)
    saved = store.search(website_domain="jobs.example.com", success_only=False)
    assert [m["success"] for m in saved] == [0]


def test_add_accepts_a_missing_verdict(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    assert store.add("Click the visible label, not the hidden checkbox", "jobs.example.com", "element_interaction",
                     success=None) is True
