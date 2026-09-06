from datetime import datetime, timedelta, timezone

from services.forward_needs import calculate_forward_needs_reserve, forward_horizon


NOW = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
NEXT = datetime(2026, 9, 4, tzinfo=timezone.utc)


def project(*, bills, income):
    horizon = forward_horizon(as_of=NOW, next_payday=NEXT, pay_period_days=14)
    return calculate_forward_needs_reserve(
        as_of=NOW, current_boundary=NEXT, horizon_end=horizon, bills=bills, expected_income=income,
    )


def test_horizon_is_later_of_31_days_or_payday_after_next():
    assert forward_horizon(as_of=NOW, next_payday=NEXT, pay_period_days=14) == NOW + timedelta(days=31)
    near = NOW + timedelta(days=1)
    assert forward_horizon(as_of=NOW, next_payday=near, pay_period_days=45) == near + timedelta(days=45)


def test_future_bill_reserves_only_the_current_money_income_cannot_cover():
    result = project(
        income=[{"date": NEXT, "amount_cents": 10000}],
        bills=[{"key": "rent", "due_date": NEXT + timedelta(days=2), "amount_cents": 25000}],
    )
    assert result["forward_needs_reserve_cents"] == 15000


def test_future_income_fully_covers_future_bill_without_reserve():
    result = project(
        income=[{"date": NEXT, "amount_cents": 30000}],
        bills=[{"key": "rent", "due_date": NEXT + timedelta(days=2), "amount_cents": 25000}],
    )
    assert result["forward_needs_reserve_cents"] == 0


def test_multiple_events_are_chronological_and_income_is_counted_once():
    result = project(
        income=[{"key": "first", "date": NEXT, "amount_cents": 10000}, {"key": "second", "date": NEXT + timedelta(days=14), "amount_cents": 10000}],
        bills=[{"key": "a", "due_date": NEXT + timedelta(days=1), "amount_cents": 15000}, {"key": "b", "due_date": NEXT + timedelta(days=15), "amount_cents": 9000}],
    )
    assert result["forward_needs_reserve_cents"] == 5000
    assert [row["key"] for row in result["events"]] == ["first", "a", "second", "b"]


def test_due_on_payday_uses_canonical_income_before_required_need_ordering():
    # The served authority sends an exact-payday Bill to the forward projector
    # (rather than current Needs), so this uses the same event boundary.
    result = calculate_forward_needs_reserve(
        as_of=NOW, current_boundary=NOW, horizon_end=NEXT + timedelta(days=31),
        expected_income=[{"key": "payday", "date": NEXT, "amount_cents": 10000}],
        bills=[{"key": "same-day", "due_date": NEXT, "amount_cents": 10000}],
    )
    assert [row["key"] for row in result["events"]] == ["payday", "same-day"]
    assert result["forward_needs_reserve_cents"] == 0


def test_need_immediately_after_payday_uses_expected_income_and_remains_visible():
    result = project(
        income=[{"key": "payday", "date": NEXT, "amount_cents": 12000}],
        bills=[{"key": "day-after", "due_date": NEXT + timedelta(days=1), "amount_cents": 15000}],
    )
    assert [row["key"] for row in result["events"]] == ["payday", "day-after"]
    assert result["forward_needs_reserve_cents"] == 3000


def test_impossible_forward_needs_reports_the_exact_current_cash_required():
    result = project(
        income=[{"key": "payday", "date": NEXT, "amount_cents": 5000}],
        bills=[{"key": "rent", "due_date": NEXT + timedelta(days=1), "amount_cents": 21000}],
    )
    # This is the exact amount current money must supply; the served snapshot
    # compares it against actual checking to expose any remaining shortfall.
    assert result["forward_needs_reserve_cents"] == 16000
