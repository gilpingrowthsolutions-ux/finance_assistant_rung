"""Durable reviewed Plaid identity for savings transfers."""
from alembic import op
import sqlalchemy as sa

revision = 'e22f6c0a4b44'
down_revision = 'd21e5b9f3a33'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('savings_transfer') as batch:
        batch.add_column(sa.Column('plaid_transaction_id', sa.String(120), nullable=True))
        batch.create_unique_constraint('uq_savings_transfer_plaid_transaction_id', ['plaid_transaction_id'])
    op.create_table(
        'savings_transfer_reconciliation',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('household_id', sa.Integer(), nullable=False),
        sa.Column('owner_scope', sa.String(80), nullable=False, server_default='anonymous'),
        sa.Column('savings_transfer_id', sa.Integer(), nullable=True),
        sa.Column('plaid_transaction_id', sa.String(120), nullable=False),
        sa.Column('status', sa.String(30), nullable=False, server_default='proposed'),
        sa.Column('user_confirmed', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(), nullable=False), sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['household_id'], ['household.id']),
        sa.ForeignKeyConstraint(['savings_transfer_id'], ['savings_transfer.id']),
        sa.UniqueConstraint('household_id', 'plaid_transaction_id', name='uq_savings_transfer_recon_household_plaid'),
        sa.CheckConstraint("status IN ('proposed','matched','rejected')", name='ck_savings_transfer_recon_status'),
    )
    op.create_index('ix_savings_transfer_reconciliation_household_id', 'savings_transfer_reconciliation', ['household_id'])


def downgrade():
    op.drop_index('ix_savings_transfer_reconciliation_household_id', table_name='savings_transfer_reconciliation')
    op.drop_table('savings_transfer_reconciliation')
    with op.batch_alter_table('savings_transfer') as batch:
        batch.drop_constraint('uq_savings_transfer_plaid_transaction_id', type_='unique')
        batch.drop_column('plaid_transaction_id')
