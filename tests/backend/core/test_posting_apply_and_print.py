"""A posting page's own "Apply" button opens the application; Print / PDF controls are never clicked.

Seen in the user's runs (2026-11): on tal.net the posting's Apply is <input type=submit value="Apply"> in
a form of hidden values. It counted as the application's final Submit, so every job there was marked
"may have been sent" from its first click, and Trial could not start one at all. "Print Application"
opened the print window, which froze the page until the job timed out.
"""
import pytest

from core import submit_guard
from core.submit_guard import Control, SubmitGuard


def posting_apply(**changes):
    fields = dict(text="Apply", tag="input", type="submit", in_form=True, form_fields=0)
    fields.update(changes)
    return Control(**fields)


def test_the_postings_apply_button_is_only_a_possible_submit_in_full_auto():
    decision = SubmitGuard(mode="auto").decide("click", posting_apply())
    assert decision.allowed and decision.submit and decision.tentative      # the mark comes off when the form goes on


def test_control_without_this_fix_was_a_firm_submit():
    """The control: the same button when the page says nothing about its form's fields stays final."""
    decision = SubmitGuard(mode="auto").decide("click", posting_apply(form_fields=None))
    assert decision.allowed and decision.submit and not decision.tentative


def test_trial_may_press_the_postings_apply_button():
    decision = SubmitGuard(mode="trial", live_steps=True).decide("click", posting_apply())
    assert decision.allowed and not decision.submit


@pytest.mark.parametrize("label", ["Apply", "Apply now", "Apply for this job", "Jetzt bewerben"])
def test_other_wordings_of_apply_open_the_application_too(label):
    assert SubmitGuard(mode="trial", live_steps=True).decide("click", posting_apply(text=label)).allowed


@pytest.mark.parametrize("mode,live", [("auto", False), ("trial", True)])
def test_apply_in_a_form_with_fields_is_still_the_final_submit(mode, live):
    """A one-page application: the form has fields, so its Apply sends it."""
    decision = SubmitGuard(mode=mode, live_steps=live).decide("click", posting_apply(form_fields=3))
    assert decision.submit and not decision.tentative
    assert decision.allowed == (mode == "auto")


@pytest.mark.parametrize("earlier", [
    ("input", Control(tag="input", type="text", in_form=True)),                              # a field was filled in
    ("click", Control(text="Next", tag="button", type="submit", in_form=True, form_fields=4)),  # a page was sent on
    ("click", Control(text="Sign In", tag="button", type="submit", in_form=True)),             # signed in
    ("select_dropdown", Control(tag="option", in_form=False)),
])
def test_apply_after_form_work_in_this_run_is_the_final_submit(earlier):
    """A review page shows the answers as text: its Apply has no fields either, but the agent got there
    by filling in, signing in or pressing Next, so it is the final submit and Trial never presses it."""
    guard = SubmitGuard(mode="trial", live_steps=True)
    guard.decide(*earlier, text="x")
    decision = guard.decide("click", posting_apply())
    assert decision.submit and not decision.allowed


@pytest.mark.parametrize("changes", [dict(final_context=True), dict(application_review=True),
                                     dict(text="Submit application"), dict(text="Apply", text_alternative="Submit")])
def test_a_final_heading_or_a_submit_label_keeps_it_final(changes):
    decision = SubmitGuard(mode="trial", live_steps=True).decide("click", posting_apply(**changes))
    assert decision.submit and not decision.allowed


def test_two_posting_apply_buttons_in_a_row_both_open():
    """An employer's page, then the hiring site's own posting: neither is form work."""
    guard = SubmitGuard(mode="auto")
    assert guard.decide("click", posting_apply()).tentative
    assert guard.decide("click", posting_apply(text="Apply now")).tentative
    assert guard.form_steps == 0


# ── Print / PDF ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mode,live", [("auto", False), ("trial", True), ("training", False)])
@pytest.mark.parametrize("label", ["Print Application", "Print", "Printable version", "Download PDF",
                                   "Save as PDF", "Download application", "View application PDF copy"])
def test_print_and_pdf_controls_are_refused_in_every_mode(mode, live, label):
    decision = SubmitGuard(mode=mode, live_steps=live).decide("click", Control(text=label, tag="a", in_form=False))
    assert not decision.allowed and not decision.submit
    assert "printing or downloading the application isn't part of applying" in decision.reason


@pytest.mark.parametrize("control", [
    Control(text="Print advertisement", tag="li", role="option"),            # an answer to "How did you hear"
    Control(text="Print media", tag="input", type="radio", in_form=True),
    Control(text="Upload resume (PDF)", tag="button", type="button", in_form=True),
    Control(text="Blueprint Careers", tag="a", in_form=False),
    Control(text="Fingerprint consent", tag="a", in_form=False),
    Control(text="Save and Continue", tag="button", type="button", in_form=False),
    Control(text="Save application", tag="button", type="button", in_form=False),      # saves a draft
    Control(text="View application", tag="a", in_form=False),
    Control(text="Download resume template", tag="a", in_form=False),
])
def test_answers_and_ordinary_controls_that_mention_print_are_not_refused(control):
    assert not submit_guard.is_print_control(control)
    assert SubmitGuard(mode="auto").decide("click", control).allowed


@pytest.mark.parametrize("keys", ["Enter", "Space", "Return"])
def test_pressing_enter_or_space_on_a_print_link_is_refused_too(keys):
    """The user, 2026-11-17: "dont print". Not by click, not by a key on the focused link."""
    decision = SubmitGuard(mode="auto").decide("send_keys", Control(text="Print Application", tag="a", in_form=False),
                                               text=keys)
    assert not decision.allowed and "printing or downloading" in decision.reason


def test_control_tab_on_a_print_link_just_moves_on():
    assert SubmitGuard(mode="auto").decide("send_keys", Control(text="Print Application", tag="a"), text="Tab").allowed


@pytest.mark.parametrize("url,printing", [
    ("https://acme.tal.net/vx/lang-en-GB/candidate/eform/123456/print", True),
    ("https://acme.example/application/print?id=4", True),
    ("https://acme.example/apply/42/printable", True),
    ("https://acme.example/apply/42/print-application", True),
    ("https://acme.example/apply/42?print=1", True),
    ("https://acme.example/careers/printers-and-imaging-intern", False),
    ("https://acme.example/jobs/blueprint-analyst", False),
    ("https://acme.example/jobs/posting.pdf", False),            # a posting can be a PDF: it is opened
    ("https://acme.example/apply/42/declaration", False),
    ("", False),
])
def test_which_addresses_are_a_print_copy_of_the_application(url, printing):
    assert submit_guard.is_print_address(url) is printing


def test_a_refused_print_click_is_not_a_possible_submit():
    guard = SubmitGuard(mode="auto")
    guard.decide("click", Control(text="Print Application", tag="a", in_form=False))
    assert guard.attempts == 0 and guard.form_steps == 0
