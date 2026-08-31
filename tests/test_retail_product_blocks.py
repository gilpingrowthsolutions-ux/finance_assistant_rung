from __future__ import annotations
import json, os
os.environ['RUNG_DB_PATH'] = ':memory:'
from app import app
from extensions import db
from models import Account, GroceryItem, Household, RetailProductBlock, ShoppingCartLine
from services.household_context import household_id
from services.retail import ProductSearchResult, RetailProduct, RetailStore, ShoppingRequirement
from services.retail.cart import build_verified_retail_cart
from services.retail.preferences import save_product_block
from services.authoritative_cart import current_cart, replace_current_from_resolution, stage_store_change
from services.selected_store import ensure_store_identity, select_store
from sqlalchemy.exc import IntegrityError

STORE = RetailStore('A', 'A', '', '', True)
class Provider:
    def __init__(self, rows): self.rows=rows
    def search_products(self, requirement, *, store, limit=20):
        return ProductSearchResult(store,store,[RetailProduct.now(requested_query=requirement.search_query(),retailer='walmart',store=store,product_id=x[0],us_item_id=x[0],upc=None,title=x[1],brand=x[2],variant=None,package_size='1 ct',price=x[3],availability='in_stock',price_type='unknown',product_url='',source='test',verified_location=True) for x in self.rows],len(self.rows))
    def get_product(self,*a,**k): raise AssertionError
def setup(req):
    with app.app_context():
        db.drop_all(); db.create_all(); hid=household_id(); db.session.add(GroceryItem(household_id=hid,item_name=req.item_name,shopping_requirement_json=json.dumps(req.__dict__))); db.session.commit(); return hid

def test_feature5_exact_product_block_filters_positive_preference_and_household_isolation():
    hid=setup(ShoppingRequirement('detergent','detergent')); p=Provider([('blocked','Blocked','Acme',1),('ok','Okay','Good',2)])
    with app.app_context():
        save_product_block(block_type='exact_product',retailer='walmart',product_id='blocked',us_item_id='blocked')
        cart=build_verified_retail_cart(retailer='walmart',store=STORE,provider=p)
        assert cart['cart_items'][0]['selected_product']['product_id']=='ok'
        other=Household(legacy_scope_key='other'); db.session.add(other); db.session.commit()
        import services.retail.cart as cart_service
        old=cart_service.current_household_id; cart_service.current_household_id=lambda: other.id
        try: assert build_verified_retail_cart(retailer='walmart',store=STORE,provider=p)['cart_items']==[]
        finally: cart_service.current_household_id=old

def test_feature5_brand_block_and_explicit_current_request_override_are_request_scoped():
    hid=setup(ShoppingRequirement('detergent','detergent')); p=Provider([('acme','Acme detergent','Acme',1),('good','Good detergent','Good',2)])
    with app.app_context():
        save_product_block(block_type='brand',brand=' ACME ')
        assert build_verified_retail_cart(retailer='walmart',store=STORE,provider=p)['cart_items'][0]['selected_product']['product_id']=='good'
        GroceryItem.query.delete(); explicit=ShoppingRequirement('Acme detergent','detergent',brand='Acme')
        db.session.add(GroceryItem(household_id=hid,item_name='Acme detergent',shopping_requirement_json=json.dumps(explicit.__dict__))); db.session.commit()
        assert build_verified_retail_cart(retailer='walmart',store=STORE,provider=p)['cart_items'][0]['selected_product']['product_id']=='acme'
        GroceryItem.query.delete(); generic=ShoppingRequirement('detergent','detergent')
        db.session.add(GroceryItem(household_id=hid,item_name='detergent',shopping_requirement_json=json.dumps(generic.__dict__))); db.session.commit()
        assert build_verified_retail_cart(retailer='walmart',store=STORE,provider=p)['cart_items'][0]['selected_product']['product_id']=='good'

def test_feature5_block_shape_and_database_logical_uniqueness():
    setup(ShoppingRequirement('detergent','detergent'))
    with app.app_context():
        with __import__('pytest').raises(ValueError): save_product_block(block_type='exact_product',product_id='x')
        with __import__('pytest').raises(ValueError): save_product_block(block_type='brand',retailer='walmart',brand='Acme')
        first=save_product_block(block_type='exact_product',retailer='walmart',product_id='x')
        from models import RetailProductBlock
        db.session.add(RetailProductBlock(household_id=first.household_id,block_type='exact_product',retailer='walmart',retailer_product_id='x',block_key=first.block_key))
        with __import__('pytest').raises(IntegrityError): db.session.commit()
        db.session.rollback()


def test_exact_block_aliases_merge_only_when_a_provider_observation_supplies_both_ids():
    setup(ShoppingRequirement('detergent', 'detergent'))
    with app.app_context():
        # A: product id first, then the same observed product with both forms.
        first = save_product_block(block_type='exact_product', retailer='walmart', product_id='sku-a')
        merged = save_product_block(block_type='exact_product', retailer='walmart', product_id='sku-a', us_item_id='us-a')
        assert merged.id == first.id
        assert RetailProductBlock.query.filter_by(household_id=first.household_id).count() == 1
        assert (merged.retailer_product_id, merged.retailer_us_item_id) == ('sku-a', 'us-a')
        # B: the reverse arrival order is the same logical block.
        first_b = save_product_block(block_type='exact_product', retailer='walmart', us_item_id='us-b')
        merged_b = save_product_block(block_type='exact_product', retailer='walmart', product_id='sku-b', us_item_id='us-b')
        assert merged_b.id == first_b.id
        # D: an actually different retailer product remains independent.
        different = save_product_block(block_type='exact_product', retailer='walmart', product_id='sku-c', us_item_id='us-c')
        assert different.id not in {first.id, first_b.id}
        assert RetailProductBlock.query.filter_by(household_id=first.household_id).count() == 3


def test_exact_block_partial_unique_indexes_reject_replayed_identity_forms_and_api_is_household_scoped():
    hid = setup(ShoppingRequirement('detergent', 'detergent'))
    with app.app_context():
        row = save_product_block(block_type='exact_product', retailer='walmart', product_id='sku', us_item_id='us')
        for values in ({'retailer_product_id': 'sku'}, {'retailer_us_item_id': 'us'}):
            db.session.add(RetailProductBlock(household_id=hid, block_type='exact_product', retailer='walmart', block_key='replay:' + str(values), **values))
            with __import__('pytest').raises(IntegrityError): db.session.commit()
            db.session.rollback()
        other = Household(legacy_scope_key='block-other'); db.session.add(other); db.session.commit()
        client = app.test_client()
        # A foreign household cannot address another household's block id.
        import services.retail.preferences as preferences
        old = preferences.current_household_id; preferences.current_household_id = lambda: other.id
        try:
            assert client.delete('/api/retail/product-block', json={'block_id': row.id}).status_code == 404
        finally:
            preferences.current_household_id = old


class StoreProvider(Provider):
    def __init__(self, by_store): self.by_store = by_store
    def search_products(self, requirement, *, store, limit=20):
        rows = self.by_store.get(store.store_id, [])
        return ProductSearchResult(store, store, [RetailProduct.now(
            requested_query=requirement.search_query(), retailer='walmart', store=store,
            product_id=row[0], us_item_id=row[1], upc=None, title=row[2], brand=row[3], variant=None,
            package_size='64 loads', price=row[4], availability='in_stock', price_type='unknown',
            product_url='', source='fixture', verified_location=True,
        ) for row in rows], len(rows))


def test_store_change_rebuilds_durable_requirement_and_reapplies_exact_and_brand_blocks():
    requirement = ShoppingRequirement('Laundry detergent', 'laundry detergent', quantity=3, unit='bottle', source_requirement_id=91)
    hid = setup(requirement)
    store_a = RetailStore('A', 'Store A', '', '', True); store_b = RetailStore('B', 'Store B', '', '', True)
    provider = StoreProvider({
        'A': [('a-sku', 'a-us', 'Store A detergent', 'A Brand', 12)],
        'B': [('blocked-sku', 'blocked-us', 'Blocked exact', 'Blocked Brand', 4), ('ok-sku', 'ok-us', 'Valid detergent', 'Good Brand', 8)],
    })
    with app.app_context():
        account = Account(household_id=hid, checking_balance=200); db.session.add(account); db.session.flush()
        selected = select_store(hid, retailer='walmart', store_id='A', store_name='Store A', account=account)
        cart_a = replace_current_from_resolution(household_id=hid, store_identity_id=selected['retail_store_identity_id'], resolved_cart=build_verified_retail_cart(retailer='walmart', store=store_a, provider=provider))
        db.session.commit(); source_line = ShoppingCartLine.query.filter_by(cart_id=cart_a.id).one()
        save_product_block(block_type='exact_product', retailer='walmart', product_id='blocked-sku', us_item_id='blocked-us')
        target = ensure_store_identity(retailer='walmart', store_id='B', store_name='Store B')
        rebuilt = build_verified_retail_cart(retailer='walmart', store=store_b, provider=provider)
        review = stage_store_change(household_id=hid, current=cart_a, target_store_identity_id=target.id, resolved_cart=rebuilt, operation_id='exact-block-store-b')
        db.session.commit(); staged = ShoppingCartLine.query.filter_by(cart_id=review.staged_cart_id).one()
        assert current_cart(hid).id == cart_a.id and staged.provider_product_id == 'ok-sku'
        assert staged.provider_product_id != source_line.provider_product_id
        assert __import__('json').loads(staged.requirement_json)['quantity'] == 3
        assert __import__('json').loads(staged.requirement_json)['unit'] == 'bottle'
        assert __import__('json').loads(staged.requirement_json)['source_requirement_id'] == 91
        save_product_block(block_type='brand', brand=' good brand ')
        rebuilt_all_blocked = build_verified_retail_cart(retailer='walmart', store=store_b, provider=provider)
        review_all = stage_store_change(household_id=hid, current=cart_a, target_store_identity_id=target.id, resolved_cart=rebuilt_all_blocked, operation_id='all-blocked-store-b')
        db.session.commit(); blocked_stage = ShoppingCartLine.query.filter_by(cart_id=review_all.staged_cart_id).one()
        assert blocked_stage.resolution_state == 'unresolved' and blocked_stage.provider_product_id is None
        assert current_cart(hid).id == cart_a.id
