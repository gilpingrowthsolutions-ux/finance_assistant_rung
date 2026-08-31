"""Feature 5 SQLite contention probes.

These run in a fresh interpreter because Flask-SQLAlchemy binds its engine at
import time.  Keeping the database path explicit here prevents this probe from
ever inheriting the protected historical database.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_file_backed_current_cart_contention_preserves_one_current_cart(tmp_path: Path) -> None:
    database = tmp_path / "feature5_cart_contention.sqlite"
    script = r'''
import os, threading
from concurrent.futures import ThreadPoolExecutor
from app import app
from extensions import db
from models import Household, RetailStoreIdentity, ShoppingCart, ShoppingCartLine
from services.authoritative_cart import replace_current_from_resolution
from sqlalchemy.exc import IntegrityError, OperationalError

payload = lambda sku, amount: {"subtotal": amount, "total_cart_cost": amount, "cart_items": [{
 "requirement": {"item_name":"Laundry detergent", "base_item":"laundry detergent", "source_kind":"manual", "source_requirement_id": 71},
 "selected_product": {"product_id":sku, "retailer":"walmart", "title":sku, "price":amount, "availability":"in_stock"},
 "packages_to_buy": 1, "needs_user_choice": False, "availability":"in_stock"}]}
with app.app_context():
 db.create_all(); household=Household(legacy_scope_key="contention"); store=RetailStoreIdentity(retailer="walmart", retailer_store_id="357", store_name="Store")
 db.session.add_all([household, store]); db.session.commit(); hid, sid=household.id, store.id
barrier=threading.Barrier(2)
def replace(sku):
 with app.app_context():
  barrier.wait()
  try:
   cart=replace_current_from_resolution(household_id=hid, store_identity_id=sid, resolved_cart=payload(sku, 4.25)); db.session.commit(); return ("ok", cart.id)
  except (IntegrityError, OperationalError):
   db.session.rollback(); return ("conflict", None)
with ThreadPoolExecutor(max_workers=2) as pool: outcomes=list(pool.map(replace, ["sku-a", "sku-b"]))
with app.app_context():
 rows=ShoppingCart.query.filter_by(household_id=hid, status="current").all()
 assert len(rows)==1, rows
 assert sum(1 for status, _ in outcomes if status=="ok") == 1, outcomes
 print("OK", outcomes, rows[0].id)
'''
    env = dict(os.environ)
    env["RUNG_DB_PATH"] = str(database)
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
        env=env, text=True, capture_output=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_file_backed_current_cart_replacement_race_rolls_back_loser_and_keeps_other_household(tmp_path: Path) -> None:
    database = tmp_path / "feature5_cart_replacement_contention.sqlite"
    script = r'''
import threading
from concurrent.futures import ThreadPoolExecutor
from app import app
from extensions import db
from models import Household, RetailStoreIdentity, ShoppingCart, ShoppingCartLine
from services.authoritative_cart import replace_current_from_resolution
from sqlalchemy.exc import IntegrityError, OperationalError
payload=lambda sku: {"subtotal":4.25,"total_cart_cost":4.25,"cart_items":[{"requirement":{"item_name":"Laundry detergent","base_item":"laundry detergent","source_kind":"manual","source_requirement_id":71},"selected_product":{"product_id":sku,"retailer":"walmart","title":sku,"price":4.25,"availability":"in_stock"},"packages_to_buy":1,"needs_user_choice":False,"availability":"in_stock"}]}
with app.app_context():
 db.create_all(); a=Household(legacy_scope_key="a"); b=Household(legacy_scope_key="b"); store=RetailStoreIdentity(retailer="walmart",retailer_store_id="357",store_name="Store")
 db.session.add_all([a,b,store]); db.session.commit(); aid,bid,sid=a.id,b.id,store.id
 cart_a=replace_current_from_resolution(household_id=aid,store_identity_id=sid,resolved_cart=payload("A")); cart_b=replace_current_from_resolution(household_id=bid,store_identity_id=sid,resolved_cart=payload("B-household")); db.session.commit(); cart_a_id=cart_a.id; cart_b_id=cart_b.id
barrier=threading.Barrier(2)
def replace(sku):
 with app.app_context():
  barrier.wait()
  try:
   row=replace_current_from_resolution(household_id=aid,store_identity_id=sid,resolved_cart=payload(sku)); db.session.commit(); return "ok",row.id
  except (IntegrityError,OperationalError): db.session.rollback(); return "conflict",None
with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(replace,["B","C"]))
with app.app_context():
 assert sum(x[0]=="ok" for x in results)==1, results
 winner_id=next(cid for status,cid in results if status=="ok")
 current=ShoppingCart.query.filter_by(household_id=aid,status="current").all()
 assert len(current)==1 and current[0].id==winner_id, current
 assert db.session.get(ShoppingCart,cart_a_id).status=="retired"
 assert ShoppingCart.query.filter_by(household_id=aid,status="retired").count()==1
 assert ShoppingCart.query.filter_by(household_id=aid).count()==2
 assert ShoppingCart.query.filter_by(household_id=aid,status="staged").count()==0
 assert ShoppingCart.query.filter_by(household_id=aid,status="completed").count()==0
 assert ShoppingCartLine.query.filter_by(cart_id=winner_id).one().provider_product_id in {"B","C"}
 bcart=db.session.get(ShoppingCart,cart_b_id); assert bcart.status=="current"
 bline=ShoppingCartLine.query.filter_by(cart_id=cart_b_id).one(); assert bline.provider_product_id=="B-household" and bline.line_total_cents==425
 print("OK",results)
'''
    env = dict(os.environ); env["RUNG_DB_PATH"] = str(database)
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1], env=env, text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_file_backed_finished_shopping_contention_has_one_economic_effect(tmp_path: Path) -> None:
    database = tmp_path / "feature5_finish_contention.sqlite"
    script = r'''
import os, threading
from concurrent.futures import ThreadPoolExecutor
from app import app
from extensions import db
from models import Account, ExpenseTransaction, Household, ShoppingCart, ShoppingCartLine, ShoppingTripCompletion
from services.selected_store import select_store
from services.household_context import household_id
with app.app_context():
 db.create_all(); hid=household_id(); account=Account(household_id=hid, checking_balance=1000); db.session.add(account); db.session.flush()
 store=select_store(hid, retailer="walmart", store_id="357", store_name="Store", account=account)
 cart=ShoppingCart(household_id=hid, retail_store_identity_id=store["retail_store_identity_id"], status="current", subtotal_cents=825, total_cents=825)
 db.session.add(cart); db.session.flush()
 cart_id=cart.id
 db.session.add(ShoppingCartLine(cart_id=cart_id, requirement_key="manual:71", requirement_json="{}", retailer="walmart", provider_product_id="sku", title="Detergent", package_count=3, unit_price_cents=275, line_total_cents=825, availability="in_stock", resolution_state="resolved", provenance_json="{}")); db.session.commit()
barrier=threading.Barrier(2)
def finish(_):
 client=app.test_client(); barrier.wait()
 response=client.post("/api/grocery/finished-shopping/complete", json={"confirm":True, "operation_id":"finish-contention", "actual_total":0.01, "retailer":"fake", "store_id":"fake"})
 return response.status_code, response.get_json() or {}
with ThreadPoolExecutor(max_workers=2) as pool: outcomes=list(pool.map(finish, range(2)))
with app.app_context():
 assert ShoppingTripCompletion.query.count()==1
 assert ExpenseTransaction.query.count()==1
 assert ShoppingCart.query.filter_by(status="completed").count()==1
 assert round(float(Account.query.first().checking_balance),2)==991.75
 trip=ShoppingTripCompletion.query.one(); tx=ExpenseTransaction.query.one()
 assert trip.shopping_cart_id == cart_id and tx.id == trip.transaction_id
 assert any(body.get("already_completed") for _, body in outcomes), outcomes
 print("OK", outcomes)
'''
    env = dict(os.environ)
    env["RUNG_DB_PATH"] = str(database)
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
        env=env, text=True, capture_output=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_file_backed_finished_shopping_distinct_operation_ids_still_has_one_winner(tmp_path: Path) -> None:
    database = tmp_path / "feature5_finish_distinct_ops.sqlite"
    # This is intentionally a fresh interpreter and two fresh Flask clients;
    # unlike the preceding test the operation id is deliberately different.
    script = r'''
import threading
from concurrent.futures import ThreadPoolExecutor
from app import app
from extensions import db
from models import Account, ExpenseTransaction, ShoppingCart, ShoppingCartLine, ShoppingTripCompletion
from app import _compute_safe_to_spend_snapshot
from services.selected_store import select_store
from services.household_context import household_id
with app.app_context():
 db.create_all(); hid=household_id(); account=Account(household_id=hid,checking_balance=1000); db.session.add(account); db.session.flush(); store=select_store(hid,retailer="walmart",store_id="357",store_name="Store",account=account)
 cart=ShoppingCart(household_id=hid,retail_store_identity_id=store["retail_store_identity_id"],status="current",subtotal_cents=825,total_cents=825); db.session.add(cart); db.session.flush(); cid=cart.id
 db.session.add(ShoppingCartLine(cart_id=cid,requirement_key="manual:71",requirement_json="{}",retailer="walmart",provider_product_id="sku",title="Detergent",package_count=3,unit_price_cents=275,line_total_cents=825,availability="in_stock",resolution_state="resolved",provenance_json="{}")); db.session.commit(); before=float(Account.query.first().checking_balance); safe_before=_compute_safe_to_spend_snapshot(Account.query.first()).get("safe_to_spend_cents")
barrier=threading.Barrier(2)
def finish(op):
 client=app.test_client(); barrier.wait(); response=client.post("/api/grocery/finished-shopping/complete",json={"confirm":True,"operation_id":op,"actual_total":999,"retailer":"fake","store_id":"fake","cart_id":999,"cart_signature":"fake"}); return response.status_code,response.get_json() or {}
with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(finish,["finish-a","finish-b"]))
with app.app_context():
 assert ShoppingTripCompletion.query.count()==1 and ExpenseTransaction.query.count()==1
 assert ShoppingCart.query.filter_by(status="completed").count()==1
 assert round(before-float(Account.query.first().checking_balance),2)==8.25
 safe_after=_compute_safe_to_spend_snapshot(Account.query.first()).get("safe_to_spend_cents")
 assert safe_before is None or safe_after == safe_before-825
 trip=ShoppingTripCompletion.query.one(); assert trip.shopping_cart_id==cid
 assert ExpenseTransaction.query.one().id==trip.transaction_id
 assert sum(1 for code,_ in results if code==200)==1 or any(body.get("already_completed") for _,body in results),results
 print("OK",results)
'''
    env = dict(os.environ); env["RUNG_DB_PATH"] = str(database)
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1], env=env, text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
