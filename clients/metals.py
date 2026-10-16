"""Live precious-metal spot prices (USD per troy ounce) from gold-api.com: free, no key.

GET https://api.gold-api.com/price/{XAU|XAG|XPT} -> {"price": 4134.5, "updatedAt": ...}. Cached so a
scan cycle costs at most one refresh; if a refresh fails the last known price is kept (and reported
stale after `max_age`), and with no price at all melt valuation is simply skipped.
"""
import logging
import time

import httpx

log = logging.getLogger("arbitrage.metals")

URL = "https://api.gold-api.com/price/{}"
SYMBOLS = {"gold": "XAU", "silver": "XAG", "platinum": "XPT"}


class SpotPriceClient:
    def __init__(self, http: httpx.AsyncClient, ttl: float = 900, max_age: float = 6 * 3600):
        self._http, self._ttl, self._max_age = http, ttl, max_age
        self._prices: dict[str, float] = {}
        self._fetched_at = 0.0
        self._ok_at = 0.0

    @property
    def prices(self) -> dict[str, float]:
        """Last known prices; empty if none, or older than `max_age` (don't value off a stale price)."""
        return dict(self._prices) if self._prices and time.time() - self._ok_at < self._max_age else {}

    async def refresh(self) -> dict[str, float]:
        if self._fetched_at and time.time() - self._fetched_at < self._ttl:
            return self.prices
        self._fetched_at = time.time()
        fresh = {}
        for metal, symbol in SYMBOLS.items():
            try:
                resp = await self._http.get(URL.format(symbol), timeout=15)
                resp.raise_for_status()
                price = float(resp.json()["price"])
                if price > 0:
                    fresh[metal] = price
            except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
                log.warning("spot price fetch failed for %s: %r", metal, e)
        if fresh:
            self._prices.update(fresh)
            self._ok_at = time.time()
        return self.prices
