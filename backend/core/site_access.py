"""Where an application's account details may be typed, and which password a site gets.

The user's decision (2026-11): when a job link leads to a hiring site (the employer's own
pages, Workday, SuccessFactors, iCIMS, Taleo, Greenhouse or anything else), the app signs in
there, or creates the account, with the email and the one job-site password saved in
Settings. Earlier versions released account details only on a site they could prove to be
"the employer's own origin and tenant", which refused real applications (a posting on
careers.<employer>.com whose account lives on career4.successfactors.com).

What stays out of reach, always:
- Gmail and other mail pages of the user's email account (the agent never opens them);
- this computer, the local network and bare addresses;
- pages that aren't https;
- job boards (LinkedIn, Indeed, ...): those are the user's own sign-ins, never this password;
- a site the job link didn't lead to: one the agent opened by typing an address that no page
  of this application linked to.
"""
from __future__ import annotations

import re
import string
from urllib.parse import urlsplit

MAIL_HOSTS = ("mail.google.com", "gmail.com", "inbox.google.com", "googlemail.com",
              "accounts.google.com", "outlook.live.com", "outlook.office.com", "mail.yahoo.com", "mail.proton.me")
# The most sites one application's account details go to (the posting's site, its application
# system, a sign-in host). More than that is not an application any more.
MAX_CREDENTIAL_SITES = 4

_SYMBOLS = "!@#$%^&*-_+="


def host_of(url: str) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _under(host: str, names) -> bool:
    return any(host == name or host.endswith("." + name) for name in names)


def is_mail_host(host: str) -> bool:
    return _under((host or "").lower().rstrip("."), MAIL_HOSTS)


def registrable(host: str) -> str:
    try:
        from core.domains import registrable as site_of
    except ImportError:  # pragma: no cover
        from backend.core.domains import registrable as site_of
    return site_of(host)


def refusal(url: str, board_hosts=(), is_private=None) -> str:
    """Why account details can never be typed on `url` ("" when nothing rules it out).

    `is_private(host)` tells this computer and the local network apart from the public web
    (core.shared_config.is_private_host); a name that can't be looked up isn't private.
    """
    try:
        parts = urlsplit(url or "")
        port = parts.port
    except ValueError:
        return "this page's address can't be read"
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not host:
        return "this page isn't a secure (https) page"
    if port not in (None, 443):
        return "this page uses an unusual port"
    if is_mail_host(host):
        return "this is an email site; the app reads verification emails itself"
    if _under(host.removeprefix("www."), board_hosts):
        return "this is a job board (sign in to it yourself in Settings → Accounts & email)"
    if "." not in host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return "this is a local address"
    if is_private is not None and is_private(host):
        return "this is a local or private address"
    return ""


_LINK_HOSTS_JS = """() => {
  const hosts = new Set();
  const add = (value) => { try { hosts.add(new URL(value, document.baseURI).hostname.toLowerCase()); } catch (e) {} };
  document.querySelectorAll('a[href], area[href]').forEach((a) => add(a.getAttribute('href')));
  document.querySelectorAll('form[action]').forEach((f) => add(f.getAttribute('action')));
  document.querySelectorAll('iframe[src]').forEach((f) => add(f.getAttribute('src')));
  return JSON.stringify(Array.from(hosts).slice(0, 500));
}"""


class SiteTrail:
    """The sites this job's link led to during one application.

    A page counts when the browser got there from the posting by itself: a redirect, a click,
    a form, a link, a new tab, or the verification link the app opened. A site the agent
    opened by typing an address that no page of this application linked to does not count,
    and neither does anything reached from there.
    """

    def __init__(self, job_url: str):
        self.led = {host_of(job_url)} - {""}
        self.outside: set[str] = set()
        self.typed: set[str] = set()         # registrable sites that received account details
        self._last = host_of(job_url)

    def allow(self, url: str) -> None:
        """A page the app itself opened for this application (the verification link from the email)."""
        host = host_of(url)
        if host:
            self.led.add(host)
            self.outside.discard(host)

    def navigating(self, target: str, linked_hosts) -> None:
        """The agent is about to open `target` by address: led there only if a page linked to it."""
        host = host_of(target)
        if not host or host in self.led:
            return
        if host in set(linked_hosts or ()) or registrable(host) in {registrable(h) for h in self.led}:
            self.led.add(host)
        else:
            self.outside.add(host)

    def saw(self, url: str) -> None:
        """The browser is on `url` now (called before each step and after each action)."""
        host = host_of(url)
        if not host:
            return
        if host not in self.led and host not in self.outside:
            # Reached from where it was: from one of this job's pages, or from an outside one.
            (self.outside if self._last in self.outside else self.led).add(host)
        self._last = host

    def led_to(self, url: str) -> bool:
        host = host_of(url)
        return bool(host) and host in self.led and host not in self.outside

    def may_type(self, url: str) -> str:
        """"" when account details may go to `url`'s site, else why not (the per-application limit)."""
        site = registrable(host_of(url))
        if site in self.typed or len(self.typed) < MAX_CREDENTIAL_SITES:
            return ""
        return f"account details already went to {len(self.typed)} sites in this application"

    def typed_on(self, url: str) -> None:
        self.typed.add(registrable(host_of(url)))


async def linked_hosts(browser_session, evaluate) -> set[str]:
    """Hosts the current page links to (its links, forms and frames); empty when it can't be read."""
    import json
    try:
        return set(json.loads(await evaluate(browser_session, _LINK_HOSTS_JS) or "[]"))
    except Exception:
        return set()


def site_key(url: str) -> str:
    """What a site's own password is saved under: its host (one account system per host)."""
    return host_of(url)


# ── The user's password, made to fit a site's rules ─────────────────────────
class PasswordRules:
    """What a site's sign-up form demanded when it rejected the password."""

    def __init__(self, min_length: int = 0, max_length: int = 0, uppercase: bool = False, lowercase: bool = False,
                 digit: bool = False, symbol: bool = False, forbidden: str = ""):
        self.min_length = max(0, int(min_length or 0))
        self.max_length = max(0, int(max_length or 0))
        self.uppercase, self.lowercase, self.digit, self.symbol = bool(uppercase), bool(lowercase), bool(digit), bool(symbol)
        self.forbidden = forbidden or ""


class PasswordCannotFit(ValueError):
    """Adding characters can't make the user's password meet this site's rules."""


def fit_password(base: str, rules: PasswordRules, previous: str = "") -> str:
    """The user's password with characters added to its END until it meets `rules`.

    Never replaced, shortened or reordered: the user can work out every site's password from
    their own. `previous` is the version this site already rejected (a further one is made).
    Raises PasswordCannotFit when adding characters can't help (too long for the site, or the
    password itself holds a character the site forbids).
    """
    base = base or ""
    if not base:
        raise PasswordCannotFit("no job-site password is saved in Settings → Accounts & email")
    if any(c in rules.forbidden for c in base):
        raise PasswordCannotFit("your password contains a character this site doesn't allow")
    if rules.max_length and len(base) > rules.max_length:
        raise PasswordCannotFit(f"your password is longer than this site allows ({rules.max_length} characters)")
    allowed_symbols = [c for c in _SYMBOLS if c not in rules.forbidden] or [""]
    start = previous if previous.startswith(base) and len(previous) > len(base) else base
    out = start
    if rules.uppercase and not any(c.isupper() for c in out):
        out += "A"
    if rules.lowercase and not any(c.islower() for c in out):
        out += "a"
    if rules.digit and not any(c.isdigit() for c in out):
        out += "1"
    if rules.symbol and not any(c in string.punctuation for c in out):
        out += allowed_symbols[0]
    filler = [c for c in ("A", "a", "1", allowed_symbols[0]) if c and c not in rules.forbidden] or ["x"]
    position = 0
    while len(out) < rules.min_length:
        out += filler[position % len(filler)]
        position += 1
    if out == start:
        # Rejected without a rule the page named (or the same rules again): the usual four kinds.
        for extra in filler:
            out += extra
    if rules.max_length and len(out) > rules.max_length:
        out = out[:rules.max_length]
        if len(out) <= len(base) or out == previous:
            raise PasswordCannotFit(f"this site allows at most {rules.max_length} characters")
    if out == previous or out == base:
        raise PasswordCannotFit("no further version of the password fits this site's rules")
    return out


def describe_rules(text: str) -> PasswordRules:
    """Password rules read from a site's own error text ("at least 12 characters, one symbol ...")."""
    low = (text or "").lower()
    rules = PasswordRules()
    m = re.search(r"(?:at least|minimum(?: of)?|min\.?)\s*(\d{1,2})\s*(?:characters|chars|long)?", low) \
        or re.search(r"(\d{1,2})\s*(?:or more|\+)\s*characters", low)
    if m:
        rules.min_length = int(m.group(1))
    m = re.search(r"(?:at most|maximum(?: of)?|max\.?|no more than|up to)\s*(\d{1,3})\s*(?:characters|chars)?", low)
    if m:
        rules.max_length = int(m.group(1))
    rules.uppercase = bool(re.search(r"upper ?case|capital", low))
    rules.lowercase = bool(re.search(r"lower ?case", low))
    rules.digit = bool(re.search(r"\b(digit|number|numeric|numeral)", low))
    rules.symbol = bool(re.search(r"symbol|special|punctuation|non-?alphanumeric", low))
    return rules
