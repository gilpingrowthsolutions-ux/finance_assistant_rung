"""Package 17 canonical effective-dated income plans.

Revision ID: a17c4d9e2b60
Revises: f6b8d2a41c90
"""
from alembic import op
import sqlalchemy as sa

revision = "a17c4d9e2b60"
down_revision = "f6b8d2a41c90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "income_plan_version",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.String(120), nullable=False),
        sa.Column("expected_income_cents", sa.Integer(), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["household_id"], ["household.id"]),
        sa.UniqueConstraint("household_id", "operation_id", name="uq_income_plan_household_operation"),
        sa.CheckConstraint("expected_income_cents > 0", name="ck_income_plan_expected_positive"),
    )
    op.create_index("ix_income_plan_version_household_id", "income_plan_version", ["household_id"])
    op.create_index("ix_income_plan_version_effective_at", "income_plan_version", ["effective_at"])


def downgrade() -> None:
    op.drop_index("ix_income_plan_version_effective_at", table_name="income_plan_version")
    op.drop_index("ix_income_plan_version_household_id", table_name="income_plan_version")
    op.drop_table("income_plan_version")
