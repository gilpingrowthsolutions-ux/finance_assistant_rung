"""usage limit counter

Revision ID: 2d9f3b6c8a4e
Revises: 8f3d2f6a1b1c
Create Date: 2026-08-15 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "2d9f3b6c8a4e"
down_revision = "8f3d2f6a1b1c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_limit_counter",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("limit_key", sa.String(length=120), nullable=False),
        sa.Column("period_type", sa.String(length=20), nullable=False),
        sa.Column("period_start", sa.DateTime(), nullable=False),
        sa.Column("used_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["household_id"], ["household.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "household_id",
            "limit_key",
            "period_type",
            "period_start",
            name="uq_usage_limit_counter_period",
        ),
    )
    op.create_index(
        "ix_usage_limit_counter_lookup",
        "usage_limit_counter",
        ["household_id", "limit_key", "period_type", "period_start"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_usage_limit_counter_lookup", table_name="usage_limit_counter")
    op.drop_table("usage_limit_counter")
