"""Disposable PostgreSQL migration and parallel PYF qualification.

Run directly with ``venv/bin/python tests/owner_beta_postgres_qualification.py``.
It never contacts a system or production database.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading

import testing.postgresql


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(ROOT, 'venv', 'bin', 'python')


def main() -> None:
    with testing.postgresql.Postgresql() as postgres:
        url = postgres.url()
        env = dict(os.environ, DATABASE_URL=url, RUNG_ENV='development', FLASK_APP='app.py',
                   SECRET_KEY='owner-beta-postgres-qualification-secret', PLAID_ENABLED='0')
        env.pop('RUNG_DB_PATH', None)
        migration = subprocess.run(
            [PYTHON, '-m', 'flask', 'db', 'upgrade'], cwd=ROOT, env=env,
            text=True, capture_output=True, check=False,
        )
        assert migration.returncode == 0, migration.stderr or migration.stdout

        os.environ.update({key: value for key, value in env.items() if key in {'DATABASE_URL', 'RUNG_ENV', 'SECRET_KEY', 'PLAID_ENABLED'}})
        os.environ.pop('RUNG_DB_PATH', None)
        from sqlalchemy import inspect
        from app import app
        from extensions import db
        from models import (Account, ExpenseTransaction, Household, IncomePyfProtection,
                            SavingsDestination, SavingsTransfer, UserSetting)
        from services.household_context import household_id
        from services.income_pyf import establish_for_income

        with app.app_context():
            inspector = inspect(db.engine)
            assert inspector.get_table_names()
            for table, column in [
                ('expense_transactions', 'operation_id'),
                ('income_pyf_protection', 'income_transaction_id'),
                ('recurring_required_obligation', 'recurrence'),
                ('savings_transfer', 'income_pyf_protection_id'),
                ('savings_transfer', 'plaid_transaction_id'),
                ('savings_transfer', 'economic_date'),
                ('savings_transfer', 'superseded_by_transfer_id'),
                ('savings_transfer_reconciliation', 'plaid_transaction_id'),
            ]:
                assert column in {item['name'] for item in inspector.get_columns(table)}
            pyf_uniques = {item['name'] for item in inspector.get_unique_constraints('income_pyf_protection')}
            assert {'uq_income_pyf_household_income', 'uq_income_pyf_household_operation'} <= pyf_uniques
            transfer_fks = inspector.get_foreign_keys('savings_transfer')
            assert any(item['referred_table'] == 'income_pyf_protection' for item in transfer_fks)
            assert any(item['referred_table'] == 'savings_transfer' for item in transfer_fks)
            assert any(item['name'] == 'ck_savings_transfer_not_self_superseded' for item in inspector.get_check_constraints('savings_transfer'))
            assert any(item['name'] == 'ck_income_pyf_amounts' for item in inspector.get_check_constraints('income_pyf_protection'))

            hid = household_id()
            db.session.add_all([
                Account(household_id=hid, checking_balance=1800, pay_period_days=14),
                UserSetting(household_id=hid, key='pyf_long_term_target_percent', value='10'),
            ])
            db.session.flush()
            income = ExpenseTransaction(household_id=hid, description='Committed payroll', amount=1800,
                                        category='income', source='manual', operation_id='pg-income')
            db.session.add(income); db.session.commit()
            income_id = income.id

        barrier = threading.Barrier(2)
        establish_errors: list[str] = []
        def establish_worker(index: int) -> None:
            try:
                with app.app_context():
                    tx = db.session.get(ExpenseTransaction, income_id)
                    # This unrelated caller write proves a unique-key race
                    # does not roll back the whole caller transaction.
                    db.session.add(UserSetting(household_id=hid, key=f'pg-worker-{index}', value='survives'))
                    barrier.wait()
                    establish_for_income(tx)
                    db.session.commit()
            except Exception as exc:  # surfaced below
                establish_errors.append(f'{type(exc).__name__}: {exc}')

        workers = [threading.Thread(target=establish_worker, args=(index,)) for index in range(2)]
        [worker.start() for worker in workers]; [worker.join() for worker in workers]
        assert not establish_errors, establish_errors
        with app.app_context():
            assert IncomePyfProtection.query.filter_by(household_id=hid, income_transaction_id=income_id).count() == 1
            assert UserSetting.query.filter(UserSetting.key.like('pg-worker-%')).count() == 2
            protection = IncomePyfProtection.query.filter_by(household_id=hid, income_transaction_id=income_id).one()
            db.session.add(SavingsDestination(household_id=hid, kind='reserve', name='PG savings', priority=1))
            db.session.commit()
            destination_id, protection_id = SavingsDestination.query.filter_by(household_id=hid, name='PG savings').one().id, protection.id

        barrier = threading.Barrier(2)
        fulfillment_statuses: list[int] = []
        fulfillment_errors: list[str] = []
        def fulfill_worker(index: int) -> None:
            try:
                client = app.test_client()
                barrier.wait()
                response = client.post('/api/savings/transfer', json={
                    'confirm': True, 'operation_id': f'pg-fulfill-{index}', 'amount_cents': 18000,
                    'destination_id': destination_id, 'transfer_type': 'deposit',
                    'income_pyf_protection_id': protection_id,
                })
                fulfillment_statuses.append(response.status_code)
            except Exception as exc:
                fulfillment_errors.append(f'{type(exc).__name__}: {exc}')

        workers = [threading.Thread(target=fulfill_worker, args=(index,)) for index in range(2)]
        [worker.start() for worker in workers]; [worker.join() for worker in workers]
        assert not fulfillment_errors, fulfillment_errors
        assert sorted(fulfillment_statuses) == [200, 400], fulfillment_statuses
        with app.app_context():
            protection = db.session.get(IncomePyfProtection, protection_id)
            account = Account.query.filter_by(household_id=hid).one()
            assert (protection.fulfilled_cents, protection.status) == (18000, 'fulfilled')
            assert SavingsTransfer.query.filter_by(household_id=hid, income_pyf_protection_id=protection_id).count() == 1
            assert account.checking_balance == 1620
        print(f'PASS disposable_postgres={url} migration_head=b25c9d3e7f77 establish=1 fulfillment_statuses={sorted(fulfillment_statuses)}')


if __name__ == '__main__':
    main()
