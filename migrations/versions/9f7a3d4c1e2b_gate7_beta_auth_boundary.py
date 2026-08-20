"""gate7 beta auth boundary

Revision ID: 9f7a3d4c1e2b
Revises: 5e1c9d7a4b2f
Create Date: 2026-08-16 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "9f7a3d4c1e2b"
down_revision = "5e1c9d7a4b2f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_user",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("auth_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )

    op.create_table(
        "household_membership",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=40), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["household_id"], ["household.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["auth_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "household_id", name="uq_household_membership_user_household"),
    )
    op.create_index("ix_household_membership_household_id", "household_membership", ["household_id"], unique=False)
    op.create_index("ix_household_membership_user_id", "household_membership", ["user_id"], unique=False)

    op.create_table(
        "auth_login_throttle",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subject_key", sa.String(length=320), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(), nullable=False),
        sa.Column("blocked_until", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subject_key"),
    )


def downgrade() -> None:
    op.drop_table("auth_login_throttle")

    op.drop_index("ix_household_membership_user_id", table_name="household_membership")
    op.drop_index("ix_household_membership_household_id", table_name="household_membership")
    op.drop_table("household_membership")

    op.drop_table("auth_user")
