"""The legacy tailored-apply CLI never adds a skill the resume doesn't have, nor drops one.

It used to overwrite a block containing 'Collaboration' and 'SQL' with a hardcoded
list (someone else's skills), so a real resume lost Python and gained 'Conflict
Resolution' and 'Microsoft Office Suite'. These build real PDFs and read them back.
"""
import fitz
import pytest

from cli import apply_jobs_tailored as legacy


def _pdf(path, blocks):
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    for y, text in blocks:
        page.insert_text((72, y), text, fontsize=11)
    doc.save(str(path))
    doc.close()
    return path


def _text(path):
    with fitz.open(path) as doc:
        return "\n".join(page.get_text() for page in doc)


@pytest.fixture
def resume(tmp_path, monkeypatch):
    path = _pdf(tmp_path / "cv.pdf", [(60, "Ada Lovelace"), (120, "Skills"),
                                      (150, "Collaboration · SQL · Python · TypeScript · React")])
    monkeypatch.setattr(legacy, "RESUME_PATH", str(path))
    monkeypatch.setattr(legacy, "RESUMES_DIR", tmp_path / "out")
    monkeypatch.setattr(legacy.config, "CANDIDATE_PROFILE", tmp_path / "no-profile.json")
    return path


def test_the_skills_come_from_the_resume_itself(resume):
    assert legacy.resume_skills(str(resume)) == ["Collaboration", "SQL", "Python", "TypeScript", "React"]
    picked = legacy.extract_skills_from_description("We need Python and React; Tableau is a plus.", "Engineer")
    assert picked == ["Python", "React"]


def test_the_tailored_resume_reorders_and_never_adds_or_drops(resume):
    out = legacy.customize_resume({"url": "https://acme.example/1", "title": "Engineer", "company": "Acme"},
                                  ["Python", "React", "Tableau"])
    text = _text(out)
    for skill in ("Collaboration", "SQL", "Python", "TypeScript", "React"):
        assert skill in text
    assert "Tableau" not in text and "Conflict Resolution" not in text and "Microsoft Office" not in text
    assert text.index("Python") < text.index("Collaboration")   # the job's skills first


def test_a_resume_without_a_skills_section_is_left_as_it_is(tmp_path, monkeypatch):
    """Codex's case: skills in a sentence, no heading: nothing to reorder, nothing inserted."""
    path = _pdf(tmp_path / "cv.pdf", [(72, "Collaboration and SQL, Python, TypeScript, React, Excel and data analysis")])
    monkeypatch.setattr(legacy, "RESUME_PATH", str(path))
    monkeypatch.setattr(legacy, "RESUMES_DIR", tmp_path / "out")
    monkeypatch.setattr(legacy.config, "CANDIDATE_PROFILE", tmp_path / "no-profile.json")
    out = legacy.customize_resume({"url": "https://acme.example/1", "title": "Intern", "company": "Acme"}, [])
    text = _text(out)
    assert "Microsoft Office Suite" not in text and "Conflict Resolution" not in text
    assert "Python, TypeScript, React" in text
