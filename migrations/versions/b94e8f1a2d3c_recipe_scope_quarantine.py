"""secure mixed recipe ownership and quarantine unclassified legacy rows

Revision ID: b94e8f1a2d3c
Revises: a17c4d9e2b60
"""

from alembic import op
import sqlalchemy as sa


revision = 'b94e8f1a2d3c'
down_revision = 'a17c4d9e2b60'
branch_labels = None
depends_on = None


def upgrade():
    # Add as nullable first so SQLite/PostgreSQL can preserve every legacy row.
    with op.batch_alter_table('recipe') as batch_op:
        batch_op.add_column(sa.Column('recipe_scope', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('household_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_recipe_household_id_household', 'household', ['household_id'], ['id'])

    # No historic ownership/provenance authority exists in the old schema.
    # Quarantine every legacy row rather than guessing from its title, URL, or
    # incidental current usage.  This specifically preserves Test Chicken Bowl.
    op.execute("UPDATE recipe SET recipe_scope = 'legacy_quarantined' WHERE recipe_scope IS NULL")
    with op.batch_alter_table('recipe') as batch_op:
        batch_op.alter_column('recipe_scope', nullable=False)
        batch_op.create_index('ix_recipe_scope_household', ['recipe_scope', 'household_id'])
        batch_op.create_check_constraint(
            'ck_recipe_scope_owner',
            "(recipe_scope = 'canonical' AND household_id IS NULL) OR "
            "(recipe_scope = 'household_private' AND household_id IS NOT NULL) OR "
            "(recipe_scope = 'legacy_quarantined' AND household_id IS NULL)",
        )


def downgrade():
    with op.batch_alter_table('recipe') as batch_op:
        batch_op.drop_constraint('ck_recipe_scope_owner', type_='check')
        batch_op.drop_index('ix_recipe_scope_household')
        batch_op.drop_constraint('fk_recipe_household_id_household', type_='foreignkey')
        batch_op.drop_column('household_id')
        batch_op.drop_column('recipe_scope')
