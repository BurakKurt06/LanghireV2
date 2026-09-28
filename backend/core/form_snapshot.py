"""Read the visible form fields of the agent's current page, to spot what the user changed.

When the user pauses a run (or reviews a page before it is submitted) and
fixes fields by hand, comparing a snapshot taken before with one taken after
tells which fields they changed. Those values are then kept for the rest of
the run and saved as the user's answers.
"""

import json

# Field label and displayed value for inputs, selects, textareas, radio groups
# and ARIA dropdowns (Workday uses buttons with aria-haspopup="listbox").
_SNAPSHOT_JS = r"""() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return (r.width > 0 || r.height > 0) && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const text = (el) => (el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '');
  const labelOf = (el) => {
    if (el.labels && el.labels.length) return text(el.labels[0]);
    const ids = el.getAttribute('aria-labelledby');
    if (ids) {
      const t = ids.split(/\s+/).map((id) => text(document.getElementById(id))).join(' ').trim();
      if (t) return t;
    }
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const group = el.closest('fieldset, [role="radiogroup"], [role="group"]');
    if (group) {
      const legend = group.querySelector('legend, label');
      if (legend) return text(legend);
    }
    return el.getAttribute('placeholder') || el.name || el.id || '';
  };
  const skip = new Set(['hidden', 'submit', 'button', 'password', 'file', 'image', 'reset', 'search']);
  const radios = new Set();
  const out = [];
  const els = document.querySelectorAll('input, select, textarea, button[aria-haspopup="listbox"], [role="combobox"]:not(input)');
  for (const el of els) {
    if (!visible(el)) continue;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (el.tagName === 'INPUT' && skip.has(type)) continue;
    let label = labelOf(el);
    let value;
    if (type === 'checkbox') {
      value = el.checked ? 'checked' : 'unchecked';
    } else if (type === 'radio') {
      if (!el.name || radios.has(el.name)) continue;
      radios.add(el.name);
      const group = el.closest('fieldset, [role="radiogroup"], [role="group"]');
      const legend = group && group.querySelector('legend');
      if (legend) label = text(legend);
      const checked = document.querySelector('input[type="radio"][name="' + CSS.escape(el.name) + '"]:checked');
      value = checked ? labelOf(checked) : '';
    } else if (el.tagName === 'SELECT') {
      value = el.selectedOptions.length ? text(el.selectedOptions[0]) : '';
    } else if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
      value = el.value || '';
    } else {
      value = text(el);
    }
    const key = el.id || el.name || label;
    if (!key) continue;
    out.push({ key: key.slice(0, 200), label: label.slice(0, 200), value: String(value).slice(0, 500) });
  }
  return JSON.stringify(out);
}"""


async def snapshot(browser_session) -> dict[str, tuple[str, str]]:
    """{field key: (label, value)} for the current page; {} if it can't be read."""
    try:
        page = await browser_session.get_current_page()
        if page is None:
            return {}
        raw = await page.evaluate(_SNAPSHOT_JS)
        fields = json.loads(raw) if raw else []
    except Exception:
        return {}
    return {f["key"]: (f.get("label") or f["key"], f.get("value") or "") for f in fields if f.get("key")}


def changed_fields(before: dict[str, tuple[str, str]], after: dict[str, tuple[str, str]]) -> list[tuple[str, str, str]]:
    """(label, old value, new value) for each field whose value changed, or that appeared filled."""
    changes = []
    for key, (label, value) in after.items():
        old = before.get(key, (label, ""))[1]
        if value != old and value.strip():
            changes.append((label, old, value))
    return changes


def describe_changes(changes: list[tuple[str, str, str]]) -> str:
    """Prompt text for the fields the user set by hand."""
    return "\n".join(f'- "{label}" = "{new}"' for label, _old, new in changes)
