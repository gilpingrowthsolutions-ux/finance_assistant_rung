"""Deterministic, advisory household behavior intelligence (Package 16).

The service interprets the reconciled ExpenseTransaction economic projection.
It never reads raw Plaid rows, mutates financial state, or treats hypothetical
savings as available money.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from statistics import median
from typing import Any


LOOKBACK_DAYS = 90
MIN_ABSOLUTE_MATERIAL_CENTS = 2500
NEED_CATEGORIES = {"grocery", "groceries", "fuel", "transport", "transportation", "housing", "rent", "utilities", "utility", "medical", "prescription", "childcare", "insurance"}
TRANSFER_CATEGORIES = {"transfer", "savings", "investment", "investments", "reserve", "income", "balance", "adjustment"}
DISCRETIONARY_CATEGORIES = {"discretionary", "coffee", "dining", "restaurant", "entertainment", "subscription", "shopping"}
NEED_WORDS = {"rent", "mortgage", "utility", "utilities", "electric", "water", "prescription", "pharmacy", "childcare", "daycare", "grocery", "groceries", "fuel", "insurance"}
TRANSFER_WORDS = {"transfer", "savings", "investment", "reserve", "brokerage", "401k", "ira"}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _cents(value: Any) -> int:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(amount * 100)


def _money(cents: int) -> float:
    return float((Decimal(int(cents)) / 100).quantize(Decimal("0.01")))


def canonical_merchant(raw: str) -> str:
    """Normalize merchant identity while callers retain raw evidence."""
    text = str(raw or "").lower().replace("&", " and ")
    text = re.sub(r"\b(?:pos|debit|purchase|payment|online|recurring)\b", " ", text)
    text = re.sub(r"\b(?:inc|llc|corp|corporation|company|co)\b", " ", text)
    text = re.sub(r"[#*]\s*\d+\b|\bstore\s*\d+\b|\b\d{3,}\b", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    words = [word for word in text.split() if word]
    aliases = {
        "mcdonald s": "mcdonalds", "mcdonalds restaurant": "mcdonalds",
        "planet fitness club": "planet fitness", "starbucks coffee": "starbucks",
    }
    normalized = " ".join(words).strip()
    return aliases.get(normalized, normalized)[:120]


def _classification(category: str, merchant: str, correction: str | None) -> str:
    if correction in {"need", "discretionary", "transfer"}:
        return correction
    cat = str(category or "").strip().lower()
    words = set(merchant.split())
    if cat in TRANSFER_CATEGORIES or words & TRANSFER_WORDS:
        return "transfer"
    if cat in NEED_CATEGORIES or words & NEED_WORDS:
        return "need"
    if cat in DISCRETIONARY_CATEGORIES:
        return "discretionary"
    return "unclassified"


def _cadence(dates: list[datetime]) -> tuple[int | None, str | None, list[int]]:
    ordered = sorted({_utc(row).date() for row in dates})
    intervals = [(ordered[i] - ordered[i - 1]).days for i in range(1, len(ordered))]
    if not intervals:
        return None, None, []
    typical = int(Decimal(str(median(intervals))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    label = None
    if 6 <= typical <= 8 and all(4 <= day <= 10 for day in intervals): label = "weekly"
    elif 12 <= typical <= 16 and all(9 <= day <= 19 for day in intervals): label = "biweekly"
    elif 25 <= typical <= 35 and all(20 <= day <= 40 for day in intervals): label = "monthly"
    return typical, label, intervals


def _pattern_signature(merchant: str, category: str, typical: int, cadence_days: int | None, cadence: str | None) -> str:
    raw = f"{merchant}|{category}|{typical}|{cadence_days or 0}|{cadence or 'irregular'}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _latest_decisions(rows: list[Any]) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for row in sorted(rows, key=lambda item: (item.created_at, item.id)):
        latest[row.candidate_key] = row
    return latest


def _material_change(decision: Any, *, typical_cents: int, cadence_days: int | None, occurrence_count: int) -> bool:
    old_amount = int(decision.typical_amount_cents or 0)
    amount_changed = old_amount > 0 and abs(typical_cents - old_amount) * 100 >= old_amount * 25
    old_cadence = int(decision.cadence_days or 0)
    cadence_changed = old_cadence > 0 and cadence_days is not None and abs(cadence_days - old_cadence) >= max(4, old_cadence // 3)
    count_changed = occurrence_count >= int(decision.occurrence_count or 0) + 3
    return amount_changed or cadence_changed or count_changed


def _suppressed(decision: Any | None, *, typical_cents: int, cadence_days: int | None, occurrence_count: int) -> bool:
    return bool(decision and decision.action == "ignore" and not _material_change(
        decision, typical_cents=typical_cents, cadence_days=cadence_days, occurrence_count=occurrence_count))


def reduction_projection(observed_cents: int, period_days: int = LOOKBACK_DAYS) -> dict[str, Any]:
    annualized = int((Decimal(observed_cents) * Decimal(365) / Decimal(period_days)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    reductions = {}
    for pct in (25, 50, 75):
        observed_save = int((Decimal(observed_cents) * Decimal(pct) / Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        annual_save = int((Decimal(annualized) * Decimal(pct) / Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        reductions[str(pct)] = {"period_savings_cents": observed_save, "period_savings": _money(observed_save), "annualized_savings_cents": annual_save, "annualized_savings": _money(annual_save)}
    return {"basis": f"Observed over the last {period_days} days; annualized as observed amount × 365 ÷ {period_days}.", "annualized_cents": annualized, "annualized": _money(annualized), "reductions": reductions, "replacement_cost_cents": None}


def build_behavior_intelligence(*, household_id: int, transactions: list[Any], bills: list[Any], decisions: list[Any], now: datetime, checking_cents: int | None = None) -> dict[str, Any]:
    now = _utc(now); start = now - timedelta(days=LOOKBACK_DAYS)
    transactions = [row for row in transactions if int(row.household_id) == int(household_id)]
    bills = [row for row in bills if int(row.household_id) == int(household_id)]
    decisions = [row for row in decisions if int(row.household_id) == int(household_id)]
    latest = _latest_decisions(decisions)
    corrections = {key.removeprefix("merchant:"): row.classification for key, row in latest.items() if key.startswith("merchant:") and row.action == "classify"}
    bill_merchants = {canonical_merchant(row.name) for row in bills}
    grouped: dict[str, list[Any]] = {}
    income_cents = 0
    for row in transactions:
        if row.date is None or not (start <= _utc(row.date) <= now): continue
        if str(row.category or "").lower() == "income":
            income_cents += _cents(row.amount); continue
        merchant = canonical_merchant(row.description)
        if merchant: grouped.setdefault(merchant, []).append(row)

    relative_cents = int((Decimal(max(0, income_cents)) * Decimal("0.01")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    materiality_cents = max(MIN_ABSOLUTE_MATERIAL_CENTS, relative_cents)
    candidates: list[dict[str, Any]] = []; opportunities: list[dict[str, Any]] = []; suppressed_count = 0
    for merchant, rows in sorted(grouped.items()):
        rows.sort(key=lambda row: (_utc(row.date), row.id or 0))
        categories = Counter(str(row.category or "unclassified").lower() for row in rows)
        category = categories.most_common(1)[0][0]
        interpretation = _classification(category, merchant, corrections.get(merchant))
        if interpretation == "transfer": continue
        amounts = [_cents(row.amount) for row in rows]
        typical = int(Decimal(str(median(amounts))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        cadence_days, cadence_label, intervals = _cadence([row.date for row in rows])
        amount_range = max(amounts) - min(amounts)
        stable_amount = amount_range <= max(300, int(typical * .20))
        raw_evidence = [{"transaction_id": row.id, "raw_description": row.description, "date": _utc(row.date).date().isoformat(), "amount_cents": _cents(row.amount), "source": row.source, "plaid_linked": bool(row.plaid_transaction_id)} for row in rows]
        evidence = {"occurrence_count": len(rows), "observed_dates": [row["date"] for row in raw_evidence], "cadence_days": cadence_days, "cadence": cadence_label or "irregular", "interval_days": intervals, "typical_amount_cents": typical, "amount_min_cents": min(amounts), "amount_max_cents": max(amounts), "raw_activity": raw_evidence, "sources": sorted({str(row.source or "manual") for row in rows})}

        # High-frequency lifestyle merchants can coincidentally resemble a
        # cadence. Keep those as spending patterns unless the household has
        # already established the merchant as a bill.
        pattern_only_category = category in {"coffee", "dining", "restaurant", "shopping", "grocery", "groceries"}
        if len(rows) >= 3 and cadence_label and stable_amount and (not pattern_only_category or merchant in bill_merchants):
            key = f"recurring:{merchant}"
            ignored = _suppressed(latest.get(key), typical_cents=typical, cadence_days=cadence_days, occurrence_count=len(rows))
            if ignored: suppressed_count += 1
            else:
                candidates.append({"candidate_key": key, "canonical_merchant": merchant, "category": category, "classification": interpretation, "confidence": "high" if len(rows) >= 4 and amount_range <= max(100, int(typical*.10)) else "moderate", "existing_bill": merchant in bill_merchants, "evidence": evidence, "pattern_signature": _pattern_signature(merchant, category, typical, cadence_days, cadence_label), "actions": ["review_activity"] if merchant in bill_merchants else ["add_to_recurring_bills", "ignore", "review_activity"]})

        observed = sum(amounts)
        if interpretation != "discretionary" or observed < materiality_cents or len(rows) < 3: continue
        key = f"opportunity:{merchant}"
        ignored = _suppressed(latest.get(key), typical_cents=typical, cadence_days=cadence_days, occurrence_count=len(rows))
        if ignored: suppressed_count += 1; continue
        projection = reduction_projection(observed)
        opportunities.append({"candidate_key": key, "kind": "potential_savings", "title": f"Potential savings opportunity at {merchant.title()}", "canonical_merchant": merchant, "category": category, "classification": interpretation, "observed_cents": observed, "observed": _money(observed), "observation_period": {"label": f"Last {LOOKBACK_DAYS} days", "days": LOOKBACK_DAYS, "start": start.date().isoformat(), "end": now.date().isoformat()}, "projection": projection, "evidence": evidence, "pattern_signature": _pattern_signature(merchant, category, typical, cadence_days, cadence_label), "materiality": {"threshold_cents": materiality_cents, "rule": "Greater of $25 or 1% of confirmed household income observed in the same 90-day period."}, "hypothetical_only": True, "actions": ["important", "dont_suggest", "review_activity", "preview_savings_plan"]})

    candidates.sort(key=lambda row: (0 if latest.get(row["candidate_key"]) and latest[row["candidate_key"]].action == "important" else 1, -row["evidence"]["occurrence_count"], -row["evidence"]["typical_amount_cents"], row["canonical_merchant"]))
    opportunities.sort(key=lambda row: (0 if latest.get(row["candidate_key"]) and latest[row["candidate_key"]].action == "important" else 1, -row["projection"]["reductions"]["50"]["annualized_savings_cents"], row["canonical_merchant"]))
    return {"authority": "household_behavior_intelligence_v1", "read_only": True, "status": "available", "observation_period": {"label": f"Last {LOOKBACK_DAYS} days", "days": LOOKBACK_DAYS, "start": start.date().isoformat(), "end": now.date().isoformat()}, "materiality": {"threshold_cents": materiality_cents, "absolute_floor_cents": MIN_ABSOLUTE_MATERIAL_CENTS, "household_income_one_percent_cents": relative_cents, "rule": "Greater of $25 or 1% of confirmed household income observed in the same 90-day period."}, "recurring_candidates": candidates, "opportunities": opportunities[:3], "overview_opportunity": opportunities[0] if opportunities else None, "suppressed_count": suppressed_count, "empty": not candidates and not opportunities, "safe_to_spend_effect_cents": 0, "financial_mutations": False}
