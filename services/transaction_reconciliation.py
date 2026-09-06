from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from extensions import db
from models import (Account, ExpenseTransaction, PlaidAccount, PlaidTransaction,
                    SavingsTransfer, SavingsTransferReconciliation,
                    ShoppingTripCompletion, TransactionReconciliation)
from sqlalchemy.exc import IntegrityError
from services.household_context import household_id as current_household_id
from services.financial_state import apply_balance_delta, get_household_account
from services.income_pyf import establish_for_income


PROPOSAL_STATUS = "proposed"
MATCHED_STATUS = "matched"
REJECTED_STATUS = "rejected"

DATE_WINDOW_DAYS = 3
STRONG_MATCH_SCORE = 58


def _tx_query(household_id: int):
    return ExpenseTransaction.query.filter_by(household_id=household_id)


def _trip_query(household_id: int):
    return ShoppingTripCompletion.query.filter_by(household_id=household_id)


def _recon_query(household_id: int):
    return TransactionReconciliation.query.filter_by(household_id=household_id)


@dataclass
class MatchCandidate:
    plaid_transaction_id: str
    score: int
    reason: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_cents(value: Any) -> int:
    dec = Decimal(str(value or 0))
    cents = (dec.copy_abs() * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def _manual_direction(tx: ExpenseTransaction) -> str:
    return "inflow" if str(tx.category or "").strip().lower() == "income" else "outflow"


def _manual_amount_cents(tx: ExpenseTransaction) -> int:
    return _to_cents(tx.amount)


def _manual_date(tx: ExpenseTransaction) -> Optional[date]:
    if tx.date is None:
        return None
    return tx.date.date()


def _normalized_tokens(value: str) -> set[str]:
    lowered = str(value or "").lower()
    lowered = re.sub(r"\b\d+\b", " ", lowered)
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    tokens = {tok for tok in lowered.split() if len(tok) > 1}
    noise = {
        "debit", "credit", "purchase", "payment", "card", "pos", "pending",
        "supercenter", "store", "market", "inc", "llc", "co", "usa",
    }
    return {tok for tok in tokens if tok not in noise}


def _merchant_similarity(a: str, b: str) -> float:
    a_tokens = _normalized_tokens(a)
    b_tokens = _normalized_tokens(b)
    if not a_tokens or not b_tokens:
        return 0.0
    inter = len(a_tokens & b_tokens)
    union = len(a_tokens | b_tokens)
    if union <= 0:
        return 0.0
    return inter / union


def _distance_days(left: Optional[date], right: Optional[date]) -> Optional[int]:
    if left is None or right is None:
        return None
    return abs((left - right).days)


def _manual_text(tx: ExpenseTransaction) -> str:
    text = str(tx.description or "")
    trip = _trip_query(int(tx.household_id)).filter_by(transaction_id=tx.id).first()
    if trip is not None:
        text = " ".join([text, str(trip.retailer or ""), str(trip.store_name or "")]).strip()
    return text


def _is_manual_candidate(tx: ExpenseTransaction) -> bool:
    if tx.plaid_transaction_id:
        return False
    category = str(tx.category or "").strip().lower()
    if category == "balance_reconciliation":
        return False
    return True


def _rejected_pair_exists(owner_scope: str, manual_tx_id: int, plaid_tx_id: str) -> bool:
    hid = current_household_id()
    row = _recon_query(hid).filter_by(
        owner_scope=owner_scope,
        manual_transaction_id=manual_tx_id,
        plaid_transaction_id=plaid_tx_id,
        status=REJECTED_STATUS,
    ).first()
    return row is not None


def _score_candidate(manual_tx: ExpenseTransaction, plaid_tx: PlaidTransaction) -> tuple[int, str]:
    if _manual_direction(manual_tx) != str(plaid_tx.direction or ""):
        return (0, "direction_mismatch")
    if _manual_amount_cents(manual_tx) != int(plaid_tx.amount_cents or 0):
        return (0, "amount_mismatch")

    if manual_tx.local_account_id is not None:
        plaid_account = PlaidAccount.query.filter_by(
            household_id=int(manual_tx.household_id),
            plaid_account_id=plaid_tx.plaid_account_id,
        ).first()
        mapped_local = plaid_account.rung_account_id if plaid_account is not None else None
        if mapped_local is not None and int(mapped_local) != int(manual_tx.local_account_id):
            return (0, "account_mismatch")

    distance = _distance_days(_manual_date(manual_tx), plaid_tx.transaction_date)
    if distance is not None and distance > DATE_WINDOW_DAYS:
        return (0, "date_too_far")

    score = 0
    reason_bits: list[str] = []

    if distance is None:
        score += 10
    elif distance == 0:
        score += 30
        reason_bits.append("same_day")
    elif distance <= 1:
        score += 24
        reason_bits.append("near_day")
    else:
        score += 18
        reason_bits.append("within_window")

    similarity = _merchant_similarity(_manual_text(manual_tx), " ".join([
        str(plaid_tx.merchant_name or ""),
        str(plaid_tx.name or ""),
        str(plaid_tx.description or ""),
    ]))
    if similarity >= 0.66:
        score += 45
        reason_bits.append("merchant_strong")
    elif similarity >= 0.40:
        score += 30
        reason_bits.append("merchant_medium")
    elif similarity >= 0.20:
        score += 15
        reason_bits.append("merchant_light")

    trip = _trip_query(int(manual_tx.household_id)).filter_by(transaction_id=manual_tx.id).first()
    if trip is not None:
        trip_tokens = _normalized_tokens(" ".join([str(trip.retailer or ""), str(trip.store_name or "")]))
        plaid_tokens = _normalized_tokens(" ".join([str(plaid_tx.merchant_name or ""), str(plaid_tx.name or "")]))
        if trip_tokens and plaid_tokens and (trip_tokens & plaid_tokens):
            score += 25
            reason_bits.append("shopping_trip_context")

    if score <= 0:
        return (0, "no_signal")
    return (score, ",".join(reason_bits) or "signal")


def _upsert_proposal(owner_scope: str, household_id: int, manual_tx_id: int, plaid_tx_id: str, score: int) -> TransactionReconciliation:
    row = _recon_query(household_id).filter_by(
        owner_scope=owner_scope,
        manual_transaction_id=manual_tx_id,
        plaid_transaction_id=plaid_tx_id,
    ).first()
    if row is None:
        row = TransactionReconciliation(
            household_id=household_id,
            owner_scope=owner_scope,
            manual_transaction_id=manual_tx_id,
            plaid_transaction_id=plaid_tx_id,
            status=PROPOSAL_STATUS,
        )
    if row.status == PROPOSAL_STATUS:
        row.match_strength = max(int(score), int(row.match_strength or 0))
        db.session.add(row)
    return row


def _plaid_to_category(plaid_tx: PlaidTransaction) -> str:
    if str(plaid_tx.direction or "") == "inflow":
        return "income"
    try:
        cats = json.loads(plaid_tx.category_json or "[]")
    except Exception:
        cats = []
    token_blob = " ".join(str(x or "") for x in (cats or []))
    token_blob = token_blob.lower()
    if "grocery" in token_blob or "supermarket" in token_blob:
        return "grocery"
    text = " ".join([str(plaid_tx.name or ""), str(plaid_tx.merchant_name or "")]).lower()
    if "grocery" in text or "market" in text:
        return "grocery"
    return "discretionary"


def _is_plaid_transfer(plaid_tx: PlaidTransaction) -> bool:
    """Use provider classification only; never guess a transfer from its memo."""
    try:
        categories = json.loads(plaid_tx.category_json or '[]')
    except Exception:
        categories = []
    return any('transfer' in str(value or '').lower() for value in (categories or []))


def _transfer_recon_query(household_id: int):
    return SavingsTransferReconciliation.query.filter_by(household_id=household_id)


def _stage_transfer_reconciliation(owner_scope: str, household_id: int, plaid_tx: PlaidTransaction) -> bool:
    """Stage a Plaid transfer without creating a second economic effect.

    Exact amount/date is useful only to attach a *proposal*.  It is never an
    authorization to link the provider identity or fulfill PYF automatically.
    """
    prior = _transfer_recon_query(household_id).filter_by(plaid_transaction_id=plaid_tx.plaid_transaction_id).first()
    existing = SavingsTransfer.query.filter_by(plaid_transaction_id=plaid_tx.plaid_transaction_id).first()
    if existing is not None:
        return False
    if prior is not None and prior.status == REJECTED_STATUS:
        # KEEP SEPARATE is final and its request already recorded this distinct
        # bank movement.  A later provider replay is therefore a no-op.
        return False
    if prior is not None:
        return prior.status == PROPOSAL_STATUS
    candidates = SavingsTransfer.query.filter(
        SavingsTransfer.household_id == household_id,
        SavingsTransfer.plaid_transaction_id.is_(None),
        SavingsTransfer.superseded_by_transfer_id.is_(None),
        SavingsTransfer.external_direction == 'outflow',
        SavingsTransfer.source_destination_id.is_(None),
        SavingsTransfer.destination_id.isnot(None),
        SavingsTransfer.amount_cents == int(plaid_tx.amount_cents or 0),
    ).all()
    if plaid_tx.transaction_date is not None:
        # economic_date is event authority.  Legacy NULL rows may only fall
        # back to creation metadata until they are reviewed/enriched.
        candidates = [row for row in candidates if (
            (row.economic_date is not None and abs((row.economic_date - plaid_tx.transaction_date).days) <= DATE_WINDOW_DAYS)
            or (row.economic_date is None and row.created_at is not None
                and abs((row.created_at.date() - plaid_tx.transaction_date).days) <= DATE_WINDOW_DAYS)
        )]
    candidate_id = candidates[0].id if len(candidates) == 1 else None
    db.session.add(SavingsTransferReconciliation(
        household_id=household_id, owner_scope=owner_scope,
        savings_transfer_id=candidate_id, plaid_transaction_id=plaid_tx.plaid_transaction_id,
        status=PROPOSAL_STATUS,
    ))
    # A confident manual candidate is held for reviewed identity matching;
    # otherwise this posted bank movement must be represented immediately.
    if candidate_id is None:
        _apply_plaid_transfer_effect(household_id, plaid_tx)
    return True


def _apply_plaid_transfer_effect(household_id: int, plaid_tx: PlaidTransaction) -> SavingsTransfer:
    """Record one provider-identified transfer effect, without a purpose/PYF guess."""
    existing = SavingsTransfer.query.filter_by(plaid_transaction_id=plaid_tx.plaid_transaction_id).first()
    if existing is not None:
        return existing
    amount_cents = int(plaid_tx.amount_cents or 0)
    row = SavingsTransfer(household_id=household_id, operation_id=f'plaid-transfer:{plaid_tx.plaid_transaction_id}',
        plaid_transaction_id=plaid_tx.plaid_transaction_id, amount_cents=amount_cents,
        transfer_type='plaid_observation', purpose='Posted Plaid transfer; destination and PYF linkage require review.',
        economic_date=plaid_tx.transaction_date, external_direction=str(plaid_tx.direction or 'outflow'))
    db.session.add(row); db.session.flush()
    amount = float(Decimal(amount_cents) / Decimal('100'))
    apply_balance_delta(household_id, amount if str(plaid_tx.direction or '') == 'inflow' else -amount)
    return row


def decide_transfer_reconciliation(*, owner_scope: str, savings_transfer_id: int, plaid_transaction_id: str, action: str) -> dict[str, Any]:
    """Approve/reject a staged transfer identity without moving money again."""
    hid = current_household_id()
    if str(action or '').strip().lower() not in {'match', 'keep_separate'}:
        raise ValueError("action must be 'match' or 'keep_separate'.")
    row = _transfer_recon_query(hid).filter_by(plaid_transaction_id=plaid_transaction_id).with_for_update().first()
    plaid = PlaidTransaction.query.filter_by(household_id=hid, owner_scope=owner_scope, plaid_transaction_id=plaid_transaction_id, is_removed=False, is_active_event=True).with_for_update().first()
    transfer = SavingsTransfer.query.filter_by(household_id=hid, id=savings_transfer_id).with_for_update().first()
    if row is None or plaid is None or transfer is None:
        raise ValueError('Transfer reconciliation candidate was not found.')
    requested_action = str(action).lower()
    if row.status == MATCHED_STATUS:
        if requested_action != 'match':
            raise ValueError('Transfer reconciliation is already finalized as matched.')
        return {'status': 'already_matched', 'transfer_id': transfer.id}
    if row.status == REJECTED_STATUS:
        if requested_action != 'keep_separate':
            raise ValueError('Transfer reconciliation is already finalized as kept separate.')
        return {'status': 'already_kept_separate', 'transfer_id': transfer.id}
    if row.savings_transfer_id is not None and int(row.savings_transfer_id) != int(transfer.id):
        raise ValueError('This reviewed proposal is bound to a different savings transfer.')
    if requested_action == 'keep_separate':
        # The decision says these are two real events.  Apply the held provider
        # movement in this same atomic decision, never on a future sync.
        _apply_plaid_transfer_effect(hid, plaid)
        row.status = REJECTED_STATUS; row.user_confirmed = True; db.session.add(row); db.session.commit()
        return {'status': 'kept_separate', 'transfer_id': transfer.id}
    observation = SavingsTransfer.query.filter_by(household_id=hid, plaid_transaction_id=plaid_transaction_id).with_for_update().first()
    if observation is not None and observation.id != transfer.id:
        if observation.amount_cents != transfer.amount_cents:
            raise ValueError('Provider observation and manual transfer amounts differ.')
        if str(observation.external_direction or 'outflow') != 'outflow':
            raise ValueError('An inbound provider transfer cannot be matched to a checking-to-savings transfer.')
        # The observation already owns both provider identity and checking
        # movement.  Preserve the manual row as audit provenance but remove its
        # duplicate economic contribution exactly once.
        observation.destination_id = transfer.destination_id
        observation.source_destination_id = transfer.source_destination_id
        observation.transfer_type = transfer.transfer_type if transfer.transfer_type != 'plaid_observation' else 'deposit'
        observation.purpose = transfer.purpose or observation.purpose
        observation.income_pyf_protection_id = transfer.income_pyf_protection_id
        if observation.economic_date is None:
            observation.economic_date = transfer.economic_date
        transfer.superseded_by_transfer_id = observation.id
        # Only rows explicitly recorded by the served physical-transfer path
        # own a checking debit. Internal allocation ledger rows never do.
        if transfer.external_direction == 'outflow':
            apply_balance_delta(hid, float(Decimal(transfer.amount_cents) / Decimal('100')))
        row.savings_transfer_id = observation.id; row.status = MATCHED_STATUS; row.user_confirmed = True
        db.session.add_all([observation, transfer, row]); db.session.commit()
        return {'status': 'matched', 'transfer_id': observation.id, 'superseded_transfer_id': transfer.id}
    if transfer.plaid_transaction_id not in (None, plaid_transaction_id):
        raise ValueError('Savings transfer already has a different provider identity.')
    # A transfer-classified Plaid event may be linked only through this review.
    transfer.plaid_transaction_id = plaid_transaction_id
    row.savings_transfer_id = transfer.id; row.status = MATCHED_STATUS; row.user_confirmed = True
    db.session.add_all([transfer, row]); db.session.commit()
    return {'status': 'matched', 'transfer_id': transfer.id}


def _apply_plaid_financial_effect(owner_scope: str, household_id: int, plaid_tx: PlaidTransaction) -> ExpenseTransaction:
    existing = _tx_query(household_id).filter_by(plaid_transaction_id=plaid_tx.plaid_transaction_id).first()
    if existing is not None:
        return existing

    amount = float(Decimal(int(plaid_tx.amount_cents or 0)) / Decimal("100"))
    category = _plaid_to_category(plaid_tx)
    desc = str(plaid_tx.merchant_name or plaid_tx.name or plaid_tx.description or "Plaid transaction")[:150]

    tx = ExpenseTransaction(
        household_id=household_id,
        description=desc,
        amount=amount,
        category=category,
        source="plaid_import",
        plaid_transaction_id=plaid_tx.plaid_transaction_id,
        local_account_id=None,
        date=datetime.combine(plaid_tx.transaction_date, datetime.min.time(), tzinfo=timezone.utc)
        if plaid_tx.transaction_date else _utcnow(),
    )
    db.session.add(tx)
    db.session.flush()

    account = None
    plaid_account = PlaidAccount.query.filter_by(
        household_id=household_id,
        plaid_account_id=plaid_tx.plaid_account_id,
    ).first()
    if plaid_account is not None and plaid_account.rung_account_id is not None:
        account = Account.query.filter_by(household_id=household_id, id=plaid_account.rung_account_id).first()
        tx.local_account_id = plaid_account.rung_account_id
    if account is None:
        account = get_household_account(household_id)
        if account is not None:
            tx.local_account_id = account.id
    if account is not None:
        if str(plaid_tx.direction or "") == "inflow":
            apply_balance_delta(household_id, amount)
        else:
            apply_balance_delta(household_id, -amount)

    if category == "income":
        establish_for_income(tx)

    return tx


def _migrate_pending_identity(owner_scope: str, household_id: int, plaid_tx: PlaidTransaction) -> None:
    pending_id = str(plaid_tx.replaces_pending_transaction_id or "").strip()
    if not pending_id:
        return

    prior_effect = _tx_query(household_id).filter_by(plaid_transaction_id=pending_id).first()
    if prior_effect is not None and not prior_effect.plaid_transaction_id == plaid_tx.plaid_transaction_id:
        if _tx_query(household_id).filter_by(plaid_transaction_id=plaid_tx.plaid_transaction_id).first() is None:
            prior_effect.plaid_transaction_id = plaid_tx.plaid_transaction_id
            prior_effect.description = str(plaid_tx.merchant_name or plaid_tx.name or prior_effect.description)[:150]
            prior_effect.amount = float(Decimal(int(plaid_tx.amount_cents or 0)) / Decimal("100"))
            prior_effect.category = _plaid_to_category(plaid_tx)
            db.session.add(prior_effect)
            if str(prior_effect.category or "").lower() == "income":
                establish_for_income(prior_effect)

    rows = _recon_query(household_id).filter_by(owner_scope=owner_scope, plaid_transaction_id=pending_id).all()
    for row in rows:
        dup = _recon_query(household_id).filter_by(
            owner_scope=owner_scope,
            manual_transaction_id=row.manual_transaction_id,
            plaid_transaction_id=plaid_tx.plaid_transaction_id,
        ).first()
        if dup is not None:
            db.session.delete(row)
            continue
        row.plaid_transaction_id = plaid_tx.plaid_transaction_id
        db.session.add(row)


def _manual_candidates_for_plaid(owner_scope: str, household_id: int, plaid_tx: PlaidTransaction) -> list[MatchCandidate]:
    manual_rows = (
        _tx_query(household_id)
        .filter(ExpenseTransaction.plaid_transaction_id.is_(None))
        .order_by(ExpenseTransaction.date.desc(), ExpenseTransaction.id.desc())
        .limit(300)
        .all()
    )

    candidates: list[MatchCandidate] = []
    for manual in manual_rows:
        if not _is_manual_candidate(manual):
            continue
        if _rejected_pair_exists(owner_scope, manual.id, plaid_tx.plaid_transaction_id):
            continue
        score, reason = _score_candidate(manual, plaid_tx)
        if score >= STRONG_MATCH_SCORE:
            candidates.append(MatchCandidate(plaid_transaction_id=plaid_tx.plaid_transaction_id, score=score, reason=reason))
            _upsert_proposal(owner_scope, household_id, manual.id, plaid_tx.plaid_transaction_id, score)

    return candidates


def project_plaid_transactions(owner_scope: str, plaid_item_id: Optional[int] = None) -> dict[str, int]:
    hid = current_household_id()
    q = PlaidTransaction.query.filter_by(household_id=hid, owner_scope=owner_scope, is_removed=False, is_active_event=True)
    if plaid_item_id is not None:
        q = q.filter_by(plaid_item_id=plaid_item_id)
    rows = q.order_by(PlaidTransaction.id.asc()).all()

    stats = {"applied": 0, "proposed": 0, "skipped": 0}

    for plaid_tx in rows:
        # A pending Plaid row is bank-provided context, not a confirmed cash
        # event.  In particular it must not establish actual income or its
        # income-linked PYF protection.  When Plaid posts the replacement,
        # its pending_transaction_id is migrated below before the one actual
        # economic effect is applied.
        if plaid_tx.is_pending:
            stats["skipped"] += 1
            continue
        _migrate_pending_identity(owner_scope, hid, plaid_tx)

        if _tx_query(hid).filter_by(plaid_transaction_id=plaid_tx.plaid_transaction_id).first() is not None:
            stats["skipped"] += 1
            continue

        matched = _recon_query(hid).filter_by(
            owner_scope=owner_scope,
            plaid_transaction_id=plaid_tx.plaid_transaction_id,
            status=MATCHED_STATUS,
        ).first()
        if matched is not None:
            manual = _tx_query(hid).filter_by(id=matched.manual_transaction_id).first()
            if manual is not None and not manual.plaid_transaction_id:
                manual.plaid_transaction_id = plaid_tx.plaid_transaction_id
                db.session.add(manual)
            stats["skipped"] += 1
            continue

        if _is_plaid_transfer(plaid_tx):
            if _stage_transfer_reconciliation(owner_scope, hid, plaid_tx):
                stats['proposed'] += 1
            else:
                stats['skipped'] += 1
            continue

        candidates = _manual_candidates_for_plaid(owner_scope, hid, plaid_tx)
        if candidates:
            stats["proposed"] += 1
            continue

        _apply_plaid_financial_effect(owner_scope, hid, plaid_tx)
        stats["applied"] += 1

    db.session.commit()
    return stats


def list_reconciliation_proposals(owner_scope: str) -> list[dict[str, Any]]:
    hid = current_household_id()
    rows = (
        _recon_query(hid)
        .filter_by(owner_scope=owner_scope, status=PROPOSAL_STATUS)
        .order_by(TransactionReconciliation.match_strength.desc(), TransactionReconciliation.id.asc())
        .all()
    )

    payload: list[dict[str, Any]] = []
    for row in rows:
        manual = _tx_query(hid).filter_by(id=row.manual_transaction_id).first()
        plaid = PlaidTransaction.query.filter_by(
            household_id=hid,
            owner_scope=owner_scope,
            plaid_transaction_id=row.plaid_transaction_id,
            is_removed=False,
            is_active_event=True,
        ).first()
        if manual is None or plaid is None:
            continue
        payload.append({
            "id": row.id,
            "status": row.status,
            "match_strength": int(row.match_strength or 0),
            "manual": {
                "transaction_id": manual.id,
                "description": manual.description,
                "amount": round(float(manual.amount or 0.0), 2),
                "category": manual.category,
                "date": manual.date.isoformat() if manual.date else None,
            },
            "bank": plaid.to_summary(),
        })
    transfer_rows = (
        _transfer_recon_query(hid).filter_by(owner_scope=owner_scope, status=PROPOSAL_STATUS)
        .order_by(SavingsTransferReconciliation.id.asc()).all()
    )
    for row in transfer_rows:
        plaid = PlaidTransaction.query.filter_by(household_id=hid, owner_scope=owner_scope,
            plaid_transaction_id=row.plaid_transaction_id, is_removed=False, is_active_event=True).first()
        if plaid is None:
            continue
        transfer = db.session.get(SavingsTransfer, row.savings_transfer_id) if row.savings_transfer_id else None
        if transfer is None:
            # A Plaid-first observation may be reviewed after a manual record
            # is added. Expose only one date/amount-compatible local candidate;
            # ambiguity remains safely un-actionable.
            candidates = SavingsTransfer.query.filter(
                SavingsTransfer.household_id == hid,
                SavingsTransfer.plaid_transaction_id.is_(None),
                SavingsTransfer.superseded_by_transfer_id.is_(None),
                SavingsTransfer.external_direction == 'outflow',
                SavingsTransfer.amount_cents == int(plaid.amount_cents or 0),
                SavingsTransfer.destination_id.isnot(None),
            ).all()
            candidates = [candidate for candidate in candidates if (
                plaid.transaction_date is None or (
                    candidate.economic_date is not None and abs((candidate.economic_date - plaid.transaction_date).days) <= DATE_WINDOW_DAYS
                ) or (
                    candidate.economic_date is None and candidate.created_at is not None and abs((candidate.created_at.date() - plaid.transaction_date).days) <= DATE_WINDOW_DAYS
                )
            )]
            transfer = candidates[0] if len(candidates) == 1 else None
        if transfer is None:
            continue
        payload.append({
            'id': f'transfer:{row.id}', 'kind': 'transfer', 'status': row.status,
            'match_strength': 100, 'transfer': {
                'transfer_id': transfer.id, 'purpose': transfer.purpose or 'Savings transfer',
                'amount_cents': int(transfer.amount_cents),
                'economic_date': transfer.economic_date.isoformat() if transfer.economic_date else None,
                'destination_id': transfer.destination_id,
            }, 'bank': plaid.to_summary(),
        })
    return payload


def _find_proposal(owner_scope: str, manual_tx_id: int, plaid_tx_id: str) -> Optional[TransactionReconciliation]:
    return _recon_query(current_household_id()).filter_by(
        owner_scope=owner_scope,
        manual_transaction_id=manual_tx_id,
        plaid_transaction_id=plaid_tx_id,
    ).first()


def decide_reconciliation_pair(
    *,
    owner_scope: str,
    manual_transaction_id: int,
    plaid_transaction_id: str,
    action: str,
    user_id: str,
) -> dict[str, Any]:
    action_key = str(action or "").strip().lower()
    if action_key not in {"match", "keep_separate"}:
        raise ValueError("action must be 'match' or 'keep_separate'.")

    hid = current_household_id()
    if action_key == "match":
        # Two-attempt optimistic retry for concurrent process races.
        for attempt in range(2):
            manual = _tx_query(hid).filter_by(id=manual_transaction_id).with_for_update().first()
            plaid = (
                PlaidTransaction.query
                .filter_by(
                    household_id=hid,
                    owner_scope=owner_scope,
                    plaid_transaction_id=plaid_transaction_id,
                    is_removed=False,
                    is_active_event=True,
                )
                .with_for_update()
                .first()
            )
            if manual is None or plaid is None:
                raise ValueError("Manual or Plaid transaction was not found.")

            row = (
                _recon_query(hid)
                .filter_by(
                    owner_scope=owner_scope,
                    manual_transaction_id=manual_transaction_id,
                    plaid_transaction_id=plaid_transaction_id,
                )
                .with_for_update()
                .first()
            )
            if row is None:
                score, _ = _score_candidate(manual, plaid)
                if score < STRONG_MATCH_SCORE:
                    raise ValueError("Transactions are not an eligible reconciliation pair.")
                row = TransactionReconciliation(
                    household_id=hid,
                    owner_scope=owner_scope,
                    manual_transaction_id=manual_transaction_id,
                    plaid_transaction_id=plaid_transaction_id,
                    status=PROPOSAL_STATUS,
                    match_strength=int(max(score, 0)),
                )
                db.session.add(row)
                try:
                    db.session.flush()
                except IntegrityError:
                    db.session.rollback()
                    if attempt == 0:
                        continue
                    row = _find_proposal(owner_scope, manual_transaction_id, plaid_transaction_id)
                    if row is not None and row.status == MATCHED_STATUS:
                        return {"status": "already_matched", "pair": row.to_summary()}
                    raise

            if row.status == MATCHED_STATUS:
                return {"status": "already_matched", "pair": row.to_summary()}
            if row.status == REJECTED_STATUS:
                raise ValueError("Transactions were explicitly kept separate.")

            # Lock all rows that currently claim this plaid identity so we can
            # collapse duplicate economic effect atomically.
            duplicate_effects = (
                _tx_query(hid)
                .filter_by(plaid_transaction_id=plaid_transaction_id)
                .with_for_update()
                .all()
            )
            for duplicate_effect in duplicate_effects:
                if duplicate_effect.id == manual.id:
                    continue
                amt = float(duplicate_effect.amount or 0.0)
                if get_household_account(hid) is not None:
                    if str(plaid.direction or "") == "inflow":
                        apply_balance_delta(hid, -amt)
                    else:
                        apply_balance_delta(hid, amt)
                db.session.delete(duplicate_effect)

            # Apply deletes before assigning plaid identity to the manual row
            # so UNIQUE(plaid_transaction_id) cannot fail on flush ordering.
            db.session.flush()

            manual.plaid_transaction_id = plaid_transaction_id
            db.session.add(manual)

            row.status = MATCHED_STATUS
            row.user_confirmed = True
            row.confirmation_action = "match"
            row.confirmed_at = _utcnow()
            db.session.add(row)

            try:
                db.session.commit()
                return {"status": "matched", "pair": row.to_summary()}
            except IntegrityError:
                db.session.rollback()
                if attempt == 0:
                    continue
                # Winner committed while this request was in flight.
                manual_after = _tx_query(hid).filter_by(id=manual_transaction_id).first()
                row_after = _find_proposal(owner_scope, manual_transaction_id, plaid_transaction_id)
                if (
                    manual_after is not None
                    and manual_after.plaid_transaction_id == plaid_transaction_id
                    and row_after is not None
                    and row_after.status == MATCHED_STATUS
                ):
                    return {"status": "already_matched", "pair": row_after.to_summary()}
                raise

        raise RuntimeError("Could not resolve reconciliation match race safely.")

    manual = _tx_query(hid).filter_by(id=manual_transaction_id).first()
    plaid = PlaidTransaction.query.filter_by(
        household_id=hid,
        owner_scope=owner_scope,
        plaid_transaction_id=plaid_transaction_id,
        is_removed=False,
        is_active_event=True,
    ).first()
    if manual is None or plaid is None:
        raise ValueError("Manual or Plaid transaction was not found.")

    row = _find_proposal(owner_scope, manual_transaction_id, plaid_transaction_id)
    if row is None:
        score, _ = _score_candidate(manual, plaid)
        row = TransactionReconciliation(
            household_id=hid,
            owner_scope=owner_scope,
            manual_transaction_id=manual_transaction_id,
            plaid_transaction_id=plaid_transaction_id,
            status=PROPOSAL_STATUS,
            match_strength=int(max(score, 0)),
        )

    # keep_separate
    if row.status == REJECTED_STATUS:
        return {"status": "already_kept_separate", "pair": row.to_summary()}

    row.status = REJECTED_STATUS
    row.user_confirmed = True
    row.confirmation_action = "keep_separate"
    row.confirmed_at = _utcnow()
    db.session.add(row)

    _apply_plaid_financial_effect(owner_scope, hid, plaid)
    db.session.commit()
    return {"status": "kept_separate", "pair": row.to_summary()}


def detect_plaid_candidates_for_manual_input(
    *,
    owner_scope: str,
    amount: float,
    direction: str,
    merchant_or_description: str,
    transaction_date: Optional[str],
) -> list[dict[str, Any]]:
    hid = current_household_id()
    amount_cents = _to_cents(amount)
    tx_date: Optional[date] = None
    if transaction_date:
        try:
            tx_date = date.fromisoformat(str(transaction_date))
        except Exception:
            tx_date = None

    rows = (
        PlaidTransaction.query
        .filter_by(household_id=hid, owner_scope=owner_scope, is_removed=False, is_active_event=True, direction=direction)
        .filter(PlaidTransaction.amount_cents == amount_cents)
        .order_by(PlaidTransaction.transaction_date.desc(), PlaidTransaction.id.desc())
        .limit(100)
        .all()
    )

    results: list[dict[str, Any]] = []
    for row in rows:
        if _tx_query(hid).filter_by(plaid_transaction_id=row.plaid_transaction_id).first() is None:
            # If it is not yet financially applied, allow it as candidate anyway.
            pass
        if tx_date is not None and row.transaction_date is not None:
            if abs((tx_date - row.transaction_date).days) > DATE_WINDOW_DAYS:
                continue

        similarity = _merchant_similarity(
            merchant_or_description,
            " ".join([str(row.merchant_name or ""), str(row.name or ""), str(row.description or "")]),
        )
        if similarity < 0.40:
            continue

        results.append({
            "plaid_transaction_id": row.plaid_transaction_id,
            "name": row.name,
            "merchant_name": row.merchant_name,
            "transaction_date": row.transaction_date.isoformat() if row.transaction_date else None,
            "amount": round(float(Decimal(int(row.amount_cents or 0)) / Decimal("100")), 2),
            "direction": row.direction,
            "match_score": int(round(similarity * 100)),
        })

    return results


def keep_separate_after_manual_creation(
    *,
    owner_scope: str,
    manual_transaction_id: int,
    plaid_transaction_id: str,
) -> None:
    hid = current_household_id()
    row = _find_proposal(owner_scope, manual_transaction_id, plaid_transaction_id)
    if row is None:
        row = TransactionReconciliation(
            household_id=hid,
            owner_scope=owner_scope,
            manual_transaction_id=manual_transaction_id,
            plaid_transaction_id=plaid_transaction_id,
        )
    row.status = REJECTED_STATUS
    row.user_confirmed = True
    row.confirmation_action = "record_another"
    row.confirmed_at = _utcnow()
    db.session.add(row)


def ensure_plaid_effect_exists(*, owner_scope: str, plaid_transaction_id: str) -> None:
    hid = current_household_id()
    tx = PlaidTransaction.query.filter_by(
        household_id=hid,
        owner_scope=owner_scope,
        plaid_transaction_id=plaid_transaction_id,
        is_removed=False,
        is_active_event=True,
    ).first()
    if tx is None:
        return
    if tx.is_pending:
        return
    _migrate_pending_identity(owner_scope, hid, tx)
    _apply_plaid_financial_effect(owner_scope, hid, tx)
