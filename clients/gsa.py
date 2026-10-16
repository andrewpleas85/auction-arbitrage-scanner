"""GSA Auctions client (official government feed, https://api.gsa.gov/assets/gsaauctions/v2/auctions).

The endpoint answers 303 with a pre-signed link to one JSON file of every active and preview
auction (~4 MB, {"Results": [...]}). There is no server-side filtering, no category field and no
server-side paging, so we download the feed at most once per `min_interval` (DEMO_KEY is limited
to 10 requests/hour) and filter locally by keywords in the item name.

Fields used: saleNo + lotNo (id), itemName, highBidAmount (current bid), aucEndDt (a DATE with no
time; treated as 23:59 US Eastern that day), propertyCity/State/Zip, itemDescURL, imageURL, lotInfo.
Lots with no bid yet are skipped so they are retried once someone bids.
Closing day: lots whose end date is today (US Eastern) are tagged extra["closes_today"] and are
re-read from the official feed on a SHORTER interval (`closing_interval`) than the 30-minute
discovery cache, so their bids are minutes old instead of up to 30. A lot's own page cannot be
scraped for the exact closing time: gsaauctions.gov is a JavaScript app whose data API answers
401 "Please login again" to anonymous callers, and no closing time appears in the feed or lot text.
So the exact time stays unknown; once a lot closes it drops out of the feed and is no longer alerted.

Because only a date is given, these are listing_type "daily_auction" (not the minutes-left auction
loop); extra["end_date"] is the ISO date in US Eastern, used to alert on the day the lot closes.
"""
import html
import re
import time
from datetime import date, datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

import httpx

from models import Listing

URL = "https://api.gsa.gov/assets/gsaauctions/v2/auctions"
EASTERN = ZoneInfo("America/New_York")

# Target categories: electronics/IT, scientific & lab, AV/audio, tools. The feed has no category
# field, so match the item name.
_INCLUDE = (
    r"electronics?|computers?|laptops?|notebooks?|tablets?|ipads?|monitors?|servers?|routers?|switch(?:es)?|"
    r"cisco|network|printers?|phones?|radios?|cameras?|lens(?:es)?|projectors?|televisions?|tvs?|"
    r"audio|video|speakers?|amplifiers?|receivers?|microphones?|mixers?|headphones?|"
    r"oscilloscopes?|analy[sz]ers?|multimeters?|fluke|tektronix|keysight|microscopes?|centrifuges?|"
    r"spectrometers?|lab(?:oratory)?|scientific|calibrat\w*|"
    r"tools?|drills?|saws?|wrenches|grinders?|welders?|compressors?|toolbox"
)
INCLUDE_RE = re.compile(rf"\b(?:{_INCLUDE})\b", re.I)
EXCLUDE_RE = re.compile(
    r"\b(?:trucks?|trailers?|suvs?|sedans?|vans?|boats?|aircraft|helicopters?|tractors?|vehicles?|"
    r"blackhawk|chevrolet|ford|gmc|dodge|toyota)\b", re.I)


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _end_time(raw: str) -> datetime | None:
    try:
        d = date.fromisoformat(raw[:10])
    except (TypeError, ValueError):
        return None
    return datetime.combine(d, dtime(23, 59), EASTERN).astimezone(timezone.utc)  # display only


def today_eastern() -> str:
    return datetime.now(EASTERN).date().isoformat()


def closes_today(listing: Listing, today: str | None = None) -> bool:
    return listing.extra.get("end_date") == (today or today_eastern())


def parse_item(raw: dict, premium_pct: float = 15, freight: float = 0.0) -> Listing | None:
    """Raw feed item -> Listing; None if inactive, off-category or not yet bid on."""
    if (raw.get("auctionStatus") or "").lower() != "active":
        return None
    title = (raw.get("itemName") or "").strip()
    if not title or not INCLUDE_RE.search(title) or EXCLUDE_RE.search(title):
        return None
    try:
        bid = float(raw.get("highBidAmount") or 0)
    except ValueError:
        return None
    end = _end_time(raw.get("aucEndDt") or "")
    if bid <= 0 or end is None or end <= datetime.now(timezone.utc):
        return None
    place = ", ".join(p.strip() for p in (raw.get("propertyCity"), raw.get("propertyState")) if p)
    zip_code = (raw.get("propertyZip") or "").strip()
    return Listing(
        platform="gsa",
        listing_id=f"{raw['saleNo']}-{raw['lotNo']}",
        title=title,
        price=bid,
        shipping_cost=freight,
        url=raw.get("itemDescURL") or "https://gsaauctions.gov",
        image_url=raw.get("imageURL") or None,
        category="gsa",
        listing_type="daily_auction",
        end_time=end,
        location=f"{place} {zip_code}".strip() or None,
        extra_cost_pct=premium_pct,
        extra={"zip_code": zip_code, "bidders": raw.get("biddersCount"),
               "end_time_precision": "date", "end_date": raw["aucEndDt"][:10],
               "description": _strip_html(raw.get("lotInfo") or "")[:500]},
    )


class GSAClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str = "DEMO_KEY",
                 min_interval: float = 1800, premium_pct: float = 15, freight: float = 0.0,
                 closing_interval: float = 600):
        self._http = http
        self._key = api_key
        self._min_interval = min_interval
        self._closing_interval = min(closing_interval, min_interval)
        self._premium = premium_pct
        self._freight = freight
        self._cache: list[Listing] = []
        self._fetched_at = 0.0

    async def fetch(self) -> list[Listing]:
        """Target-category active auctions with a bid. Cached; shared by both scan loops."""
        return await self._load(self._min_interval)

    async def fetch_closing_day(self) -> list[Listing]:
        """Lots that close TODAY (US Eastern), from a feed no older than `closing_interval`.

        Bypasses the longer discovery cache. Keep closing_interval >= ~600s on DEMO_KEY (10 requests/hour).
        """
        today = today_eastern()
        return [l for l in await self._load(self._closing_interval) if closes_today(l, today)]

    async def _load(self, max_age: float) -> list[Listing]:
        if self._fetched_at and time.time() - self._fetched_at < max_age:
            return self._cache
        self._fetched_at = time.time()  # also throttles retries after a failure
        resp = await self._http.get(URL, params={"api_key": self._key, "format": "JSON"},
                                    follow_redirects=True, timeout=60)
        resp.raise_for_status()
        items = (parse_item(r, self._premium, self._freight) for r in resp.json().get("Results", []))
        today = today_eastern()
        self._cache = [l for l in items if l]
        for l in self._cache:
            l.extra["closes_today"] = closes_today(l, today)
        return self._cache
