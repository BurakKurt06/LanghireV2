"""Reading pay the way postings state it, the yearly estimate, and the minimum-pay comparison."""
import pytest

from core import pay


@pytest.mark.parametrize("text, low, high, currency, period", [
    ("$22 - $26 an hour", 22, 26, "USD", "hour"),
    ("$76,000.00/yr - $133,000.00/yr", 76000, 133000, "USD", "year"),
    ("$70,481.60 - $83,475.00 a year", 70481.6, 83475, "USD", "year"),
    ("£40,000 - £60,000 per annum", 40000, 60000, "GBP", "year"),
    ("$80K–$100K/yr", 80000, 100000, "USD", "year"),
    ("$80-100K", 80000, 100000, "USD", "year"),
    ("Up to $25/hr", 25, 25, "USD", "hour"),
    ("$18.50 - $22.00 per hour", 18.5, 22, "USD", "hour"),
    ("Compensation: $22.00 - $28.00 Hourly", 22, 28, "USD", "hour"),
    ("$1,200/week", 1200, 1200, "USD", "week"),
    ("3.500 € brutto/Monat", 3500, 3500, "EUR", "month"),
    ("€3.5k per month", 3500, 3500, "EUR", "month"),
    ("1-4.25 Lacs PA", 100000, 425000, "INR", "year"),
    ("₹ 6,00,000 - 8,00,000 per annum", 600000, 800000, "INR", "year"),
    ("USD 90,000 - 110,000", 90000, 110000, "USD", "year"),
    ("$200 per day", 200, 200, "USD", "day"),
    ("49000 - 73000 per year", 49000, 73000, "USD", "year"),
])
def test_listing_pay_lines(text, low, high, currency, period):
    found = pay.parse(text)
    assert found is not None, text
    assert (found.low, found.high, found.currency, found.period) == (pytest.approx(low), pytest.approx(high), currency, period)


@pytest.mark.parametrize("text", [
    "New York, NY 10016", "Posted 3 days ago", "2026 graduates", "Easy Apply · 25 applicants",
    "Hybrid work in New York, NY 10016", "", "Full-time",
])
def test_numbers_that_are_not_pay(text):
    assert pay.parse(text) is None


def test_a_dollar_means_the_users_own_dollar():
    assert pay.parse("$30/hr", "CAD").currency == "CAD"
    assert pay.parse("$30/hr", "EUR").currency == "USD"
    assert pay.parse("A$90,000 a year", "USD").currency == "AUD"


@pytest.mark.parametrize("text, expected", [
    ("The pay range for this role is $22-$26 per hour, plus benefits.", (22, 26, "hour")),
    ("Salary: $90,000 - $120,000 plus bonus and equity.", (90000, 120000, "year")),
    ("Base salary range $95,000 – $125,000 USD.", (95000, 125000, "year")),
    ("We pay $19/hr to start.", (19, 19, "hour")),
])
def test_pay_in_a_description(text, expected):
    found = pay.from_description(text)
    assert found is not None, text
    assert (found.low, found.high, found.period) == expected


@pytest.mark.parametrize("text", [
    "We offer a $5,000 sign-on bonus to new hires.",
    "Relocation assistance of up to $5,000 per year.",
    "Tuition reimbursement up to $5,250 per year.",
    "We raised $20 million in Series B funding.",
    "Enjoy a $50 gift card every month.",
    "401(k) with 4% match and a 30 hour work week.",
    "Our revenue grew to $3,000,000 last year.",
])
def test_money_in_a_description_that_isnt_pay(text):
    assert pay.from_description(text) is None


def test_posting_data():
    item = {"baseSalary": {"@type": "MonetaryAmount", "currency": "USD",
                           "value": {"@type": "QuantitativeValue", "minValue": 20, "maxValue": 25, "unitText": "HOUR"}}}
    found = pay.from_json_ld(item)
    assert (found.low, found.high, found.currency, found.period) == (20, 25, "USD", "hour")
    found = pay.from_json_ld({"baseSalary": {"currency": "GBP", "value": "45000", "unitText": "YEAR"}})
    assert (found.low, found.high, found.currency, found.period) == (45000, 45000, "GBP", "year")
    assert pay.from_json_ld({"baseSalary": {"currency": "USD", "value": {"minValue": "x"}}}) is None
    assert pay.from_json_ld({}) is None


def test_yearly_estimates():
    hourly = pay.parse("$24/hr")
    assert hourly.to_dict()["yearly_high"] == 24 * 40 * 52
    assert hourly.to_dict()["estimated"] is True
    hourly.hours = 20
    assert hourly.to_dict()["yearly_high"] == 24 * 20 * 52
    monthly = pay.parse("€3,000 per month", "EUR")
    assert monthly.to_dict()["yearly_low"] == 36000
    yearly = pay.parse("$60,000 a year")
    assert yearly.to_dict()["estimated"] is False


def test_hours_a_week():
    assert pay.hours_per_week(["part_time"], "", 20) == 20
    assert pay.hours_per_week(["part_time"], "This role is 25-30 hours per week.", 20) == 27.5
    assert pay.hours_per_week(["full_time"], "") == 40
    assert pay.hours_per_week([], "about 32 hrs a week") == 32
    assert pay.hours_per_week(["part_time", "full_time"], "") == 40


@pytest.mark.parametrize("text, hours", [
    # UK postings (Reed, 2026) state half hours: "37.5" was read as 5 hours a week.
    ("Hours: 37.5 hours per week", 37.5), ("Hours: 37.5 hrs/week", 37.5), ("22.5 hours a week", 22.5),
    ("37.5-40 hours per week", 38.75), ("16-20 hours a week", 18),
])
def test_hours_a_week_with_half_hours(text, hours):
    assert pay.hours_per_week(["full_time"], text) == hours


def test_an_hourly_job_stating_half_hours_is_not_left_out_as_below_a_yearly_minimum():
    found = pay.parse("£13.50 an hour", "GBP")
    found.hours = pay.hours_per_week(["full_time"], "£13.50 an hour. Hours: 37.5 hours per week, Monday to Friday.")
    # £13.50 x 37.5 h x 52 = £26,325 a year (at 5 h a week it was £3,510).
    assert pay.compare(found, {"amount": 25000, "period": "year", "currency": "GBP"}) == "meets"


def test_minimum_pay_is_compared_in_the_users_own_period():
    part_time = pay.parse("$22/hr")
    part_time.hours = 20
    assert pay.compare(part_time, {"amount": 20, "period": "hour", "currency": "USD"}) == "meets"
    # $22/hr for 20 hours a week is $22,880 a year: below a yearly minimum of $45,000.
    assert pay.compare(part_time, {"amount": 45000, "period": "year", "currency": "USD"}) == "below"
    full_time = pay.parse("$45,000 a year")
    assert pay.compare(full_time, {"amount": 20, "period": "hour", "currency": "USD"}) == "meets"
    # The top of the range counts.
    assert pay.compare(pay.parse("$40k-$60k/year"), {"amount": 50000, "period": "year", "currency": "USD"}) == "meets"
    assert pay.compare(pay.parse("$40k-$45k/year"), {"amount": 50000, "period": "year", "currency": "USD"}) == "below"


def test_pay_that_cant_be_compared_is_not_below():
    assert pay.compare(None, {"amount": 50000, "period": "year", "currency": "USD"}) == "unknown"
    assert pay.compare(pay.parse("£30,000 a year"), {"amount": 50000, "period": "year", "currency": "USD"}) == "unknown"
    assert pay.compare(pay.parse("$30,000 a year"), {"amount": 0, "period": "year"}) == "unknown"
    assert pay.compare(pay.parse("$30,000 a year"), None) == "unknown"


def test_stored_pay_round_trips():
    stored = pay.parse("$22 - $26 an hour").to_dict()
    again = pay.from_dict(stored)
    assert (again.low, again.high, again.period) == (22, 26, "hour")
    assert pay.from_dict({"period": "fortnight"}) is None
    assert pay.from_dict("x") is None
    assert "≈" in pay.describe(stored) and pay.describe(None) == "pay not listed"
