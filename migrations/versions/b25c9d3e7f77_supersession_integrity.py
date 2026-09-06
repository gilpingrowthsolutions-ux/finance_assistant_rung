"""Guard transfer supersession self-reference at the database boundary."""
from alembic import op

revision = 'b25c9d3e7f77'
down_revision = 'a24f8b2c6d66'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('savings_transfer') as batch:
        batch.create_check_constraint('ck_savings_transfer_not_self_superseded', 'superseded_by_transfer_id IS NULL OR superseded_by_transfer_id <> id')


def downgrade():
    with op.batch_alter_table('savings_transfer') as batch:
        batch.drop_constraint('ck_savings_transfer_not_self_superseded', type_='check')
