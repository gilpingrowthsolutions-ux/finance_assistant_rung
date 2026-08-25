"""Packages 13-14 household savings authority.

Revision ID: e4a7c2d91f30
Revises: c6a4e2f9b731
"""
from alembic import op
import sqlalchemy as sa

revision = "e4a7c2d91f30"
down_revision = "c6a4e2f9b731"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('savings_destination',
        sa.Column('id', sa.Integer(), primary_key=True), sa.Column('household_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(30), nullable=False), sa.Column('name', sa.String(120), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='100'), sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False), sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['household_id'], ['household.id']),
        sa.UniqueConstraint('household_id','kind','name', name='uq_savings_destination_household_kind_name'),
        sa.CheckConstraint("kind IN ('goal','reserve','flexible','wealth_cash','wealth_investment')", name='ck_savings_destination_kind'))
    op.create_index('ix_savings_destination_household_id', 'savings_destination', ['household_id'])
    op.create_table('savings_goal',
        sa.Column('id', sa.Integer(), primary_key=True), sa.Column('household_id', sa.Integer(), nullable=False), sa.Column('destination_id', sa.Integer(), nullable=False, unique=True), sa.Column('create_operation_id', sa.String(120), nullable=False),
        sa.Column('target_cents', sa.Integer(), nullable=False), sa.Column('target_date', sa.Date(), nullable=True), sa.Column('status', sa.String(20), nullable=False, server_default='active'),
        sa.Column('completed_at', sa.DateTime(), nullable=True), sa.Column('created_at', sa.DateTime(), nullable=False), sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['household_id'], ['household.id']), sa.ForeignKeyConstraint(['destination_id'], ['savings_destination.id']), sa.UniqueConstraint('household_id','create_operation_id', name='uq_savings_goal_household_create_operation'),
        sa.CheckConstraint('target_cents > 0', name='ck_savings_goal_target_positive'), sa.CheckConstraint("status IN ('active','paused','completed')", name='ck_savings_goal_status'))
    op.create_index('ix_savings_goal_household_id', 'savings_goal', ['household_id'])
    op.create_table('savings_reserve',
        sa.Column('id', sa.Integer(), primary_key=True), sa.Column('household_id', sa.Integer(), nullable=False), sa.Column('destination_id', sa.Integer(), nullable=False, unique=True), sa.Column('create_operation_id', sa.String(120), nullable=False),
        sa.Column('category', sa.String(40), nullable=False), sa.Column('target_cents', sa.Integer(), nullable=False), sa.Column('status', sa.String(20), nullable=False, server_default='active'),
        sa.Column('created_at', sa.DateTime(), nullable=False), sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['household_id'], ['household.id']), sa.ForeignKeyConstraint(['destination_id'], ['savings_destination.id']), sa.UniqueConstraint('household_id','create_operation_id', name='uq_savings_reserve_household_create_operation'),
        sa.CheckConstraint('target_cents > 0', name='ck_savings_reserve_target_positive'), sa.CheckConstraint("status IN ('active','paused')", name='ck_savings_reserve_status'))
    op.create_index('ix_savings_reserve_household_id', 'savings_reserve', ['household_id'])
    op.create_table('savings_transfer',
        sa.Column('id', sa.Integer(), primary_key=True), sa.Column('household_id', sa.Integer(), nullable=False), sa.Column('operation_id', sa.String(120), nullable=False),
        sa.Column('source_destination_id', sa.Integer(), nullable=True), sa.Column('destination_id', sa.Integer(), nullable=True), sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('transfer_type', sa.String(30), nullable=False), sa.Column('purpose', sa.String(200), nullable=True), sa.Column('metadata_version', sa.String(30), nullable=False, server_default='savings_ledger_v1'), sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['household_id'], ['household.id']), sa.ForeignKeyConstraint(['source_destination_id'], ['savings_destination.id']), sa.ForeignKeyConstraint(['destination_id'], ['savings_destination.id']),
        sa.UniqueConstraint('household_id','operation_id', name='uq_savings_transfer_household_operation'), sa.CheckConstraint('amount_cents > 0', name='ck_savings_transfer_amount_positive'),
        sa.CheckConstraint('source_destination_id IS NULL OR destination_id IS NULL OR source_destination_id <> destination_id', name='ck_savings_transfer_distinct_destinations'),
        sa.CheckConstraint("transfer_type IN ('pyf_allocation','deposit','transfer','reserve_use','goal_use','withdrawal','adjustment')", name='ck_savings_transfer_type'))
    op.create_index('ix_savings_transfer_household_id', 'savings_transfer', ['household_id'])
    op.create_table('savings_allocation_run',
        sa.Column('id', sa.Integer(), primary_key=True), sa.Column('household_id', sa.Integer(), nullable=False), sa.Column('operation_id', sa.String(120), nullable=False), sa.Column('cycle_key', sa.String(80), nullable=False),
        sa.Column('feasible_cents', sa.Integer(), nullable=False), sa.Column('allocated_cents', sa.Integer(), nullable=False), sa.Column('authority', sa.String(40), nullable=False, server_default='canonical_pyf_v1'), sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['household_id'], ['household.id']), sa.UniqueConstraint('household_id','operation_id', name='uq_savings_allocation_household_operation'), sa.UniqueConstraint('household_id','cycle_key', name='uq_savings_allocation_household_cycle'),
        sa.CheckConstraint('feasible_cents >= 0 AND allocated_cents >= 0 AND allocated_cents <= feasible_cents', name='ck_savings_allocation_bounds'))
    op.create_index('ix_savings_allocation_run_household_id', 'savings_allocation_run', ['household_id'])


def downgrade() -> None:
    op.drop_table('savings_allocation_run'); op.drop_table('savings_transfer'); op.drop_table('savings_reserve'); op.drop_table('savings_goal'); op.drop_table('savings_destination')
