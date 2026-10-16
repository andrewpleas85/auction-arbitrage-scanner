"""Retail price check against Best Buy's Products API (https://api.bestbuy.com/v1/products).

Purpose: a NEW-retail price is a ceiling on what a used unit can resell for, so it sanity-checks a
sold-comps estimate (and the discounted active-ask fallback). It is not a comp itself.

Matching is deliberately strict: the listing title must contain a model number, Best Buy's
`modelNumber` must match it, and the brand (if known) must appear in the product name. Wrong-product
matches would silently distort valuations, so "no match" is the normal, safe outcome (the API only
carries items Best Buy sells: mostly consumer electronics and appliances).

Terms: Best Buy forbids caching beyond a temporary basis, so results are held in memory for `ttl`
seconds only, never stored in the database. Query syntax follows the published docs; not yet
verified against a live key.
"""
import re
import time
from dataclasses import dataclass

import httpx

from engine.title_cleaner import extract_brand, extract_models

URL = "https://api.bestbuy.com/v1/products"
SHOW = "sku,name,modelNumber,upc,salePrice,regularPrice,onSale,condition,url"


@dataclass
class RetailPrice:
    name: str
    model: str
    sale_price: float  # current new price
    regular_price: float
    url: str


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def model_matches(listing_model: str, store_model: str) -> bool:
    a, b = _norm(listing_model), _norm(store_model)
    if not a or not b:
        return False
    if a == b:
        return True
    # titles often carry a shortened model ("S918U" vs "SM-S918UZKAXAA"); allow containment for long ones
    return len(a) >= 5 and not a.isdigit() and (a in b or (len(b) >= 5 and b in a))


class BestBuyClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str, ttl: float = 3600):
        self._http, self._key, self._ttl = http, api_key, ttl
        self._cache: dict[str, tuple[float, RetailPrice | None]] = {}
        self.disabled_reason: str | None = None  # set when the key is rejected / limit hit

    @staticmethod
    def parse(products: list[dict], model: str, brand: str | None) -> RetailPrice | None:
        """Cheapest NEW product whose modelNumber matches (and name contains the brand, if known)."""
        best = None
        for p in products:
            if (p.get("condition") or "new").lower() != "new" or not p.get("salePrice"):
                continue
            if not model_matches(model, p.get("modelNumber", "")):
                continue
            if brand and brand not in (p.get("name") or "").lower():
                continue
            rp = RetailPrice(p.get("name", ""), p.get("modelNumber", ""), float(p["salePrice"]),
                             float(p.get("regularPrice") or p["salePrice"]), p.get("url", ""))
            if best is None or rp.sale_price < best.sale_price:
                best = rp
        return best

    async def lookup(self, title: str) -> RetailPrice | None:
        """New retail price for the item in `title`, or None (no model number, no match, or disabled)."""
        if self.disabled_reason:
            return None
        models = extract_models(title)[:2]
        if not models:
            return None
        brand = extract_brand(title)
        for model in models:
            hit = self._cache.get(model)
            if hit and time.time() - hit[0] < self._ttl:
                if hit[1]:
                    return hit[1]
                continue
            found = None
            for query in (f"modelNumber={model}", f"modelNumber={model}*"):  # exact, then prefix
                found = self.parse(await self._search(query), model, brand)
                if found or self.disabled_reason:
                    break
            if self.disabled_reason:
                return None  # key rejected / limit hit: stop now and don't cache the non-answer
            self._cache[model] = (time.time(), found)
            if found:
                return found
        return None

    async def _search(self, query: str) -> list[dict]:
        resp = await self._http.get(f"{URL}({query})", params={
            "apiKey": self._key, "format": "json", "show": SHOW, "pageSize": 10})
        if resp.status_code == 403:  # invalid key, or the call limit is exhausted
            self.disabled_reason = "Best Buy returned 403 (invalid key or call limit exceeded)"
            return []
        if resp.status_code in (400, 404):
            return []
        resp.raise_for_status()
        return resp.json().get("products", [])
