"""Reverb API client (HAL+JSON). Docs: https://www.reverb.dev/api"""
import httpx

from models import Listing

BASE = "https://reverb.com/api"


class ReverbClient:
    def __init__(self, http: httpx.AsyncClient, token: str = ""):
        self._http = http
        self._headers = {
            "Accept": "application/hal+json",
            "Content-Type": "application/hal+json",
            "Accept-Version": "3.0",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    async def new_listings(self, category: str, limit: int = 50) -> list[Listing]:
        """Newest live listings in a category slug (e.g. 'amps', 'synths')."""
        resp = await self._http.get(
            f"{BASE}/listings",
            headers=self._headers,
            params={
                "product_type": category,
                "state": "live",
                "ships_to": "US",
                "sort": "published_at|desc",
                "per_page": limit,
            },
        )
        resp.raise_for_status()
        return [self._parse(raw, category) for raw in resp.json().get("listings", [])]

    @staticmethod
    def _parse(raw: dict, category: str) -> Listing:
        price = float(raw["price"]["amount"])
        ship = 0.0
        rates = (raw.get("shipping") or {}).get("rates") or []
        # Prefer the US rate; fall back to the first listed rate.
        for rate in rates:
            if rate.get("region_code") in ("US", "US_CON"):
                ship = float(rate["rate"]["amount"])
                break
        else:
            if rates:
                ship = float(rates[0]["rate"]["amount"])
        photos = raw.get("photos") or []
        image = None
        if photos:
            links = photos[0].get("_links", {})
            image = (links.get("small_crop") or links.get("thumbnail") or {}).get("href")
        return Listing(
            platform="reverb",
            listing_id=str(raw["id"]),
            title=raw["title"],
            price=price,
            shipping_cost=ship,
            url=raw["_links"]["web"]["href"],
            image_url=image,
            category=category,
        )
