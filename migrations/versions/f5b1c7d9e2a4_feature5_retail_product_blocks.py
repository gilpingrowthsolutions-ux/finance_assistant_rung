"""Feature 5 household negative retail preferences.

Revision ID: f5b1c7d9e2a4
Revises: e7a5d9b2c4f6
"""
from alembic import op
import sqlalchemy as sa

revision = 'f5b1c7d9e2a4'
down_revision = 'e7a5d9b2c4f6'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('retail_product_block',
        sa.Column('id', sa.Integer(), primary_key=True), sa.Column('household_id', sa.Integer(), sa.ForeignKey('household.id'), nullable=False),
        sa.Column('block_type', sa.String(20), nullable=False), sa.Column('retailer', sa.String(50)), sa.Column('retailer_product_id', sa.String(100)), sa.Column('retailer_us_item_id', sa.String(100)), sa.Column('normalized_brand', sa.String(150)), sa.Column('block_key', sa.String(260), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False), sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint("block_type IN ('exact_product','brand')", name='ck_retail_product_block_type'),
        sa.CheckConstraint("(block_type = 'exact_product' AND retailer IS NOT NULL AND (retailer_product_id IS NOT NULL OR retailer_us_item_id IS NOT NULL) AND normalized_brand IS NULL) OR (block_type = 'brand' AND retailer IS NULL AND normalized_brand IS NOT NULL AND retailer_product_id IS NULL AND retailer_us_item_id IS NULL)", name='ck_retail_product_block_target'),
        sa.UniqueConstraint('household_id','block_key', name='uq_retail_product_block_target'))
    op.create_index('ix_retail_product_block_household_id', 'retail_product_block', ['household_id'])
    op.create_index('uq_retail_product_block_exact_product_id', 'retail_product_block', ['household_id', 'retailer', 'retailer_product_id'], unique=True,
                    sqlite_where=sa.text("block_type = 'exact_product' AND retailer_product_id IS NOT NULL"),
                    postgresql_where=sa.text("block_type = 'exact_product' AND retailer_product_id IS NOT NULL"))
    op.create_index('uq_retail_product_block_exact_us_item_id', 'retail_product_block', ['household_id', 'retailer', 'retailer_us_item_id'], unique=True,
                    sqlite_where=sa.text("block_type = 'exact_product' AND retailer_us_item_id IS NOT NULL"),
                    postgresql_where=sa.text("block_type = 'exact_product' AND retailer_us_item_id IS NOT NULL"))

def downgrade():
    op.drop_index('uq_retail_product_block_exact_us_item_id', table_name='retail_product_block')
    op.drop_index('uq_retail_product_block_exact_product_id', table_name='retail_product_block')
    op.drop_table('retail_product_block')
