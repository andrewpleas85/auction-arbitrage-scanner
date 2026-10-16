"""Shared listing model used by every supply client."""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Listing:
    platform: str
    listing_id: str
    title: str
    price: float  # buy price (BIN price, or current bid for auctions, or lot opening bid)
    shipping_cost: float
    url: str
    image_url: str | None
    category: str
    listing_type: str = "buy_it_now"  # 'buy_it_now' | 'auction'
    end_time: datetime | None = None  # timezone-aware, UTC
    location: str | None = None
    lot_items: list[dict] | None = None  # [{'title': str, 'qty': int}] for liquidation lots
    extra_cost_pct: float = 0.0  # buyer's premium applied to price
    extra: dict = field(default_factory=dict)
