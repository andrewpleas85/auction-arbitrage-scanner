"""BidProwl: a documented JSON API over ~27 US government surplus sources (GovDeals, GSA, Public Surplus, PropertyRoom, Municibid...).

Free key (BIDPROWL_API_KEY, 1,000 calls/month, 60/min). ZIP-radius search is what makes it useful here: only lots near the buyer.
Terms: link back to the listing (we keep `source_url`), do not resell the data or rebuild a competing index. Spec:
https://bidprowl.com/api/v1/openapi.json. Each call is counted, so callers page sparingly (50 lots per call).
"""
import logging

import httpx

log = logging.getLogger("arbitrage.bidprowl")
BASE = "https://bidprowl.com/api/v1"
UA = "arbitrage-scanner/1.0 (personal use)"


def to_lot(raw: dict) -> dict | None:
    """One API listing -> the flat dict stored in `bidprowl_lots` (prices in dollars). None when unusable."""
    if not raw.get("id") or not raw.get("title"):
        return None

    def dollars(cents):
        return None if cents is None else round(float(cents) / 100, 2)

    return {"id": str(raw["id"]), "source": raw.get("source_name") or raw.get("source") or "", "title": str(raw["title"]).strip(),
            "category": raw.get("category") or "", "state": raw.get("state") or "", "city": raw.get("city") or "",
            "zip": raw.get("zip") or "", "bid": dollars(raw.get("current_bid_cents")) or 0.0,
            "buy_now": dollars(raw.get("buy_now_cents")), "start_bid": dollars(raw.get("starting_bid_cents")),
            "bids": raw.get("num_bids"), "end_date": raw.get("end_date"), "quantity": raw.get("quantity"),
            "url": raw.get("source_url") or raw.get("bidprowl_url") or "", "bidprowl_url": raw.get("bidprowl_url") or "",
            "condition": raw.get("condition_grade") or ""}


class BidProwlClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str):
        self._http = http
        self._headers = {"Authorization": f"Bearer {api_key}", "User-Agent": UA}
        self.calls = 0

    async def usage(self) -> dict:
        r = await self._http.get(f"{BASE}/usage", headers=self._headers)
        r.raise_for_status()
        return r.json().get("data", {})

    async def search(self, zip_code: str, radius: int, *, category: str | None = None, q: str | None = None,
                     pages: int = 3, per_page: int = 50, sort: str = "ending_soon") -> list[dict]:
        """Lots within `radius` miles of `zip_code`, soonest-ending first; stops at the first short page."""
        out: list[dict] = []
        for page in range(1, pages + 1):
            params = {"zip": zip_code, "radius_miles": radius, "per_page": per_page, "page": page, "sort": sort}
            if category:
                params["category"] = category
            if q:
                params["q"] = q
            r = await self._http.get(f"{BASE}/listings", headers=self._headers, params=params)
            self.calls += 1
            if r.status_code == 429:
                log.warning("BidProwl rate limit hit; stopping this sync")
                break
            r.raise_for_status()
            data = r.json().get("data", [])
            data = data if isinstance(data, list) else data.get("listings", [])
            out += [lot for raw in data if (lot := to_lot(raw))]
            if len(data) < per_page:
                break
        return out
