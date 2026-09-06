"""Recurring required-Need authority."""
from alembic import op
import sqlalchemy as sa

revision = 'c19f2a7d4e11'
down_revision = 'b18d5e7f9a01'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('recurring_required_obligation',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('household_id', sa.Integer(), nullable=False), sa.Column('name', sa.String(100), nullable=False),
        sa.Column('category', sa.String(50), nullable=False, server_default='required'),
        sa.Column('expected_amount_cents', sa.Integer(), nullable=True), sa.Column('next_due_date', sa.DateTime(), nullable=False),
        sa.Column('recurrence', sa.String(16), nullable=False), sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('source', sa.String(40), nullable=False, server_default='user_confirmed'),
        sa.Column('created_at', sa.DateTime(), nullable=False), sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['household_id'], ['household.id']),
        sa.CheckConstraint("recurrence IN ('weekly','biweekly','monthly','quarterly','yearly')", name='ck_recurring_required_recurrence'),
        sa.CheckConstraint('expected_amount_cents IS NULL OR expected_amount_cents > 0', name='ck_recurring_required_amount'),
    )
    op.create_index('ix_recurring_required_obligation_household_id', 'recurring_required_obligation', ['household_id'])
    with op.batch_alter_table('bill') as batch:
        batch.add_column(sa.Column('recurring_obligation_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_bill_recurring_required_obligation', 'recurring_required_obligation', ['recurring_obligation_id'], ['id'])
        batch.create_index('ix_bill_recurring_obligation_id', ['recurring_obligation_id'])

def downgrade():
    with op.batch_alter_table('bill') as batch:
        batch.drop_index('ix_bill_recurring_obligation_id'); batch.drop_constraint('fk_bill_recurring_required_obligation', type_='foreignkey'); batch.drop_column('recurring_obligation_id')
    op.drop_index('ix_recurring_required_obligation_household_id', table_name='recurring_required_obligation')
    op.drop_table('recurring_required_obligation')
