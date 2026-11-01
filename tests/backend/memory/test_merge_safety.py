"""Q&A merges and de-duplication never erase answers or join questions that mean different things."""
import pytest

from memory.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "m.db")


def _q(store, question, answer=""):
    store.qa_add(question=question, answer=answer)
    return next(q["id"] for q in store.qa_list() if q["question"] == question)


def test_a_question_cant_be_merged_into_itself(store):
    a = _q(store, "Are you authorized to work in the US?", "Yes")
    assert store.qa_merge(a, a) is False
    assert a in {q["id"] for q in store.qa_list()}  # still there (it used to disappear)


def test_merging_into_a_missing_question_is_refused(store):
    a = _q(store, "Are you authorized to work in the US?", "Yes")
    assert store.qa_merge(a, 999999) is False
    assert store.qa_merge_problem(a, 999999) == "That question no longer exists."


def test_merges_cant_form_a_loop_or_chain_onto_a_merged_question(store):
    a, b, c = (_q(store, f"Question {n} about relocation?") for n in "abc")
    assert store.qa_merge(a, b) is True
    assert store.qa_merge(b, a) is False          # a now points at b: b -> a would loop
    assert "itself merged" in store.qa_merge_problem(c, a)


def test_different_answers_are_never_combined_without_asking(store):
    a = _q(store, "Are you willing to relocate?", "Yes")
    b = _q(store, "Willing to relocate for this role?", "No")
    assert store.qa_merge(b, a) is False and "different answers" in store.qa_merge_problem(b, a)
    assert store.qa_merge(b, a, allow_conflict=True) is True   # the user's explicit choice


@pytest.mark.parametrize("a, b, same", [
    ("are you legally authorized to work in canada", "are you legally authorized to work in australia", False),
    ("are you authorized to work in the united states", "are you not authorized to work in the united states", False),
    ("always click the final submit button", "never click the final submit button", False),
    ("are you authorized to work in the united states", "are you legally authorized to work in the united states", True),
])
def test_meaning_not_just_word_overlap(a, b, same):
    assert MemoryStore.same_meaning(a, b, 0.5) is same


def test_auto_squash_keeps_scoped_and_conflicting_questions_apart(store):
    _q(store, "Are you legally authorized to work in Canada?", "Yes")
    _q(store, "Are you legally authorized to work in Australia?", "No")
    assert store.qa_auto_squash() == 0
    assert len(store.qa_list()) == 2


def test_opposite_tips_stay_separate(store):
    assert store.add("Always click the final submit button on the review page", "a.example", "form_strategy") is True
    assert store.add("Never click the final submit button on the review page", "a.example", "form_strategy") is True
    assert len(store.search(website_domain="a.example", success_only=False)) == 2


def test_a_narrower_question_keeps_its_own_answer(tmp_path):
    """Codex's case: '... using Python' added on one side is a different question."""
    from memory.store import MemoryStore
    store = MemoryStore(tmp_path / "qa.db")
    general = "Do you have at least five years of professional experience managing complex software development projects?"
    specific = general.rstrip("?") + " using Python?"
    store.qa_set_answer(general, "Yes", source="user")
    store.qa_set_answer(specific, "No", source="user")
    answers = {q["question"]: q["answer"] for q in store.qa_list()}
    assert answers == {general: "Yes", specific: "No"}
    assert store.qa_auto_squash() == 0


def test_filler_words_alone_still_match(tmp_path):
    from memory.store import MemoryStore
    assert MemoryStore.same_meaning("are you legally authorized to work in the us",
                                    "are you authorized to work in the us", 0.7)
