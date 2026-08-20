"""Package 11 one-account-per-household authority.

Revision ID: c6a4e2f9b731
Revises: 9f7a3d4c1e2b
Create Date: 2026-08-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "c6a4e2f9b731"
down_revision = "9f7a3d4c1e2b"
branch_labels = None
depends_on = None


def _duplicate_values(connection, statement: str) -> list[str]:
    return [str(row[0]) for row in connection.execute(sa.text(statement)).fetchall()]


def upgrade() -> None:
    connection = op.get_bind()

    # Fail closed. Duplicate household/account authorities require an explicit
    # operator-reviewed merge policy; this migration must never pick a winner.
    duplicate_public_ids = _duplicate_values(
        connection,
        "SELECT public_id FROM household GROUP BY public_id HAVING COUNT(*) > 1",
    )
    duplicate_scope_keys = _duplicate_values(
        connection,
        "SELECT legacy_scope_key FROM household "
        "WHERE legacy_scope_key IS NOT NULL "
        "GROUP BY legacy_scope_key HAVING COUNT(*) > 1",
    )
    duplicate_account_households = _duplicate_values(
        connection,
        "SELECT household_id FROM account GROUP BY household_id HAVING COUNT(*) > 1",
    )
    if duplicate_public_ids or duplicate_scope_keys or duplicate_account_households:
        raise RuntimeError(
            "Package 11 uniqueness migration blocked: duplicate canonical authority exists; "
            f"household public_ids={duplicate_public_ids}, "
            f"household scope keys={duplicate_scope_keys}, "
            f"account household_ids={duplicate_account_households}. "
            "Resolve under an explicit merge policy before retrying."
        )

    with op.batch_alter_table("account", schema=None) as batch_op:
        batch_op.create_unique_constraint("uq_account_household_id", ["household_id"])


def downgrade() -> None:
    with op.batch_alter_table("account", schema=None) as batch_op:
        batch_op.drop_constraint("uq_account_household_id", type_="unique")
