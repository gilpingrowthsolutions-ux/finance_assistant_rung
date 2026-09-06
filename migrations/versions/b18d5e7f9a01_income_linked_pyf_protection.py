"""Canonical income-linked PYF protection.

Revision ID: b18d5e7f9a01
Revises: b94e8f1a2d3c, f5b1c7d9e2a4
"""
from alembic import op
import sqlalchemy as sa

revision = 'b18d5e7f9a01'
down_revision = ('b94e8f1a2d3c', 'f5b1c7d9e2a4')
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('income_pyf_protection',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('household_id', sa.Integer(), nullable=False),
        sa.Column('income_transaction_id', sa.Integer(), nullable=False),
        sa.Column('operation_id', sa.String(120), nullable=False),
        sa.Column('target_percent', sa.Numeric(8,4), nullable=False),
        sa.Column('target_cents', sa.Integer(), nullable=False),
        sa.Column('protected_cents', sa.Integer(), nullable=False),
        sa.Column('fulfilled_cents', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(20), nullable=False, server_default='active'),
        sa.Column('created_at', sa.DateTime(), nullable=False), sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['household_id'], ['household.id']),
        sa.ForeignKeyConstraint(['income_transaction_id'], ['expense_transactions.id']),
        sa.UniqueConstraint('household_id','income_transaction_id', name='uq_income_pyf_household_income'),
        sa.UniqueConstraint('household_id','operation_id', name='uq_income_pyf_household_operation'),
        sa.CheckConstraint('target_cents >= 0 AND protected_cents >= 0 AND fulfilled_cents >= 0 AND fulfilled_cents <= protected_cents', name='ck_income_pyf_amounts'),
        sa.CheckConstraint("status IN ('active','fulfilled','reversed')", name='ck_income_pyf_status'),
    )
    op.create_index('ix_income_pyf_protection_household_id', 'income_pyf_protection', ['household_id'])

def downgrade():
    op.drop_index('ix_income_pyf_protection_household_id', table_name='income_pyf_protection')
    op.drop_table('income_pyf_protection')
