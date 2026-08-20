from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Optional

Availability = Literal["in_stock", "out_of_stock", "unknown"]
PriceType = Literal["pickup", "in_store", "online", "unknown"]


class RetailProviderError(RuntimeError):
    pass


class RetailConfigurationError(RetailProviderError):
    pass


class RetailLocationMismatchError(RetailProviderError):
    pass


@dataclass(frozen=True)
class RetailStore:
    store_id: str
    name: Optional[str]
    address: Optional[str]
    postal_code: Optional[str]
    verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ShoppingRequirement:
    item_name: str
    base_item: str
    brand: Optional[str] = None
    variant: Optional[str] = None
    quantity: Optional[float] = 1.0
    unit: Optional[str] = None
    requested_package_size: Optional[str] = None
    category: str = "General"
    source_kind: str = "manual"
    source_recipe_id: Optional[int] = None
    source_recipe_title: Optional[str] = None
    source_text: Optional[str] = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ShoppingRequirement:
        item_name = str(value.get("item_name") or value.get("base_item") or "").strip()
        base_item = str(value.get("base_item") or item_name).strip()
        return cls(
            item_name=item_name,
            base_item=base_item,
            brand=_optional_text(value.get("brand")),
            variant=_optional_text(value.get("variant")),
            quantity=_optional_quantity(value.get("quantity")),
            unit=_optional_text(value.get("unit")),
            requested_package_size=_optional_text(value.get("requested_package_size")),
            category=str(value.get("category") or "General").strip() or "General",
            source_kind=str(value.get("source_kind") or "manual").strip() or "manual",
            source_recipe_id=_optional_int(value.get("source_recipe_id")),
            source_recipe_title=_optional_text(value.get("source_recipe_title")),
            source_text=_optional_text(value.get("source_text")),
        )

    def search_query(self) -> str:
        pieces: list[str] = []
        for value in (self.brand, self.variant, self.base_item):
            text = str(value or "").strip()
            if text and text.lower() not in " ".join(pieces).lower():
                pieces.append(text)
        return " ".join(pieces) or self.item_name


@dataclass(frozen=True)
class RetailProduct:
    requested_query: str
    retailer: str
    store: RetailStore
    product_id: Optional[str]
    us_item_id: Optional[str]
    upc: Optional[str]
    title: str
    brand: Optional[str]
    variant: Optional[str]
    package_size: Optional[str]
    price: Optional[float]
    availability: Availability
    price_type: PriceType
    product_url: Optional[str]
    source: str
    retrieved_at: str
    verified_location: bool
    regular_price: Optional[float] = None
    promo_price: Optional[float] = None
    fulfillment: Optional[dict[str, bool]] = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["store"] = self.store.to_dict()
        return value

    @classmethod
    def now(cls, **kwargs: Any) -> RetailProduct:
        return cls(retrieved_at=datetime.now(timezone.utc).isoformat(), **kwargs)


@dataclass(frozen=True)
class ProductSearchResult:
    requested_store: RetailStore
    response_store: RetailStore
    products: list[RetailProduct]
    raw_result_count: int


class RetailProvider(ABC):
    retailer: str

    @abstractmethod
    def find_stores(self, *, postal_code: str) -> list[RetailStore]:
        raise NotImplementedError

    @abstractmethod
    def search_products(
        self,
        requirement: ShoppingRequirement,
        *,
        store: RetailStore,
        limit: int = 20,
    ) -> ProductSearchResult:
        raise NotImplementedError

    @abstractmethod
    def get_product(
        self,
        product_id: str,
        *,
        store: RetailStore,
        requested_query: str,
    ) -> RetailProduct:
        raise NotImplementedError


def _optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _optional_quantity(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
