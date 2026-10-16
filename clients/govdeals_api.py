"""GovDeals via the search API its own website calls (maestro.lqdt1.com/search/list).

GovDeals is a JavaScript app, so the HTML has no listings. The page's own bundle ships a public x-api-key (the same one every
visitor's browser sends) and the search endpoint answers anonymous POSTs when x-user-id and a correlation id are present.
This is undocumented and may change or be withdrawn without notice. robots.txt asks for a 5s crawl delay on govdeals.com, so
requests here are sequential with a pause between pages. Winning bidders pay a seller-set buyer's premium (unknown per listing;
10% is assumed); most items are local pickup.
"""
import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone

import httpx

from models import Listing

log = logging.getLogger("arbitrage.govdeals_api")
URL = "https://maestro.lqdt1.com/search/list"
API_KEY = os.getenv("GOVDEALS_MAESTRO_KEY", "")  # the key govdeals.com's own front-end sends; copy it from your browser's network tab
PREMIUM_PCT = 10.0
PAGE_ROWS = 100
# GovDeals category id -> catalog domain (vehicles, heavy equipment, real estate, industrial machinery are left out)
CATEGORIES = {"29": "computers", "243": "electronics", "22": "electronics", "28": "electronics", "23": "electronics",
              "DR0": "electronics", "222": "electronics", "242": "electronics", "76": "cameras", "206": "fashion",
              "10": "fashion", "200": "collectibles", "249": "tools", "203": "collectibles"}


MENU_URL = "https://maestro.lqdt1.com/menus/categories"


def leaves(menus: list, domain: str | None = None) -> dict[str, str]:
    """{leaf id: domain} for every leaf under a CATEGORIES entry (a category with no children is its own leaf)."""
    out: dict[str, str] = {}
    for m in menus:
        dom = CATEGORIES.get(m["menuId"], domain)
        kids = m.get("children") or []
        if kids:
            out.update(leaves(kids, dom))
        elif dom:
            out[m["menuId"]] = dom
    return out


def _body(category: str, page: int) -> dict:
    return {"businessId": "GD", "categoryIds": category, "searchText": "", "page": page, "displayRows": PAGE_ROWS,
            "sortField": "auctionclose", "sortOrder": "asc", "requestType": "search", "responseStyle": "productsOnly",
            "facets": [], "facetsFilter": [], "timeType": "", "sellerTypeId": None, "accountIds": [], "zipcode": "",
            "proximityWithinDistance": "", "isSimpleTimeSearch": False}


def _headers() -> dict:
    return {"x-api-key": API_KEY, "x-user-id": "-1", "x-api-correlation-id": str(uuid.uuid4()), "content-type": "application/json",
            "user-agent": "arbitrage-scanner/1.0 (personal use)"}


def _end(raw: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
    except ValueError:
        return None


def parse_results(data: dict, category: str = "") -> list[Listing]:
    """Listings in one search response. Entries without an id or a bid price are skipped (no bid yet is priced at 0)."""
    out = []
    for a in data.get("assetSearchResults") or []:
        if not a.get("assetId") or a.get("isSoldAuction"):
            continue
        brand = (a.get("makebrand") or "").strip()
        title = " ".join(x for x in (brand, (a.get("assetShortDescription") or "").strip(), (a.get("model") or "").strip()) if x)
        bid = a.get("currentBid")
        loc = ", ".join(x for x in (a.get("locationCity"), a.get("locationState")) if x)
        if loc and a.get("locationZip"):
            loc = f"{loc} {str(a['locationZip']).strip()[:5]}"  # 'Greeley, CO 80634': the page needs the ZIP to judge pickup distance
        out.append(Listing(
            platform="govdeals", listing_id=f"{a['accountId']}-{a['assetId']}", title=title,
            price=float(bid) if bid is not None else float(a.get("assetBidPrice") or 0), shipping_cost=0.0,
            url=f"https://www.govdeals.com/asset/{a['assetId']}/{a['accountId']}", image_url=None, category=category,
            listing_type="auction", end_time=_end(a.get("assetAuctionEndDateUtc")), location=loc or None,
            extra_cost_pct=PREMIUM_PCT,
            extra={"bids": a.get("bidCount"), "agency": a.get("companyName"), "pickup_only": True,
                   "reserve_not_met": a.get("isReserveNotMet"), "gd_category": a.get("categoryDescription")}))
    return out


class GovDealsApiClient:
    def __init__(self, http: httpx.AsyncClient, delay: float = 2.0):
        self._http, self._delay = http, delay

    async def fetch_category(self, category: str, max_pages: int | None = None) -> list[Listing]:
        out: dict[str, Listing] = {}
        page = 1
        while max_pages is None or page <= max_pages:
            resp = await self._http.post(URL, json=_body(category, page), headers=_headers())
            resp.raise_for_status()
            data = resp.json()
            got = parse_results(data, category)
            new = [l for l in got if l.listing_id not in out]
            for l in new:
                out[l.listing_id] = l
            if not new or len(data.get("assetSearchResults") or []) < PAGE_ROWS:
                break
            page += 1
            await asyncio.sleep(self._delay)
        return list(out.values())

    async def leaf_categories(self) -> dict[str, str]:
        """{leaf category id: catalog domain}. Searching a PARENT category returns only a sample (e.g. 388 of 1,659 computer
        items); every leaf below it returns its full list, so the crawl walks the leaves."""
        resp = await self._http.post(MENU_URL, json={"businessId": "GD", "accountIds": "", "menuType": "categories",
                                                    "menuFacetValue": ["categoryName"], "facetsFilter": [], "siteId": "GD"},
                                     headers=_headers())
        resp.raise_for_status()
        return leaves(resp.json().get("menus") or [])

    async def fetch_all(self, max_pages: int | None = None) -> dict[str, list[Listing]]:
        res: dict[str, list[Listing]] = {}
        try:
            cats = await self.leaf_categories() or CATEGORIES
        except (httpx.HTTPError, ValueError) as e:
            log.warning("govdeals category menu failed (%s); using top-level categories", e)
            cats = CATEGORIES
        for cat, domain in cats.items():
            try:
                res.setdefault(domain, []).extend(await self.fetch_category(cat, max_pages))
            except (httpx.HTTPError, ValueError) as e:
                log.warning("govdeals category %s failed: %s", cat, e)
            await asyncio.sleep(self._delay)
        return res
