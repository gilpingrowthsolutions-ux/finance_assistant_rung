"""gate5 owned tax engine

Revision ID: 5e1c9d7a4b2f
Revises: 2d9f3b6c8a4e
Create Date: 2026-08-15 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5e1c9d7a4b2f'
down_revision = '2d9f3b6c8a4e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'tax_source_dataset',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source_key', sa.String(length=80), nullable=False),
        sa.Column('source_type', sa.String(length=40), nullable=False),
        sa.Column('jurisdiction_state', sa.String(length=2), nullable=True),
        sa.Column('source_name', sa.String(length=200), nullable=False),
        sa.Column('source_reference', sa.Text(), nullable=True),
        sa.Column('source_hash', sa.String(length=128), nullable=False),
        sa.Column('version_tag', sa.String(length=80), nullable=False),
        sa.Column('published_at', sa.DateTime(), nullable=True),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('imported_at', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source_key', 'version_tag', name='uq_tax_source_dataset_source_version'),
    )
    op.create_index('ix_tax_source_dataset_status_effective', 'tax_source_dataset', ['status', 'effective_from', 'effective_to'], unique=False)

    op.create_table(
        'tax_jurisdiction',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('jurisdiction_type', sa.String(length=30), nullable=False),
        sa.Column('canonical_code', sa.String(length=120), nullable=False),
        sa.Column('state', sa.String(length=2), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('parent_jurisdiction_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['parent_jurisdiction_id'], ['tax_jurisdiction.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('jurisdiction_type', 'canonical_code', name='uq_tax_jurisdiction_type_code'),
    )
    op.create_index('ix_tax_jurisdiction_state_name', 'tax_jurisdiction', ['state', 'name'], unique=False)

    op.create_table(
        'tax_rate',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('dataset_id', sa.Integer(), nullable=False),
        sa.Column('jurisdiction_id', sa.Integer(), nullable=False),
        sa.Column('tax_code', sa.String(length=120), nullable=False),
        sa.Column('tax_class', sa.String(length=40), nullable=False),
        sa.Column('rate_basis_points', sa.Integer(), nullable=False),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('source_confidence', sa.String(length=30), nullable=False),
        sa.ForeignKeyConstraint(['dataset_id'], ['tax_source_dataset.id']),
        sa.ForeignKeyConstraint(['jurisdiction_id'], ['tax_jurisdiction.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dataset_id', 'jurisdiction_id', 'tax_code', 'tax_class', 'effective_from', name='uq_tax_rate_dataset_jurisdiction_class_start'),
    )
    op.create_index('ix_tax_rate_lookup', 'tax_rate', ['jurisdiction_id', 'tax_class', 'effective_from', 'effective_to'], unique=False)
    op.create_index('ix_tax_rate_dataset', 'tax_rate', ['dataset_id'], unique=False)

    op.create_table(
        'tax_boundary_assignment',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('dataset_id', sa.Integer(), nullable=False),
        sa.Column('geographic_key_type', sa.String(length=30), nullable=False),
        sa.Column('geographic_key', sa.String(length=200), nullable=False),
        sa.Column('assignment_precision', sa.String(length=30), nullable=False),
        sa.Column('jurisdiction_id', sa.Integer(), nullable=False),
        sa.Column('tax_code', sa.String(length=120), nullable=False),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('source_confidence', sa.String(length=30), nullable=False),
        sa.ForeignKeyConstraint(['dataset_id'], ['tax_source_dataset.id']),
        sa.ForeignKeyConstraint(['jurisdiction_id'], ['tax_jurisdiction.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dataset_id', 'geographic_key_type', 'geographic_key', 'tax_code', 'effective_from', name='uq_tax_boundary_dataset_key_code_start'),
    )
    op.create_index('ix_tax_boundary_lookup', 'tax_boundary_assignment', ['geographic_key_type', 'geographic_key', 'effective_from', 'effective_to'], unique=False)
    op.create_index('ix_tax_boundary_dataset', 'tax_boundary_assignment', ['dataset_id'], unique=False)

    op.create_table(
        'store_tax_profile',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('retailer', sa.String(length=50), nullable=False),
        sa.Column('retailer_store_id', sa.String(length=80), nullable=False),
        sa.Column('store_name', sa.String(length=200), nullable=True),
        sa.Column('normalized_address', sa.String(length=300), nullable=True),
        sa.Column('postal_code', sa.String(length=10), nullable=True),
        sa.Column('city', sa.String(length=120), nullable=True),
        sa.Column('county', sa.String(length=120), nullable=True),
        sa.Column('state', sa.String(length=2), nullable=True),
        sa.Column('latitude', sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column('longitude', sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column('resolved_jurisdiction_id', sa.Integer(), nullable=True),
        sa.Column('resolved_tax_code', sa.String(length=120), nullable=True),
        sa.Column('location_precision', sa.String(length=30), nullable=False),
        sa.Column('confidence', sa.String(length=30), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('general_rate_basis_points', sa.Integer(), nullable=True),
        sa.Column('grocery_rate_basis_points', sa.Integer(), nullable=True),
        sa.Column('prepared_rate_basis_points', sa.Integer(), nullable=True),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('source_dataset_id', sa.Integer(), nullable=True),
        sa.Column('source_version', sa.String(length=80), nullable=True),
        sa.Column('source_hash', sa.String(length=128), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['resolved_jurisdiction_id'], ['tax_jurisdiction.id']),
        sa.ForeignKeyConstraint(['source_dataset_id'], ['tax_source_dataset.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_store_tax_profile_lookup', 'store_tax_profile', ['retailer', 'retailer_store_id', 'effective_from', 'effective_to'], unique=False)
    op.create_index('ix_store_tax_profile_dataset', 'store_tax_profile', ['source_dataset_id'], unique=False)

    op.create_table(
        'taxability_rule',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('dataset_id', sa.Integer(), nullable=False),
        sa.Column('jurisdiction_id', sa.Integer(), nullable=True),
        sa.Column('state', sa.String(length=2), nullable=False),
        sa.Column('tax_class', sa.String(length=40), nullable=False),
        sa.Column('treatment', sa.String(length=40), nullable=False),
        sa.Column('override_rate_basis_points', sa.Integer(), nullable=True),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('source_confidence', sa.String(length=30), nullable=False),
        sa.ForeignKeyConstraint(['dataset_id'], ['tax_source_dataset.id']),
        sa.ForeignKeyConstraint(['jurisdiction_id'], ['tax_jurisdiction.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dataset_id', 'state', 'jurisdiction_id', 'tax_class', 'effective_from', name='uq_taxability_rule_dataset_scope_class_start'),
    )
    op.create_index('ix_taxability_rule_lookup', 'taxability_rule', ['state', 'tax_class', 'effective_from', 'effective_to'], unique=False)

    op.create_table(
        'retail_product_tax_class',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('retailer', sa.String(length=50), nullable=False),
        sa.Column('retailer_product_id', sa.String(length=120), nullable=True),
        sa.Column('upc', sa.String(length=50), nullable=True),
        sa.Column('canonical_tax_class', sa.String(length=40), nullable=False),
        sa.Column('source', sa.String(length=40), nullable=False),
        sa.Column('confidence', sa.String(length=30), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('retailer', 'retailer_product_id', name='uq_retail_product_tax_class_retailer_product'),
    )
    op.create_index('ix_retail_product_tax_class_retailer_upc', 'retail_product_tax_class', ['retailer', 'upc'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_retail_product_tax_class_retailer_upc', table_name='retail_product_tax_class')
    op.drop_table('retail_product_tax_class')

    op.drop_index('ix_taxability_rule_lookup', table_name='taxability_rule')
    op.drop_table('taxability_rule')

    op.drop_index('ix_store_tax_profile_dataset', table_name='store_tax_profile')
    op.drop_index('ix_store_tax_profile_lookup', table_name='store_tax_profile')
    op.drop_table('store_tax_profile')

    op.drop_index('ix_tax_boundary_dataset', table_name='tax_boundary_assignment')
    op.drop_index('ix_tax_boundary_lookup', table_name='tax_boundary_assignment')
    op.drop_table('tax_boundary_assignment')

    op.drop_index('ix_tax_rate_dataset', table_name='tax_rate')
    op.drop_index('ix_tax_rate_lookup', table_name='tax_rate')
    op.drop_table('tax_rate')

    op.drop_index('ix_tax_jurisdiction_state_name', table_name='tax_jurisdiction')
    op.drop_table('tax_jurisdiction')

    op.drop_index('ix_tax_source_dataset_status_effective', table_name='tax_source_dataset')
    op.drop_table('tax_source_dataset')
