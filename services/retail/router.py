from __future__ import annotations

from typing import Optional

import requests

from services.retail.base import RetailProvider
from services.retail.kroger import KrogerProvider
from services.retail.walmart_serpapi import WalmartSerpApiProvider


def get_retail_provider(
    retailer: str,
    *,
    api_key: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> RetailProvider:
    normalized = str(retailer or "").strip().lower()
    if normalized == "walmart":
        return WalmartSerpApiProvider(api_key=api_key, session=session)
    if normalized in {"kroger", "gerbes"}:
        return KrogerProvider(session=session)
    raise ValueError(f"Unsupported retail provider: {retailer}")
