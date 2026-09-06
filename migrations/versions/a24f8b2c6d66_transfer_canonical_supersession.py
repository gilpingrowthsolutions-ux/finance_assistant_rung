"""Retain superseded duplicate transfer provenance without double counting."""
from alembic import op
import sqlalchemy as sa

revision = 'a24f8b2c6d66'
down_revision = 'f23a7d1b5c55'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('savings_transfer') as batch:
        batch.add_column(sa.Column('superseded_by_transfer_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_savings_transfer_superseded_by', 'savings_transfer', ['superseded_by_transfer_id'], ['id'])
        batch.create_index('ix_savings_transfer_superseded_by_transfer_id', ['superseded_by_transfer_id'])


def downgrade():
    with op.batch_alter_table('savings_transfer') as batch:
        batch.drop_index('ix_savings_transfer_superseded_by_transfer_id')
        batch.drop_constraint('fk_savings_transfer_superseded_by', type_='foreignkey')
        batch.drop_column('superseded_by_transfer_id')
