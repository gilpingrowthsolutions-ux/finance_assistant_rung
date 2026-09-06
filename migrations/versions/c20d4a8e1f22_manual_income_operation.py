"""Durable manual financial-operation identity."""
from alembic import op
import sqlalchemy as sa

revision = 'c20d4a8e1f22'
down_revision = 'c19f2a7d4e11'
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table('expense_transactions') as batch:
        batch.add_column(sa.Column('operation_id', sa.String(120), nullable=True))
        batch.create_unique_constraint('uq_expense_transaction_household_operation', ['household_id', 'operation_id'])

def downgrade():
    with op.batch_alter_table('expense_transactions') as batch:
        batch.drop_constraint('uq_expense_transaction_household_operation', type_='unique')
        batch.drop_column('operation_id')
