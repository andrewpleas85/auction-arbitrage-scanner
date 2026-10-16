"""ShopGoodwill client (public buyer search API, no auth), with two polling modes.

fetch_recent_listings(): newest listings first, to catch fresh Buy It Now drops within minutes.
fetch_ending_auctions(): auctions ending inside the final window (default 15 min).

Pricing: an item with a buyNowPrice is 'buy_it_now' at that price (guaranteed, buyable now).
Anything else is an 'auction' priced at the CURRENT bid, with no uplift assumed.

API sort quirks (verified live): sortColumn=1 is end time and sorts correctly both ways;
sortColumn=3 sorts by listing start time but only ascending (descending is not start-ordered),
and its final page is NOT the newest: in a full scan of one category the 30 newest items sat on
the second-to-last page (27) and the one before it (3), with the last page holding older items.
So we read the last few pages and order the result by itemId (monotonic, higher = newer).
"""
import asyncio
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from models import Listing

URL = "https://buyerapi.shopgoodwill.com/api/Search/ItemListing"
SITE = "https://shopgoodwill.com/item"
PACIFIC = ZoneInfo("America/Los_Angeles")  # endTime is returned as naive Pacific time
SORT_END_TIME, SORT_START_TIME = "1", "3"
PAGE_SIZE = 40


def _body(category_id: str, page: int, sort_column: str, search_text: str = "") -> dict:
    return {
        "searchText": search_text, "selectedCategoryIds": category_id, "selectedSellerIds": "",
        "lowPrice": "0", "highPrice": "999999", "searchBuyNowOnly": "false",
        "searchPickupOnly": "false", "searchNoPickupOnly": "false",
        "searchOneCentShippingOnly": "false", "searchDescriptions": "false",
        "searchClosedAuctions": "false", "closedAuctionEndingDate": "1/1/2026",
        "closedAuctionDaysBack": "7", "searchCanadaShipping": "false",
        "searchInternationalShippingOnly": "false", "sortColumn": sort_column,
        "page": str(page), "pageSize": str(PAGE_SIZE), "sortDescending": "false",
        "savedSearchId": 0, "useBuyerPrefs": "true", "searchUSOnlyShipping": "true",
        "categoryLevelNo": "1", "categoryLevel": 1, "categoryId": 0,
        "partNumber": "", "catIds": "",
    }


def _parse_end(raw: str) -> datetime:
    return datetime.fromisoformat(raw).replace(tzinfo=PACIFIC).astimezone(timezone.utc)


def parse_item(raw: dict) -> Listing | None:
    """Raw API item -> Listing; None if already ended."""
    end = _parse_end(raw["endTime"])
    if end <= datetime.now(timezone.utc):
        return None
    bin_price = float(raw.get("buyNowPrice") or 0)
    current = float(raw["currentPrice"])
    # Verified live (item detail: isAuction=true, startingPrice, bidIncrement): almost every item is an AUCTION
    # whose Buy It Now price sits ABOVE the current bid (113 of 120 sampled). Price it at the current bid and keep
    # the Buy It Now figure as a guaranteed alternative. Only a Buy It Now equal to the price is a fixed-price item.
    extra = {"bids": raw.get("numBids")}
    if bin_price > current + 0.005:
        ltype, price = "auction", current
        extra["buy_now_price"] = bin_price
    elif bin_price > 0:
        ltype, price = "buy_it_now", bin_price
    else:
        ltype, price = "auction", current
    image = (raw.get("imageURL") or "").replace("\\", "/") or None
    return Listing(
        platform="shopgoodwill",
        listing_id=str(raw["itemId"]),
        title=raw["title"],
        price=price,
        shipping_cost=float(raw.get("shippingPrice") or 0),
        url=f"{SITE}/{raw['itemId']}",
        image_url=image,
        category=raw.get("catFullName") or raw.get("categoryName") or "",
        listing_type=ltype,
        end_time=end,
        extra=extra,
    )


class ShopGoodwillClient:
    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    async def _page(self, cid: str, page: int, sort: str, text: str = "") -> dict:
        resp = await self._http.post(URL, json=_body(cid, page, sort, text))
        resp.raise_for_status()
        return resp.json()["searchResults"]

    async def fetch_recent_listings(self, category_ids: list[str], pages: int = 3) -> list[Listing]:
        """Last `pages` pages of the start-time sort per category (fresh BIN drops and auctions), newest first."""
        out: dict[str, Listing] = {}
        for cid in category_ids:
            first = await self._page(cid, 1, SORT_START_TIME)  # also tells us the item count
            last_page = max(1, math.ceil(first["itemCount"] / PAGE_SIZE))
            want = range(last_page, max(0, last_page - pages), -1)
            for n in want:
                await asyncio.sleep(0.5)
                items = first["items"] if n == 1 else (await self._page(cid, n, SORT_START_TIME))["items"]
                for raw in reversed(items):  # ascending sort -> newest at the end of the page
                    listing = parse_item(raw)
                    if listing:
                        out.setdefault(listing.listing_id, listing)
        return sorted(out.values(), key=lambda l: int(l.listing_id), reverse=True)

    async def fetch_all(self, category_ids: list[str], max_pages: int | None = None,
                        sleep: float = 0.5) -> list[Listing]:
        """EVERY active listing in the categories (end-time order), for cataloging. ~40 items per request."""
        out: dict[str, Listing] = {}
        for cid in category_ids:
            page = 1
            while max_pages is None or page <= max_pages:
                items = (await self._page(cid, page, SORT_END_TIME))["items"]
                for raw in items:
                    listing = parse_item(raw)
                    if listing:
                        out.setdefault(listing.listing_id, listing)
                if len(items) < PAGE_SIZE:
                    break
                page += 1
                await asyncio.sleep(sleep)
        return list(out.values())

    async def search(self, terms: list[str], max_pages: int = 5, sleep: float = 0.5) -> list[Listing]:
        """Keyword search across ALL categories (a designer piece is often filed somewhere unexpected), end-time order."""
        out: dict[str, Listing] = {}
        for term in terms:
            for page in range(1, max_pages + 1):
                items = (await self._page("", page, SORT_END_TIME, term))["items"]
                for raw in items:
                    listing = parse_item(raw)
                    if listing:
                        out.setdefault(listing.listing_id, listing)
                if len(items) < PAGE_SIZE:
                    break
                await asyncio.sleep(sleep)
            await asyncio.sleep(sleep)
        return list(out.values())

    async def fetch_ending_auctions(self, category_ids: list[str], minutes: float = 15,
                                    max_pages: int = 5) -> list[Listing]:
        """Items ending within `minutes` (end-time ascending, stops at the first later item)."""
        cutoff = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        out: dict[str, Listing] = {}
        for cid in category_ids:
            for page in range(1, max_pages + 1):
                items = (await self._page(cid, page, SORT_END_TIME))["items"]
                done = False
                for raw in items:
                    listing = parse_item(raw)
                    if listing is None:
                        continue  # already ended; list is a few seconds stale
                    if listing.end_time > cutoff:
                        done = True
                        break
                    out.setdefault(listing.listing_id, listing)
                if done or len(items) < PAGE_SIZE:
                    break
                await asyncio.sleep(0.5)
        return list(out.values())
