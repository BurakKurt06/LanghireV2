"""The @@MARKER payload parser: strict JSON first, then the Python-style dicts models also write."""
from types import SimpleNamespace

import pytest

from core.markers import parse_marker


@pytest.mark.parametrize("payload,expected", [
    ('{"question": "Degree", "answer": "BS"}', {"question": "Degree", "answer": "BS"}),
    ("{'question': 'Discipline', 'answer': 'Computer Science', 'type': 'text'}",
     {"question": "Discipline", "answer": "Computer Science", "type": "text"}),
    ("{'title': 'Intern', 'easy_apply': true, 'salary': null}", {"title": "Intern", "easy_apply": True, "salary": None}),
    ("{'answer': \"Master's\"}", {"answer": "Master's"}),
    ("{'note': 'this is true'}", {"note": "this is true"}),  # a quoted "true" stays text
])
def test_parses_json_and_python_style(payload, expected):
    assert parse_marker(payload) == expected


@pytest.mark.parametrize("payload", ["{not a dict}", "[1, 2]", "{'a': __import__('os')}", "", "{'unterminated: 1}"])
def test_rejects_non_dicts_and_code(payload):
    assert parse_marker(payload) is None


def _history(*memories):
    return SimpleNamespace(history=[SimpleNamespace(model_output=SimpleNamespace(memory=m)) for m in memories])


def test_extract_from_history_keeps_single_quoted_questions():
    """flash-lite wrote these in a real run; they used to be dropped."""
    from core.shared_config import extract_from_history
    jobs, questions = extract_from_history(_history(
        "@@QUESTION: {'question': 'Discipline', 'answer': 'Computer Science', 'type': 'text'}",
        '@@JOB_APPLIED: {"title": "Intern", "company": "Acme", "location": "Boston"}',
    ))
    assert questions == {"Discipline": "Computer Science"}
    assert jobs == ["Intern at Acme - Boston"]


def test_learning_markers_accept_python_style():
    from memory.extractors import extract_learnings_from_markers
    out = extract_learnings_from_markers(_history(
        "@@LEARNING: {'domain': 'greenhouse.io', 'category': 'navigation', 'insight': 'Apply opens in the same tab'}"
    ))
    assert [l["content"] for l in out] == ["Apply opens in the same tab"]
