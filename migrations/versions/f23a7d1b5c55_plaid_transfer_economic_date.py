"""Represent posted Plaid transfer checking activity without guessing purpose."""
from alembic import op
import sqlalchemy as sa

revision = 'f23a7d1b5c55'
down_revision = 'e22f6c0a4b44'
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table('savings_transfer') as batch:
        batch.add_column(sa.Column('economic_date', sa.Date(), nullable=True))
        batch.add_column(sa.Column('external_direction', sa.String(20), nullable=True))
        batch.drop_constraint('ck_savings_transfer_type', type_='check')
        batch.create_check_constraint('ck_savings_transfer_type', "transfer_type IN ('pyf_allocation','deposit','transfer','reserve_use','goal_use','withdrawal','adjustment','plaid_observation')")

def downgrade():
    with op.batch_alter_table('savings_transfer') as batch:
        batch.drop_constraint('ck_savings_transfer_type', type_='check')
        batch.create_check_constraint('ck_savings_transfer_type', "transfer_type IN ('pyf_allocation','deposit','transfer','reserve_use','goal_use','withdrawal','adjustment')")
        batch.drop_column('external_direction')
        batch.drop_column('economic_date')
