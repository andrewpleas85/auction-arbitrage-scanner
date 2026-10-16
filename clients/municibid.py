"""Municibid (municipal / school / police surplus auctions), read from its public browse pages.

The browse pages are server-rendered with every listing embedded as JSON (id, title, current price, bid count, end time,
location, agency), so no login and no API key are needed. robots.txt allows /browse and /listing. Requests are sequential
and polite. Winning bidders pay a 9% buyer's fee (tiered down above $100k); most items are LOCAL PICKUP only.
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import httpx

from models import Listing

log = logging.getLogger("arbitrage.municibid")
BASE = "https://municibid.com"
PREMIUM_PCT = 9.0
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
           "Accept-Language": "en-US,en;q=0.9"}
# category path -> catalog domain
CATEGORIES = {"/browse/c160887/computers_it": "computers", "/browse/c169069/electronics": "electronics",
              "/browse/c169121/music": "instruments", "/browse/c169170/tools": "tools"}
_LISTING_RE = re.compile(r'"listing":(\{"id":\d+[^{}]*\})')
_HREF_RE = re.compile(r'href="(/listing/(\d+)/[^"]*)"')


def _end(raw: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.split(".")[0]).replace(tzinfo=timezone.utc) if raw else None
    except ValueError:
        return None


def parse_page(html: str, category: str = "") -> list[Listing]:
    """Listings embedded in one browse page. Unparseable entries are skipped."""
    text = html.replace("\\", "")  # the payload sits inside an escaped JS string
    hrefs = {int(m.group(2)): m.group(1) for m in _HREF_RE.finditer(html)}
    out, seen = [], set()
    for m in _LISTING_RE.finditer(text):
        try:
            raw = json.loads(m.group(1))
        except ValueError:
            continue
        lid = raw.get("id")
        if lid in seen or raw.get("currentPrice") is None:
            continue
        seen.add(lid)
        path = hrefs.get(lid, f"/listing/{lid}")
        out.append(Listing(
            platform="municibid", listing_id=str(lid), title=raw.get("title") or "", price=float(raw["currentPrice"]),
            shipping_cost=0.0, url=BASE + path, image_url=raw.get("primaryImageUrl"), category=category,
            listing_type="auction", end_time=_end(raw.get("endsAtUtc")), location=raw.get("location"),
            extra_cost_pct=PREMIUM_PCT,
            extra={"bids": raw.get("bidCount"), "agency": raw.get("agencyName"), "pickup_only": True,
                   "reserve": raw.get("reservePrice")}))
    return out


def parse_detail(html: str, listing_id: str) -> dict:
    """Live price, bid count, end time and ended flag for one listing from its page."""
    text = html.replace("\\", "")
    m = re.search(r'"initial":\{"currentPrice":([\d.]+),"bidCount":(\d+),"endsAtUtc":"([^"]+)"', text)
    if not m:
        return {}
    return {"price": float(m.group(1)), "bids": int(m.group(2)), "end": _end(m.group(3)),
            "ended": '"ended":true' in text}


class MunicibidClient:
    def __init__(self, http: httpx.AsyncClient, delay: float = 1.0):
        self._http, self._delay = http, delay

    async def _get(self, url: str) -> str:
        resp = await self._http.get(url, headers=HEADERS, follow_redirects=True)
        resp.raise_for_status()
        return resp.text

    async def fetch_category(self, path: str, max_pages: int | None = None) -> list[Listing]:
        out: dict[str, Listing] = {}
        page = 0
        while max_pages is None or page < max_pages:
            html = await self._get(f"{BASE}{path}" + (f"?page={page}" if page else ""))
            items = parse_page(html, path)
            new = [l for l in items if l.listing_id not in out]
            for l in new:
                out[l.listing_id] = l
            if not new:
                break
            page += 1
            await asyncio.sleep(self._delay)
        return list(out.values())

    async def fetch_all(self, max_pages: int | None = None) -> dict[str, list[Listing]]:
        """{catalog domain: listings} across the categories worth cataloging."""
        res: dict[str, list[Listing]] = {}
        for path, domain in CATEGORIES.items():
            try:
                res.setdefault(domain, []).extend(await self.fetch_category(path, max_pages))
            except httpx.HTTPError as e:
                log.warning("municibid %s failed: %s", path, e)
        return res

    async def detail(self, listing_id: str, url: str) -> dict:
        return parse_detail(await self._get(url), listing_id)
