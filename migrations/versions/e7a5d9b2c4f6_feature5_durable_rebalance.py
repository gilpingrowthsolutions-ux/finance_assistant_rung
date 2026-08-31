"""Feature 5 durable shopping rebalance proposals.

Revision ID: e7a5d9b2c4f6
Revises: d2e5f5a9c1a3
"""
from alembic import op
import sqlalchemy as sa

revision = 'e7a5d9b2c4f6'
down_revision = 'd2e5f5a9c1a3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('shopping_rebalance_proposal',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('household_id', sa.Integer(), sa.ForeignKey('household.id'), nullable=False),
        sa.Column('base_cart_id', sa.Integer(), sa.ForeignKey('shopping_cart.id'), nullable=False),
        sa.Column('base_cart_version', sa.Integer(), nullable=False),
        sa.Column('operation_id', sa.String(100), nullable=False),
        sa.Column('status', sa.String(24), nullable=False, server_default='pending'),
        sa.Column('created_at', sa.DateTime(), nullable=False), sa.Column('decided_at', sa.DateTime()),
        sa.UniqueConstraint('household_id', 'operation_id', name='uq_rebalance_proposal_household_operation'),
        sa.CheckConstraint("status IN ('pending','approved','rejected','stale')", name='ck_rebalance_proposal_status'))
    op.create_index('ix_shopping_rebalance_proposal_household_id', 'shopping_rebalance_proposal', ['household_id'])
    op.create_index('ix_shopping_rebalance_proposal_base_cart_id', 'shopping_rebalance_proposal', ['base_cart_id'])
    op.create_table('shopping_rebalance_proposal_line',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('proposal_id', sa.Integer(), sa.ForeignKey('shopping_rebalance_proposal.id'), nullable=False),
        sa.Column('source_cart_line_id', sa.Integer(), sa.ForeignKey('shopping_cart_line.id'), nullable=False),
        sa.Column('requirement_key', sa.String(200), nullable=False), sa.Column('old_product_id', sa.String(120)), sa.Column('proposed_product_id', sa.String(120)),
        sa.Column('proposed_title', sa.String(300), nullable=False), sa.Column('proposed_brand', sa.String(150)), sa.Column('proposed_package_size', sa.String(160)),
        sa.Column('package_count', sa.Integer(), nullable=False, server_default='1'), sa.Column('old_line_total_cents', sa.Integer()), sa.Column('proposed_unit_price_cents', sa.Integer()), sa.Column('proposed_line_total_cents', sa.Integer()),
        sa.Column('proposed_availability', sa.String(40), nullable=False, server_default='unknown'), sa.Column('proposed_resolution_state', sa.String(40), nullable=False, server_default='unresolved'), sa.Column('provenance_json', sa.Text(), nullable=False, server_default='{}'),
        sa.UniqueConstraint('proposal_id', 'source_cart_line_id', name='uq_rebalance_proposal_source_line'), sa.CheckConstraint('package_count > 0', name='ck_rebalance_proposal_package_count'))
    op.create_index('ix_shopping_rebalance_proposal_line_proposal_id', 'shopping_rebalance_proposal_line', ['proposal_id'])


def downgrade():
    op.drop_table('shopping_rebalance_proposal_line')
    op.drop_table('shopping_rebalance_proposal')
