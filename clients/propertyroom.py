"""PropertyRoom client (police seizures / government surplus) via the public category pages.

PropertyRoom has no public API (robots.txt disallows /api/), so this reads the HTML of
https://www.propertyroom.com/c/<category>?page=N, which robots.txt allows. We only use the
default sort (the ?sort= parameter is disallowed), which is ending-soonest. Requests are
sequential, spaced out, and cached briefly so the discovery and ending loops share them.

Each listing card carries: listing id, title, format (FixedPrice | English auction), end time,
price (current bid for auctions) and, for auctions, "N bids". Cards do NOT carry a category
(taken from the page we fetched), shipping cost or a local-pickup flag, so shipping is a
configured estimate and the pickup flag is reported as None.
"""
import asyncio
import html
import logging
import re
import time
from datetime import datetime, timezone

import httpx

from engine.details import Assessment, parse_description
from models import Listing

log = logging.getLogger("arbitrage.propertyroom")

BASE = "https://www.propertyroom.com"
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_CARD_RE = re.compile(r'<a href="(/l/[^"]+)" class="listing-card".*?</a>', re.S)
_FIELD = {
    "id": re.compile(r'data-listing-id="(\d+)"'),
    "format": re.compile(r'data-format="([^"]+)"'),
    "title": re.compile(r'class="listing-title">(.*?)</div>', re.S),
    "end": re.compile(r'data-end="([^"]+)"'),
    "price": re.compile(r'class="listing-price">\s*\$([\d,]+(?:\.\d+)?)'),
    "bids": re.compile(r'class="listing-bids">\s*(\d+)\s*bids?'),
    "image": re.compile(r'<img[^>]+src="([^"]+)"'),
}


def _parse_end(raw: str) -> datetime | None:
    # '2026-10-08T20:00:00.0000000Z' has 7 fractional digits, which fromisoformat rejects before 3.11
    try:
        return datetime.fromisoformat(re.sub(r"(\.\d{1,6})\d*Z$", r"\1+00:00", raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_cards(page: str, category: str, premium_pct: float = 15,
                shipping: float = 0.0) -> list[Listing]:
    out = []
    for m in _CARD_RE.finditer(page):
        card = m.group(0)
        f = {k: (rx.search(card) or [None, None])[1] for k, rx in _FIELD.items()}
        end = _parse_end(f["end"] or "")
        if not (f["id"] and f["title"] and f["price"] and end) or end <= datetime.now(timezone.utc):
            continue
        auction = f["format"] != "FixedPrice"
        out.append(Listing(
            platform="propertyroom",
            listing_id=f["id"],
            title=html.unescape(f["title"]).strip(),
            price=float(f["price"].replace(",", "")),
            shipping_cost=shipping,
            url=BASE + m.group(1),
            image_url=f["image"],
            category=category,
            listing_type="auction" if auction else "buy_it_now",
            end_time=end,
            extra_cost_pct=premium_pct,
            extra={"bid_count": int(f["bids"]) if f["bids"] else (0 if auction else None),
                   "local_pickup_only": None},  # not on the card
        ))
    return out


class PropertyRoomClient:
    def __init__(self, http: httpx.AsyncClient | None, categories: list[str], pages: int = 2,
                 premium_pct: float = 15, shipping: float = 0.0, delay: float = 1.5):
        self._http = http or httpx.AsyncClient(
            http2=True, headers=BROWSER_HEADERS, follow_redirects=True, timeout=20)
        self._cats = categories
        self._pages = pages
        self._premium = premium_pct
        self._shipping = shipping
        self._delay = delay
        self._cache: dict[tuple[str, int], tuple[float, list[Listing]]] = {}
        self._detail_cache: dict[str, tuple[float, Assessment]] = {}
        self._detail_gate = asyncio.Semaphore(1)  # one detail page at a time, spaced out

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _page(self, cat: str, page: int, ttl: float) -> list[Listing]:
        hit = self._cache.get((cat, page))
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        url = f"{BASE}/c/{cat}" + (f"?page={page}" if page > 1 else "")
        resp = await self._http.get(url)
        resp.raise_for_status()
        listings = parse_cards(resp.text, cat, self._premium, self._shipping)
        self._cache[(cat, page)] = (time.time(), listings)
        await asyncio.sleep(self._delay)
        return listings

    async def assess(self, listing: Listing, fix_costs: dict[str, float] | None = None,
                     ttl: float = 900) -> Assessment | None:
        """Fetch a listing's own page (allowed by robots.txt) and read its test results / missing parts.

        Called only for listings that already look profitable. None on any fetch failure. If the
        description block isn't found, the Assessment has found=False and implies nothing.
        """
        hit = self._detail_cache.get(listing.listing_id)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        async with self._detail_gate:
            try:
                resp = await self._http.get(listing.url)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("source=propertyroom event=detail_failed id=%s reason=%r", listing.listing_id, e)
                return None
            await asyncio.sleep(self._delay)
        assessment = parse_description(resp.text, listing.title, fix_costs)
        self._detail_cache[listing.listing_id] = (time.time(), assessment)
        return assessment

    async def fetch(self, pages: int | None = None, ttl: float = 300) -> list[Listing]:
        """Soonest-ending listings across the configured categories (one failure skips only that page)."""
        out: list[Listing] = []
        for cat in self._cats:
            for page in range(1, (pages or self._pages) + 1):
                try:
                    out += await self._page(cat, page, ttl)
                except httpx.HTTPError as e:
                    log.warning("source=propertyroom category=%s page=%d event=fetch_failed reason=%r",
                                cat, page, e)
                    break
        return out
