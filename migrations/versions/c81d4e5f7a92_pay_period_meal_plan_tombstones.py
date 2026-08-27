"""persist pay-period plan identity and private recipe tombstones

Revision ID: c81d4e5f7a92
Revises: b94e8f1a2d3c
"""
from alembic import op
import sqlalchemy as sa

revision = 'c81d4e5f7a92'
down_revision = 'b94e8f1a2d3c'
branch_labels = None
depends_on = None


def upgrade():
    # Do this before any DDL. SQLite DDL is non-transactional, so discovering
    # ambiguous rows afterwards could leave an unsafe partial upgrade.
    bind = op.get_bind()
    existing_count = bind.execute(sa.text("SELECT COUNT(*) FROM meal_plan")).scalar_one()
    if existing_count:
        raise RuntimeError(
            "meal_plan contains legacy rows without immutable cycle authority; "
            "stop for explicit reviewed migration classification."
        )
    with op.batch_alter_table('recipe') as batch_op:
        batch_op.add_column(sa.Column('tombstoned_at', sa.DateTime(), nullable=True))
        batch_op.create_index('ix_recipe_tombstoned_at', ['tombstoned_at'])
    # Never invent a cycle for legacy activations. The protected legacy
    # database has zero rows; another nonempty legacy database must be
    # explicitly reviewed before this migration can proceed.
    with op.batch_alter_table('meal_plan') as batch_op:
        batch_op.add_column(sa.Column('cycle_key', sa.String(length=80), nullable=False))
        batch_op.add_column(sa.Column('cycle_start', sa.DateTime(), nullable=False))
        batch_op.add_column(sa.Column('cycle_end', sa.DateTime(), nullable=False))
        batch_op.drop_constraint('uq_meal_plan_household_recipe', type_='unique')
        batch_op.create_unique_constraint('uq_meal_plan_household_cycle_recipe', ['household_id', 'cycle_key', 'recipe_id'])
        batch_op.create_index('ix_meal_plan_cycle_key', ['cycle_key'])


def downgrade():
    with op.batch_alter_table('meal_plan') as batch_op:
        batch_op.drop_index('ix_meal_plan_cycle_key')
        batch_op.drop_constraint('uq_meal_plan_household_cycle_recipe', type_='unique')
        batch_op.create_unique_constraint('uq_meal_plan_household_recipe', ['household_id', 'recipe_id'])
        batch_op.drop_column('cycle_end')
        batch_op.drop_column('cycle_start')
        batch_op.drop_column('cycle_key')
    with op.batch_alter_table('recipe') as batch_op:
        batch_op.drop_index('ix_recipe_tombstoned_at')
        batch_op.drop_column('tombstoned_at')
