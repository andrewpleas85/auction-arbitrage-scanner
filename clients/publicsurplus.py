"""Public Surplus (school / city / university surplus auctions), read from its public category pages.

The category pages are server-rendered: each card carries the auction id, title, state, current price and the end time
(epoch ms inside the countdown script). 25 cards per page, `page=N` is zero-based, sorted by time left. No login or key.
robots.txt only bars /images/ for ordinary crawlers (5s delay asked of Bing/Slurp; we wait 1s between pages like the other
clients). Winning bidders pay a buyer's premium that varies by seller (10.5% on the sample listing); most items are pickup only.
"""
import asyncio
import html
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from models import Listing

log = logging.getLogger("arbitrage.publicsurplus")
BASE = "https://www.publicsurplus.com"
PREMIUM_PCT = 10.5
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
           "Accept-Language": "en-US,en;q=0.9"}
# category id -> catalog domain (vehicles, heavy equipment, real estate, livestock and the like are deliberately left out)
CATEGORIES = {"1": "computers", "2": "electronics", "3": "computers", "13": "instruments", "18": "collectibles",
              "11": "collectibles", "6": "tools", "12": "tools", "5": "collectibles", "16": "fashion"}
_CARD = re.compile(r'<div class="auction-item" id="(\d+)catGrid">(.*?)(?=<div class="auction-item" id="\d+catGrid">|</section>|$)', re.S)
_TITLE = re.compile(r'href="(/sms/auction/view\?auc=\d+)"[^>]*?title="#\d+ - ([^"]*)"')
_PRICE = re.compile(r'id="val_\d+catGrid">\s*\$([\d,]+(?:\.\d+)?)')
_STATE = re.compile(r'auction-item-state">\s*([A-Z]{2})\b')
_END = re.compile(r"updateTimeLeftSpan\(timeLeftInfoMap,\s*\d+,\s*\"\d+catGrid\",\s*\d+,\s*(\d{10,})")
_IMG = re.compile(r'<img[^>]+src="(https://[^"]+)"')


def parse_page(html_text: str, category: str = "") -> list[Listing]:
    """Listings on one category page. Cards missing a price or title are skipped."""
    out = []
    for m in _CARD.finditer(html_text):
        lid, body = m.group(1), m.group(2)
        t, p = _TITLE.search(body), _PRICE.search(body)
        if not t or not p:
            continue
        end = _END.search(body)
        state, img = _STATE.search(body), _IMG.search(body)
        out.append(Listing(
            platform="publicsurplus", listing_id=lid, title=html.unescape(t.group(2)).strip(), price=float(p.group(1).replace(",", "")),
            shipping_cost=0.0, url=BASE + t.group(1), image_url=img.group(1) if img else None, category=category,
            listing_type="auction", end_time=datetime.fromtimestamp(int(end.group(1)) / 1000, timezone.utc) if end else None,
            location=state.group(1) if state else None, extra_cost_pct=PREMIUM_PCT, extra={"pickup_only": True}))
    return out


_TZ = {"EDT": -4, "EST": -5, "CDT": -5, "CST": -6, "MDT": -6, "MST": -7, "PDT": -7, "PST": -8, "AKDT": -8, "HST": -10}


def parse_detail(page: str) -> dict:
    """Live price, bid count, end time and ended flag from an auction page ({} when it can't be read)."""
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", page, flags=re.S)))
    price = re.search(r"(?:Current|Opening) Price:?\s*\$([\d,]+(?:\.\d+)?)", text)  # a lot with no bids shows 'Opening Price'
    nobids = bool(re.search(r"# of Bids\s*No Bids", text))
    bids = re.search(r"# of Bids\s*(\d+)", text)
    bids_n = int(bids.group(1)) if bids else (0 if nobids else None)
    end = re.search(r"Auction Ends\s+(\w{3} \d{1,2}, \d{4} \d{1,2}:\d{2} [AP]M) (\w{3,4})", text)
    ended = bool(re.search(r"Auction (?:has )?(?:closed|ended)|Bidding (?:is )?closed|Time Left:\s*(?:Closed|Ended)", text, re.I))
    if not price and not ended and "Shipping" not in text:
        return {}
    end_dt = None
    if end and end.group(2) in _TZ:
        try:
            naive = datetime.strptime(end.group(1), "%b %d, %Y %I:%M %p")
            end_dt = (naive - timedelta(hours=_TZ[end.group(2)])).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    ship = re.search(r"\bShipping (.{3,80}?) (?:Bid on Item|Region:)", text)
    pick = re.search(r"Pick-up Location (.{0,200}?) \[(.*?)\]", text)
    ship_text = ship.group(1).strip() if ship else ""
    mode = ("pickup" if re.search(r"must pick ?up", ship_text, re.I) else "ask" if re.search(r"contact", ship_text, re.I)
            else "ships" if ship_text else "unknown")
    zm = re.search(r"\b([A-Z]{2})\s+(\d{5})\b", pick.group(2) if pick else "")
    prem = re.search(r"Buyers? Premium of ([\d.]+)%", text, re.I)
    low = text.lower()
    facts = {
        "premium_pct": float(prem.group(1)) if prem else None,
        "free_shipping": bool(re.search(r"shipped for free|free shipping|ships? free", low)),
        "drives_removed": bool(re.search(r"(?:hard drive|hdd|ssd|storage|drive)[^.]{0,80}(?:removed|not included|wiped)", low)),
        "no_power_supply": bool(re.search(r"(?:power supply|charger|adapter|ac adapter)[^.]{0,60}(?:not included|not provided|no )", low)),
        "untested": bool(re.search(r"no further testing|not tested|untested|unknown issues|sold as[- ]is|powers? up to bios", low)),
        "needs_30_day_account": bool(re.search(r"new accounts need to wait \d+ days", low)),
    }
    return {**facts, "ship_mode": mode, "ship_text": ship_text, "pickup_state": zm.group(1) if zm else None,
            "pickup_zip": zm.group(2) if zm else None, "price": float(price.group(1).replace(",", "")) if price else None, "bids": bids_n,
            "end": end_dt, "ended": ended}


def near_chicago(state: str | None, zip_code: str | None) -> bool:
    """Chicago metro by ZIP: Illinois 600-608 (city, Cook/DuPage/Lake/Will/Kane/McHenry) and northwest Indiana 463-464."""
    if not zip_code:
        return False
    return (state == "IL" and zip_code[:3] in {"600", "601", "602", "603", "604", "605", "606", "607", "608"}) or \
           (state == "IN" and zip_code[:3] in {"463", "464"})


class PublicSurplusClient:
    def __init__(self, http: httpx.AsyncClient, delay: float = 1.0):
        self._http, self._delay = http, delay

    async def _get(self, url: str, **params) -> str:
        resp = await self._http.get(url, params=params or None, headers=HEADERS, follow_redirects=True)
        resp.raise_for_status()
        return resp.text

    async def fetch_category(self, catid: str, max_pages: int | None = None) -> list[Listing]:
        out: dict[str, Listing] = {}
        page = 0
        while max_pages is None or page < max_pages:
            html_text = await self._get(f"{BASE}/sms/browse/cataucs", slth="y", catid=catid, page=page, sortBy="timeLeft", sortDesc="N")
            new = [l for l in parse_page(html_text, catid) if l.listing_id not in out]
            for l in new:
                out[l.listing_id] = l
            if len(new) == 0:
                break
            page += 1
            await asyncio.sleep(self._delay)
        return list(out.values())

    async def fetch_all(self, max_pages: int | None = None) -> dict[str, list[Listing]]:
        """{catalog domain: listings} across the categories worth cataloging."""
        res: dict[str, list[Listing]] = {}
        for catid, domain in CATEGORIES.items():
            try:
                res.setdefault(domain, []).extend(await self.fetch_category(catid, max_pages))
            except httpx.HTTPError as e:
                log.warning("publicsurplus category %s failed: %s", catid, e)
        return res

    async def detail(self, url: str) -> dict:
        return parse_detail(await self._get(url))
