"""Link reviewed physical savings transfers to income PYF protections.

Revision ID: d21e5b9f3a33
Revises: c20d4a8e1f22
"""
from alembic import op
import sqlalchemy as sa

revision = 'd21e5b9f3a33'
down_revision = 'c20d4a8e1f22'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('savings_transfer') as batch:
        batch.add_column(sa.Column('income_pyf_protection_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_savings_transfer_income_pyf_protection', 'income_pyf_protection', ['income_pyf_protection_id'], ['id'])
        batch.create_index('ix_savings_transfer_income_pyf_protection_id', ['income_pyf_protection_id'])


def downgrade():
    with op.batch_alter_table('savings_transfer') as batch:
        batch.drop_index('ix_savings_transfer_income_pyf_protection_id')
        batch.drop_constraint('fk_savings_transfer_income_pyf_protection', type_='foreignkey')
        batch.drop_column('income_pyf_protection_id')
