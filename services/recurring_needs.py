"""Calendar-accurate recurring required-Need projection authority."""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _add_months(anchor: datetime, months: int) -> datetime:
    """Advance from the original anchor so short months never cause drift."""
    year = anchor.year + (anchor.month - 1 + months) // 12
    month = (anchor.month - 1 + months) % 12 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)


def occurrence_at(anchor: datetime, recurrence: str, index: int) -> datetime:
    anchor = _utc(anchor)
    if recurrence == 'weekly': return anchor + timedelta(days=7 * index)
    if recurrence == 'biweekly': return anchor + timedelta(days=14 * index)
    if recurrence == 'monthly': return _add_months(anchor, index)
    if recurrence == 'quarterly': return _add_months(anchor, 3 * index)
    if recurrence == 'yearly': return _add_months(anchor, 12 * index)
    raise ValueError('Unsupported recurrence')


def project_occurrences(*, obligations: Iterable[Any], as_of: datetime, horizon_end: datetime, explicit_bill_links: set[tuple[int, str]]) -> dict[str, Any]:
    rows, incomplete = [], []
    for obligation in obligations:
        if not obligation.is_active:
            continue
        if obligation.expected_amount_cents is None:
            incomplete.append({'obligation_id': obligation.id, 'name': obligation.name, 'reason': 'expected_amount_missing'})
            continue
        index = 0
        while True:
            due = occurrence_at(obligation.next_due_date, obligation.recurrence, index)
            if due > horizon_end: break
            if due >= as_of and (obligation.id, due.date().isoformat()) not in explicit_bill_links:
                rows.append({'key': f'recurring:{obligation.id}:{due.date().isoformat()}', 'obligation_id': obligation.id,
                             'due_date': due, 'amount_cents': int(obligation.expected_amount_cents), 'label': obligation.name,
                             'provenance': 'recurring_required_obligation'})
            index += 1
    rows.sort(key=lambda row: (row['due_date'], row['key']))
    return {'occurrences': rows, 'incomplete': incomplete}
