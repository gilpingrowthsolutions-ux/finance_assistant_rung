"""Feature 5 persisted, store-bound shopping cart authority.

Revision ID: d2e5f5a9c1a3
Revises: c81d4e5f7a92
"""
from alembic import op
import sqlalchemy as sa

revision = "d2e5f5a9c1a3"
down_revision = "c81d4e5f7a92"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('shopping_cart',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('household_id', sa.Integer(), sa.ForeignKey('household.id'), nullable=False),
        sa.Column('retail_store_identity_id', sa.Integer(), sa.ForeignKey('retail_store_identity.id'), nullable=False),
        sa.Column('status', sa.String(24), nullable=False, server_default='current'),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('source', sa.String(60), nullable=False, server_default='retail_resolution'),
        sa.Column('subtotal_cents', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_cents', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False), sa.Column('updated_at', sa.DateTime(), nullable=False), sa.Column('completed_at', sa.DateTime()),
        sa.CheckConstraint("status IN ('current','staged','completed','retired')", name='ck_shopping_cart_status'),
        sa.CheckConstraint('subtotal_cents >= 0', name='ck_shopping_cart_subtotal_nonnegative'), sa.CheckConstraint('total_cents >= 0', name='ck_shopping_cart_total_nonnegative'))
    op.create_index('ix_shopping_cart_household_id', 'shopping_cart', ['household_id'])
    op.create_index('ix_shopping_cart_retail_store_identity_id', 'shopping_cart', ['retail_store_identity_id'])
    op.create_index('ix_shopping_cart_household_status', 'shopping_cart', ['household_id', 'status'])
    op.create_index(
        'uq_shopping_cart_one_current_household', 'shopping_cart', ['household_id'], unique=True,
        sqlite_where=sa.text("status = 'current'"),
        postgresql_where=sa.text("status = 'current'"),
    )
    op.create_table('shopping_cart_line',
        sa.Column('id', sa.Integer(), primary_key=True), sa.Column('cart_id', sa.Integer(), sa.ForeignKey('shopping_cart.id'), nullable=False),
        sa.Column('requirement_key', sa.String(200), nullable=False), sa.Column('requirement_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('retailer', sa.String(50), nullable=False), sa.Column('provider_product_id', sa.String(120)), sa.Column('provider_us_item_id', sa.String(120)),
        sa.Column('title', sa.String(300), nullable=False), sa.Column('brand', sa.String(150)), sa.Column('package_size', sa.String(160)),
        sa.Column('package_count', sa.Integer(), nullable=False, server_default='1'), sa.Column('unit_price_cents', sa.Integer()), sa.Column('line_total_cents', sa.Integer()),
        sa.Column('availability', sa.String(40), nullable=False, server_default='unknown'), sa.Column('resolution_state', sa.String(40), nullable=False, server_default='unresolved'),
        sa.Column('provider_source', sa.String(100)), sa.Column('resolved_at', sa.DateTime()), sa.Column('provenance_json', sa.Text(), nullable=False, server_default='{}'), sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('cart_id', 'requirement_key', name='uq_shopping_cart_line_requirement'),
        sa.CheckConstraint('package_count > 0', name='ck_shopping_cart_line_package_count'), sa.CheckConstraint('unit_price_cents IS NULL OR unit_price_cents >= 0', name='ck_shopping_cart_line_unit_price'), sa.CheckConstraint('line_total_cents IS NULL OR line_total_cents >= 0', name='ck_shopping_cart_line_total'))
    op.create_index('ix_shopping_cart_line_cart_id', 'shopping_cart_line', ['cart_id'])
    op.create_table('shopping_store_change_review',
        sa.Column('id', sa.Integer(), primary_key=True), sa.Column('household_id', sa.Integer(), sa.ForeignKey('household.id'), nullable=False),
        sa.Column('current_cart_id', sa.Integer(), sa.ForeignKey('shopping_cart.id'), nullable=False), sa.Column('staged_cart_id', sa.Integer(), sa.ForeignKey('shopping_cart.id'), nullable=False),
        sa.Column('from_store_identity_id', sa.Integer(), sa.ForeignKey('retail_store_identity.id'), nullable=False), sa.Column('to_store_identity_id', sa.Integer(), sa.ForeignKey('retail_store_identity.id'), nullable=False),
        sa.Column('status', sa.String(24), nullable=False, server_default='pending'), sa.Column('operation_id', sa.String(100), nullable=False), sa.Column('created_at', sa.DateTime(), nullable=False), sa.Column('decided_at', sa.DateTime()),
        sa.UniqueConstraint('household_id', 'operation_id', name='uq_store_change_review_household_operation'), sa.CheckConstraint("status IN ('pending','approved','cancelled')", name='ck_store_change_review_status'))
    op.create_index('ix_store_change_review_household_id', 'shopping_store_change_review', ['household_id'])
    op.create_index('ix_store_change_review_household_status', 'shopping_store_change_review', ['household_id', 'status'])
    with op.batch_alter_table('shopping_trip_completion') as batch:
        batch.add_column(sa.Column('shopping_cart_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_trip_completion_shopping_cart', 'shopping_cart', ['shopping_cart_id'], ['id'])
        batch.create_unique_constraint('uq_trip_completion_shopping_cart', ['shopping_cart_id'])


def downgrade() -> None:
    with op.batch_alter_table('shopping_trip_completion') as batch:
        batch.drop_constraint('uq_trip_completion_shopping_cart', type_='unique')
        batch.drop_constraint('fk_trip_completion_shopping_cart', type_='foreignkey')
        batch.drop_column('shopping_cart_id')
    op.drop_table('shopping_store_change_review'); op.drop_table('shopping_cart_line'); op.drop_table('shopping_cart')
