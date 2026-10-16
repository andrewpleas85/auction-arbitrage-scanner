"""GovDeals / PublicSurplus client, driven by RSS feed URLs you configure.

There is no documented public API. This parses standard RSS 2.0 feeds (GOVDEALS_FEED_URLS)
such as category or saved-search feeds, extracting bid/location/end-time best-effort from the
title + description text. Requests use ordinary browser headers over HTTP/2 and are made
sequentially. Plain GovDeals pages answered 200 with these headers in testing; if a feed
still answers 403 (or fails to parse) it is logged as a structured warning and skipped, so one
bad feed never breaks the scan. No TLS-fingerprint impersonation is attempted.
"""
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlparse

import httpx

from models import Listing

log = logging.getLogger("arbitrage.govdeals")

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "application/rss+xml,application/xml;q=0.9,text/xml;q=0.8,text/html;q=0.7,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

_BID_RE = re.compile(r"(?:current\s*bid|bid|price)\s*[:=]?\s*\$\s*([\d,]+(?:\.\d+)?)", re.I)
_LOC_RE = re.compile(
    r"location\s*[:=]\s*(.+?)(?=\s+(?:auction|ends?|closing|current|bid|item)\b|[<\n|]|$)", re.I)
_END_RE = re.compile(r"(?:ends?|closing|auction\s*end)[^:]*:\s*([^<\n|]+)", re.I)
_IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)", re.I)


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s)


def _parse_dt(text: str) -> datetime | None:
    text = text.strip()
    for parser in (parsedate_to_datetime, datetime.fromisoformat):
        try:
            dt = parser(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)  # tz unknown: assume UTC
        except ValueError:
            continue
    return None


class GovDealsClient:
    def __init__(self, http: httpx.AsyncClient | None, feed_urls: list[str], keywords: list[str],
                 home_location: str = "", freight_usd: float = 75, premium_pct: float = 10):
        self._http = http or httpx.AsyncClient(
            http2=True, headers=BROWSER_HEADERS, follow_redirects=True, timeout=20)
        self._feeds = feed_urls
        self._keywords = [k.lower() for k in keywords]
        self._home = home_location.lower()
        self._freight = freight_usd
        self._premium = premium_pct

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch(self) -> list[Listing]:
        out: dict[str, Listing] = {}
        for url in self._feeds:
            try:
                resp = await self._http.get(url, headers=BROWSER_HEADERS)
                resp.raise_for_status()
                root = ET.fromstring(resp.content)
            except httpx.HTTPStatusError as e:
                log.warning("source=govdeals event=feed_http_error status=%s url=%s",
                            e.response.status_code, url)
                continue
            except httpx.HTTPError as e:
                log.warning("source=govdeals event=feed_request_failed url=%s reason=%r", url, e)
                continue
            except ET.ParseError as e:
                log.warning("source=govdeals event=feed_parse_error url=%s reason=%r", url, e)
                continue
            for item in root.iter("item"):
                try:
                    listing = self._parse(item)
                except (ValueError, TypeError) as e:
                    log.warning("source=govdeals event=item_parse_error reason=%r", e)
                    continue
                if listing:
                    out.setdefault(listing.listing_id, listing)
        return list(out.values())

    def _parse(self, item: ET.Element) -> Listing | None:
        text = lambda tag: (item.findtext(tag) or "").strip()
        title, link, desc = text("title"), text("link"), text("description")
        if not title or not link:
            return None
        if self._keywords and not any(k in title.lower() for k in self._keywords):
            return None
        plain = _strip_html(f"{title} | {desc}")
        bid = _BID_RE.search(plain)
        if not bid:
            return None
        loc_m = _LOC_RE.search(plain)
        location = loc_m.group(1).strip() if loc_m else None
        end_m = _END_RE.search(plain)
        end = _parse_dt(end_m.group(1)) if end_m else None
        qs = parse_qs(urlparse(link).query)
        auction_id = (qs.get("itemid") or qs.get("auctionid") or [text("guid") or link])[0]
        local = bool(self._home and location and self._home in location.lower())
        img = _IMG_RE.search(desc)
        return Listing(
            platform="govdeals",
            listing_id=str(auction_id),
            title=title,
            price=float(bid.group(1).replace(",", "")),
            shipping_cost=0.0 if local else self._freight,  # pickup-only: freight if not local
            url=link,
            image_url=img.group(1) if img else None,
            category="surplus",
            listing_type="auction",
            end_time=end,
            location=location,
            extra_cost_pct=self._premium,
        )
