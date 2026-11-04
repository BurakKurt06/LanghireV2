"""Submission action classification, outside the model.

The production Trial wrapper never accepts a final-submit approval. Normal Trial
allows native intermediate forms and site requests like Full Auto, stopping the
agent's final-submit action. It cannot prevent a site submitting during Next or
its own scripts. Explicit strict protocol diagnostics retain their older boundary.
Standalone legacy approval
helpers remain for compatibility and cannot grant network authority.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# Text of a control that sends the application.
_SUBMIT = re.compile(
    r"^\s*submit\b"                                            # Submit, Submit Application
    r"|^\s*send\s*$"                                           # a bare Send
    r"|\b(submit|send|complete|finish|finalize)\s+(my\s+|your\s+|the\s+)?application\b"
    r"|^\s*(complete|finish|finalize)\s*$"                      # a bare Finish / Complete
    r"|\bconfirm\s*(and|&)\s*(submit|apply|send)\b"
    r"|\bsubmit\s*(and|&)\s*apply\b"
    r"|\b(?:save|finish)\s*(?:and|&)\s*submit\b"
    r"|^\s*(?:done|confirm)\s*$"
    r"|\bbaşvuruyu\s+gönder\b|\bwyślij\s+aplikację\b|応募を送信"
    # The same final controls on international employers' sites (German, French, Spanish, Portuguese,
    # Italian, Dutch): "Bewerbung absenden", "Envoyer ma candidature", "Enviar solicitud", ...
    r"|\bbewerbung\s+(jetzt\s+)?(absenden|abschicken|senden|einreichen|übermitteln|uebermitteln)\b"
    r"|^\s*(absenden|abschicken|einreichen)\s*$"
    r"|\b(envoyer|soumettre|valider)\s+(ma\s+|la\s+|votre\s+|mon\s+)?(candidature|dossier)\b"
    r"|^\s*(envoyer|soumettre)\s*$"
    r"|\b(enviar|presentar|submeter)\s+(mi\s+|la\s+|su\s+|a\s+|minha\s+|sua\s+)?(solicitud|candidatura|postulaci[oó]n|inscri[cç][aã]o)\b"
    r"|^\s*enviar\s*$"
    r"|\binvia(re)?\s+(la\s+)?(candidatura|domanda)\b"
    r"|^\s*invia\s*$"
    r"|\bsollicitatie\s+(versturen|verzenden|indienen)\b"
    r"|^\s*(versturen|verzenden|indienen)\s*$",
    re.I,
)
# A form control saying "Apply" in another language (inside a form it sends the application).
_APPLY_WORD = (r"apply(?:\s+now|\s+(?:for\s+)?(?:this\s+)?(?:job|position))?"
               r"|(?:jetzt\s+)?bewerben|postuler|postular(?:me)?|candidatar(?:-se)?|candidati|solliciteren|solliciteer")
# Submit buttons of other small forms on the way (a verification code, a search): not the application,
# unless the label also says application / apply ("Submit application verification" still counts).
_NOT_APPLICATION = re.compile(r"\b(code|otp|verif\w*|search|filter|feedback|question|comment|rating)\b", re.I)

RUN_MODES = ("auto", "trial", "training")
# The fingerprint of a form the app couldn't read (or of a control in a frame it couldn't include in the
# review): no approval ever matches it.
UNREADABLE_FORM = "unreadable"


def is_submit_label(text: str) -> bool:
    text = text or ""
    if not _SUBMIT.search(text):
        return False
    return not _NOT_APPLICATION.search(text) or bool(re.search(r"appl", text, re.I))


@dataclass
class Control:
    """What an action would act on: a clickable element, or the focused element for a key press.

    `type` is the effective type (a <button> without one is "submit"); `in_form` is None
    when it couldn't be told, and is then treated as True.
    """
    text: str = ""
    tag: str = ""
    type: str = ""
    role: str = ""
    combo: bool = False       # a combobox / searchable list (Enter picks an option)
    in_form: bool | None = None
    multiline: bool = False   # textarea / contenteditable (Enter adds a line)
    document_url: str = ""
    hidden_tree: bool = False  # inside a closed shadow root: the form scan (and so a review) can't see it
    final_context: bool = False  # readable page heading identifies the final application/review step
    application_review: bool = False  # generic review heading identifies Apply, without reclassifying Next
    submitter_text: str = ""  # native default submitter, for Enter/newline in a form field
    text_alternative: str = ""  # retain visible text when an accessible name overrides it
    submitter_alternative: str = ""
    text_pseudo: str = ""  # rendered CSS text remains evidence even when ARIA/title says Next
    submitter_pseudo: str = ""

    def __post_init__(self):
        self.tag, self.type = (self.tag or "").lower(), (self.type or "").lower()
        if self.tag == "button" and self.type not in ("button", "reset"):
            self.type = "submit"  # the HTML default
        if self.tag == "input":
            self.multiline = False  # contenteditable does not change a native input's Enter behavior

    @property
    def labels(self) -> tuple[str, ...]:
        # A page's ARIA name must not hide final-submit text we could already read.
        return tuple(dict.fromkeys(t for t in (self.text, self.text_alternative, self.text_pseudo) if t)) or ("",)

    @property
    def native_submit(self) -> bool:
        """Activating it submits its form natively (whatever its label says)."""
        if self.in_form is False:
            return False
        return (self.tag == "button" and self.type == "submit") or (self.tag == "input" and self.type in ("submit", "image"))

    @property
    def is_final_submit(self) -> bool:
        """Activating it could send the application (a possible submit)."""
        if any(is_submit_label(text) for text in self.labels):
            return True
        if (self.in_form is True or self.final_context or self.application_review) and any(
            re.fullmatch(r"\s*(?:" + _APPLY_WORD + r")[.!\s]*", text, re.I) for text in self.labels
        ):
            return True
        return self.native_submit

    @property
    def activatable(self) -> bool:
        """Enter or Space on it acts like a click."""
        return (self.tag in ("button", "a", "summary") or self.role in ("button", "link", "menuitem", "tab")
                or (self.tag == "input" and self.type in ("submit", "image", "button", "reset")))

    @property
    def enter_submits(self) -> bool:
        """Enter here would submit: on a submit control, or in a single-line field of a form."""
        if self.tag == "input" and self.type not in ("submit", "image", "button", "reset"):
            # ARIA roles/autocomplete and contenteditable describe the page; none
            # prevents a native field's implicit submit. Check native behavior first.
            return (self.in_form is not False
                    and self.type not in ("checkbox", "radio", "file", "range", "color"))
        if self.activatable:
            return self.is_final_submit
        return False


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    submit: bool = False      # the action is a final-submit attempt


@dataclass
class SubmitGuard:
    mode: str = "auto"
    job_url: str = ""
    on_submit: Callable[[], Awaitable[None] | None] | None = None
    _approval: dict | None = field(default=None, repr=False)
    attempts: int = 0
    live_steps: bool = False  # enabled only by the canonical network-guarded application runner
    strict_network: bool = False  # explicit protocol diagnostics, not normal Trial

    def __post_init__(self):
        if self.mode not in RUN_MODES:
            self.mode = "auto"

    # ── approvals (trial runs) ──────────────────────────────────────────────
    def approve(self, page_url: str, fingerprint: str) -> None:
        """The user approved submitting this page in this state (single use)."""
        self._approval = {"page_url": _page_key(page_url), "fingerprint": fingerprint, "used": False}

    def revoke(self) -> None:
        self._approval = None

    def _approved(self, page_url: str, fingerprint: str) -> tuple[bool, str]:
        a = self._approval
        if fingerprint == UNREADABLE_FORM:
            return False, ("Trial run: the app can't read this form (or the frame from another site that this "
                           "button is in), so submitting it can't be approved here. Call ask_user_for_help so "
                           "the user can submit it themselves.")
        if a is None:
            return False, ("Trial run: submitting needs the user's approval. Call request_submit_review "
                           "first; click Submit only after it says approved.")
        if a["used"]:
            return False, ("Trial run: that approval was already used. If the application wasn't sent, "
                           "call request_submit_review again.")
        if a["page_url"] != _page_key(page_url) or a["fingerprint"] != fingerprint:
            return False, ("Trial run: the page or its answers changed since the user approved it. Call "
                           "request_submit_review again so the user can check the current state.")
        return True, ""

    # ── the check every action passes ───────────────────────────────────────
    def decide(self, action: str, control: Control | None, page_url: str = "", fingerprint: str = "",
               text: str = "") -> Decision:
        """Whether `action` may run. `control` is the element clicked / focused; `text` is typed text."""
        # Steps on the way (an account, a code, an upload) are allowed like Next. A label that reads as a
        # final submit and names the resume or documents ("Submit resume", "Submit documents") is final:
        # one-page applications use it as their only Submit, often in a form with a file input, so neither
        # the label nor the form tells it apart from an upload step. Attaching the file needs no button,
        # and upload buttons ("Upload resume") stay allowed. (Codex review R01 and its re-check.)
        labels = control.labels if control is not None else ()

        def intermediate(label: str) -> bool:
            return bool(self.mode == "trial" and self.live_steps and not self.strict_network
                and control is not None and not control.final_context
                and not re.search(r"\bappl(?:y|ication)\b", label, re.I)
                and re.search(r"\b(?:profile|account|password|verification|code|otp|resume|document|upload)\b", label, re.I)
                and not (is_submit_label(label) and re.search(r"\b(?:resume|cv|documents?)\b", label, re.I)))

        def apply_label(label: str) -> bool:
            return bool(re.fullmatch(r"\s*(?:" + _APPLY_WORD + r")[.!\s]*", label, re.I))

        label_final = any(is_submit_label(label) and not intermediate(label) for label in labels)
        final_apply = bool((self.mode == "auto" or (self.mode == "trial" and self.live_steps and not self.strict_network))
            and control is not None and (control.in_form is True or control.final_context or control.application_review)
            and any(apply_label(label) for label in labels))
        context_final = bool(control is not None and control.final_context and (
            control.native_submit or (control.activatable and not control.combo and control.role != "combobox"
                and any(re.fullmatch(r"\s*(?:next|continue)[.!\s]*", label, re.I)
                        or not re.search(r"\w", label) for label in labels))))
        # Enter in an ordinary text field activates its form's default button. Inspect that button,
        # rather than exempting the action because the field's own label does not say Submit.
        implicit_final = bool(control is not None and (control.final_context or
            any((is_submit_label(label) and not re.search(
                    r"\b(?:profile|account|password|verification|code|otp)\b", label, re.I))
                or (self.mode == "trial" and self.live_steps and not self.strict_network
                    and control.tag == "input" and not control.activatable and control.enter_submits
                    and apply_label(label))
                for label in (control.submitter_text, control.submitter_alternative, control.submitter_pseudo))))
        label_final = label_final or context_final
        final_control = bool(control is not None and (control.native_submit or label_final or final_apply))
        if action == "evaluate":
            if self.mode == "trial" and self.live_steps and self.strict_network:
                # Only the canonical runner enables this, with a healthy broker
                # independently refusing terminal/unknown requests. A page can
                # already run the same script from an ordinary click handler.
                return Decision(True)
            if self.mode == "auto":
                # An arbitrary model script can call submit/requestSubmit/fetch.
                # Persist its possible effect before it runs, just like a native submit.
                return Decision(True, submit=True)
            if self.live_steps:
                return Decision(False, "Trial run: arbitrary agent scripts can press the final submit control. "
                    "Use indexed clicks, input and dropdown actions instead; no approval is needed.")
            return Decision(False, f"{self._mode_name()}: running page scripts is disabled (it can submit the "
                                   "form). Use clicks and typing, or call ask_user_for_help.")
        if control is None and (
            action in ("click", "input", "select_dropdown")
            or (action == "send_keys" and (_has_enter(text) or _has_space(text)))
        ):
            if self.mode == "trial" and self.live_steps and self.strict_network:
                return Decision(True)  # the broker remains authoritative
            if self.mode == "auto":
                # Unknown controls can submit. Preserve the attempt before running
                # them so a crash cannot make the worker retry a sent application.
                return Decision(True, submit=True)
            if self.live_steps:
                return Decision(False, "Trial run: refresh the browser state and retry using a readable indexed "
                    "control so the final submit button can be checked. No approval is needed.")
            return Decision(False, f"{self._mode_name()}: the control could not be read, so this action "
                                   "cannot be checked safely. Ask the user for help.")
        submit = False
        if action in ("click", "select_dropdown") and control is not None:
            submit = final_control  # select_dropdown clicks the matching item
        elif action == "send_keys":
            if self.mode != "auto" and not self.strict_network and re.search(r"\balt\b", text or "", re.I):
                return Decision(False, f"{self._mode_name()}: Alt shortcuts can activate another form's access-key "
                                       "button. Click the intended control so it can be checked.")
            if self.mode != "auto" and not self.strict_network and _moves_focus(text) and (_has_enter(text) or _has_space(text)):
                # The focused element is read before the keys run: after a Tab it's another one.
                return Decision(False, f"{self._mode_name()}: press Tab and Enter/Space in separate steps, so the "
                                       "app can check what Enter or Space would press.")
            if control is not None:
                submit = ((_has_enter(text) and (control.enter_submits or (final_apply and control.activatable)))
                          or (_has_space(text) and control.activatable and final_control))
        elif action == "input" and control is not None:
            if control.activatable and re.search(r"[ \r\n]", text or ""):
                # Input focuses the element and types key by key: a space or line break "typed into" a
                # button presses it.
                submit = final_control
            elif re.search(r"[\r\n]", text or "") and not control.multiline:
                submit = control.in_form
                if self.mode != "auto" and not self.live_steps:
                    return Decision(False, f"{self._mode_name()}: type text without line breaks (a line break "
                                           "in this field can submit the form).", submit=submit)
        if not submit:
            return Decision(True)
        native_only = (control is not None and not label_final and not final_apply
                       and not (implicit_final and action in ("send_keys", "input")))
        if self.mode == "trial" and self.live_steps and native_only:
            # Native form submission also powers login, saves and Next. Normal
            # Trial deliberately permits those requests, including site scripts;
            # only an agent action identified as the final button is stopped.
            return Decision(True)
        if self.mode == "training":
            if native_only:
                return Decision(False, "Training run: this button sends the form to the employer (it's the form's submit "
                                       "button, whatever it says), so practice stops here. Call done with success=true "
                                       "and a summary of what you filled in.", submit=True)
            return Decision(False, "Training run: the final submit is blocked by the app. Don't submit: call "
                                   "done with success=true and a summary of what you filled in.", submit=True)
        if self.mode == "trial":
            ok, why = self._approved(page_url, fingerprint)
            if not ok:
                if native_only:
                    why = ("Trial run: this button sends the form to the employer (it's the form's submit button, even "
                           "if it isn't the last page). " + why.split(": ", 1)[-1])
                return Decision(False, why, submit=True)
            self._approval["used"] = True
        return Decision(True, submit=True)

    def _mode_name(self) -> str:
        return "Training run" if self.mode == "training" else "Trial run"


def _has_enter(keys: str) -> bool:
    return bool(re.search(r"\b(enter|return)\b|\r|\n", keys or "", re.I))


def _has_space(keys: str) -> bool:
    # A space anywhere in typed text presses Space too ("ok " on a focused button activates it).
    return bool(re.search(r"\bspace\b", keys or "", re.I)) or " " in (keys or "")


def _moves_focus(keys: str) -> bool:
    return bool(re.search(r"\btab\b|\t", keys or "", re.I))


def _page_key(url: str) -> str:
    """Keep the fragment too: application sites can route to another job using it."""
    return url or ""


def fingerprint(fields: list[dict], approval_state: list[dict] | None = None) -> str:
    """A stable hash of a form's visible answers (form_check.scan fields)."""
    canonical = [(f.get("label", ""), f.get("kind", ""), f.get("value", ""),
                  f.get("id", ""), f.get("name", ""), f.get("form_action", ""),
                  f.get("form_method", ""), f.get("form_target", "")) for f in fields or []]
    return hashlib.sha256(json.dumps((canonical, approval_state), ensure_ascii=False).encode()).hexdigest()


# Shared name lookup in the target's isolated world. Prefer explicit accessible names, but retain
# the old visible/value label separately: ARIA saying "Next" must not hide a visible "Submit".
_CONTROL_NAME_JS = r"""
  function legacyControlName(el) {
    return String(el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().slice(0,120);
  }
  function controlName(el, pseudo) {
    const trim = value => String(value || '').trim().slice(0,120);
    const root = el.getRootNode();
    const ids = String(el.getAttribute('aria-labelledby') || '').trim().split(/\s+/).slice(0,32);
    const referenced = ids.map(id => root.getElementById && root.getElementById(id))
      .filter(Boolean).map(n => trim(n.innerText || n.textContent)).filter(Boolean).join(' ');
    if (referenced) return trim(referenced);
    const aria = trim(el.getAttribute('aria-label'));
    if (aria) return aria;
    if (el.tagName === 'IMG' || (el.tagName === 'INPUT' && el.type === 'image')) {
      const alt = trim(el.getAttribute('alt'));
      if (alt) return alt;
    }
    const text = trim(el.innerText || el.value);
    if (text) return text;
    const images = Array.from(el.querySelectorAll('img[alt]')).slice(0,16)
      .map(n => trim(n.getAttribute('alt'))).filter(Boolean).join(' ');
    if (images) return trim(images);
    const title = trim(el.getAttribute('title'));
    if (title) return title;
    return trim(pseudo);
  }
  function generatedControlName(el) {
    // Replaced/native field widgets don't render these pseudo-elements as button labels.
    if (['INPUT','IMG','TEXTAREA','SELECT'].includes(el.tagName)) return '';
    const fragments = [];
    for (const pseudo of ['::before','::after']) {
      const style = el.ownerDocument.defaultView.getComputedStyle(el, pseudo);
      if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') continue;
      const content = style.content;
      // Only string content, never url()/counter()/none/normal. Decode CSS string escapes as text.
      const strings = content.match(/"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g);
      if (!strings || content.replace(/"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g, '').trim()) continue;
      fragments.push(strings.map(s => s.slice(1,-1).replace(/\\([0-9a-fA-F]{1,6}\s?|[\s\S])/g, (_, escaped) => {
        if (!/^[0-9a-fA-F]/.test(escaped)) return /[\r\n\f]/.test(escaped) ? '' : escaped;
        const code = parseInt(escaped.trim(),16);
        return code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : '\ufffd';
      })).join(''));
    }
    return fragments.join(' ').trim().slice(0,120);
  }
"""

# Read in the same isolated world as the target. A final-step heading is positive evidence;
# a missing heading is not proof that a step is intermediate. The default submitter also matters
# when Enter/newline in a field activates it without directly clicking that button.
_FORM_CONTEXT_JS = r"""
  function formContext(el) {
    const doc = el.ownerDocument;
    const visibleHeadings = Array.from(doc.querySelectorAll('h1,h2,h3,[aria-current=step]')).slice(0,30)
      .filter(n => { const r=n.getBoundingClientRect(); return r.width>0 && r.height>0 &&
        !n.closest('[hidden],[aria-hidden=true]') &&
        (!n.checkVisibility || n.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})); });
    const headings = visibleHeadings.map(n => n.innerText || n.textContent || '').join(' ').slice(0,2000);
    const final = /\bfinal\b.{0,50}\b(application|review|step)\b|\blast\s+step\b|\breview\s+(and|&)\s+submit\b|\bstep\s+(\d+)\s+(?:of|\/)\s+\3\b/i.test(headings);
    const application_review = visibleHeadings.some(n => /^\s*(?:review(?:\s+(?:your|the))?\s+application|application\s+review)\s*[.!:]?\s*$/i.test(n.innerText || n.textContent || ''));
    const form = el.form || (el.closest && el.closest('form'));
    const root = form && form.getRootNode();
    const buttons = root ? Array.from(root.querySelectorAll('button,input')) : [];
    const submitter = buttons.find(n => n.form === form && (n.type === 'submit' || n.type === 'image'));
    const submitter_pseudo = submitter ? generatedControlName(submitter) : '';
    return {final_context: final, application_review, submitter_text: submitter ? controlName(submitter, submitter_pseudo) : '',
      submitter_alternative: submitter ? legacyControlName(submitter) : '', submitter_pseudo};
  }
"""

# The focused element (through shadow roots), for key presses.
FOCUSED_JS = r"""() => {
  let el = document.activeElement;
  for (let depth = 0; el && depth < 50; depth++) {
    if (el.shadowRoot && el.shadowRoot.activeElement) { el = el.shadowRoot.activeElement; continue; }
    if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
      try {
        const doc = el.contentDocument;
        if (!doc || !doc.activeElement) return JSON.stringify(null);
        el = doc.activeElement;
        continue;
      } catch (_) { return JSON.stringify(null); }
    }
    break;
  }
  if (!el || el === document.body || el === document.documentElement) return JSON.stringify(null);
  return JSON.stringify(describe(el));
  function describe(el) {
    const text_pseudo = generatedControlName(el), text = controlName(el, text_pseudo), text_alternative = legacyControlName(el);
    const role = el.getAttribute('role') || '';
    const combo = role === 'combobox' || el.hasAttribute('aria-autocomplete') || el.getAttribute('aria-haspopup') === 'listbox'
      || !!(el.closest && el.closest('[role=listbox],[role=combobox]'));
    return { ...formContext(el), text, text_alternative, text_pseudo, tag: el.tagName.toLowerCase(), type: String(el.type || el.getAttribute('type') || '').toLowerCase(), role,
             combo, in_form: !!(el.form || (el.closest && el.closest('form'))),
             multiline: el.tagName === 'TEXTAREA' || el.isContentEditable,
             document_url: el.ownerDocument.location.href };
  }
}"""

# The clickable element under a point (for coordinate clicks).
AT_POINT_JS = r"""(x, y) => {
  let el = document.elementFromPoint(x, y);
  while (el && el.shadowRoot) { const inner = el.shadowRoot.elementFromPoint(x, y); if (!inner || inner === el) break; el = inner; }
  // A coordinate hits the frame element in this document, but the browser
  // dispatches it inside the frame. Its actual target has not been checked.
  // Use an element-index action there so its own document can be inspected.
  if (!el || el.tagName === 'IFRAME' || el.tagName === 'FRAME') return JSON.stringify(null);
  const target = el.closest('button, a, input, summary, [role=button], [role=link]') || el;
  const text_pseudo = generatedControlName(target), text = controlName(target, text_pseudo), text_alternative = legacyControlName(target);
  return JSON.stringify({ ...formContext(target), text, text_alternative, text_pseudo, tag: target.tagName.toLowerCase(), type: String(target.type || target.getAttribute('type') || '').toLowerCase(),
                          role: target.getAttribute('role') || '', in_form: !!(target.form || target.closest('form')),
                          document_url: target.ownerDocument.location.href });
}"""

# The control an element-index click activates, read in the page (for a span inside a button, the button).
NODE_JS = """function() {
  const start = this.nodeType === 1 ? this : this.parentElement;   // a hit test can return a text node
  if (!start) return JSON.stringify(null);
  // Clicking a <label> (outside a control of its own) presses the control it labels.
  // An ARIA role (or an anchor without href) does not make a label descendant
  // natively interactive: the browser still activates the label's control.
  const own = start.closest && start.closest('button, a[href], input, select, textarea, summary');
  const label = !own && start.closest && start.closest('label');
  const hit = (label && label.control) || start;
  const el = (hit.closest && hit.closest('button, a, input, summary, [role=button], [role=link]')) || hit;
  const text_pseudo = generatedControlName(el), text = controlName(el, text_pseudo), text_alternative = legacyControlName(el);
  let hidden = false;
  for (let root = el.getRootNode(); root instanceof ShadowRoot; root = root.host.getRootNode()) hidden = hidden || root.mode === 'closed';
  return JSON.stringify({ ...formContext(el), text, text_alternative, text_pseudo, tag: (el.tagName || '').toLowerCase(), type: String(el.type || (el.getAttribute && el.getAttribute('type')) || '').toLowerCase(),
                          role: (el.getAttribute && el.getAttribute('role')) || '', in_form: !!(el.form || (el.closest && el.closest('form'))),
                          document_url: el.ownerDocument.location.href, hidden_tree: hidden });
}"""

# Keep all action entry points on the same form-context reader.
FOCUSED_JS = FOCUSED_JS.replace("() => {", "() => {" + _CONTROL_NAME_JS + _FORM_CONTEXT_JS, 1)
AT_POINT_JS = AT_POINT_JS.replace("(x, y) => {", "(x, y) => {" + _CONTROL_NAME_JS + _FORM_CONTEXT_JS, 1)
NODE_JS = NODE_JS.replace("function() {", "function() {" + _CONTROL_NAME_JS + _FORM_CONTEXT_JS, 1)

# What select_dropdown could click for `target` (run on its element): browser-use clicks an item whose
# text or data-value matches ([role=option]/[role=menuitem], or .item/.option/[data-value] under a
# "ui"/"dropdown" element, up to 4 levels down). This lists every such element in the subtree (a
# superset), each described as NODE_JS describes a click target.
DROPDOWN_ITEMS_JS = "function(target) {\n  const describe = " + NODE_JS + r""";
  if (this.tagName === 'SELECT') return JSON.stringify([]);
  const want = String(target || '').trim().toLowerCase(), out = [];
  for (const item of this.querySelectorAll('[role="menuitem"], [role="option"], .item, .option, [data-value]')) {
    const text = (item.textContent || '').trim().toLowerCase(), value = (item.getAttribute('data-value') || '').toLowerCase();
    if (text === want || value === want) out.push(JSON.parse(describe.call(item)));
  }
  return JSON.stringify(out);
}"""

# The focused element itself (the same walk as FOCUSED_JS), for checking it with the browser's own DOM:
# a page script sees the host of a closed shadow root where the element inside it has focus.
FOCUSED_ELEMENT_JS = r"""() => {
  let el = document.activeElement;
  for (let depth = 0; el && depth < 50; depth++) {
    if (el.shadowRoot && el.shadowRoot.activeElement) { el = el.shadowRoot.activeElement; continue; }
    if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
      try { const doc = el.contentDocument; if (!doc || !doc.activeElement) return null; el = doc.activeElement; continue; }
      catch (_) { return null; }
    }
    break;
  }
  return el || null;
}"""


def _in_form(node: Any) -> bool | None:
    """Whether the node sits in a <form> (from browser-use's parent links); None when it can't be told."""
    if (getattr(node, "attributes", None) or {}).get("form"):
        return True  # <button form="...">: belongs to that form wherever it sits
    if not hasattr(node, "parent_node"):
        return None
    current, depth = node, 0
    while current is not None and depth < 200:
        name = (getattr(current, "tag_name", "") or getattr(current, "node_name", "") or "").lower()
        if name == "form":
            return True
        current, depth = getattr(current, "parent_node", None), depth + 1
    return False if depth < 200 else None


def _in_closed_shadow(node: Any) -> bool:
    current, depth = getattr(node, "parent_node", None), 0
    while current is not None and depth < 200:
        if str(getattr(current, "shadow_root_type", "") or "").lower().endswith("closed"):
            return True
        current, depth = getattr(current, "parent_node", None), depth + 1
    return False


def control_from_node(node: Any) -> Control:
    """A Control from browser-use's DOM node (EnhancedDOMTreeNode)."""
    attrs = getattr(node, "attributes", None) or {}
    try:
        text = node.get_all_children_text() or ""
    except Exception:
        text = ""
    text = (text or attrs.get("aria-label") or attrs.get("value") or attrs.get("title") or attrs.get("alt") or "").strip()
    tag = (getattr(node, "tag_name", "") or getattr(node, "node_name", "") or "").lower()
    role = attrs.get("role", "")
    return Control(text=text[:120], tag=tag, type=(attrs.get("type") or "").lower(), role=role, in_form=_in_form(node),
                   combo=role == "combobox" or "aria-autocomplete" in attrs or attrs.get("aria-haspopup") == "listbox",
                   multiline=tag == "textarea" or attrs.get("contenteditable") in ("", "true"),
                   hidden_tree=_in_closed_shadow(node))


def control_from_json(raw: str | None) -> Control | None:
    try:
        data = json.loads(raw or "null")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    fields = {k: data.get(k, Control.__dataclass_fields__[k].default) for k in Control.__dataclass_fields__}
    return Control(**fields)
