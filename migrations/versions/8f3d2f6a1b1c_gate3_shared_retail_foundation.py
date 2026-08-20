"""gate3 shared retail foundation

Revision ID: 8f3d2f6a1b1c
Revises: 76760617295f
Create Date: 2026-08-15 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "8f3d2f6a1b1c"
down_revision = "76760617295f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "retail_product",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("retailer", sa.String(length=50), nullable=False),
        sa.Column("retailer_product_id", sa.String(length=120), nullable=False),
        sa.Column("upc", sa.String(length=50), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("brand", sa.String(length=150), nullable=True),
        sa.Column("package_size", sa.String(length=100), nullable=True),
        sa.Column("variant", sa.String(length=150), nullable=True),
        sa.Column("category", sa.String(length=150), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("retailer", "retailer_product_id", name="uq_retail_product_identity"),
    )
    op.create_index("ix_retail_product_retailer_upc", "retail_product", ["retailer", "upc"], unique=False)

    op.create_table(
        "retail_store_identity",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("retailer", sa.String(length=50), nullable=False),
        sa.Column("retailer_store_id", sa.String(length=80), nullable=False),
        sa.Column("store_name", sa.String(length=200), nullable=False),
        sa.Column("address", sa.String(length=300), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=40), nullable=True),
        sa.Column("postal_code", sa.String(length=20), nullable=True),
        sa.Column("latitude", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("longitude", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("retailer", "retailer_store_id", name="uq_retail_store_identity"),
    )

    op.create_table(
        "store_product_observation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("retail_store_id", sa.Integer(), nullable=False),
        sa.Column("retail_product_id", sa.Integer(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=True),
        sa.Column("price_type", sa.String(length=40), nullable=False),
        sa.Column("price_observed_at", sa.DateTime(), nullable=True),
        sa.Column("price_source", sa.String(length=80), nullable=True),
        sa.Column("price_confidence", sa.String(length=60), nullable=True),
        sa.Column("availability_status", sa.String(length=60), nullable=True),
        sa.Column("fulfillment_data_json", sa.Text(), nullable=True),
        sa.Column("availability_observed_at", sa.DateTime(), nullable=True),
        sa.Column("availability_source", sa.String(length=80), nullable=True),
        sa.Column("availability_confidence", sa.String(length=60), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["retail_store_id"], ["retail_store_identity.id"]),
        sa.ForeignKeyConstraint(["retail_product_id"], ["retail_product.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("retail_store_id", "retail_product_id", name="uq_store_product_observation_identity"),
    )
    op.create_index(
        "ix_store_product_observation_store_product",
        "store_product_observation",
        ["retail_store_id", "retail_product_id"],
        unique=False,
    )
    op.create_index(
        "ix_store_product_observation_price_observed_at",
        "store_product_observation",
        ["price_observed_at"],
        unique=False,
    )
    op.create_index(
        "ix_store_product_observation_availability_observed_at",
        "store_product_observation",
        ["availability_observed_at"],
        unique=False,
    )

    op.create_table(
        "retail_search_cache",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("retailer", sa.String(length=50), nullable=False),
        sa.Column("retail_store_id", sa.Integer(), nullable=False),
        sa.Column("normalized_query", sa.String(length=300), nullable=False),
        sa.Column("retailer_product_ids_json", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["retail_store_id"], ["retail_store_identity.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("retailer", "retail_store_id", "normalized_query", name="uq_retail_search_cache_identity"),
    )
    op.create_index(
        "ix_retail_search_cache_lookup",
        "retail_search_cache",
        ["retailer", "retail_store_id", "normalized_query"],
        unique=False,
    )
    op.create_index("ix_retail_search_cache_observed_at", "retail_search_cache", ["observed_at"], unique=False)

    op.create_table(
        "retail_refresh_lease",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("resource_key", sa.String(length=300), nullable=False),
        sa.Column("lease_owner", sa.String(length=120), nullable=False),
        sa.Column("lease_until", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("resource_key", name="uq_retail_refresh_lease_resource_key"),
    )
    op.create_index(
        "ix_retail_refresh_lease_resource_until",
        "retail_refresh_lease",
        ["resource_key", "lease_until"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_retail_refresh_lease_resource_until", table_name="retail_refresh_lease")
    op.drop_table("retail_refresh_lease")

    op.drop_index("ix_retail_search_cache_observed_at", table_name="retail_search_cache")
    op.drop_index("ix_retail_search_cache_lookup", table_name="retail_search_cache")
    op.drop_table("retail_search_cache")

    op.drop_index("ix_store_product_observation_availability_observed_at", table_name="store_product_observation")
    op.drop_index("ix_store_product_observation_price_observed_at", table_name="store_product_observation")
    op.drop_index("ix_store_product_observation_store_product", table_name="store_product_observation")
    op.drop_table("store_product_observation")

    op.drop_table("retail_store_identity")

    op.drop_index("ix_retail_product_retailer_upc", table_name="retail_product")
    op.drop_table("retail_product")
