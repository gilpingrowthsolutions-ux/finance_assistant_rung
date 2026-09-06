from datetime import datetime, timezone
from types import SimpleNamespace

from services.recurring_needs import occurrence_at, project_occurrences


UTC = timezone.utc


def test_calendar_recurrences_preserve_anchor_month_day_and_leap_rules():
    jan31 = datetime(2027, 1, 31, tzinfo=UTC)
    assert occurrence_at(jan31, 'monthly', 1).date().isoformat() == '2027-02-28'
    assert occurrence_at(jan31, 'monthly', 2).date().isoformat() == '2027-03-31'
    leap = datetime(2024, 2, 29, tzinfo=UTC)
    assert occurrence_at(leap, 'yearly', 1).date().isoformat() == '2025-02-28'
    assert occurrence_at(leap, 'yearly', 4).date().isoformat() == '2028-02-29'


def test_projection_deduplicates_explicit_link_and_reports_missing_amount():
    due = datetime(2026, 9, 10, tzinfo=UTC)
    obligation = SimpleNamespace(id=7, name='Rent', expected_amount_cents=120000, next_due_date=due, recurrence='monthly', is_active=True)
    incomplete = SimpleNamespace(id=8, name='Unknown utility', expected_amount_cents=None, next_due_date=due, recurrence='monthly', is_active=True)
    result = project_occurrences(obligations=[obligation, incomplete], as_of=datetime(2026, 9, 1, tzinfo=UTC), horizon_end=datetime(2026, 10, 15, tzinfo=UTC), explicit_bill_links={(7, '2026-09-10')})
    assert [row['key'] for row in result['occurrences']] == ['recurring:7:2026-10-10']
    assert result['incomplete'] == [{'obligation_id': 8, 'name': 'Unknown utility', 'reason': 'expected_amount_missing'}]


def test_two_weekly_occurrences_before_boundary_are_both_projected_except_the_linked_one():
    first = datetime(2026, 9, 4, tzinfo=UTC)
    obligation = SimpleNamespace(id=9, name='Childcare', expected_amount_cents=2500, next_due_date=first, recurrence='weekly', is_active=True)
    result = project_occurrences(obligations=[obligation], as_of=datetime(2026, 9, 1, tzinfo=UTC), horizon_end=datetime(2026, 9, 20, tzinfo=UTC), explicit_bill_links={(9, '2026-09-04')})
    # The first is represented by its explicit Bill; the second is still a
    # real pre-payday occurrence and cannot vanish from current Needs.
    assert [row['key'] for row in result['occurrences']] == ['recurring:9:2026-09-11', 'recurring:9:2026-09-18']
