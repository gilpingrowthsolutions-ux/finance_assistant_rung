"""Package 16 household behavior decision history.

Revision ID: f6b8d2a41c90
Revises: e4a7c2d91f30
"""
from alembic import op
import sqlalchemy as sa

revision = "f6b8d2a41c90"
down_revision = "e4a7c2d91f30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "behavior_intelligence_decision",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.String(120), nullable=False),
        sa.Column("candidate_key", sa.String(180), nullable=False),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("classification", sa.String(30), nullable=True),
        sa.Column("pattern_signature", sa.String(64), nullable=True),
        sa.Column("typical_amount_cents", sa.Integer(), nullable=True),
        sa.Column("cadence_days", sa.Integer(), nullable=True),
        sa.Column("occurrence_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["household_id"], ["household.id"]),
        sa.UniqueConstraint("household_id", "operation_id", name="uq_behavior_decision_household_operation"),
        sa.CheckConstraint("action IN ('ignore','important','classify')", name="ck_behavior_decision_action"),
        sa.CheckConstraint("classification IS NULL OR classification IN ('need','discretionary','transfer')", name="ck_behavior_decision_classification"),
    )
    op.create_index("ix_behavior_intelligence_decision_household_id", "behavior_intelligence_decision", ["household_id"])
    op.create_index("ix_behavior_intelligence_decision_candidate_key", "behavior_intelligence_decision", ["candidate_key"])


def downgrade() -> None:
    op.drop_index("ix_behavior_intelligence_decision_candidate_key", table_name="behavior_intelligence_decision")
    op.drop_index("ix_behavior_intelligence_decision_household_id", table_name="behavior_intelligence_decision")
    op.drop_table("behavior_intelligence_decision")
