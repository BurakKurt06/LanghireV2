"""Pay on a posting: read it, put it on a common footing, and compare it with the user's minimum.

Postings state pay in every shape: "$22 - $26 an hour", "$76,000.00/yr - $133,000.00/yr",
"£40,000 - £60,000 per annum", "1-4.25 Lacs PA", "3.500 € brutto/Monat", or in the
posting data (JSON-LD baseSalary). Hourly, daily, weekly and monthly pay is turned
into a yearly estimate: hours a week x 52 (40 hours for full-time; for part-time the
posting's hours or the user's), 5 days a week, 52 weeks, 12 months.

"$" is the user's dollar currency (USD unless their country uses another dollar), and
pay in another currency than the user's minimum is never compared: "not comparable"
is not "below".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

PERIODS = ("hour", "day", "week", "month", "year")
FULL_TIME_HOURS = 40.0
DAYS_PER_WEEK = 5
WEEKS_PER_YEAR = 52

DOLLARS = {"USD", "CAD", "AUD", "NZD", "SGD", "HKD"}
_CODES = ("USD", "CAD", "AUD", "NZD", "SGD", "HKD", "GBP", "EUR", "INR", "JPY", "CHF", "SEK", "NOK", "DKK",
          "PLN", "BRL", "MXN", "AED", "ZAR")
_SYMBOLS = {"us$": "USD", "ca$": "CAD", "c$": "CAD", "au$": "AUD", "a$": "AUD", "nz$": "NZD", "s$": "SGD",
            "hk$": "HKD", "r$": "BRL", "£": "GBP", "€": "EUR", "₹": "INR", "¥": "JPY", "rs": "INR", "rs.": "INR",
            "zł": "PLN"}
# Units of a currency worth about one US dollar: for telling an hourly rate from a
# salary when the posting doesn't say (₹500 is not an hourly rate).
_SCALE = {"INR": 80, "JPY": 150, "SEK": 10, "NOK": 10, "DKK": 7, "PLN": 4, "BRL": 5, "MXN": 18, "ZAR": 18,
          "AED": 3.7, "HKD": 7.8}

_CODE_RX = r"(?:" + "|".join(_CODES) + r")(?![A-Za-z])"
_CURRENCY = r"US\$|CA\$|C\$|AU\$|A\$|NZ\$|S\$|HK\$|R\$|[$£€₹¥]|Rs\.?(?=\s?\d)|zł|" + _CODE_RX
_AMOUNT = re.compile(
    rf"(?P<cur1>{_CURRENCY})?\s?(?P<num>\d{{1,2}}(?:,\d{{2}})+,\d{{3}}(?!\d)|\d{{1,3}}(?:[,.\u00a0\u202f']\d{{3}})+(?!\d)|\d+)(?:[.,](?P<dec>\d{{1,2}})(?!\d))?"
    rf"(?:\s?(?P<mult>[kK](?![A-Za-z])|lakhs?|lacs?|lpa|crores?|cr(?![A-Za-z])))?"
    rf"(?:\s?(?P<cur2>€|£|₹|zł|{_CODE_RX}))?", re.I)
_RANGE = re.compile(r"\s*(?:-|–|—|to|bis|and)\s*", re.I)
_PERIOD_RES = tuple((period, re.compile(rx, re.I)) for period, rx in (
    ("hour", r"(?:/|\bper\s+|\ban?\s+)\s?(?:hr|hrs|hour|h)\b|\bhourly\b|\bp/?h\b|\bph\b|/\s?std\b|pro\s+stunde"),
    ("day", r"(?:/|\bper\s+|\ba\s+)\s?(?:day|shift)\b|\bdaily\b|\bper\s+diem\b|\bp/?d\b|/\s?tag\b|pro\s+tag"),
    ("week", r"(?:/|\bper\s+|\ba\s+)\s?(?:wk|week)\b|\bweekly\b|\bp/?w\b"),
    ("month", r"(?:/|\bper\s+|\ba\s+)\s?(?:mo|mon|month|mth)\b|\bmonthly\b|\bpcm\b|\bp\.m\.?|/\s?monat\b|"
              r"pro\s+monat|monatlich"),
    ("year", r"(?:/|\bper\s+|\ban?\s+)\s?(?:yr|year|annum|ann)\b|\bannual(?:ly)?\b|\byearly\b|\bp\.?\s?a\b\.?|"
             r"\blpa\b|/\s?jahr\b|pro\s+jahr|jährlich|\bctc\b"),
))
_FILLER = re.compile(r"\b(?:a|an|per|gross|brutto|base|est\.?|estimated|usd|eur|gbp|cad|aud|inr)\b", re.I)
_UNITS = {"HOUR": "hour", "DAY": "day", "WEEK": "week", "MONTH": "month", "YEAR": "year"}
# Description money that isn't the job's pay, and words that say it is.
_NOT_PAY = re.compile(r"bonus|sign[- ]?on|relocation|reimburs|tuition|401|funding|raised|revenue|valuation|"
                      r"budget|million|billion|gift|referral|equity|stock|insurance|allowance|\bfees?\b|deposit|"
                      r"\bcosts?\b|donat|grant|loan|scholarship|prize|award|discount|credit", re.I)
_IS_PAY = re.compile(r"\bpay\b|\bpaid\b|salar|compensation|wage|\brate\b|\brange\b|\bearn|\bbase\b|hourly|annual|"
                     r"stipend|\bote\b|remuneration|gehalt|vergütung|\bctc\b|\bstarting\b|\bpackage\b", re.I)
# "37.5 hours per week" is 37.5 (UK postings), never the 5 after its decimal point.
_HOURS = re.compile(r"(?<!\d)(?<!\d[.,])(\d{1,2}(?:[.,]\d{1,2})?)(?:\s?(?:-|–|to)\s?(\d{1,2}(?:[.,]\d{1,2})?))?"
                    r"\s?(?:hours|hrs|h)\s?(?:per|a|/|each)\s?week", re.I)
_LAKH = ("lakh", "lac", "lpa", "cr")


def _other_money(text: str, match) -> bool:
    """Recognize money attached to benefits/funding, including words AFTER it.

    A later 'plus bonus' doesn't turn a stated base salary into a bonus.
    Stop context at sentence/line separators and the next monetary amount.
    """
    before = re.split(r"[\n;.!?]", text[max(0, match.start()-60):match.start()])[-1]
    pay_at = max((m.end() for m in _IS_PAY.finditer(before)), default=-1)
    other_at = max((m.end() for m in _NOT_PAY.finditer(before)), default=-1)
    if other_at > pay_at:
        return True
    after = re.split(r"[\n;!?]", text[match.end():match.end()+60])[0]
    _, used = _period_after(after)
    after = after[used:]
    after = re.split(r"\b(?:plus|and|with)\b", after, maxsplit=1, flags=re.I)[0]
    # Range amounts and period/year words may precede a directly attached noun.
    return bool(re.match(r"\s*(?:per\s+year\s+)?(?:sign[- ]?on\s+|signing\s+|annual\s+|performance\s+)?"
                         r"(?:bonus|gift\s+card|relocation|reimburs|tuition|million|billion|funding|allowance)\b",
                         after, re.I))


@dataclass
class Pay:
    low: float
    high: float
    currency: str          # ISO code; "" when unknown
    period: str            # one of PERIODS
    text: str = ""         # what the posting said, short
    hours: float = FULL_TIME_HOURS   # hours a week the estimates use
    inferred: bool = False           # the period was told from the size of the number
    source_estimate: bool = False    # a board's estimate, rather than stated base salary

    def per(self, period: str, hours: float | None = None) -> tuple[float, float]:
        """(low, high) as pay per `period`."""
        hours = hours or self.hours
        return convert(self.low, self.period, period, hours), convert(self.high, self.period, period, hours)

    def to_dict(self) -> dict:
        low, high = self.per("year")
        return {"low": round(self.low, 2), "high": round(self.high, 2), "currency": self.currency,
                "period": self.period, "text": self.text[:80], "hours": self.hours,
                "yearly_low": round(low), "yearly_high": round(high),
                "estimated": self.period != "year" or self.inferred or self.source_estimate,
                "period_inferred": self.inferred, "source_estimate": self.source_estimate}


def convert(amount: float, from_period: str, to_period: str, hours: float = FULL_TIME_HOURS) -> float:
    per_year = {"hour": hours * WEEKS_PER_YEAR, "day": DAYS_PER_WEEK * WEEKS_PER_YEAR,
                "week": WEEKS_PER_YEAR, "month": 12, "year": 1}
    return amount * per_year[from_period] / per_year[to_period]


def _plain(m) -> float:
    digits = re.sub(r"[,.\u00a0\u202f']", "", m.group("num"))
    return float(digits) + (float("0." + m.group("dec")) if m.group("dec") else 0)


def _factor(m) -> float:
    mult = (m.group("mult") or "").lower()
    if mult == "k":
        return 1_000
    if mult.startswith(("lakh", "lac", "lpa")):
        return 100_000
    if mult.startswith("cr"):
        return 10_000_000
    return 1


def _currency(m, default: str) -> str:
    raw = (m.group("cur1") or m.group("cur2") or "").strip().lower()
    if not raw:
        return ""
    if raw == "$":
        return default if default in DOLLARS else "USD"
    return _SYMBOLS.get(raw, raw.upper())


def _period_after(text: str) -> tuple[str, int]:
    """The pay period said right after an amount ("/hr", " an hour", " per annum"), and where it ends.

    Only up to the next number: in "$22 - $26 an hour" the period belongs to the range's end.
    """
    cut = re.search(r"[\d$£€₹¥]", text)
    head = text[:min(28, cut.start() if cut else 28)]
    best = None
    for period, rx in _PERIOD_RES:
        m = rx.search(head)
        if m and (best is None or m.start() < best[2]):
            best = (period, m.end(), m.start())
    if best is None:
        return "", 0
    if re.search(r"[A-Za-z]{3,}", _FILLER.sub("", head[:best[2]])):
        return "", 0      # "$25 gift card each hour": other words in between
    return best[0], best[1]


def _infer_period(value: float, currency: str) -> str:
    scale = _SCALE.get(currency, 1)
    if value < 300 * scale:
        return "hour"
    if value < 1_000 * scale:
        return ""          # a day or a week: can't tell
    if value < 15_000 * scale:
        return "month"
    return "year"


def _plausible(pay: Pay, *, allow_small=False) -> bool:
    scale = _SCALE.get(pay.currency, 1)
    if pay.low < 0 or pay.high < pay.low:
        return False
    return (0 if allow_small else 3_000 * scale) <= convert(pay.high, pay.period, "year", pay.hours) <= 3_000_000 * scale


def _read_at(text: str, m, default_currency: str) -> tuple[Pay, bool, bool] | None:
    """The pay starting at amount match `m` (one amount or a range, and its period):
    (pay, whether a currency was written, whether both ends of a range carried one)."""
    currency = _currency(m, default_currency)
    low, high = _plain(m) * _factor(m), _plain(m) * _factor(m)
    units = [(m.group("mult") or "").lower()]
    pos = m.end()
    period, used = _period_after(text[pos:])
    pos += used
    both_marked = False
    sep = _RANGE.match(text, pos)
    second = _AMOUNT.match(text, sep.end()) if sep else None
    if second is not None and second.group("num") and second.start() == sep.end():
        high = _plain(second) * _factor(second)
        units.append((second.group("mult") or "").lower())
        if second.group("mult") and not m.group("mult") and _plain(m) < 1_000 <= high:
            low = _plain(m) * _factor(second)          # "$80-100K": the K is shared
        both_marked = bool(currency and (second.group("cur1") or second.group("cur2")))
        currency = currency or _currency(second, default_currency)
        later, used = _period_after(text[second.end():])
        period = period or later
        pos = second.end() + used
    lakhs = any(u.startswith(_LAKH) for u in units)
    if lakhs and not currency:
        currency = "INR"
    inferred = not period
    if inferred:
        period = _infer_period(max(low, high), currency or default_currency)
        if not period:
            return None
    if high < low:
        low, high = high, low
    raw = re.sub(r"\s+", " ", text[m.start():pos]).strip(" ,;(")
    pay = Pay(low=low, high=high, currency=currency, period=period, text=raw, inferred=inferred)
    return (pay, bool(currency), both_marked) if _plausible(pay, allow_small=bool(currency) and not inferred) else None


def parse(text: str, default_currency: str = "USD") -> Pay | None:
    """Pay from a short pay line: a listing's "$25 - $30 an hour", "49000 - 73000 per year".

    An amount counts only with a currency or a period: a listing's "NY 10016" or "3 days ago" isn't pay.
    """
    text = (text or "")[:400]
    for m in _AMOUNT.finditer(text):
        if _other_money(text, m):
            continue
        found = _read_at(text, m, default_currency)
        if found and (found[1] or not found[0].inferred):
            pay = found[0]
            pay.currency = pay.currency or default_currency
            return pay
    return None


def from_description(text: str, default_currency: str = "USD") -> Pay | None:
    """Pay stated in a posting's text: an amount with a currency, said as pay ("The pay range
    is $22-$26 per hour"), never a bonus, a fee, a reimbursement or a funding round."""
    text = text or ""
    for m in _AMOUNT.finditer(text):
        if not (m.group("cur1") or m.group("cur2") or (m.group("mult") or "").lower().startswith(_LAKH)):
            continue
        if _other_money(text, m):
            continue
        found = _read_at(text, m, default_currency)
        if not found:
            continue
        pay, _marked, both_marked = found
        before = text[max(0, m.start() - 60):m.start()]
        pay_at = max((x.end() for x in _IS_PAY.finditer(before)), default=-1)
        not_pay_at = max((x.end() for x in _NOT_PAY.finditer(before[-40:])), default=-1)
        if not_pay_at >= 0 and not_pay_at + len(before) - min(40, len(before)) > pay_at:
            continue      # "relocation assistance up to $5,000"
        if not pay.inferred or pay_at >= 0 or both_marked:
            return pay
    return None


def from_json_ld(item: dict, default_currency: str = "USD") -> Pay | None:
    """Pay from a JobPosting's baseSalary (or estimatedSalary)."""
    base = item.get("baseSalary") or item.get("estimatedSalary")
    if isinstance(base, list):
        base = base[0] if base else None
    if not isinstance(base, dict):
        return None
    value = base.get("value")
    unit = str(base.get("unitText") or "").upper()
    if isinstance(value, dict):
        unit = str(value.get("unitText") or unit).upper()
        low, high = value.get("minValue"), value.get("maxValue")
        if low is None and high is None:
            low = high = value.get("value")
    else:
        low = high = value
    try:
        low = float(str(low if low is not None else high).replace(",", ""))
        high = float(str(high if high is not None else low).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if max(low, high) <= 0:
        # Boards such as Reed write 0 when the pay is only in the text: no pay information here
        # (user's decision 11-01: never drop a paying job over a placeholder 0). The caller reads the
        # description next; with no pay anywhere the job takes the no-pay rule.
        return None
    currency = str(base.get("currency") or (value.get("currency") if isinstance(value, dict) else "") or "").upper()
    period = _UNITS.get(unit, "")
    inferred = not period
    if inferred:
        period = _infer_period(max(low, high), currency or default_currency)
        if not period:
            return None
    if high < low:
        low, high = high, low
    symbol = {"USD": "$", "GBP": "£", "EUR": "€", "INR": "₹"}.get(currency, (currency + " ") if currency else "")
    text = f"{symbol}{low:,.0f}" + (f"–{symbol}{high:,.0f}" if high != low else "") + f"/{period}"
    pay = Pay(low=low, high=high, currency=currency, period=period, text=text, inferred=inferred)
    pay.source_estimate = not bool(item.get("baseSalary")) and bool(item.get("estimatedSalary"))
    return pay if _plausible(pay, allow_small=True) else None


def hours_per_week(job_types=(), text: str = "", part_time_hours: float = 20) -> float:
    """Hours a week for estimates: the posting's own ("20-25 hours per week"), else 40, or the
    user's part-time hours for a part-time job."""
    m = _HOURS.search(text or "")
    if m:
        low, high = (float(g.replace(",", ".")) for g in (m.group(1), m.group(2) or m.group(1)))
        hours = (low + high) / 2
        if 4 <= hours <= 60:
            return hours
    types = tuple(job_types or ())
    if "part_time" in types and "full_time" not in types:
        return float(part_time_hours or 20)
    return FULL_TIME_HOURS


def compare(pay: Pay | None, minimum: dict | None) -> str:
    """"meets", "below" or "unknown" (no pay, no minimum, or pay in another currency)."""
    if pay is None or not minimum or pay.inferred or pay.source_estimate:
        return "unknown"
    try:
        amount = float(minimum.get("amount") or 0)
    except (TypeError, ValueError):
        return "unknown"
    if amount <= 0:
        return "unknown"
    period = minimum.get("period") if minimum.get("period") in PERIODS else "year"
    wanted = str(minimum.get("currency") or "").upper()
    if wanted and pay.currency != wanted:
        return "unknown"
    # The top of the range: the job can pay what the user asks for.
    # Compare exact decimal products instead of tolerating a 0.1% shortfall
    # or rejecting an exact conversion due to binary float division.
    try:
        high, low, hours, wanted_amount = (Decimal(str(v)) for v in (pay.high, pay.low, pay.hours, amount))
        if not all(v.is_finite() for v in (high, low, hours, wanted_amount)) or hours <= 0 or low < 0 or high < low:
            return "unknown"
        factors = {"hour": hours * 52, "day": Decimal(260), "week": Decimal(52),
                   "month": Decimal(12), "year": Decimal(1)}
        return "meets" if high * factors[pay.period] >= wanted_amount * factors[period] else "below"
    except (InvalidOperation, KeyError, ValueError):
        return "unknown"


def from_dict(data: dict | None) -> Pay | None:
    """A stored pay dict back as Pay (None for anything malformed)."""
    if not isinstance(data, dict) or data.get("period") not in PERIODS:
        return None
    try:
        result = Pay(low=float(data["low"]), high=float(data["high"]), currency=str(data.get("currency") or ""),
                   period=data["period"], text=str(data.get("text") or "")[:80],
                   hours=float(data.get("hours") or FULL_TIME_HOURS),
                   inferred=bool(data.get("period_inferred", data.get("estimated") and data["period"]=="year")),
                   source_estimate=data.get("source_estimate") is True)
        return result if _plausible(result, allow_small=True) else None
    except (KeyError, TypeError, ValueError):
        return None


def describe(pay: dict | None) -> str:
    """'$24/hr (≈ 49,920/yr)' from a stored pay dict, for the log."""
    if not pay:
        return "pay not listed"
    text = pay.get("text") or ""
    if pay.get("period") != "year" and pay.get("yearly_high"):
        low, high = pay.get("yearly_low"), pay.get("yearly_high")
        return f"{text} (≈ {low:,}/yr)" if low == high else f"{text} (≈ {low:,}–{high:,}/yr)"
    return text
