"""Every question on the agent's current page, in page order: required? answered? error?

Agents filled forms out of order and clicked Next with required questions
still empty (Workday's "Have You Worked For ... Before?"), because:
  - Workday marks required fields with a red "*" that is aria-hidden, and
    sets no `required` attribute on most inputs, so the element list the
    agent gets says nothing about which questions are required;
  - its Yes/No radio buttons reach the agent as two blank boxes;
  - text inputs sit in shadow roots, out of reach of plain DOM queries.

This module reads the page itself (walking shadow roots) and
  - writes a short checklist into every step's prompt: the required questions
    still unanswered and the fields showing an error, top to bottom;
  - stops a click on Next / Continue / Submit while required questions are
    unanswered (once per set of missing questions, so a wrong guess about a
    field can never block the run).
"""

from __future__ import annotations

import json
import re
from . import browser_policy

try:
    from core.config import split_name
except ImportError:  # pragma: no cover
    from backend.core.config import split_name

_SCAN_JS = r"""() => {
  const out = [];
  // Only the backend hashes these values; the checklist/prompt uses `fields`.
  const approval = [];
  const documents = [];
  const visited = new Set();
  const seenRadio = new Set();
  // Named form controls can shadow these properties even in an isolated world.
  const formValue = (form, key) => Object.getOwnPropertyDescriptor(HTMLFormElement.prototype, key).get.call(form);
  const text = (el) => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const shown = (el) => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0);
  };
  const byId = (root, id) => (root.getElementById ? root.getElementById(id) : null) || document.getElementById(id);
  const labelFor = (el, root) => {
    if (el.id) {
      const sel = 'label[for="' + CSS.escape(el.id) + '"]';
      const l = (root.querySelector && root.querySelector(sel)) || document.querySelector(sel);
      if (l) return l;
    }
    return el.closest('label');
  };
  const labelText = (el, root) => {
    const l = labelFor(el, root);
    if (l && text(l)) return { t: text(l), star: /\*/.test(l.textContent || '') };
    const ids = el.getAttribute('aria-labelledby');
    if (ids) {
      const parts = ids.split(/\s+/).map((id) => byId(root, id)).filter(Boolean);
      const t = parts.map(text).join(' ').trim();
      if (t) return { t, star: parts.some((p) => /\*/.test(p.textContent || '')) };
    }
    const aria = el.getAttribute('aria-label');
    if (aria) return { t: aria, star: /\bRequired\b/.test(aria) };
    const group = el.closest('fieldset');
    const legend = group && group.querySelector('legend');
    if (legend) return { t: text(legend), star: /\*/.test(legend.textContent || '') };
    return { t: el.getAttribute('placeholder') || el.name || '', star: false };
  };
  const requiredOf = (el, star) => {
    if (el.required || el.getAttribute('aria-required') === 'true' || star) return true;
    for (let p = el.parentElement, i = 0; p && i < 6; p = p.parentElement, i++) {
      if (p.getAttribute && p.getAttribute('aria-required') === 'true') return true;
    }
    return false;
  };
  const errorOf = (el) => {
    if (el.getAttribute('aria-invalid') === 'true') {
      const ids = (el.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
      const msg = ids.map((id) => text(document.getElementById(id))).join(' ').trim();
      return msg || 'shows an error';
    }
    return '';
  };
  const clean = (label) => label.replace(/\s*\*\s*/g, ' ').replace(/\s+(Select One\s*)?Required$/i, '')
    .replace(/\s+Select One$/i, '').replace(/\s+/g, ' ').trim();

  const add = (el, root) => {
    const tag = el.tagName, type = (el.getAttribute('type') || '').toLowerCase();
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') return;
    if (tag === 'INPUT' && ['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) return;
    if (!shown(el)) return;
    let { t: label, star } = labelText(el, root);
    if (/robots only|do not enter if you'?re human/i.test(label)) return;  // honeypot
    let kind = 'text', value = '', options = [];
    if (type === 'password') {
      kind = 'password';
      // Report completeness, never put an account password into a prompt.
      value = el.value ? 'entered' : '';
    } else if (type === 'file') {
      kind = 'file';
      value = el.files && el.files.length ? 'attached' : '';
    } else if (type === 'radio') {
      if (!el.name || seenRadio.has(el.name)) return;
      seenRadio.add(el.name);
      const group = Array.from((root.querySelectorAll ? root : document).querySelectorAll('input[type=radio]'))
        .filter((r) => r.name === el.name);
      options = group.map((r) => labelText(r, root).t).filter(Boolean);
      const fs = el.closest('fieldset');
      const legend = fs && fs.querySelector('legend');
      const holder = el.closest('[aria-labelledby]');
      if (legend) { label = text(legend); star = /\*/.test(legend.textContent || ''); }
      else if (holder) { const r = labelText(holder, root); label = r.t; star = r.star; }
      const checked = group.find((r) => r.checked);
      value = checked ? labelText(checked, root).t || 'selected' : '';
      kind = 'choice';
      el = holder || el;
    } else if (type === 'checkbox' || el.getAttribute('role') === 'checkbox') {
      kind = 'checkbox';
      value = (el.checked || el.getAttribute('aria-checked') === 'true') ? 'ticked' : '';
    } else if (tag === 'SELECT') {
      kind = 'dropdown';
      const o = el.selectedOptions[0];
      value = o && o.value !== '' && !/^\s*(select|choose|no response|--)/i.test(o.text) ? text(o) : '';
    } else if (tag === 'BUTTON' || el.getAttribute('role') === 'combobox') {
      kind = 'dropdown';
      const shownText = text(el) || el.value || '';
      value = /^(select one|select|choose one|choose|no response|--)?$/i.test(shownText) ? '' : shownText;
      // Workday's searchable lists show the choice as a pill next to the input.
      if (!value) {
        const box = el.closest('[data-automation-id^="formField"]') || el.parentElement;
        const pill = box && box.querySelector('[data-automation-id="selectedItem"], [data-automation-id="selectedItemList"] li');
        if (pill && text(pill)) value = text(pill);
      }
    } else {
      value = (el.value || '').trim();
    }
    // Workday's picker input is a search query, not a committed answer.
    // Its selected pill survives when the query clears. Treating query text as
    // an answer let Next proceed with 'FinTech' and Review show 'No Response'.
    const workday = el.closest('[data-automation-id^="formField"]');
    if (workday && (el.getAttribute('role') === 'combobox' ||
        el.getAttribute('data-automation-id') === 'searchBox')) {
      const pills = Array.from(workday.querySelectorAll('[data-automation-id="selectedItem"], [data-automation-id="selectedItemList"] li'));
      kind = 'dropdown';
      value = pills.map(text).filter(Boolean).join('; ');
      if (!value && el.value) options = ['Select a matching option; typed search text is not saved'];
    }
    label = clean(label);
    if (!label) return;
    const form = el.form || el.closest('form');
    out.push({ label: label.slice(0, 160), kind, required: requiredOf(el, star), value,
               id: el.id || '', name: el.name || '', form_action: form ? formValue(form, 'action') : '',
               form_method: form ? formValue(form, 'method') : '', form_target: form ? formValue(form, 'target') : '',
               error: errorOf(el).slice(0, 160), options: options.slice(0, 6) });
  };

  const SELECTOR = 'input, select, textarea, button[aria-haspopup="listbox"], [role="combobox"]:not(input), [role="checkbox"]';
  const walk = (root) => {
    const all = root.querySelectorAll('*');
    for (const el of all) {
      if (el.matches(SELECTOR)) add(el, root);
      if (el.tagName === 'FORM') {
        approval.push({ document: el.ownerDocument.location.href, id: el.getAttribute('id') || '', action: formValue(el, 'action'),
          method: formValue(el, 'method'), target: formValue(el, 'target'), controls: Array.from(formValue(el, 'elements')).map(c => ({
            tag: c.tagName, type: c.type || '', name: c.name || '', id: c.id || '',
            value: c.value || '', checked: !!c.checked, disabled: !!c.disabled,
            action: c.getAttribute('formaction') || '', method: c.getAttribute('formmethod') || '',
            target: c.getAttribute('formtarget') || ''
          })) });
      }
      if (el.shadowRoot) walk(el.shadowRoot);
      if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
        try { if (el.contentDocument) visit(el.contentDocument); } catch (_) { /* unreadable: never authorize its submit */ }
      }
    }
  };
  const visit = (doc) => {
    if (visited.has(doc)) return;
    visited.add(doc);
    documents.push(doc.location.href);
    walk(doc);
  };
  visit(document);
  const errorBanner = Array.from(document.querySelectorAll('[data-automation-id="errorBanner"], [data-automation-id="errorSummary"], [role="alert"]'))
    .map(text).filter(Boolean).join(' ').slice(0, 300);
  return JSON.stringify({ fields: out, errorBanner, approval_state: approval, scanned_documents: documents });
}"""

# Buttons that move on from the page.
_ADVANCE = re.compile(r"^\s*(next|continue|save (and|&) continue|submit( application)?|review|apply|send application)\b",
                      re.I)
# Not worth a checklist: a page with fewer than this many form fields (search boxes, cookie banners).
MIN_FIELDS = 3


async def scan(browser_session) -> dict:
    """{"fields": [...], "errorBanner": "..."} for the current page; {} when it can't be read."""
    try:
        page = await browser_session.get_current_page()
        if page is None:
            return {}
        raw = await browser_policy.evaluate(browser_session, _SCAN_JS)
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def missing_required(data: dict) -> list[dict]:
    """Required questions still unanswered, in page order."""
    return [f for f in data.get("fields", []) if f.get("required") and not (f.get("value") or "").strip()]


def _describe(field: dict) -> str:
    opts = f" ({' / '.join(field['options'])})" if field.get("options") else ""
    return f'"{field["label"]}"{opts}'


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _degree_level(value: str) -> str:
    value = re.sub(r"[.\s]", "", value or "").casefold()
    for level, pattern in (("doctorate", r"doctor|phd|dphil"), ("master", r"master|^m(sc|s|a|ba|eng)$"),
                           ("bachelor", r"bachelor|^b(sc|s|a|ba|eng)$"), ("associate", r"associate|^a(as|a|s)$")):
        if re.search(pattern, value):
            return level
    return ""


def profile_mismatches(data: dict, profile: dict | None) -> list[tuple[str, str, str]]:
    """(label, shown, expected) for phone / email / ZIP / name fields that don't match the profile.

    An agent typed a number with two digits swapped, and the site then
    rejected it as invalid; nothing pointed at the typo.
    """
    if not profile:
        return []
    first, last = split_name(profile.get("name") or "")
    address = profile.get("address") or {}
    checks = [
        (r"\bphone\b(?!.*(type|device|extension|code))", profile.get("phone") or "", "phone"),
        (r"\be-?mail\b", profile.get("email") or "", "email"),
        (r"\b(postal|zip)\b", address.get("zip") or "", "zip"),
        (r"\bfirst name\b|\bgiven name\b", first, "text"),
        (r"\blast name\b|\bfamily name\b|\bsurname\b", last, "text"),
    ]
    out = []
    for field in data.get("fields", []):
        shown = (field.get("value") or "").strip()
        degree = (profile.get("education") or {}).get("degree") if isinstance(profile.get("education"), dict) else ""
        if shown and re.search(r"\bdegree\b|education level", field.get("label") or "", re.I):
            expected_level, shown_level = _degree_level(degree or ""), _degree_level(shown)
            if expected_level and shown_level and expected_level != shown_level:
                out.append((field.get("label") or "Degree", shown, degree))
            continue
        if not shown or field.get("kind") != "text":
            continue
        label = field.get("label") or ""
        for pattern, expected, kind in checks:
            if not expected or not re.search(pattern, label, re.I):
                continue
            if kind == "phone":
                a, b = _digits(shown), _digits(expected)
                wrong = len(a) >= 7 and len(b) >= 7 and a[-10:] != b[-10:] and not b.endswith(a) and not a.endswith(b)
            elif kind == "zip":
                wrong = _digits(shown)[:5] != _digits(expected)[:5]
            elif kind == "email":
                # Account pages (Create Account / Sign In) take the account email.
                allowed = {e.strip().lower() for e in (expected, profile.get("account_email") or "") if e}
                wrong = shown.lower() not in allowed
            else:
                wrong = shown.lower() != expected.strip().lower()
            if wrong:
                out.append((label, shown, expected))
            break
    return out


def checklist_text(data: dict, profile: dict | None = None) -> str:
    """The per-step prompt note: what is still missing on this page, top to bottom. "" when nothing."""
    fields = data.get("fields", [])
    if len(fields) < MIN_FIELDS:
        return ""
    missing = missing_required(data)
    errors = [f for f in fields if f.get("error")]
    mismatched = profile_mismatches(data, profile)
    if not missing and not errors and not data.get("errorBanner") and not mismatched:
        return ("FORM CHECK: every required question on this page is answered. Check the values are right, "
                "then continue.")
    lines = ["FORM CHECK (read from the page; top to bottom; a red * marks a required question even when the "
             "element list doesn't say so):"]
    if missing:
        lines.append("Required questions still unanswered:")
        lines += [f"  {i}. {_describe(f)}" for i, f in enumerate(missing, 1)]
    if errors:
        lines.append("Fields showing an error:")
        lines += [f'  - "{f["label"]}": {f["error"]}' for f in errors]
    if mismatched:
        lines.append("Fields that don't match the profile (clear them and retype the profile value):")
        lines += [f'  - "{label}" shows "{shown}", profile: "{expected}"' for label, shown, expected in mismatched]
    if data.get("errorBanner"):
        lines.append(f"Page error message: {data['errorBanner']}")
    lines.append("Fix these, top to bottom, before clicking Next / Continue / Submit.")
    return "\n".join(lines)


def is_advance_button(text: str) -> bool:
    return bool(_ADVANCE.match(text or ""))
