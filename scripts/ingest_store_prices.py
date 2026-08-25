#!/usr/bin/env python3
"""
StorePriceCache ingestion CLI
=============================
Pulls live retail product prices from the Kroger Developer API and
upserts them into the local StorePriceCache table so the Rung
2-stage grocery engine (see /api/grocery/sync-store-cart) has real
data to work with.

Why this exists
---------------
The Rung engine does NOT fabricate prices. Its only source of real
retail prices is the StorePriceCache table. Without ingestion the
engine surfaces every line as price_pending and the user's locked
preferences cannot be priced. This CLI is the offline pipeline that
fills the cache.

Usage
-----
  # Dry-run: print what would be inserted without writing.
  python scripts/ingest_store_prices.py --config config.json --dry-run

  # Real run: upsert into the configured DATABASE_URL.
  python scripts/ingest_store_prices.py --config config.json

  # One-off term at one location, ad hoc.
  python scripts/ingest_store_prices.py \\
      --store "Kroger" --location-id 14100943 \\
      --terms "milk,eggs,cheddar cheese"

Configuration
-------------
The JSON config file shape is:
  {
    "stores": [
      {
        "store_name": "Kroger",          # canonical name used in StorePriceCache.store_name
        "location_id": "14100943",        # Kroger location id (required)
        "terms": ["milk", "eggs", "bread", ...],
        "limit": 5                        # results per term (default 5)
      },
      ...
    ]
  }

Environment
-----------
KROGER_CLIENT_ID      API client id from developer.kroger.com
KROGER_CLIENT_SECRET  API client secret
DATABASE_URL          PostgreSQL in beta/production; SQLite is local/disposable only

Auth
----
OAuth2 client_credentials flow. Tokens are cached in-memory until
expiry minus a 60s safety margin. Failed token requests surface as
exits with non-zero status.
"""
import argparse
import json
import logging
import os
import sys
import time
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta
from sqlalchemy.engine import make_url

try:
    import requests
except ImportError:
    print('ERROR: requests not installed. Run: pip install requests', file=sys.stderr)
    sys.exit(2)


# ------------------------------------------------------------------------------
# Kroger OAuth2 + products search
# ------------------------------------------------------------------------------
KROGER_TOKEN_URL = 'https://api.kroger.com/v1/connect/oauth2/token'
KROGER_PRODUCTS_URL = 'https://api.kroger.com/v1/products'
DEFAULT_SCOPE = 'product.compact'
DEFAULT_LIMIT = 5


def validate_database_contract() -> None:
    """Fail closed if a hosted ingest job is pointed at SQLite."""
    mode = str(os.getenv("RUNG_ENV") or "development").strip().lower()
    if mode not in {"beta", "production", "prod"}:
        return
    raw_url = str(os.getenv("DATABASE_URL") or "").strip()
    if not raw_url:
        raise RuntimeError("DATABASE_URL is required for hosted ingest.")
    driver = make_url(raw_url).drivername
    if not driver.startswith("postgresql"):
        raise RuntimeError("PostgreSQL is required for beta/production ingest.")

# Known Kroger / regional store-brand strings the ingest uses to mark
# rows is_store_brand=1. Add more as you discover them.
KROGER_STORE_BRAND_TOKENS = (
    'kroger',
    'private selection',
    'simple truth',
    'kroger naturals',
    'kroger brand',
    'ps',
    'st',
)

LOGGER = logging.getLogger('ingest_store_prices')


def _kroger_token(client_id: str, client_secret: str) -> Optional[Dict[str, Any]]:
    """Fetch a fresh OAuth2 access token via client_credentials.

    Returns the parsed JSON dict on success, None on failure. Tokens
    are returned with their expires_at precomputed (datetime UTC).
    """
    try:
        resp = requests.post(
            KROGER_TOKEN_URL,
            auth=(client_id, client_secret),
            data={
                'grant_type': 'client_credentials',
                'scope': DEFAULT_SCOPE,
            },
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        LOGGER.error('Kroger token request failed: %s', exc)
        return None
    data = resp.json()
    if 'access_token' not in data:
        LOGGER.error('Kroger token response missing access_token: %s', data)
        return None
    expires_in = int(data.get('expires_in', 1800))
    data['expires_at'] = datetime.utcnow() + timedelta(seconds=max(60, expires_in - 60))
    return data


class KrogerClient:
    """Tiny Kroger API client with token caching and product search.

    Not a full SDK -- just the two endpoints the ingest needs.
    """

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: Optional[Dict[str, Any]] = None

    def _ensure_token(self) -> Optional[str]:
        if self._token and self._token.get('expires_at') and \
                self._token['expires_at'] > datetime.utcnow():
            return self._token['access_token']
        self._token = _kroger_token(self.client_id, self.client_secret)
        if self._token is None:
            return None
        return self._token['access_token']

    def search_products(self, term: str, location_id: str,
                        limit: int = DEFAULT_LIMIT) -> Optional[List[Dict[str, Any]]]:
        """Search Kroger products for `term` at `location_id`.

        Returns a list of normalised product dicts on success, None on
        transport / auth failure. Network errors are logged and the
        caller can choose to skip the term or abort.
        """
        token = self._ensure_token()
        if not token:
            return None
        try:
            resp = requests.get(
                KROGER_PRODUCTS_URL,
                headers={
                    'Authorization': f'Bearer {token}',
                    'Accept': 'application/json',
                },
                params={
                    'filter.term': term,
                    'filter.locationId': location_id,
                    'filter.limit': limit,
                },
                timeout=10,
            )
        except requests.RequestException as exc:
            LOGGER.warning('Kroger products search "%s" transport error: %s',
                           term, exc)
            return None
        if resp.status_code == 401:
            # Token may have been revoked since we cached it; force a refresh once.
            self._token = None
            token = self._ensure_token()
            if not token:
                return None
            try:
                resp = requests.get(
                    KROGER_PRODUCTS_URL,
                    headers={
                        'Authorization': f'Bearer {token}',
                        'Accept': 'application/json',
                    },
                    params={
                        'filter.term': term,
                        'filter.locationId': location_id,
                        'filter.limit': limit,
                    },
                    timeout=10,
                )
            except requests.RequestException as exc:
                LOGGER.warning('Kroger products search retry "%s" failed: %s',
                               term, exc)
                return None
        if not resp.ok:
            LOGGER.warning('Kroger products search "%s" status %s: %s',
                           term, resp.status_code, resp.text[:200])
            return None
        try:
            body = resp.json()
        except ValueError:
            LOGGER.warning('Kroger products search "%s" returned non-JSON', term)
            return None
        return [_normalise_kroger_product(p) for p in body.get('data', [])]


def _normalise_kroger_product(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Translate one Kroger product payload to the StorePriceCache shape.

    The fields we persist: store_name, item_keyword (the search term
    is passed in by the caller so we don't normalise it here),
    product_title, price, package_size, image_url, retailer.
    is_store_brand is set from the brand field via _is_store_brand().
    """
    description = (raw.get('description') or '').strip()
    brand = (raw.get('brand') or '').strip()
    items = raw.get('items') or [{}]
    item0 = items[0] if items else {}
    price_info = item0.get('price') or {}
    regular_price = price_info.get('regular')
    if regular_price is None:
        regular_price = price_info.get('promo') or 0
    size = (item0.get('size') or '').strip()
    image_url = ''
    images = raw.get('images') or []
    if images:
        sizes = images[0].get('sizes') or []
        if sizes:
            image_url = sizes[0].get('url') or ''
    return {
        'product_title': description,
        'brand': brand,
        'price': float(regular_price or 0),
        'package_size': size,
        'image_url': image_url,
        'is_store_brand': _is_store_brand(brand, description),
    }


def _is_store_brand(brand: str, description: str) -> int:
    """Heuristic: is this product a Kroger / regional store brand?

    The check is loose on purpose: real Kroger private-label lines
    can carry brand strings like 'Kroger', 'Private Selection',
    'Simple Truth', 'Kroger Naturals', or simply 'PS' / 'ST'.
    When in doubt, fall back to 0 (name brand) -- it's safer to
    under-mark than over-mark so the engine's stage-1 default
    (cheapest store-brand) doesn't silently pick a name-brand row.
    """
    text = f'{brand} {description}'.lower()
    if not text.strip():
        return 0
    return 1 if any(tok in text for tok in KROGER_STORE_BRAND_TOKENS) else 0


# ------------------------------------------------------------------------------
# DB upsert
# ------------------------------------------------------------------------------
def upsert_store_price(app, store_name: str, item_keyword: str,
                        product: Dict[str, Any]) -> str:
    """Insert or update a single StorePriceCache row.

    The cache intentionally allows MULTIPLE rows per
    (store_name, item_keyword) -- name-brand and store-brand rows
    coexist so the Rung stage-2 rebalancer has alternatives to swap
    to. We key the upsert on (store_name, item_keyword, product_title)
    so re-runs of the same ingest don't duplicate rows when the
    upstream description hasn't changed.
    """
    from app import db, StorePriceCache
    with app.app_context():
        existing = StorePriceCache.query.filter_by(
            store_name=store_name,
            item_keyword=item_keyword,
            product_title=product['product_title'][:200],
        ).first()
        now = datetime.utcnow()
        if existing is None:
            row = StorePriceCache(
                store_name=store_name,
                item_keyword=item_keyword,
                product_title=product['product_title'][:200],
                price=product['price'],
                unit='each',
                package_size=product['package_size'][:100] if product['package_size'] else None,
                image_url=product['image_url'][:500] if product['image_url'] else None,
                retailer='kroger',
                is_store_brand=product['is_store_brand'],
                last_updated=now,
            )
            db.session.add(row)
            db.session.commit()
            return 'inserted'
        # Update mutable fields; preserve id for stable references.
        existing.price = product['price']
        existing.package_size = product['package_size'][:100] if product['package_size'] else None
        existing.image_url = product['image_url'][:500] if product['image_url'] else None
        existing.is_store_brand = product['is_store_brand']
        existing.last_updated = now
        db.session.commit()
        return 'updated'


# ------------------------------------------------------------------------------
# Ingest driver
# ------------------------------------------------------------------------------
def load_config(path: Optional[str]) -> Dict[str, Any]:
    """Load the JSON config or return a default empty one."""
    if not path:
        return {'stores': []}
    with open(path, 'r', encoding='utf-8') as fh:
        return json.load(fh)


def run_ingest(app, client: KrogerClient, config: Dict[str, Any],
               dry_run: bool = False) -> Dict[str, int]:
    """Walk every (store, term) pair, fetch, upsert.

    Returns a stats dict: {inserted, updated, skipped, errors}.
    """
    stats = {'inserted': 0, 'updated': 0, 'skipped': 0, 'errors': 0}
    stores = config.get('stores') or []
    if not stores:
        LOGGER.warning('Config has no stores; nothing to ingest.')
        return stats
    for store_cfg in stores:
        store_name = (store_cfg.get('store_name') or '').strip()
        location_id = (store_cfg.get('location_id') or '').strip()
        terms = store_cfg.get('terms') or []
        limit = int(store_cfg.get('limit') or DEFAULT_LIMIT)
        if not store_name or not location_id:
            LOGGER.error('Skipping store config without store_name or location_id: %s',
                         store_cfg)
            stats['errors'] += 1
            continue
        if not terms:
            LOGGER.warning('Store %s has no terms; skipping.', store_name)
            continue
        LOGGER.info('Ingesting %d term(s) for store %s @ %s',
                    len(terms), store_name, location_id)
        for term in terms:
            term = term.strip().lower()
            if not term:
                continue
            products = client.search_products(term, location_id, limit=limit)
            if products is None:
                stats['errors'] += 1
                time.sleep(0.25)  # gentle backoff
                continue
            if not products:
                LOGGER.info('  %s: 0 results', term)
                stats['skipped'] += 1
                continue
            for product in products:
                if product['price'] <= 0:
                    # Skip rows without a real price -- they cannot
                    # contribute to the engine and would just create
                    # noise in the cache.
                    stats['skipped'] += 1
                    continue
                if dry_run:
                    LOGGER.info('  DRY-RUN %s -> %s @ $%.2f (store_brand=%s)',
                                term, product['product_title'][:50],
                                product['price'], bool(product['is_store_brand']))
                    stats['inserted'] += 1
                else:
                    try:
                        outcome = upsert_store_price(
                            app, store_name, term, product,
                        )
                        stats[outcome] += 1
                        LOGGER.info('  %s -> %s (%s) @ $%.2f (store_brand=%s)',
                                    term, product['product_title'][:50],
                                    outcome, product['price'],
                                    bool(product['is_store_brand']))
                    except Exception as exc:
                        LOGGER.warning('  %s -> upsert error: %s', term, exc)
                        stats['errors'] += 1
            time.sleep(0.15)  # be polite to the upstream API
    return stats


# ------------------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------------------
def build_app():
    """Build the Flask app the same way app.py does, so we can use
    its SQLAlchemy session + StorePriceCache model. Imports inside
    the function so this CLI is runnable from a bare checkout."""
    from app import app  # noqa: WPS433 -- intentional late import
    return app


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description='Ingest Kroger retail prices into StorePriceCache.',
    )
    parser.add_argument('--config', '-c', type=str,
                        help='Path to JSON config file (see header for schema)')
    parser.add_argument('--store', type=str,
                        help='One-off store_name for an ad-hoc run')
    parser.add_argument('--location-id', type=str,
                        help='Kroger locationId for the ad-hoc run')
    parser.add_argument('--terms', type=str,
                        help='Comma-separated search terms for the ad-hoc run')
    parser.add_argument('--limit', type=int, default=DEFAULT_LIMIT,
                        help=f'Results per term (default {DEFAULT_LIMIT})')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print what would be inserted without writing')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Verbose INFO logging')
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
    )

    try:
        validate_database_contract()
    except (RuntimeError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 2

    client_id = os.getenv('KROGER_CLIENT_ID', '').strip()
    client_secret = os.getenv('KROGER_CLIENT_SECRET', '').strip()
    if not client_id or not client_secret:
        LOGGER.error('KROGER_CLIENT_ID and KROGER_CLIENT_SECRET must be set.')
        return 2
    client = KrogerClient(client_id, client_secret)

    if args.config:
        config = load_config(args.config)
    elif args.store and args.location_id and args.terms:
        config = {
            'stores': [{
                'store_name': args.store,
                'location_id': args.location_id,
                'terms': [t.strip() for t in args.terms.split(',') if t.strip()],
                'limit': args.limit,
            }],
        }
    else:
        LOGGER.error('Provide --config OR --store/--location-id/--terms')
        return 2

    app = build_app()
    stats = run_ingest(app, client, config, dry_run=args.dry_run)
    LOGGER.info('Ingest stats: %s', stats)
    return 0 if stats['errors'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
