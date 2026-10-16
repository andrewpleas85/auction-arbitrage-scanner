"""Etsy Open API v3 client (public listing reads only). Docs: https://developer.etsy.com/documentation"""
import httpx

from models import Listing

BASE = "https://openapi.etsy.com/v3/application"


class EtsyClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str, shared_secret: str = ""):
        self._http = http
        # Etsy's newer apps authenticate with "keystring:shared_secret"; older ones with the keystring alone.
        self._keys = [f"{api_key}:{shared_secret}", api_key] if shared_secret else [api_key]
        self._key = self._keys[0]

    async def new_listings(self, query: str, limit: int = 50) -> list[Listing]:
        """Newest active listings matching a keyword query."""
        params = {
            "keywords": query,
            "limit": limit,
            "sort_on": "created",
            "sort_order": "desc",
            "includes": "Images",
        }
        resp = await self._http.get(
            f"{BASE}/listings/active", headers={"x-api-key": self._key}, params=params
        )
        if resp.status_code in (401, 403) and len(self._keys) > 1 and self._key == self._keys[0]:
            # Fall back to the bare keystring once, and remember whichever format worked.
            resp = await self._http.get(
                f"{BASE}/listings/active", headers={"x-api-key": self._keys[1]}, params=params
            )
            if resp.is_success:
                self._key = self._keys[1]
        resp.raise_for_status()
        listings = (self._parse(raw, query) for raw in resp.json().get("results", []))
        return [l for l in listings if l is not None]

    @staticmethod
    def _parse(raw: dict, query: str) -> Listing | None:
        """Etsy listing -> Listing. Digital downloads (printables, sample packs, MIDI files) are not resellable goods: skipped."""
        if raw.get("listing_type") not in (None, "physical") or raw.get("state") not in (None, "active"):
            return None
        price = raw["price"]
        if price.get("currency_code") not in (None, "USD"):
            return None  # no FX conversion: a EUR/INR price read as dollars would fake a bargain
        images = raw.get("images") or []
        return Listing(
            platform="etsy",
            listing_id=str(raw["listing_id"]),
            title=raw["title"],
            price=price["amount"] / (price.get("divisor") or 100),
            shipping_cost=0.0,  # the listing payload carries no shipping cost
            url=raw["url"],
            image_url=(images[0].get("url_170x135") if images else None),
            category=query,
            extra={"quantity": raw.get("quantity"), "favorers": raw.get("num_favorers"),
                   "listed_at": raw.get("original_creation_timestamp") or raw.get("creation_timestamp")},
        )
